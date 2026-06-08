"""Local Ollama client (NATIVE /api/chat) implementing the shared model-client interface.

Why the native endpoint and not the OpenAI-compatible /v1 one:
    gemma4 (and qwen3) ship with reasoning/"thinking" ON by default. The Artist
    needs plain JSON, not chain-of-thought. On Ollama's /v1 endpoint the
    `think: false` flag and `options.num_ctx` we pass through the OpenAI SDK's
    extra_body are SILENTLY DROPPED — the model spends its whole token budget on
    hidden reasoning and returns empty `content` (confirmed empirically:
    /v1 → content_len=0/thinking_len=1533; /api/chat think=False → content_len=1744).
    The native /api/chat endpoint honors `think` and `options`, so we call it
    directly with httpx.

Notes:
    - Ollama serves multiple models concurrently; no manual model swapping.
    - Models auto-load on first call if downloaded locally.
    - `ensure_model_loaded` is a no-op — the model-swap polling never fires.
    - `response_format={"type": "json_object"}` maps to native `format: "json"`.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


from backend.core.errors import ModelBackendError


class OllamaError(ModelBackendError):
    """Raised when an Ollama API call fails after retries."""

    def __init__(self, message: str, original_exception: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class _RetryableStatus(Exception):
    """Internal marker for HTTP statuses worth retrying (408/429/5xx)."""


_RETRYABLE_HTTPX = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _retry_call(fn, attempts: int = 3, base_delay: float = 1.0):
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            return fn()
        except (_RetryableStatus, *_RETRYABLE_HTTPX) as exc:
            last_exc = exc
            logger.warning(
                "Ollama retryable error on attempt %d/%d: %s: %s",
                attempt + 1, attempts, type(exc).__name__, exc,
            )
        if attempt < attempts - 1:
            time.sleep(base_delay * (2 ** attempt))

    raise OllamaError(
        f"Ollama request failed after {attempts} attempts: "
        f"{type(last_exc).__name__ if last_exc else 'unknown'}: {last_exc}",
        original_exception=last_exc,
    ) from last_exc


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OllamaClient:
    """httpx client for Ollama's native /api/chat endpoint."""

    DEFAULT_BASE_URL = "http://localhost:11434/v1"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = "ollama",
        default_model: str = "",
        timeout: int = 300,
        num_ctx: int = 16384,
        think: bool = False,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url
        self.api_key = api_key or "ollama"
        self.default_model = default_model
        self.timeout = timeout
        self.num_ctx = num_ctx
        # think=False suppresses reasoning so the whole token budget goes to the
        # JSON answer. The Artist (gemma4) requires this; harmless for the Critic.
        self.think = think
        self.call_count_text: int = 0
        self.call_count_vision: int = 0
        # Native API lives at the host root, not under /v1.
        api_base = base_url.rstrip("/")
        if api_base.endswith("/v1"):
            api_base = api_base[: -len("/v1")].rstrip("/")
        self.api_base = api_base
        logger.info("OllamaClient ready (default_model=%s api_base=%s think=%s)",
                    default_model, self.api_base, self.think)

    @property
    def total_calls(self) -> int:
        return self.call_count_text + self.call_count_vision

    # ── model discovery / loading ────────────────────────────────────────

    def list_loaded_models(self) -> List[str]:
        """Return names of all models available in Ollama (via /api/tags)."""
        url = f"{self.api_base}/api/tags"
        try:
            resp = httpx.get(url, timeout=10.0)
        except httpx.ConnectError as exc:
            raise OllamaError(
                f"Cannot reach Ollama at {self.api_base}. "
                "Make sure Ollama is running (`ollama serve` or open the Ollama app).",
                original_exception=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(
                f"Ollama returned an error while listing models: {type(exc).__name__}: {exc}",
                original_exception=exc,
            ) from exc
        if resp.status_code != 200:
            raise OllamaError(f"Ollama /api/tags returned HTTP {resp.status_code}")
        data = resp.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        return [m.get("name") for m in models if isinstance(m, dict) and m.get("name")]

    def ensure_model_loaded(self, model_id: str) -> None:
        """No-op for Ollama — models auto-load on demand, no manual swap needed."""
        logger.debug("ensure_model_loaded: %s (no-op for Ollama)", model_id)

    # ── chat completions ─────────────────────────────────────────────────

    def chat_text(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Single-turn text chat completion. Returns the response content as a string."""
        if not model:
            raise ValueError("model is required")
        if not user_prompt:
            raise ValueError("user_prompt is required")

        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": user_prompt},
        ]
        payload = self._build_payload(model, messages, temperature, max_tokens, response_format)
        self.call_count_text += 1
        return _retry_call(lambda: self._chat(payload, model, label="text"))

    def chat_vision(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        image_bytes: bytes,
        image_format: str = "png",
        temperature: float = 0.3,
        max_tokens: int = 1024,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Single-turn multimodal chat completion. Image sent as base64 in `images`."""
        if not model:
            raise ValueError("model is required")
        if not user_prompt:
            raise ValueError("user_prompt is required")
        if not image_bytes:
            raise ValueError("image_bytes is required")

        # Native /api/chat takes raw base64 (no data URL prefix) in `images`.
        b64 = base64.b64encode(image_bytes).decode("ascii")
        messages = [
            {"role": "system", "content": system_prompt or ""},
            {"role": "user", "content": user_prompt, "images": [b64]},
        ]
        payload = self._build_payload(model, messages, temperature, max_tokens, response_format)
        self.call_count_vision += 1
        return _retry_call(lambda: self._chat(payload, model, label="vision"))

    # ── helpers ──────────────────────────────────────────────────────────

    def _build_payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "think": self.think,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                "temperature": temperature,
            },
        }
        # Any response_format request → native JSON-constrained output.
        if response_format is not None:
            payload["format"] = "json"
        return payload

    def _chat(self, payload: Dict[str, Any], model: str, label: str) -> str:
        url = f"{self.api_base}/api/chat"
        t0 = time.monotonic()
        resp = httpx.post(url, json=payload, timeout=float(self.timeout))

        # Some Ollama builds reject `think` for models that lack reasoning
        # support. Drop it and retry once rather than failing the whole run.
        if resp.status_code == 400 and "think" in resp.text.lower() and "think" in payload:
            logger.warning("model %s rejected `think` param; retrying without it", model)
            payload = {k: v for k, v in payload.items() if k != "think"}
            resp = httpx.post(url, json=payload, timeout=float(self.timeout))

        if resp.status_code in (408, 429) or resp.status_code >= 500:
            raise _RetryableStatus(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code != 200:
            raise OllamaError(
                f"Ollama rejected request (HTTP {resp.status_code}): {resp.text[:300]}"
            )

        data = resp.json()
        dt = time.monotonic() - t0
        message = data.get("message") or {}
        content = message.get("content")
        thinking = message.get("thinking") or ""
        logger.info(
            "chat_%s OK: model=%s call#=%d elapsed=%.2fs out_tok=%s done=%s content_len=%d thinking_len=%d",
            label, model, self.total_calls, dt,
            data.get("eval_count", "?"), data.get("done_reason", "?"),
            len(content or ""), len(thinking),
        )
        if not content or not content.strip():
            raise OllamaError(
                f"Ollama returned empty content (done_reason={data.get('done_reason')}, "
                f"thinking_len={len(thinking)}). If thinking_len is large, reasoning "
                f"consumed the token budget — ensure think=False reaches the model."
            )
        return content


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    from backend.core.config import OLLAMA_API_KEY, OLLAMA_BASE_URL, OLLAMA_CRITIC_MODEL, REQUEST_TIMEOUT_SECONDS

    print(f"Connecting to Ollama at {OLLAMA_BASE_URL}")
    client = OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=OLLAMA_CRITIC_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    try:
        models = client.list_loaded_models()
    except OllamaError as exc:
        print(f"\nFAIL — cannot reach Ollama:\n  {exc}", file=sys.stderr)
        sys.exit(1)

    if not models:
        print(
            "\nOllama is reachable but no models are downloaded.\n"
            "Run `ollama pull <model-name>` to download a model.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"\nAvailable models ({len(models)}):")
    for m in models:
        print(f"  - {m}")

    test_model = models[0]
    print(f"\nSending a tiny text chat to {test_model!r}...")

    t0 = time.monotonic()
    try:
        result = client.chat_text(
            model=test_model,
            system_prompt="You are a concise assistant.",
            user_prompt="Reply with exactly the word: hello",
            temperature=0.0,
            max_tokens=16,
        )
    except OllamaError as exc:
        print(f"\nFAIL — chat_text raised:\n  {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
    elapsed = time.monotonic() - t0

    print(f"\nResponse received in {elapsed:.2f}s:")
    print(result)
    print("\nOK — Ollama client layer is working.")
