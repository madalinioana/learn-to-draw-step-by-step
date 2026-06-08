"""Backend-neutral error vocabulary for the model clients.

These error types were historically defined in `lmstudio_client.py`. The
supported backends are now Google Gemini (cloud) and Ollama (local), so the
shared errors live in a backend-neutral module that both the clients and the
orchestrator import. Each concrete client (GeminiClient, OllamaClient) raises a
subclass of `ModelBackendError`, so consumers can `except ModelBackendError`
regardless of which backend is active.
"""

from __future__ import annotations

from typing import Optional


class ModelBackendError(RuntimeError):
    """Base error for any model-backend failure.

    The original underlying exception is preserved on `original_exception`.
    """

    def __init__(self, message: str, original_exception: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.original_exception = original_exception


class ModelConnectionError(ModelBackendError):
    """The backend endpoint could not be reached."""


class ModelNotLoadedError(ModelBackendError):
    """The requested model is not available/loaded on the backend."""


class ModelRequestError(ModelBackendError):
    """The backend returned an error response for a request."""
