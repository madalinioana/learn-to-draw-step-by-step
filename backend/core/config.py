"""Configuration for the model backends.

Artist-Critic supports two deployment profiles:

    local   -> Ollama live runs, intended for a cloned repo
    hosted  -> Gemini live runs, intended for public deployment

    Artist (text):   emits SVG + reasoning + step labels
    Critic (vision): grades a rendered PNG, emits feedback

All settings can be overridden via environment variables (loaded from
`.env` if present). Module-level constants are evaluated once at import
time; if you change the env after import you must restart the process.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Tuple

import httpx
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
try:
    REPO_ROOT = BACKEND_DIR.parents[1]
except IndexError:
    REPO_ROOT = BACKEND_DIR

load_dotenv(REPO_ROOT / ".env")
load_dotenv(BACKEND_DIR / ".env", override=True)

logger = logging.getLogger(__name__)


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "")
    values = [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]
    return values or list(default or [])


# ── Deployment profile ───────────────────────────────────────────────────────
# local  = cloned repo, Ollama-only UI, model names visible
# hosted = public deployment, Gemini-only UI, recorded runs visible, model names hidden
DEPLOYMENT_PROFILE: str = os.environ.get("DEPLOYMENT_PROFILE", "local").strip().lower()
if DEPLOYMENT_PROFILE not in {"local", "hosted"}:
    logger.warning(
        "Unsupported DEPLOYMENT_PROFILE=%r; falling back to 'local'",
        DEPLOYMENT_PROFILE,
    )
    DEPLOYMENT_PROFILE = "local"

# ── Browser access ────────────────────────────────────────────────────────────
# For Vercel + Render set this on Render, for example:
# CORS_ALLOW_ORIGINS=https://sketchtrials.vercel.app,https://sketchtrials.com
CORS_ALLOW_ORIGINS: list[str] = _env_list(
    "CORS_ALLOW_ORIGINS",
    [
        "http://127.0.0.1:8001",
        "http://localhost:8001",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
    ],
)

# ── Backend selector ─────────────────────────────────────────────────────────
# Legacy flags are kept for direct module experiments. The public API resolves
# the runtime backend from DEPLOYMENT_PROFILE.
USE_GEMINI: bool = os.environ.get("USE_GEMINI", "0").strip() == "1"
USE_GEMINI_ARTIST: bool = (
    os.environ.get("USE_GEMINI_ARTIST", "1" if USE_GEMINI else "0").strip() == "1"
)
USE_GEMINI_CRITIC: bool = (
    os.environ.get("USE_GEMINI_CRITIC", "1" if USE_GEMINI else "0").strip() == "1"
)
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
# Legacy single-model flag — kept for backward compat. Per-role vars below take precedence.
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_ARTIST_MODEL: str = os.environ.get("GEMINI_ARTIST_MODEL", GEMINI_MODEL)
GEMINI_CRITIC_MODEL: str = os.environ.get("GEMINI_CRITIC_MODEL", GEMINI_MODEL)

# ── Ollama backend selector ──────────────────────────────────────────────────
# Ollama runs locally and exposes an OpenAI-compatible API and can serve
# multiple models concurrently — no manual model swapping needed.
# Backend priority per role: Gemini (cloud) → Ollama (local).
USE_OLLAMA: bool = os.environ.get("USE_OLLAMA", "0").strip() == "1"
USE_OLLAMA_ARTIST: bool = (
    os.environ.get("USE_OLLAMA_ARTIST", "1" if USE_OLLAMA else "0").strip() == "1"
)
USE_OLLAMA_CRITIC: bool = (
    os.environ.get("USE_OLLAMA_CRITIC", "1" if USE_OLLAMA else "0").strip() == "1"
)
OLLAMA_BASE_URL: str = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
# Ollama does not authenticate, but the OpenAI SDK requires a non-empty key.
OLLAMA_API_KEY: str = os.environ.get("OLLAMA_API_KEY", "ollama")
# Artist: any instruct model pulled via `ollama pull <model>`.
OLLAMA_ARTIST_MODEL: str = os.environ.get("OLLAMA_ARTIST_MODEL", "gemma3:27b")
# Critic: a vision-capable model. internvl3 is the recommended choice.
OLLAMA_CRITIC_MODEL: str = os.environ.get("OLLAMA_CRITIC_MODEL", "blaifa/InternVL3_5:8b")
# Keep Ollama's context large enough for prompt + SVG history, but avoid the
# 16K context cost on local Mac inference. Set OLLAMA_NUM_CTX=4096 for a more
# aggressive speed/quality trade-off.
OLLAMA_NUM_CTX: int = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

ARTIST_MODEL: str = os.environ.get("ARTIST_MODEL", "google/gemma-4-e4b")

# Upgraded from 3B to 7B to address perceptual limitations on minimalist sketch input.
# 7B at Q4_K_M is ~5GB, fits in 8GB VRAM when it's the actively-loaded model.
# Expected latency: 60-90s per critique on RX 6600 Vulkan.
CRITIC_MODEL: str = os.environ.get("CRITIC_MODEL", "qwen2.5-vl-7b-instruct")

ARTIST_TEMPERATURE: float = float(os.environ.get("ARTIST_TEMPERATURE", "0.7"))
# Lower temperature for revisions. Initial generations benefit from variety
# (different sketch styles for the same prompt); revisions need precision —
# they must implement the Critic's specific corrections, not improvise.
ARTIST_REVISION_TEMPERATURE: float = float(os.environ.get("ARTIST_REVISION_TEMPERATURE", "0.3"))
# The Artist prompt is short, but JSON+SVG can still need room to close. Gemma
# models on the Gemini endpoint ignore JSON mode and emit a markdown "thinking"
# preamble before the JSON, so the cap must hold preamble + full SVG or the JSON
# gets truncated mid-string. JSON-mode models (gemini-2.5-flash) stop right after
# the object, so a generous cap costs them nothing. 2400 was too tight and
# truncated real responses on hosted gemini-3.1-flash-lite; 4096 gives headroom.
ARTIST_MAX_TOKENS: int = int(os.environ.get("ARTIST_MAX_TOKENS", "4096"))

# Lower temperature for the Critic reduces hallucination and increases consistency
# of structured output. Tuned for Qwen2.5-VL which exhibits parroting tendencies
# at higher temperatures.
CRITIC_TEMPERATURE: float = 0.1
# Same preamble caveat as ARTIST_MAX_TOKENS: a Gemma critic narrates its
# reasoning before the JSON, and at 350 tokens that preamble alone truncated the
# response so the JSON (and part_status) never arrived. 1200 leaves room for the
# preamble plus the compact critique JSON.
CRITIC_MAX_TOKENS: int = int(os.environ.get("CRITIC_MAX_TOKENS", "1200"))

MAX_ITERATIONS: int = 4
CANVAS_SIZE: int = 512

# Per-request read timeout for the model backends. A 26B model on a 24GB M3
# spills partly to CPU and a single generation (esp. a cold load) can run
# 250-500s — 300s was too tight and caused intermittent Ollama ReadTimeouts.
REQUEST_TIMEOUT_SECONDS: int = 600
MODEL_SWAP_GRACE_SECONDS: int = 3

SVG_MIME_TYPE: str = "image/svg+xml"
PNG_MIME_TYPE: str = "image/png"


def check_ollama_health() -> Tuple[bool, str]:
    """Ping Ollama's /models endpoint with a 3-second timeout.

    Returns `(ok, message)` where ok=True iff the endpoint responded with
    a parseable JSON listing of available models.
    """
    url = OLLAMA_BASE_URL.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, timeout=3.0)
    except httpx.ConnectError:
        return False, (
            f"Cannot connect to Ollama at {OLLAMA_BASE_URL}. "
            "Open the Ollama app or run `ollama serve` in a terminal."
        )
    except httpx.TimeoutException:
        return False, f"Ollama at {OLLAMA_BASE_URL} did not respond within 3 seconds."
    except Exception as exc:
        return False, f"Unexpected error contacting Ollama: {type(exc).__name__}: {exc}"

    if resp.status_code != 200:
        return False, f"Ollama returned HTTP {resp.status_code} from {url}"

    try:
        data = resp.json()
    except Exception:
        return False, f"Ollama returned a non-JSON response from {url}"

    raw_models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return True, f"Ollama reachable at {OLLAMA_BASE_URL} (model list unparseable)."

    ids = [m.get("id") for m in raw_models if isinstance(m, dict) and m.get("id")]
    if not ids:
        return True, (
            f"Ollama reachable at {OLLAMA_BASE_URL}, but no models are downloaded. "
            "Run `ollama pull <model-name>` to download a model."
        )
    return True, f"Ollama reachable. {len(ids)} model(s) available: {', '.join(ids)}"


def list_ollama_models() -> Tuple[bool, list[str], str]:
    """Return locally available Ollama model ids."""
    url = OLLAMA_BASE_URL.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, timeout=3.0)
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError:
        return False, [], f"Cannot connect to Ollama at {OLLAMA_BASE_URL}."
    except httpx.TimeoutException:
        return False, [], f"Ollama at {OLLAMA_BASE_URL} did not respond within 3 seconds."
    except Exception as exc:
        return False, [], f"Unexpected error contacting Ollama: {type(exc).__name__}: {exc}"

    raw_models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return True, [], "Ollama reachable, but the model list could not be parsed."

    ids = sorted(
        m.get("id")
        for m in raw_models
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id")
    )
    return True, ids, "Ollama reachable."
