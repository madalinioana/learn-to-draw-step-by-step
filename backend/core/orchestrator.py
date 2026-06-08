"""Orchestrator: wires Generator + Renderer + Critic into the iterative loop.

Critical responsibility unique to the LM Studio backend: explicit model swap
between Artist and Critic on every iteration. LM Studio loads one model at
a time; calling ensure_model_loaded() either confirms the swap (sleeping the
grace period) or raises ModelNotLoadedError, in which case the
orchestrator polls for up to 60 seconds while the user manually swaps the
model in the LM Studio UI. The polling is what makes the demo work without
a babysitter clicking through every swap manually mid-presentation.

Two public entry points:

    - run_stream(prompt, model_swap_mode="polling", use_cache=True)
        Generator function. Yields events of the form
            {"event": <event_name>, "payload": <dict>}
        Caller can render progress incrementally (web UI / CLI tail).

    - run(prompt) -> dict
        Thin wrapper that drains run_stream and returns the loop_complete
        payload. For batch / scripted use.

The Critic returns dual-channel feedback (Prompt 14d):
    feedback_for_artist  → wrapped into a single ADD-style diff and passed
                           to the Generator's revision mode for the next
                           iteration.
    ui_message           → captured per iteration and emitted as
                           feedback_history in the final result so the UI
                           can show one short caption per iteration without
                           rendering the long technical feedback.

Caching: per-instance dict keyed by user_prompt. clear_cache() and
use_cache=False on run_stream both bypass.
"""

from __future__ import annotations

import base64
import logging
import os as _os
import re
import time
from typing import Any, Dict, Iterator, List, Optional

from core.config import (
    ARTIST_MODEL,
    CANVAS_SIZE,
    CRITIC_MODEL,
    MAX_ITERATIONS,
    REQUEST_TIMEOUT_SECONDS,
)
from core.critic import CritiqueError, VisualCritic, is_clear_accept
from core.generator import GenerationError, SVGGenerator
from core.errors import (
    ModelBackendError,
    ModelConnectionError,
    ModelNotLoadedError,
)
from core.renderer import (
    RenderError,
    render_critic_comparison,
    render_svg_for_critic,
    render_svg_progressive,
    render_svg_to_png,
)


logger = logging.getLogger(__name__)
_timing_log = logging.getLogger(__name__ + ".timing")

_FAST_INFERENCE = _os.environ.get("FAST_INFERENCE", "1").strip() != "0"
_FAST_PROGRESSIVE_FRAMES = _os.environ.get(
    "FAST_PROGRESSIVE_FRAMES",
    "0" if _FAST_INFERENCE else "1",
).strip() == "1"
_FAST_CRITIC_COMPARISON = _os.environ.get(
    "FAST_CRITIC_COMPARISON",
    "0" if _FAST_INFERENCE else "1",
).strip() == "1"

# Polling configuration when ensure_model_loaded raises during a swap.
_SWAP_POLL_ATTEMPTS = 12
_SWAP_POLL_INTERVAL_SECONDS = 5.0

_FEATURE_STOPWORDS = {
    "and", "the", "a", "an", "with", "of", "two", "four", "one", "above",
    "below", "under", "on", "to", "in", "for", "simple", "add", "draw",
    "redraw", "inside", "middle", "body", "area", "small", "large",
    "feature", "features", "detail", "details", "mark", "marks", "defining",
}

_FEATURE_ALIASES = {
    "facial": "face",
    "roof": "cabin",
    "cabin": "cabin",
    "window": "window",
    "windows": "window",
    "door": "door",
    "seam": "door",
    "headlight": "headlight",
    "headlights": "headlight",
    "light": "headlight",
    "lights": "headlight",
    "bumper": "bumper",
    "ground": "ground",
    "wheel": "wheel",
    "wheels": "wheel",
}


def _feature_words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in _FEATURE_STOPWORDS or len(word) <= 2:
            continue
        words.add(_FEATURE_ALIASES.get(word, word))
    return words


def _features_overlap(a: str, b: str) -> bool:
    wa, wb = _feature_words(a), _feature_words(b)
    return bool(wa and wb and (wa & wb))


def _status_for_feature(part_status: Any, feature: str) -> str:
    if not isinstance(part_status, list) or not feature:
        return ""
    for entry in part_status:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        name, _, status = entry.partition(":")
        if _features_overlap(name, feature):
            return status.strip().lower()
    return ""


def _append_unique_feature(features: List[str], feature: str) -> None:
    feature = (feature or "").strip()
    if not feature:
        return
    if any(_features_overlap(feature, existing) for existing in features):
        return
    features.append(feature)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode("ascii")




def _compute_evaluation_metrics(iterations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate thesis-level metrics from a finished run's iterations."""
    if not iterations:
        return {
            "iterations_used": 0,
            "final_score": 0,
            "score_trajectory": [],
            "score_delta_mean": 0.0,
            "score_delta_total": 0,
            "best_score": 0,
            "best_score_index": None,
            "first_accept_index": None,
            "total_artist_seconds": 0.0,
            "total_critic_seconds": 0.0,
            "total_render_seconds": 0.0,
            "total_swap_seconds": 0.0,
            "total_paths_restored": 0,
            "total_paths_preserved": 0,
            "total_paths_reverted": 0,
            "preservation_rate": 1.0,
            "redraw_iterations": 0,
        }

    scores = [int(it.get("critic_score", 0)) for it in iterations]

    # Stopping-calibration data: where the Critic *would* have stopped (its
    # first "accept") versus where the trajectory actually peaked. Lets the
    # thesis report whether the Critic's halting judgment is well-calibrated
    # without that judgment ever controlling the loop.
    verdicts = [str(it.get("critic_verdict", "")) for it in iterations]
    first_accept_index = next(
        (idx for idx, v in enumerate(verdicts) if v == "accept"), None
    )
    best_score = max(scores) if scores else 0
    best_score_index = scores.index(best_score) if scores else None

    if len(scores) >= 2:
        deltas = [scores[i + 1] - scores[i] for i in range(len(scores) - 1)]
        delta_mean = sum(deltas) / len(deltas)
        delta_total = scores[-1] - scores[0]
    else:
        # Single-iteration run: no delta to compute.
        delta_mean = 0.0
        delta_total = 0

    total_paths_restored = sum(
        int(it.get("paths_restored", 0)) for it in iterations
    )
    total_paths_preserved = sum(
        int((it.get("preservation_report") or {}).get("kept", 0)) for it in iterations
    )
    total_paths_reverted = sum(
        int((it.get("preservation_report") or {}).get("reverted", 0)) for it in iterations
    )
    # Overall preservation rate: fraction of iteration-pairs where no unintended
    # path changes occurred (reverted == 0 means perfect preservation that iter).
    revision_iters = [it for it in iterations[1:] if it.get("preservation_report")]
    if revision_iters:
        perfect = sum(
            1 for it in revision_iters
            if not (it.get("preservation_report") or {}).get("reverted", 0)
        )
        preservation_rate = perfect / len(revision_iters)
    else:
        preservation_rate = 1.0

    redraw_iterations = sum(1 for it in iterations if it.get("redraw_intent", False))

    return {
        "iterations_used": len(iterations),
        "final_score": scores[-1],
        "score_trajectory": scores,
        "score_delta_mean": float(delta_mean),
        "score_delta_total": int(delta_total),
        "best_score": best_score,
        "best_score_index": best_score_index,
        "first_accept_index": first_accept_index,
        "total_artist_seconds": sum(float(it.get("artist_seconds", 0.0)) for it in iterations),
        "total_critic_seconds": sum(float(it.get("critic_seconds", 0.0)) for it in iterations),
        "total_render_seconds": sum(float(it.get("render_seconds", 0.0)) for it in iterations),
        "total_swap_seconds": sum(float(it.get("model_swap_seconds", 0.0)) for it in iterations),
        "total_paths_restored": total_paths_restored,
        "total_paths_preserved": total_paths_preserved,
        "total_paths_reverted": total_paths_reverted,
        "preservation_rate": preservation_rate,
        "redraw_iterations": redraw_iterations,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class ArtistCriticLoop:
    """Drives the Artist → Renderer → Critic iterative loop with model-swap polling."""

    def __init__(
        self,
        artist_model: str = "",
        critic_model: str = "",
        max_iterations: int = MAX_ITERATIONS,
        *,
        artist_client: Optional[Any] = None,
        critic_client: Optional[Any] = None,
        region_lock_enabled: bool = True,
    ) -> None:
        # artist_client and critic_client can be different objects (e.g.,
        # GeminiClient for Artist, OllamaClient for Critic) or the same one.
        a_client = artist_client
        c_client = critic_client
        if a_client is None:
            raise ValueError("artist_client is required")
        if c_client is None:
            raise ValueError("critic_client is required")
        if not artist_model:
            raise ValueError("artist_model is required")
        if not critic_model:
            raise ValueError("critic_model is required")
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")

        self.artist_client = a_client
        self.critic_client = c_client
        # Legacy alias used only by code paths that pre-date the split.
        self.client = a_client
        self.artist_model = artist_model
        self.critic_model = critic_model
        self.max_iterations = max_iterations
        self.region_lock_enabled = bool(region_lock_enabled)

        self.generator = SVGGenerator(a_client, artist_model)
        self.critic = VisualCritic(c_client, critic_model)
        self._cache: Dict[str, Dict[str, Any]] = {}

        same_backend = a_client is c_client
        logger.info(
            "ArtistCriticLoop ready: artist=%s critic=%s max_iter=%d backends=%s region_lock=%s",
            artist_model, critic_model, max_iterations,
            "shared" if same_backend else "split",
            self.region_lock_enabled,
        )

    # ── public ────────────────────────────────────────────────────────────

    def clear_cache(self) -> None:
        """Drop all cached results."""
        self._cache.clear()

    def run(self, user_prompt: str) -> Dict[str, Any]:
        """Drain run_stream and return the loop_complete payload."""
        for event in self.run_stream(user_prompt):
            if event["event"] == "loop_complete":
                return event["payload"]
        raise RuntimeError("run_stream ended without yielding loop_complete")

    def run_stream(
        self,
        user_prompt: str,
        model_swap_mode: str = "polling",
        use_cache: bool = True,
    ) -> Iterator[Dict[str, Any]]:
        """Generator yielding {"event": ..., "payload": ...} dicts.

        model_swap_mode: "polling" (default — waits up to 60s for the user
            to swap models in LM Studio's UI) or "strict" (fails fast on
            ModelNotLoadedError).
        use_cache: if True (default), cached results are returned without
            re-running the loop. cache_hit + loop_complete events are
            yielded back-to-back.
        """
        if not user_prompt or not user_prompt.strip():
            raise ValueError("user_prompt is required")
        if model_swap_mode not in ("polling", "strict"):
            raise ValueError(f"model_swap_mode must be 'polling' or 'strict', got {model_swap_mode!r}")

        cache_key = user_prompt.strip()

        # ── Cache hit fast path ───────────────────────────────────────────
        if use_cache and cache_key in self._cache:
            logger.info("loop cache hit: %r", cache_key)
            yield {"event": "cache_hit", "payload": {"user_prompt": cache_key}}
            yield {"event": "loop_complete", "payload": self._cache[cache_key]}
            return

        loop_t0 = time.monotonic()
        iterations: List[Dict[str, Any]] = []
        stopped_reason = "max_iterations_reached"
        error_message: Optional[str] = None
        # Cross-iteration loop state — the only feature memory the loop needs:
        #   attempted_features — parts already on the canvas (locked); the Critic
        #       must not re-request these. A part is locked once the Critic reports
        #       it present/weak OR the Artist actually drew geometry for it.
        #   requested_targets  — every feature the Critic has asked for, in order.
        #       A feature asked for twice becomes "banned" so the loop moves on —
        #       this is the "one extra chance, then advance" rule.
        attempted_features: List[str] = []
        requested_targets: List[str] = []
        logger.info(
            "loop start: prompt=%r max_iter=%d swap_mode=%s",
            user_prompt, self.max_iterations, model_swap_mode,
        )

        # ── Iteration loop ────────────────────────────────────────────────
        for i in range(self.max_iterations):
            iter_t0 = time.monotonic()
            swap_seconds_total = 0.0
            iter_failed = False

            yield {"event": "iteration_start", "payload": {
                "index": i, "total": self.max_iterations,
            }}

            # --- Swap to Artist -------------------------------------------------
            swap_state: Dict[str, Any] = {"success": False, "elapsed": 0.0, "error": None}
            yield from self._swap_with_polling(self.artist_model, "artist", model_swap_mode, swap_state)
            swap_seconds_total += float(swap_state["elapsed"])
            if not swap_state["success"]:
                yield {"event": "iteration_error", "payload": {
                    "index": i,
                    "error_type": "ModelSwapFailed",
                    "error_message": swap_state.get("error") or f"could not swap to {self.artist_model}",
                }}
                error_message = swap_state.get("error") or f"swap to {self.artist_model} failed"
                stopped_reason = "error"
                break

            # --- Generate -------------------------------------------------------
            yield {"event": "generation_start", "payload": {}}
            gen_t0 = time.monotonic()
            try:
                if i == 0 or not iterations:
                    gen_result = self.generator.generate(
                        user_prompt=user_prompt,
                        iteration=i,
                        max_iterations=self.max_iterations,
                    )
                else:
                    prev = iterations[-1]
                    generation_action = prev.get("critic_action")
                    generation_target = prev.get("critic_target_region")
                    if not self.region_lock_enabled:
                        # Ablation mode: keep the Critic loop but remove the
                        # code-enforced/additive region lock. The Artist sees
                        # the previous critique and redraws the full subject,
                        # so earlier parts may drift or regress.
                        generation_action = "redraw_all"
                        generation_target = ""
                    gen_result = self.generator.generate(
                        user_prompt=user_prompt,
                        previous_svg=prev["svg"],
                        critic_feedback=prev["critic_feedback"],
                        previous_steps=prev.get("generator_steps") or [],
                        iteration=i,
                        max_iterations=self.max_iterations,
                        critic_action=generation_action,
                        critic_target=generation_target,
                    )
            except (GenerationError, ModelBackendError, Exception) as exc:
                logger.exception("generation failed in iteration %d", i)
                yield {"event": "iteration_error", "payload": {
                    "index": i,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }}
                error_message = f"{type(exc).__name__}: {exc}"
                stopped_reason = "error"
                iter_failed = True
                break
            artist_seconds = time.monotonic() - gen_t0

            yield {"event": "generation_done", "payload": {
                "svg": gen_result["svg"],
                "reasoning": gen_result["reasoning"],
                "steps": gen_result["steps"],
                "style_notes": gen_result.get("style_notes", ""),
                "elapsed_seconds": artist_seconds,
            }}

            # --- Render ---------------------------------------------------------
            yield {"event": "render_start", "payload": {}}
            render_t0 = time.monotonic()
            # DEBUG_RENDER=1 dumps the post-generation SVG and a path-count
            # report so a flower-vs-stems regression can be diagnosed off-line.
            if _os.environ.get("DEBUG_RENDER", "").strip() == "1":
                try:
                    import datetime as _dt
                    import re as _re
                    debug_dir = _os.path.join(
                        _os.environ.get("TMPDIR") or _os.environ.get("TEMP") or "/tmp",
                        "sketch_debug",
                    )
                    _os.makedirs(debug_dir, exist_ok=True)
                    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    src_path = _os.path.join(debug_dir, f"iter{i:02d}_{ts}_source.svg")
                    with open(src_path, "w", encoding="utf-8") as f:
                        f.write(gen_result["svg"])
                    n_paths = len(_re.findall(r"<path\b", gen_result["svg"]))
                    n_steps = len(gen_result.get("steps") or [])
                    logger.info(
                        "DEBUG_RENDER iter=%d: SVG=%s paths_in_source=%d steps_in_payload=%d",
                        i, src_path, n_paths, n_steps,
                    )
                except Exception as exc:
                    logger.warning("DEBUG_RENDER pre-render dump failed: %s", exc)
            try:
                _t_ui_render = time.monotonic()
                ui_png_bytes = render_svg_to_png(gen_result["svg"], size=CANVAS_SIZE)
                _ui_render_seconds = time.monotonic() - _t_ui_render

                _t_prog_render = time.monotonic()
                if _FAST_PROGRESSIVE_FRAMES:
                    progressive_frames = render_svg_progressive(gen_result["svg"], size=CANVAS_SIZE)
                else:
                    progressive_frames = []
                _prog_render_seconds = time.monotonic() - _t_prog_render

                _t_critic_render = time.monotonic()
                critic_comparison_image = bool(iterations) and _FAST_CRITIC_COMPARISON
                if critic_comparison_image:
                    critic_png_bytes = render_critic_comparison(
                        iterations[-1]["svg"],
                        gen_result["svg"],
                    )
                else:
                    critic_png_bytes = render_svg_for_critic(gen_result["svg"])
                _critic_render_seconds = time.monotonic() - _t_critic_render
            except (RenderError, Exception) as exc:
                logger.exception("rendering failed in iteration %d", i)
                yield {"event": "iteration_error", "payload": {
                    "index": i,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }}
                error_message = f"{type(exc).__name__}: {exc}"
                stopped_reason = "error"
                iter_failed = True
                break
            render_seconds = time.monotonic() - render_t0
            _timing_log.info(
                "[render] ui=%.3fs  progressive=%d_frames/%.3fs  critic=%.3fs  total=%.3fs",
                _ui_render_seconds, len(progressive_frames), _prog_render_seconds,
                _critic_render_seconds, render_seconds,
            )

            ui_png_b64 = _b64(ui_png_bytes)
            progressive_b64 = [_b64(f) for f in progressive_frames]

            yield {"event": "render_done", "payload": {
                "png_b64": ui_png_b64,
                "progressive_frames_b64": progressive_b64,
                "elapsed_seconds": render_seconds,
            }}

            # --- Swap to Critic -------------------------------------------------
            swap_state = {"success": False, "elapsed": 0.0, "error": None}
            yield from self._swap_with_polling(self.critic_model, "critic", model_swap_mode, swap_state)
            swap_seconds_total += float(swap_state["elapsed"])
            if not swap_state["success"]:
                yield {"event": "iteration_error", "payload": {
                    "index": i,
                    "error_type": "ModelSwapFailed",
                    "error_message": swap_state.get("error") or f"could not swap to {self.critic_model}",
                }}
                error_message = swap_state.get("error") or f"swap to {self.critic_model} failed"
                stopped_reason = "error"
                break

            # --- Critique -------------------------------------------------------
            yield {"event": "critique_start", "payload": {}}
            crit_t0 = time.monotonic()
            try:
                prev_feedback: Optional[str] = None
                last_requested_feature: Optional[str] = None
                if iterations:
                    prev_feedback = iterations[-1].get("critic_feedback") or None
                    last_requested_feature = (
                        iterations[-1].get("critic_target_region") or None
                    )
                # Did the Artist add new geometry for the last requested feature on
                # *this* pass? If so it is on the canvas now even when the vision
                # model can't re-perceive thin strokes, so we lock it immediately
                # and let the Critic advance instead of re-asking for a wasted pass.
                _prev_steps_n = (
                    len(iterations[-1].get("generator_steps") or []) if iterations else 0
                )
                _added_this_iter = len(gen_result.get("steps") or []) - _prev_steps_n

                # Features the Critic may NOT request: those already on the canvas
                # (locked) and those asked for twice already (the "move on" rule).
                locked_features = list(attempted_features)
                if last_requested_feature and _added_this_iter > 0:
                    _append_unique_feature(locked_features, last_requested_feature)
                banned_features = [
                    t for t in dict.fromkeys(requested_targets)
                    if sum(1 for r in requested_targets if _features_overlap(r, t)) >= 2
                ]

                critique = self.critic.critique(
                    user_prompt=user_prompt,
                    rendered_png_bytes=critic_png_bytes,
                    iteration=i,
                    max_iterations=self.max_iterations,
                    previous_feedback=prev_feedback,
                    last_requested_feature=last_requested_feature,
                    locked_features=locked_features,
                    banned_features=banned_features,
                    comparison_image=critic_comparison_image,
                )
            except (CritiqueError, ModelBackendError, Exception) as exc:
                logger.exception("critique failed in iteration %d", i)
                yield {"event": "iteration_error", "payload": {
                    "index": i,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }}
                error_message = f"{type(exc).__name__}: {exc}"
                stopped_reason = "error"
                iter_failed = True
                break
            critic_seconds = time.monotonic() - crit_t0

            _part_status = critique.get("part_status") or []

            # Record the feature this critique asked for (the twice-max counter).
            _new_target = str(critique.get("target_region") or "").strip()
            if _new_target:
                requested_targets.append(_new_target)

            # Lock the previously requested feature once the Artist has had its
            # pass at it — either the Critic now re-perceives it (present/weak) OR
            # the Artist actually drew new geometry for it this iteration. Gating
            # purely on re-perception deadlocks the loop on thin features (a model
            # can't always re-see two hairline strokes); "the Artist drew it" is
            # the signal its eyes can't fool. At most one feature locks per pass.
            if last_requested_feature:
                _last_status = _status_for_feature(_part_status, last_requested_feature)
                if _last_status in ("present", "weak") or _added_this_iter > 0:
                    _append_unique_feature(attempted_features, last_requested_feature)

            # Cumulative present-lock: once the Critic reports a part present or
            # weak, lock it for the rest of the run so an unstable later perception
            # can't re-request a part that is already on the canvas.
            for _entry in _part_status:
                if not isinstance(_entry, str) or ":" not in _entry:
                    continue
                _name, _, _st = _entry.partition(":")
                if _st.strip().lower() in ("present", "weak") and _name.strip():
                    _append_unique_feature(attempted_features, _name.strip())

            yield {"event": "critique_done", "payload": {
                "verdict": critique["verdict"],
                "score": critique["score"],
                "part_status": critique.get("part_status", []),
                "reasoning": critique["reasoning"],
                "observations": critique.get("observations", []),
                "feedback_for_artist": critique.get("feedback_for_artist", ""),
                "remaining_feedback": critique.get("remaining_feedback", []),
                "ui_message": critique.get("ui_message", ""),
                "action": critique.get("action", ""),
                "target_region": critique.get("target_region", ""),
                "elapsed_seconds": critic_seconds,
            }}

            # --- Build iteration record -----------------------------------------
            iter_total_seconds = time.monotonic() - iter_t0
            _pres_report = gen_result.get("preservation_report") or {}
            _redraw_intent = bool(gen_result.get("redraw_intent", False))
            iteration_dict = {
                "index": i,
                "svg": gen_result["svg"],
                "png_bytes_b64": ui_png_b64,
                "progressive_frames_b64": progressive_b64,
                "generator_reasoning": gen_result["reasoning"],
                "generator_steps": gen_result["steps"],
                "generator_style_notes": gen_result.get("style_notes", ""),
                "critic_verdict": critique["verdict"],
                "critic_score": int(critique["score"]),
                "critic_part_status": critique.get("part_status", []),
                "critic_observations": critique.get("observations", []),
                "critic_feedback": critique.get("feedback_for_artist", ""),
                "critic_remaining_feedback": critique.get("remaining_feedback", []),
                "critic_ui_message": critique.get("ui_message", ""),
                "critic_action": critique.get("action", ""),
                "critic_target_region": critique.get("target_region", ""),
                "critic_reasoning": critique["reasoning"],
                "elapsed_seconds": iter_total_seconds,
                "artist_seconds": artist_seconds,
                "critic_seconds": critic_seconds,
                "render_seconds": render_seconds,
                "model_swap_seconds": swap_seconds_total,
                "preservation_report": _pres_report,
                "paths_restored": len(gen_result.get("_restored_ids", [])),
                "redraw_intent": _redraw_intent,
            }
            iterations.append(iteration_dict)

            yield {"event": "iteration_end", "payload": {"iteration_dict": iteration_dict}}

            logger.info(
                "iteration %d/%d done: verdict=%s score=%d artist=%.1fs render=%.1fs critic=%.1fs swap=%.1fs",
                i + 1, self.max_iterations,
                critique["verdict"], critique["score"],
                artist_seconds, render_seconds, critic_seconds, swap_seconds_total,
            )

            # ── Structured iteration timing report ───────────────────────────
            _gen_timing = gen_result.get("_timing", {})
            _gen_phases = _gen_timing.get("phases", {})
            _gen_retries = _gen_timing.get("retries", [])
            _retry_lines = "\n".join(
                f"      retry {j+1}: trigger={r['reason']}  time={r['seconds']:.3f}s"
                for j, r in enumerate(_gen_retries)
            ) if _gen_retries else "    (no retries)"
            _timing_log.info(
                "\n"
                "  ARTIST ITERATION TIMING BREAKDOWN  (iter %d/%d)\n"
                "  ─────────────────────────────────\n"
                "  Total iteration time:         %6.3fs\n"
                "\n"
                "  Artist (Generator):           %6.3fs\n"
                "    Setup:                      %6.3fs\n"
                "    API call (initial):         %6.3fs\n"
                "    Response processing:        %6.3fs\n"
                "    Style validation:           %6.3fs\n"
                "    Auto-repair:                %6.3fs\n"
                "    Preservation check:         %6.3fs\n"
                "    Wobblify:                   %6.3fs\n"
                "    Retries:                    %d retries  %.3fs total\n"
                "%s\n"
                "\n"
                "  Render:                       %6.3fs\n"
                "    UI render:                  %6.3fs\n"
                "    Critic render:              %6.3fs\n"
                "    Progressive frames (%d):    %6.3fs\n"
                "\n"
                "  Critic:                       %6.3fs\n"
                "  Model swap:                   %6.3fs\n",
                i + 1, self.max_iterations,
                iter_total_seconds,
                artist_seconds,
                _gen_phases.get("setup", 0.0),
                _gen_phases.get("api_initial", 0.0),
                _gen_phases.get("response_processing", 0.0),
                _gen_phases.get("style_validation", 0.0),
                _gen_phases.get("auto_repair", 0.0),
                _gen_phases.get("preservation", 0.0),
                _gen_phases.get("wobblify", 0.0),
                len(_gen_retries), sum(r["seconds"] for r in _gen_retries),
                _retry_lines,
                render_seconds,
                _ui_render_seconds,
                _critic_render_seconds,
                len(progressive_frames), _prog_render_seconds,
                critic_seconds,
                swap_seconds_total,
            )

            if is_clear_accept(critique):
                stopped_reason = "accepted"
                logger.info(
                    "loop accepted at iteration %d/%d: score=%s",
                    i + 1,
                    self.max_iterations,
                    critique.get("score"),
                )
                break

        # ── Build final result ────────────────────────────────────────────
        total_elapsed = time.monotonic() - loop_t0

        if iterations:
            final_svg = iterations[-1]["svg"]
            final_png_b64 = iterations[-1]["png_bytes_b64"]
        else:
            final_svg = ""
            final_png_b64 = ""

        feedback_history = [it["critic_ui_message"] for it in iterations if it.get("critic_ui_message")]

        result = {
            "user_prompt": user_prompt,
            "iterations": iterations,
            "final_svg": final_svg,
            "final_png_bytes_b64": final_png_b64,
            "total_elapsed_seconds": total_elapsed,
            "stopped_reason": stopped_reason,
            "error": error_message,
            "feedback_history": feedback_history,
            "evaluation_metrics": _compute_evaluation_metrics(iterations),
        }

        # Cache only successful runs.
        if stopped_reason in ("accepted", "max_iterations_reached") and use_cache:
            self._cache[cache_key] = result

        em = result["evaluation_metrics"]
        logger.info(
            "loop end: reason=%s iters=%d final_score=%s elapsed=%.1fs "
            "(artist=%.1fs critic=%.1fs render=%.1fs swap=%.1fs)",
            stopped_reason, em["iterations_used"], em["final_score"], total_elapsed,
            em["total_artist_seconds"], em["total_critic_seconds"],
            em["total_render_seconds"], em["total_swap_seconds"],
        )

        yield {"event": "loop_complete", "payload": result}

    # ── private ───────────────────────────────────────────────────────────

    def _swap_with_polling(
        self,
        target_model: str,
        role: str,
        mode: str,
        result: Dict[str, Any],
    ) -> Iterator[Dict[str, Any]]:
        """Generator: yields swap events. Writes outcome into `result` dict.

        result keys set on completion:
            success: bool
            elapsed: float seconds (total time including any polling)
            error:   str or None

        On strict mode + ModelNotLoadedError: yields no polling events,
        sets success=False and bubbles up the message.
        On polling mode: yields model_swap_required + N×model_swap_waiting +
        terminal model_swap_done. Up to _SWAP_POLL_ATTEMPTS retries with
        _SWAP_POLL_INTERVAL_SECONDS sleep between each.

        Hybrid backend support: when artist and critic clients are different
        objects (e.g., Gemini for Artist, LM Studio for Critic), each role
        polls its own client. Cloud clients have a no-op ensure_model_loaded
        so the polling never fires for them.
        """
        client = self.artist_client if role == "artist" else self.critic_client
        swap_t0 = time.monotonic()

        yield {"event": "model_swap_start", "payload": {
            "target_model": target_model, "role": role,
        }}

        # First attempt — common case (no swap needed).
        try:
            client.ensure_model_loaded(target_model)
            elapsed = time.monotonic() - swap_t0
            yield {"event": "model_swap_done", "payload": {
                "target_model": target_model, "role": role, "elapsed_seconds": elapsed,
            }}
            result["success"] = True
            result["elapsed"] = elapsed
            return
        except ModelConnectionError as exc:
            # Hard infrastructure failure — surface immediately regardless of mode.
            elapsed = time.monotonic() - swap_t0
            result["success"] = False
            result["elapsed"] = elapsed
            result["error"] = f"Backend unreachable: {exc}"
            return
        except ModelNotLoadedError as exc:
            if mode == "strict":
                elapsed = time.monotonic() - swap_t0
                result["success"] = False
                result["elapsed"] = elapsed
                result["error"] = str(exc)
                return
            # fall through to polling

        # Polling mode — surface a manual-swap prompt and wait.
        try:
            currently_loaded = client.list_loaded_models()
        except Exception:
            currently_loaded = []

        yield {"event": "model_swap_required", "payload": {
            "required_model": target_model,
            "currently_loaded": currently_loaded,
            "instruction": (
                f"Model {target_model!r} is not available on the backend. "
                f"The orchestrator will keep polling for up to "
                f"{int(_SWAP_POLL_ATTEMPTS * _SWAP_POLL_INTERVAL_SECONDS)} seconds."
            ),
        }}

        for attempt in range(1, _SWAP_POLL_ATTEMPTS + 1):
            time.sleep(_SWAP_POLL_INTERVAL_SECONDS)
            yield {"event": "model_swap_waiting", "payload": {
                "required_model": target_model,
                "attempt": attempt,
                "max_attempts": _SWAP_POLL_ATTEMPTS,
            }}
            try:
                client.ensure_model_loaded(target_model)
                elapsed = time.monotonic() - swap_t0
                yield {"event": "model_swap_done", "payload": {
                    "target_model": target_model, "role": role, "elapsed_seconds": elapsed,
                }}
                result["success"] = True
                result["elapsed"] = elapsed
                return
            except ModelNotLoadedError:
                continue
            except ModelConnectionError as exc:
                elapsed = time.monotonic() - swap_t0
                result["success"] = False
                result["elapsed"] = elapsed
                result["error"] = f"Backend unreachable during polling: {exc}"
                return

        # All polling attempts exhausted.
        elapsed = time.monotonic() - swap_t0
        result["success"] = False
        result["elapsed"] = elapsed
        result["error"] = (
            f"model {target_model!r} was not loaded after "
            f"{_SWAP_POLL_ATTEMPTS} polling attempts ({elapsed:.0f}s)"
        )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    from core.config import OLLAMA_BASE_URL, OLLAMA_API_KEY
    from core.ollama_client import OllamaClient

    print(f"Ollama:       {OLLAMA_BASE_URL}")
    print(f"Artist model: {ARTIST_MODEL}")
    print(f"Critic model: {CRITIC_MODEL}")
    print()

    client = OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=ARTIST_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    loop = ArtistCriticLoop(
        artist_client=client,
        critic_client=client,
        artist_model=ARTIST_MODEL,
        critic_model=CRITIC_MODEL,
        max_iterations=3,
    )

    prompt = "a smiling sun"
    print(f"━━━ Starting loop for: {prompt!r}\n")

    final_result: Optional[Dict[str, Any]] = None

    def ts() -> str:
        return time.strftime("%H:%M:%S")

    try:
        for event in loop.run_stream(prompt, model_swap_mode="polling"):
            evt = event["event"]
            payload = event["payload"]

            if evt == "iteration_start":
                print(f"\n{ts()}  ━━━ ITERATION {payload['index'] + 1}/{payload['total']} ━━━")
            elif evt == "model_swap_start":
                print(f"{ts()}  → swap to {payload['target_model']} ({payload['role']})")
            elif evt == "model_swap_required":
                print(f"\n{ts()}  ⚠  MANUAL SWAP REQUIRED")
                print(f"     {payload['instruction']}")
                loaded = ", ".join(payload["currently_loaded"]) if payload["currently_loaded"] else "(none)"
                print(f"     currently loaded: {loaded}")
            elif evt == "model_swap_waiting":
                print(f"{ts()}  ⋯ poll {payload['attempt']}/{payload['max_attempts']}")
            elif evt == "model_swap_done":
                print(f"{ts()}  ✓ swap done in {payload['elapsed_seconds']:.1f}s")
            elif evt == "generation_start":
                print(f"{ts()}  ✎ generating SVG...")
            elif evt == "generation_done":
                print(f"{ts()}  ✓ generated in {payload['elapsed_seconds']:.1f}s "
                      f"({len(payload['steps'])} step(s))")
            elif evt == "render_start":
                print(f"{ts()}  ⏶ rendering UI + critic PNGs...")
            elif evt == "render_done":
                print(f"{ts()}  ✓ rendered in {payload['elapsed_seconds']:.2f}s")
            elif evt == "critique_start":
                print(f"{ts()}  ◉ critiquing...")
            elif evt == "critique_done":
                print(f"{ts()}  ✓ critique in {payload['elapsed_seconds']:.1f}s — "
                      f"verdict={payload['verdict']} score={payload['score']}/10")
                print(f"     ui_message: {payload['ui_message']}")
            elif evt == "iteration_end":
                pass
            elif evt == "iteration_error":
                print(f"\n{ts()}  ✗ ITERATION ERROR: {payload['error_type']}: {payload['error_message']}")
            elif evt == "cache_hit":
                print(f"{ts()}  ⚡ cache hit for {payload['user_prompt']!r}")
            elif evt == "loop_complete":
                final_result = payload
                print(f"\n{ts()}  ━━━ LOOP COMPLETE ━━━")
            else:
                print(f"{ts()}  [{evt}] {payload}")
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)

    if final_result is None:
        print("\nFAIL — run_stream did not yield loop_complete.", file=sys.stderr)
        sys.exit(1)

    em = final_result["evaluation_metrics"]
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Stopped reason:    {final_result['stopped_reason']}")
    print(f"Iterations used:   {em['iterations_used']}")
    print(f"Final score:       {em['final_score']}/10")
    print(f"Score trajectory:  {em['score_trajectory']}")
    print(f"Mean delta/iter:   {em['score_delta_mean']:+.2f}")
    print(f"Total delta:       {em['score_delta_total']:+d}")
    print(f"Total elapsed:     {final_result['total_elapsed_seconds']:.1f}s")
    print(f"  artist time:     {em['total_artist_seconds']:.1f}s")
    print(f"  critic time:     {em['total_critic_seconds']:.1f}s")
    print(f"  render time:     {em['total_render_seconds']:.1f}s")
    print(f"  swap time:       {em['total_swap_seconds']:.1f}s")
    print()
    print("Feedback history (one ui_message per iteration):")
    for i, msg in enumerate(final_result["feedback_history"], 1):
        print(f"  {i}. {msg}")

    if final_result.get("error"):
        print(f"\nError: {final_result['error']}")
        sys.exit(2)
    sys.exit(0)
