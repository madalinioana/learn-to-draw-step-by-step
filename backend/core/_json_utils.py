"""Shared helpers for talking to Gemini: robust JSON parsing and retry-with-backoff.

Both the Artist (`generator.py`) and the Critic (`critic.py`) ask Gemini for
structured JSON. Free-tier models occasionally ignore `response_mime_type` and
emit markdown fences or trailing prose; this module normalizes that mess into
a plain dict.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, Optional, TypeVar


T = TypeVar("T")


def retry_call(fn: Callable[[], T], attempts: int = 3, base_delay: float = 1.0) -> T:
    """Call `fn()` with exponential backoff on any exception.

    Delays: base_delay, 2*base_delay, 4*base_delay, … up to `attempts` tries.
    Re-raises the final exception if every attempt fails.
    """
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            time.sleep(base_delay * (2 ** i))
    assert last_exc is not None
    raise last_exc


def strip_code_fences(text: str) -> str:
    """Remove a surrounding ```json / ``` wrapper if present."""
    stripped = text.strip()
    fence = re.match(r"^```(?:json|JSON)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return stripped


def extract_first_json_object(text: str) -> Optional[str]:
    """Return the first balanced `{...}` substring, honoring strings and escapes.

    Works even when the model prepends prose or appends trailing commentary.
    Returns None if no balanced object is found.
    """
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def parse_json_payload(raw: str) -> Dict[str, Any]:
    """Parse a Gemini response into a dict.

    Tries, in order:
        1. direct `json.loads` of the raw text
        2. `json.loads` after stripping markdown fences
        3. `json.loads` of the first balanced brace-matched substring

    Raises ValueError with the last underlying error if all strategies fail.
    """
    candidates = [raw, strip_code_fences(raw)]
    extracted = extract_first_json_object(raw)
    if extracted:
        candidates.append(extracted)

    last_err: Optional[Exception] = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except Exception as exc:
            last_err = exc
            continue

    raise ValueError(f"no valid JSON object found: {last_err}")
