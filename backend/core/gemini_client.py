"""Google Gemini client implementing the shared model-client interface.

Used to run the Artist-Critic pipeline against a cloud model without rewiring
generator.py / critic.py / orchestrator.py. Public interface:

    chat_text(model, system_prompt, user_prompt, temperature, max_tokens,
              response_format=None) -> str
    chat_vision(model, system_prompt, user_prompt, image_bytes,
                image_format="png", temperature=0.3, max_tokens=1024,
                response_format=None) -> str
    ensure_model_loaded(model_id) -> None      # always a no-op for cloud
    list_loaded_models() -> list[str]          # returns [default_model]

The model-swap polling logic in orchestrator.py never fires here because
`ensure_model_loaded` always succeeds — Gemini is cloud, the model is
"always loaded". `list_loaded_models` returns just the configured model
so the orchestrator's loaded-list display has something to show.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

try:
    import google.generativeai as genai
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "google-generativeai not installed. Run `pip install google-generativeai==0.8.4` "
        "or set USE_GEMINI=0 in .env to use the local Ollama backend."
    ) from exc


logger = logging.getLogger(__name__)


from core.errors import ModelBackendError


class GeminiError(ModelBackendError):
    """Raised when a Gemini API call fails after retries (or with a non-retryable error)."""

    def __init__(self, message: str, original_exception: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class GeminiClient:
    """Thin wrapper over `google.generativeai` matching the shared model-client interface."""

    def __init__(self, api_key: str, default_model: str = "gemini-2.5-flash") -> None:
        if not api_key:
            raise ValueError("api_key is required (set GEMINI_API_KEY in .env)")
        self.api_key = api_key
        self.default_model = default_model
        # Lifetime call counters — useful for spotting quota burn during testing.
        self.call_count_text: int = 0
        self.call_count_vision: int = 0
        try:
            genai.configure(api_key=api_key)
        except Exception as exc:
            raise GeminiError(f"genai.configure failed: {exc}", original_exception=exc) from exc
        logger.info("GeminiClient ready (default_model=%s)", default_model)

    @property
    def total_calls(self) -> int:
        return self.call_count_text + self.call_count_vision

    # ── model discovery / loading ────────────────────────────────────────

    def list_loaded_models(self) -> List[str]:
        """Return the configured model so the orchestrator's swap polling
        sees the requested model and skips the manual-load prompt.

        We could call `genai.list_models()` but it returns a long list of
        Gemini variants and the orchestrator only checks for membership of
        the requested model id.
        """
        return [self.default_model]

    def ensure_model_loaded(self, model_id: str) -> None:
        """No-op for cloud models — Gemini is always 'loaded'.

        The orchestrator's polling logic never fires because we never raise.
        """
        logger.debug("ensure_model_loaded: %s (no-op for Gemini)", model_id)

    # ── chat completions ────────────────────────────────────────────────

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

        config: Dict[str, Any] = {
            "temperature": float(temperature),
            "max_output_tokens": int(max_tokens),
        }
        if response_format and response_format.get("type") == "json_object":
            config["response_mime_type"] = "application/json"

        gen_model = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt or "",
            generation_config=config,
        )

        self.call_count_text += 1
        response = self._generate_with_retry(gen_model, user_prompt)

        text = self._extract_text(response)
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            logger.info(
                "chat_text OK: model=%s call#=%d prompt_tok=%s out_tok=%s total_tok=%s",
                model, self.total_calls,
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
                getattr(usage, "total_token_count", "?"),
            )
        else:
            logger.info(
                "chat_text OK: model=%s call#=%d response_len=%d",
                model, self.total_calls, len(text),
            )
        return text

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
        """Single-turn multimodal chat completion. Image is sent inline."""
        if not model:
            raise ValueError("model is required")
        if not user_prompt:
            raise ValueError("user_prompt is required")
        if not image_bytes:
            raise ValueError("image_bytes is required")

        config: Dict[str, Any] = {
            "temperature": float(temperature),
            "max_output_tokens": int(max_tokens),
        }
        if response_format and response_format.get("type") == "json_object":
            config["response_mime_type"] = "application/json"

        gen_model = genai.GenerativeModel(
            model_name=model,
            system_instruction=system_prompt or "",
            generation_config=config,
        )

        image_part = {"mime_type": f"image/{image_format}", "data": image_bytes}
        self.call_count_vision += 1
        response = self._generate_with_retry(gen_model, [user_prompt, image_part])

        text = self._extract_text(response)
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            logger.info(
                "chat_vision OK: model=%s call#=%d img=%dB prompt_tok=%s out_tok=%s total_tok=%s",
                model, self.total_calls, len(image_bytes),
                getattr(usage, "prompt_token_count", "?"),
                getattr(usage, "candidates_token_count", "?"),
                getattr(usage, "total_token_count", "?"),
            )
        else:
            logger.info(
                "chat_vision OK: model=%s call#=%d img=%dB response_len=%d",
                model, self.total_calls, len(image_bytes), len(text),
            )
        return text

    # ── helpers ─────────────────────────────────────────────────────────

    def _generate_with_retry(self, gen_model: Any, content: Any) -> Any:
        """Call generate_content with exponential backoff on 500 / 429 errors.

        Google returns HTTP 500 when the free-tier daily quota is exhausted
        (instead of a proper 429), so we retry both. Three attempts with
        delays of 5s and 15s covers transient server hiccups without waiting
        forever on a genuinely dead quota.
        """
        delays = [5, 15]
        last_exc: Exception
        for attempt, delay in enumerate([-1] + delays):  # attempt 0 = no sleep
            if delay >= 0:
                logger.warning(
                    "Gemini returned 500/429 (attempt %d/%d) — retrying in %ds "
                    "(may be quota exhaustion disguised as 500)",
                    attempt, len(delays) + 1, delay,
                )
                time.sleep(delay)
            try:
                return gen_model.generate_content(content)
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                # Only retry on server errors and rate-limit signals.
                if not any(sig in exc_str for sig in ("500", "429", "quota", "rate", "Internal error")):
                    break
        logger.error(
            "Gemini call#%d failed after retries: %s: %s",
            self.total_calls, type(last_exc).__name__, last_exc,
        )
        raise GeminiError(
            f"Gemini call failed after retries: {type(last_exc).__name__}: {last_exc}",
            original_exception=last_exc,
        ) from last_exc

    @staticmethod
    def _extract_text(response: Any) -> str:
        """Pull the text content out of a Gemini response. Tolerant of None/empty."""
        text = getattr(response, "text", None)
        if text:
            return text
        try:
            return response.candidates[0].content.parts[0].text
        except Exception as exc:
            raise GeminiError(
                f"Gemini response had no extractable text: {response!r}",
                original_exception=exc,
            ) from exc


if __name__ == "__main__":
    import os
    import sys
    import time

    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("FAIL — GEMINI_API_KEY not set in env or .env", file=sys.stderr)
        sys.exit(1)

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    print(f"Probing Gemini with model {model!r}")
    client = GeminiClient(api_key=api_key, default_model=model)

    t0 = time.monotonic()
    try:
        result = client.chat_text(
            model=model,
            system_prompt="You are a JSON-only assistant.",
            user_prompt='Respond with exactly this JSON object: {"message": "hi"}',
            temperature=0.0,
            max_tokens=64,
            response_format={"type": "json_object"},
        )
    except GeminiError as exc:
        print(f"FAIL — {exc}", file=sys.stderr)
        sys.exit(2)
    elapsed = time.monotonic() - t0

    print(f"\nResponse received in {elapsed:.2f}s:")
    print(result)
    print("\nOK — Gemini client is working.")
