"""Record a real Artist-Critic run for the demo gallery.

Runs ArtistCriticLoop against the Gemini (Google API) backend with a single
model for both roles (gemma-4-31b-it by default), captures the per-iteration
generation + critique, and writes a JSON file in the exact shape demo.js
consumes:

    {
      "prompt": "...",
      "iterations": [
        {"svg", "steps", "reasoning", "verdict", "score", "ui_message",
         "feedback_for_artist"},
        ...
      ]
    }

Usage (from repo root, venv active):
    python -m backend.tools.record_run "a flower" --out scratchpad/flower.json

Env: reads GEMINI_API_KEY from .env. Override models with
    GEMINI_ARTIST_MODEL / GEMINI_CRITIC_MODEL (default gemma-4-31b-it).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
load_dotenv(REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(BACKEND_DIR))

from core.gemini_client import GeminiClient  # noqa: E402
from core.orchestrator import ArtistCriticLoop  # noqa: E402


def record(prompt: str, model: str, max_iterations: int) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is empty — set it in .env")

    artist_client = GeminiClient(api_key=api_key, default_model=model)
    critic_client = GeminiClient(api_key=api_key, default_model=model)

    loop = ArtistCriticLoop(
        artist_model=model,
        critic_model=model,
        max_iterations=max_iterations,
        artist_client=artist_client,
        critic_client=critic_client,
        region_lock_enabled=True,
    )

    iterations: list[dict] = []
    pending: dict | None = None

    for event in loop.run_stream(prompt, use_cache=False):
        name = event["event"]
        payload = event["payload"]

        if name == "iteration_start":
            print(f"  [iter {payload.get('index', '?')}] start", flush=True)
            pending = {}
        elif name == "generation_done":
            assert pending is not None
            pending["svg"] = payload["svg"]
            pending["steps"] = payload.get("steps", [])
            pending["reasoning"] = payload.get("reasoning", "")
            print(f"      generated {len(pending['steps'])} step(s)", flush=True)
        elif name == "critique_done":
            assert pending is not None
            pending["verdict"] = payload.get("verdict", "")
            pending["score"] = int(payload.get("score", 0))
            pending["ui_message"] = payload.get("ui_message", "")
            pending["feedback_for_artist"] = payload.get("feedback_for_artist", "")
            print(
                f"      critic: {pending['verdict']} score={pending['score']}",
                flush=True,
            )
            iterations.append(pending)
            pending = None
        elif name == "iteration_error":
            print(f"  !! iteration_error: {payload}", flush=True)
        elif name == "loop_complete":
            print(
                f"  done: {payload.get('total_iterations', len(iterations))} iteration(s)",
                flush=True,
            )

    # Keep only fully-formed iterations (both generation + critique present).
    iterations = [
        it for it in iterations
        if it.get("svg") and "verdict" in it
    ]
    return {"prompt": prompt, "iterations": iterations}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument(
        "--model",
        default=os.environ.get("GEMINI_ARTIST_MODEL", "gemma-4-31b-it"),
    )
    ap.add_argument("--max-iterations", type=int, default=4)
    args = ap.parse_args()

    print(f"Recording '{args.prompt}' with {args.model} ...", flush=True)
    entry = record(args.prompt, args.model, args.max_iterations)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
    n = len(entry["iterations"])
    last = entry["iterations"][-1] if n else {}
    print(
        f"\nWrote {out} — {n} iteration(s), "
        f"final verdict={last.get('verdict', 'n/a')} score={last.get('score', 'n/a')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
