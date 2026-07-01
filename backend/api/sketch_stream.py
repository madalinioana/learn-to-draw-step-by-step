"""FastAPI SSE endpoint — streams ArtistCriticLoop events to the browser frontend.

Run from the repository root:
    cd apps/backend && python -m api.sketch_stream

Endpoint:
    GET /generate?prompt={text}   → text/event-stream
    GET /health                   → {"status": "ok"}

DEPLOYMENT_PROFILE controls the public UI/runtime shape:
    local  -> Ollama live runs only, local model names visible
    hosted -> Gemini live runs + recorded runs, model names hidden
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, AsyncIterator

BACKEND_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = BACKEND_DIR.parent
REPO_ROOT = APPS_DIR.parent

# Allow `from core.xxx import ...` when invoked from the backend directory.
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from core.config import (
    CORS_ALLOW_ORIGINS,
    DEPLOYMENT_PROFILE,
    GEMINI_API_KEY,
    GEMINI_ARTIST_MODEL,
    GEMINI_CRITIC_MODEL,
    GEMINI_THINKING_LEVEL,
    MAX_ITERATIONS,
    OLLAMA_API_KEY,
    OLLAMA_ARTIST_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_CRITIC_MODEL,
    OLLAMA_NUM_CTX,
    REQUEST_TIMEOUT_SECONDS,
    list_ollama_models,
)
from core.orchestrator import ArtistCriticLoop

logger = logging.getLogger(__name__)
FRONTEND_DIR = APPS_DIR / "frontend"

app = FastAPI(title="Artist-Critic sketch stream")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

@app.middleware("http")
async def _local_bridge_headers(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

# ── Runtime client cache ─────────────────────────────────────────────────

_runtime_clients: dict[str, tuple[Any, Any]] = {}


def _build_local_artist_client():
    from core.ollama_client import OllamaClient
    return OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=OLLAMA_ARTIST_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        num_ctx=OLLAMA_NUM_CTX,
    )


def _build_local_critic_client():
    from core.ollama_client import OllamaClient
    return OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=OLLAMA_CRITIC_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        num_ctx=OLLAMA_NUM_CTX,
    )


def _build_gemini_artist_client():
    from core.gemini_client import GeminiClient
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is empty — set it in .env")
    return GeminiClient(
        api_key=GEMINI_API_KEY,
        default_model=GEMINI_ARTIST_MODEL,
        thinking_level=GEMINI_THINKING_LEVEL,
    )


def _build_gemini_critic_client():
    from core.gemini_client import GeminiClient
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is empty — set it in .env")
    return GeminiClient(
        api_key=GEMINI_API_KEY,
        default_model=GEMINI_CRITIC_MODEL,
        thinking_level=GEMINI_THINKING_LEVEL,
    )


def _local_artist_model() -> str:
    return OLLAMA_ARTIST_MODEL


def _local_critic_model() -> str:
    return OLLAMA_CRITIC_MODEL


def _local_artist_backend_label() -> str:
    return "Ollama"


def _local_critic_backend_label() -> str:
    return "Ollama"


def _gemini_available() -> bool:
    return bool(GEMINI_API_KEY)


def _role_config(model: str, backend: str, role: str) -> dict:
    return {"model": model, "backend": backend, "role": role}


def _local_backend_option(available: bool = True, reason: str | None = None) -> dict:
    option = {
        "id": "local",
        "label": "local",
        "available": available,
        "artist": _role_config(_local_artist_model(), _local_artist_backend_label(), "text"),
        "critic": _role_config(_local_critic_model(), _local_critic_backend_label(), "vision"),
    }
    if reason:
        option["reason"] = reason
    return option


def _gemini_backend_option() -> dict:
    available = _gemini_available()
    option = {
        "id": "gemini",
        "label": "cloud",
        "available": available,
        "artist": _role_config(GEMINI_ARTIST_MODEL, "Google Gemini", "text"),
        "critic": _role_config(GEMINI_CRITIC_MODEL, "Google Gemini", "vision"),
    }
    if not available:
        option["reason"] = "GEMINI_API_KEY is not configured"
    return option


def _backend_options(local_available: bool = True, local_reason: str | None = None) -> list[dict]:
    if DEPLOYMENT_PROFILE == "hosted":
        return [_gemini_backend_option()]
    return [_local_backend_option(local_available, local_reason)]


def _default_backend() -> str:
    return "gemini" if DEPLOYMENT_PROFILE == "hosted" else "local"


def _resolve_runtime_backend(backend: str) -> dict:
    normalized = (backend or _default_backend()).strip().lower()
    if normalized not in {"local", "gemini"}:
        raise ValueError(f"Unsupported backend: {backend}")
    if DEPLOYMENT_PROFILE == "hosted" and normalized != "gemini":
        raise RuntimeError("This deployment only allows the hosted cloud backend")
    if DEPLOYMENT_PROFILE == "local" and normalized != "local":
        raise RuntimeError("This deployment only allows the local Ollama backend")

    if normalized == "gemini":
        if not _gemini_available():
            raise RuntimeError("Google Gemini backend is unavailable: GEMINI_API_KEY is not configured")
        if "gemini" not in _runtime_clients:
            _runtime_clients["gemini"] = (_build_gemini_artist_client(), _build_gemini_critic_client())
        artist_client, critic_client = _runtime_clients["gemini"]
        return {
            "id": "gemini",
            "artist_client": artist_client,
            "critic_client": critic_client,
            "artist_model": GEMINI_ARTIST_MODEL,
            "critic_model": GEMINI_CRITIC_MODEL,
        }

    if "local" not in _runtime_clients:
        _runtime_clients["local"] = (_build_local_artist_client(), _build_local_critic_client())
    artist_client, critic_client = _runtime_clients["local"]
    return {
        "id": "local",
        "artist_client": artist_client,
        "critic_client": critic_client,
        "artist_model": _local_artist_model(),
        "critic_model": _local_critic_model(),
    }


@app.on_event("startup")
async def _startup() -> None:
    logger.info(
        "sketch_stream ready: profile=%s local artist=%s critic=%s gemini_available=%s",
        DEPLOYMENT_PROFILE,
        _local_artist_model(),
        _local_critic_model(),
        _gemini_available(),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/config")
async def config() -> dict:
    """Live run configuration for the landing page."""
    show_model_names = DEPLOYMENT_PROFILE == "local"
    default_backend = _default_backend()
    ollama_ok, ollama_models, ollama_message = True, [], ""
    local_available = True
    local_reason = None
    if show_model_names:
        ollama_ok, ollama_models, ollama_message = list_ollama_models()
        configured = {_local_artist_model(), _local_critic_model()}
        loaded = {model.lower() for model in ollama_models}
        missing = sorted(model for model in configured if model.lower() not in loaded)
        local_available = ollama_ok and not missing
        if not ollama_ok:
            local_reason = ollama_message
        elif missing:
            local_reason = "Configured model(s) not loaded: " + ", ".join(missing)

    backend_options = _backend_options(local_available, local_reason)
    if not show_model_names:
        backend_options = [
            {
                key: value
                for key, value in option.items()
                if key in {"id", "label", "available", "reason"}
            }
            for option in backend_options
        ]

    cfg = {
        "profile": DEPLOYMENT_PROFILE,
        "features": {
            "live": True,
            "recorded": DEPLOYMENT_PROFILE == "hosted",
            "backend_picker": False,
            "show_model_names": show_model_names,
        },
        "backends": {
            "default": default_backend,
            "options": backend_options,
        },
        "runtime": {
            "default_backend": default_backend,
        },
        "max_iterations": MAX_ITERATIONS,
        "iterations_min": 1,
        "iterations_max": 8,
        # Display-only: the lightweight cloud model behind the hosted demo. Shown
        # with a quality caveat in the UI; not the (larger) local thesis models.
        "cloud_model": GEMINI_ARTIST_MODEL,
    }
    if show_model_names:
        local = _local_backend_option()
        cfg.update({
            "artist": local["artist"],
            "critic": local["critic"],
        })
        cfg["runtime"].update({
            "artist_model": _local_artist_model(),
            "critic_model": _local_critic_model(),
            "loaded_models": ollama_models,
            "models_available": ollama_ok,
            "models_message": ollama_message,
        })
    return cfg


@app.get("/generate")
async def generate(
    prompt: str = Query(..., description="What to draw"),
    max_iterations: int = Query(MAX_ITERATIONS, ge=1, le=8, description="Loop iteration cap"),
    backend: str | None = Query(None, description="Runtime backend: local or gemini"),
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(prompt, max_iterations, backend),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_stream(
    prompt: str,
    max_iterations: int = MAX_ITERATIONS,
    backend: str | None = None,
) -> AsyncIterator[str]:
    """Exhaust ArtistCriticLoop.run_stream synchronously and yield SSE frames.

    run_stream is a synchronous generator. For a single-user thesis demo
    running in the same thread is fine. A production deployment would offload
    to asyncio.to_thread.
    """
    try:
        runtime = _resolve_runtime_backend(backend)
        loop = ArtistCriticLoop(
            artist_client=runtime["artist_client"],
            critic_client=runtime["critic_client"],
            artist_model=runtime["artist_model"],
            critic_model=runtime["critic_model"],
            max_iterations=max_iterations,
        )
        for event in loop.run_stream(prompt):
            payload = json.dumps(event, default=str)
            yield f"data: {payload}\n\n"
            if event.get("event") == "loop_complete":
                return
    except Exception as exc:
        logger.exception("stream error for prompt %r", prompt)
        error_event = json.dumps({
            "event": "stream_error",
            "payload": {"message": str(exc)},
        })
        yield f"data: {error_event}\n\n"


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8001"))
    uvicorn.run("api.sketch_stream:app", host=host, port=port, reload=False)
