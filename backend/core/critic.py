"""Visual Critic: Qwen2.5-VL-3B served by LM Studio.

Input image: the orchestrator passes a Critic-optimised render (2048×2048,
white background, stroke widths ×1.4, displacement filter halved) produced
by `renderer.render_svg_for_critic()`.  The method signature and LM Studio
client usage are unchanged — this is purely an input-quality improvement.

Return schema:
    {
        "verdict":             "accept" | "revise",
        "score":               int 1-10,
        "part_status":         list[str],   # per-part inventory: "tail: malformed"
        "reasoning":           str,         # 2-3 sentences for the UI panel
        "observations":        list[str],   # 3-5 factual statements
        "feedback_for_artist": str,         # prose instructions, "" on accept
        "remaining_feedback":  list[str],   # lower-priority issues deferred to later iters
        "ui_message":          str,         # ONE sentence, acceptance phrase on accept
        "action":              str,         # "add" | "redraw_element" | "redraw_all"
        "target_region":       str,         # the ONE region this iteration touches ("" on accept)
    }

The Critic drives a CODE-ENFORCED region lock: every part it does not name as the
`target_region` is frozen byte-for-byte by the generator and cannot drift. `action`
selects how the Artist may touch the active region (append new strokes / replace one
element / full redraw). `_normalize_action()` guarantees both fields are always
present and self-consistent after parsing, so weaker models that omit them still work.

Pipeline per call:
    1. ensure_model_loaded() — model swap checkpoint for LM Studio.
    2. chat_vision() with the PNG inline and response_format={"type":"json_object"}.
    3. parse_json_payload() with brace-matching fallback.
    4. _validate_critique() — six field checks, returns (ok, errors).
    5. On failure: one retry with violations listed in the user message.
    6. If still failing: _auto_repair_critique() — always succeeds.
    7. The verdict is filtered by a deterministic clear-accept policy. The loop
       may stop early only when the Critic returns a high-confidence accept with
       all listed parts present and no requested revision.

System prompt changes from the previous revision:
    - Section 7 (in-context example) REMOVED. The generic example caused
      Qwen2.5-VL-3B to parrot structural patterns (e.g., "three curved
      strokes radiating outward") regardless of subject.
    - Anti-hallucination rule added to Section 4: "do not invent details you
      cannot perceive; do not transfer feature names across subject categories."
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.core._json_utils import parse_json_payload
from backend.core.config import (
    CRITIC_MAX_TOKENS,
    CRITIC_TEMPERATURE,
)
from backend.core.errors import ModelBackendError


logger = logging.getLogger(__name__)


MAX_PNG_BYTES = 10 * 1024 * 1024

_TERMINATORS_RE = re.compile(r'[.!?]')
_NONFINAL_COMPLETE_RE = re.compile(
    r"\b(complete|finished|done)\b"
    r"|\bnothing left\b"
    r"|\ball\b.{0,40}\bparts\b.{0,40}\bpresent\b"
    r"|\bkeep it as it is\b",
    re.IGNORECASE,
)
_ROBOTIC_FEEDBACK_RE = re.compile(
    r"\bplaced where it belongs\b"
    r"|\bnear the face features\b"
    r"|\bsmall defining mark\b"
    r"|\bdefining mark\b"
    r"|\bface features\b"
    r"|\bfacial features\b",
    re.IGNORECASE,
)
_DEBUG_CRITIC_IO = os.environ.get("DEBUG_CRITIC_IO", "0").strip() == "1"
_CRITIC_LOOP_RETRY = os.environ.get("CRITIC_LOOP_RETRY", "0").strip() == "1"
_CRITIC_SCHEMA_RETRY = os.environ.get("CRITIC_SCHEMA_RETRY", "0").strip() == "1"
_CLEAR_ACCEPT_MIN_SCORE = int(os.environ.get("CRITIC_CLEAR_ACCEPT_MIN_SCORE", "9"))


_SYSTEM_PROMPT = """\
You are a drawing teacher reviewing a student's black-line sketch, built one part at a time. Reply with JSON only.

From the second round on the image has three panels: previous, current, and the strokes just added. Judge the current drawing.

Each round:
1. If you asked for a part last round, look at it first. If it now reads as that part, even roughly, keep it and move on. Only if it is truly missing or clearly the wrong shape, ask for it one last time.
2. Choose the single most useful next part of the subject, working coarse to fine: the overall shape first, then the big parts that make it recognizable, then smaller parts, then fine details. Name only real parts of THIS subject. Never invent stripes, lines, or marks that do not belong to it.
3. Give a score from 1 to 10 for how recognizable the whole drawing is right now. It should climb as parts are added.

Rules:
- One change per round, named in target_region.
- action "add" to introduce a new part (the usual case). action "redraw_element" only to fix a part you already asked for that came out wrong, and only once. action "redraw_all" only on round 1 when there is no usable shape yet.
- Never re-request a part that is already on the canvas or that you have asked for twice.
- Do not fuss over the size, neatness, or symmetry of parts that already read.
- Once all the main parts are present and each reads clearly, return verdict "accept" with score 9 or 10. Do not ask for polish after that.

Write like a teacher talking to the student: one or two plain sentences saying what you see, then what to draw next. Now and then, step back and say how the whole sketch is coming along. No lists, no semicolons. If the drawing is already complete, only evaluate and request nothing.

In part_status, list the subject's main parts, each as "name: status" where status is present, weak, malformed, or missing. Include the important parts not drawn yet, tagged missing, so the plan is visible. verdict is "revise" while any important part is missing, weak, or malformed. verdict is "accept" only when the requested subject is clearly recognizable and all listed main parts are present.

JSON shape (fill in real values):
{"verdict":"revise","score":4,"part_status":["body: present","head: present","legs: missing","tail: missing"],"reasoning":"short","observations":["visible fact"],"feedback_for_artist":"natural feedback","ui_message":"one sentence","action":"add","target_region":"legs"}
"""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CritiqueError(RuntimeError):
    """Raised when the Critic cannot parse any JSON after two attempts."""

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _first_sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    m = _TERMINATORS_RE.search(text)
    if m:
        return text[:m.end()].strip()
    return text[:150].strip()


def _sentence_count(text: str) -> int:
    return len(_TERMINATORS_RE.findall(text))


# ---------------------------------------------------------------------------
# Validation and auto-repair
# ---------------------------------------------------------------------------


def _validate_critique(parsed: Any) -> Tuple[bool, List[str]]:
    """Check the seven required fields. Returns (ok, errors). Never raises."""
    if not isinstance(parsed, dict):
        return False, ["response is not a JSON object"]

    errors: List[str] = []

    if parsed.get("verdict") not in ("accept", "revise"):
        errors.append("verdict must be 'accept' or 'revise'")

    score = parsed.get("score")
    if not isinstance(score, int) or not (1 <= score <= 10):
        errors.append("score must be int 1-10")

    reasoning = parsed.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("reasoning must be non-empty string")

    obs = parsed.get("observations")
    if not isinstance(obs, list) or not all(isinstance(o, str) for o in obs):
        errors.append("observations must be list of strings")

    feedback = parsed.get("feedback_for_artist", "")
    if not isinstance(feedback, str):
        errors.append("feedback_for_artist must be string")
    elif parsed.get("verdict") == "revise" and not feedback.strip():
        errors.append("feedback_for_artist must be non-empty when verdict is 'revise'")

    remaining = parsed.get("remaining_feedback", [])
    if not isinstance(remaining, list) or not all(isinstance(r, str) for r in remaining):
        errors.append("remaining_feedback must be list of strings")

    ui_msg = parsed.get("ui_message", "")
    if not isinstance(ui_msg, str) or not ui_msg.strip():
        errors.append("ui_message must be non-empty string")
    elif _sentence_count(ui_msg) > 1:
        errors.append("ui_message must be exactly one sentence")

    return (len(errors) == 0, errors)


def _auto_repair_critique(parsed: Any) -> Dict[str, Any]:
    """Best-effort repair. Always returns a usable dict. Logs every synthesized field."""
    if not isinstance(parsed, dict):
        parsed = {}

    repaired: List[str] = []

    verdict = parsed.get("verdict")
    if verdict not in ("accept", "revise"):
        parsed["verdict"] = "revise"
        repaired.append("verdict")
    verdict = parsed["verdict"]

    score = parsed.get("score")
    try:
        score_int = int(score)
    except (TypeError, ValueError):
        score_int = None
    if score_int is None or not (1 <= score_int <= 10):
        parsed["score"] = 5
        repaired.append("score")
    else:
        parsed["score"] = score_int

    reasoning = parsed.get("reasoning", "")
    if not isinstance(reasoning, str) or not reasoning.strip():
        parsed["reasoning"] = "The drawing was evaluated against the original request."
        repaired.append("reasoning")

    obs = parsed.get("observations", [])
    if not isinstance(obs, list) or not all(isinstance(o, str) for o in obs):
        parsed["observations"] = []
        repaired.append("observations")

    # part_status is best-effort: never fail/retry on it, but guarantee the key
    # exists as a list of strings so the orchestrator's progress ledger is safe.
    part_status = parsed.get("part_status")
    if not isinstance(part_status, list) or not all(isinstance(p, str) for p in part_status):
        parsed["part_status"] = []
        if part_status is not None:
            repaired.append("part_status")

    remaining = parsed.get("remaining_feedback", [])
    if not isinstance(remaining, list) or not all(isinstance(r, str) for r in remaining):
        parsed["remaining_feedback"] = []
        repaired.append("remaining_feedback")

    if verdict == "accept":
        parsed["remaining_feedback"] = []

    feedback = parsed.get("feedback_for_artist", "")
    if not isinstance(feedback, str):
        feedback = str(feedback)
        parsed["feedback_for_artist"] = feedback
        repaired.append("feedback_for_artist (type coercion)")

    if verdict == "accept":
        parsed["feedback_for_artist"] = ""
    elif not parsed.get("feedback_for_artist", "").strip():
        parsed["feedback_for_artist"] = (
            "Please review the drawing against the original request and add any "
            "missing elements that were requested."
        )
        repaired.append("feedback_for_artist (content synthesized)")

    ui_msg = parsed.get("ui_message", "")
    if not isinstance(ui_msg, str):
        ui_msg = str(ui_msg)
        parsed["ui_message"] = ui_msg
        repaired.append("ui_message (type coercion)")

    if verdict == "accept":
        if not ui_msg or not ui_msg.strip():
            parsed["ui_message"] = "The drawing matches the request."
            repaired.append("ui_message (acceptance synthesized)")
    else:
        if not ui_msg or not ui_msg.strip():
            first = _first_sentence(parsed.get("feedback_for_artist", ""))
            parsed["ui_message"] = first or "Refining the drawing to better match the request."
            repaired.append("ui_message (synthesized from feedback)")
        elif _sentence_count(ui_msg) > 1:
            parsed["ui_message"] = _first_sentence(ui_msg)
            repaired.append("ui_message (truncated to one sentence)")

    if repaired:
        logger.warning("auto-repair: synthesized/fixed fields: %s", repaired)

    return parsed


def is_clear_accept(parsed: Dict[str, Any], min_score: Optional[int] = None) -> bool:
    """Return True only for high-confidence accepts that are safe to stop on."""
    if not isinstance(parsed, dict):
        return False
    if str(parsed.get("verdict") or "").strip().lower() != "accept":
        return False
    try:
        score = int(parsed.get("score", 0))
    except (TypeError, ValueError):
        return False
    if score < (min_score or _CLEAR_ACCEPT_MIN_SCORE):
        return False

    action = str(parsed.get("action") or "").strip().lower()
    target = str(parsed.get("target_region") or "").strip().lower()
    if action not in ("", "add", "accept"):
        return False
    if target not in ("", "complete", "done", "finished", "none", "nothing"):
        return False
    if str(parsed.get("feedback_for_artist") or "").strip():
        return False
    if parsed.get("remaining_feedback") not in (None, []):
        return False

    part_status = parsed.get("part_status")
    if not isinstance(part_status, list) or not part_status:
        return False
    saw_status = False
    for entry in part_status:
        if not isinstance(entry, str) or ":" not in entry:
            return False
        _, _, status = entry.partition(":")
        status = status.strip().lower()
        if not status:
            return False
        saw_status = True
        if status != "present":
            return False
    return saw_status


def _word_limit(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).rstrip(" ,;:") + "."


def _naturalize_feedback(text: str) -> str:
    """Turn checklist-style feedback into two readable critique sentences."""
    text = " ".join(str(text or "").replace("\n", " ").split())
    if not text:
        return ""

    def _try_to(match: re.Match[str]) -> str:
        return f". Try to {match.group(1).lower()}"

    text = re.sub(
        r"\s*;\s*(add|draw|place|put|redraw|replace)\b",
        _try_to,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*;\s*keep\b", ". Keep", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*;\s*", ". ", text)
    text = re.sub(
        r"([^.!?]*(?:readable|recognizable)[^.!?]*)\.\s+Keep the readable parts\.",
        r"\1, so those readable parts can stay.",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\.\s+try to\b", ". Try to", text, flags=re.IGNORECASE)
    return " ".join(text.split())


_ACTION_COMMAND_PATTERN = (
    r"(?:^|[.!?]\s+|[,;]\s*|\b(?:try to|please|next|then|and)\s+)"
    r"\b(?:add|draw|place|put|redraw|replace|refine|fix|improve|adjust)\b"
    r"|\b(?:try|consider)\s+(?:adding|drawing|placing|putting|redrawing|replacing|"
    r"refining|fixing|improving|adjusting)\b"
)

_ACTION_COMMAND_RE = re.compile(_ACTION_COMMAND_PATTERN, re.IGNORECASE)


def _shorten_feedback(text: str) -> str:
    """Keep a visual judgment plus the first actionable request.

    Earlier versions kept only the first two sentences when the second started
    with an action word. That destroyed useful notes such as "The roof is
    visible. It is rough but usable. Add a window..." by dropping the action.
    """
    text = _naturalize_feedback(text)
    if not text:
        return ""

    chunks = re.findall(r"[^.!?]+[.!?]?", text)
    sentences = [c.strip() for c in chunks if c.strip()]
    if sentences:
        action_idx = next(
            (
                idx
                for idx, sentence in enumerate(sentences)
                if _ACTION_COMMAND_RE.search(sentence)
            ),
            None,
        )
        if action_idx is None:
            selected = sentences[:2]
        else:
            # Preserve up to two visual/context sentences before the command,
            # then the first command sentence. This keeps the critic's voice
            # without handing the Artist a long list.
            selected = sentences[max(0, action_idx - 2):action_idx + 1]
        text = " ".join(selected)
    return _word_limit(text, 80)


def _compact_artist_feedback(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce the project rule that the Artist receives one next action only."""
    verdict = parsed.get("verdict")

    parsed["remaining_feedback"] = []

    if verdict == "accept":
        parsed["feedback_for_artist"] = ""
        parsed["target_region"] = ""
        ui = str(parsed.get("ui_message", "") or "").strip()
        parsed["ui_message"] = _first_sentence(ui) or "The drawing matches the request."
        return parsed

    feedback = _shorten_feedback(parsed.get("feedback_for_artist", ""))
    if not feedback:
        feedback = (
            "The main sketch is visible but still sparse. Try adding one missing "
            "identity feature in the clearest empty area."
        )
    parsed["feedback_for_artist"] = feedback

    ui = _naturalize_feedback(parsed.get("ui_message", ""))
    if not ui:
        ui = feedback
    parsed["ui_message"] = _first_sentence(ui) or _first_sentence(feedback)

    reasoning = str(parsed.get("reasoning", "") or "").strip()
    if reasoning:
        parsed["reasoning"] = _word_limit(reasoning, 32)

    observations = parsed.get("observations")
    if isinstance(observations, list):
        parsed["observations"] = [
            _word_limit(str(obs), 18)
            for obs in observations
            if isinstance(obs, str) and obs.strip()
        ][:4]

    # Keep the per-part inventory tidy: short "part: status" strings, capped.
    part_status = parsed.get("part_status")
    if isinstance(part_status, list):
        parsed["part_status"] = [
            _word_limit(str(p), 8)
            for p in part_status
            if isinstance(p, str) and p.strip()
        ][:12]
    else:
        parsed["part_status"] = []
    return parsed


def _first_missing_part_name(part_status: Any, exclude: Optional[List[str]] = None) -> str:
    if not isinstance(part_status, list):
        return ""
    for entry in part_status:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        name, _, status = entry.partition(":")
        name = name.strip()
        if status.strip().lower() == "missing" and name:
            if _is_vague_target(name):
                continue
            if exclude and any(_features_overlap(name, x) for x in exclude):
                continue  # already locked from an earlier pass — skip it
            return name
    return ""


# Vague placeholders the Critic/repair must never surface — they read poorly in
# the documented figure and recurse as phantom "missing" parts.
_VAGUE_TARGETS = {
    "small detail", "detail", "details", "more detail", "a detail",
    "additional detail", "additional details", "finishing touch",
    "finishing detail", "finishing details", "small finishing detail",
    "the next defining part", "small finishing touch", "face features",
    "facial features", "features", "small mark", "defining mark",
    "defining detail", "outline detail",
}

_VAGUE_TARGET_WORD_RE = re.compile(
    r"\b(feature|features|detail|details|mark|marks|cleanup|touch)\b",
    re.IGNORECASE,
)


def _is_vague_target(target: str) -> bool:
    text = " ".join((target or "").strip().lower().split())
    if not text:
        return True
    if text in _VAGUE_TARGETS:
        return True
    if text.endswith(" detail") or text.endswith(" details"):
        return True
    if "face feature" in text or "facial feature" in text:
        return True
    if "defining mark" in text:
        return True
    # Single-word concrete parts like "eyes" are fine; broad nouns are not.
    words = [w for w in re.findall(r"[a-z]+", text) if w not in _FEATURE_STOPWORDS]
    return bool(words and all(_VAGUE_TARGET_WORD_RE.fullmatch(w) for w in words))


def _last_present_part_name(part_status: Any) -> str:
    """Most recently present/weak part — a concrete fallback anchor when the
    model claims there is nothing else to add before the final pass."""
    if not isinstance(part_status, list):
        return ""
    last = ""
    for entry in part_status:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        name, _, status = entry.partition(":")
        if status.strip().lower() in ("present", "weak") and name.strip():
            last = name.strip()
    return last


def _subject_label(user_prompt: str) -> str:
    subj = (user_prompt or "subject").strip()
    subj = re.sub(r"^\s*(a|an|the)\s+", "", subj, flags=re.IGNORECASE)
    return subj or "subject"


def _indefinite_article(word: str) -> str:
    first = (word or "").strip()[:1].lower()
    return "an" if first in "aeiou" else "a"


def _instruction_for_part(part_name: str, user_prompt: str) -> str:
    """Generic artist instruction for a named feature.

    Keep this subject-agnostic. The critic supplies the visual specificity; this
    helper only prevents invalid/empty fallbacks from becoming no-op iterations.
    """
    part = " ".join((part_name or "").strip().split())
    if not part:
        return f"a simple visible detail on the {_subject_label(user_prompt)}"
    part = re.sub(r"^(a|an|the)\s+", "", part, flags=re.IGNORECASE)
    lower = part.lower()
    is_plural = bool(
        "/" in lower
        or lower.endswith("s")
        or lower.endswith("feet")
        or lower.endswith("teeth")
    )
    if is_plural:
        return f"{part} with a few clear strokes"
    return f"{_indefinite_article(part)} simple {part} with one or two clear strokes"


def _suggestion_sentence(instruction: str) -> str:
    instruction = (instruction or "one simple visible detail").strip()
    return f"Try adding {instruction}."


def _target_from_missing_text(text: str, attempted: Optional[List[str]] = None) -> str:
    """Extract a concrete 'X is missing' target from the model's own prose."""
    attempted = attempted or []
    patterns = [
        r"\b(?:the\s+)?([a-z][a-z /-]{1,30}?)\s+(?:is|are)\s+(?:still\s+)?(?:missing|absent|not visible)\b",
        r"\bno\s+([a-z][a-z /-]{1,30}?)\s+(?:is|are\s+)?(?:visible|drawn|present)\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            candidate = " ".join(match.group(1).strip(" .,:;").split())
            candidate = re.sub(r"^(a|an|the)\s+", "", candidate, flags=re.IGNORECASE)
            if not candidate or _is_vague_target(candidate):
                continue
            if any(_features_overlap(candidate, a) for a in attempted):
                continue
            return candidate
    return ""


def _text_says_visible(text: str, candidate: str) -> bool:
    if not text or not candidate:
        return False
    for sentence in re.findall(r"[^.!?]+", text, flags=re.IGNORECASE):
        if (
            re.search(rf"\b{re.escape(candidate)}\b", sentence, re.IGNORECASE)
            and re.search(
                r"\b(visible|present|readable|recognizable|recognisable|drawn)\b",
                sentence,
                re.IGNORECASE,
            )
        ):
            return True
    return False


def _face_subtarget(
    part_status: Any,
    attempted: Optional[List[str]] = None,
    visual: str = "",
) -> str:
    """Split broad 'face features' into concrete face parts when needed.

    This is category-level, not prompt-specific: it applies to any subject with
    a face and avoids repeated requests for a vague bucket.
    """
    attempted = attempted or []
    text_blob = " ".join([visual or ""] + [
        str(p) for p in part_status if isinstance(p, str)
    ] if isinstance(part_status, list) else [visual or ""])
    if not re.search(r"\b(face|facial|head)\b", text_blob, re.IGNORECASE):
        return ""
    for candidate in ("eyes", "nose", "mouth", "whiskers"):
        if _text_says_visible(text_blob, candidate):
            continue
        status = _status_for_feature(part_status, candidate)
        if _feature_is_visible_status(status):
            continue
        if any(_features_overlap(candidate, a) for a in attempted):
            continue
        return candidate
    return ""


def _fallback_detail_target(
    part_status: Any,
    user_prompt: str,
    attempted: Optional[List[str]] = None,
    visual: str = "",
) -> Tuple[str, str]:
    """Force a non-final add target when the critic thinks nothing is missing."""
    attempted = attempted or []
    target = (
        _target_from_missing_text(visual, attempted)
        or _first_missing_part_name(part_status, exclude=attempted)
        or _face_subtarget(part_status, attempted, visual)
    )
    if not target:
        for candidate in ("texture line", "surface stripe", "small contour line"):
            if not any(_features_overlap(candidate, a) for a in attempted):
                target = candidate
                break
    if not target:
        target = "surface stripe"
    instruction = _instruction_for_part(target, user_prompt)
    return target, instruction


def _visual_sentence_for_target(visual: str, target: str) -> str:
    sentence = (visual or "The current drawing has a usable rough structure.").strip()
    if (
        target
        and target.lower() in {"eyes", "nose", "mouth", "whiskers"}
        and re.search(r"\b(face|facial)\s+features\b", sentence, re.IGNORECASE)
    ):
        need_phrase = (
            f"clearer {target}"
            if target.lower().endswith("s")
            else f"a clearer {target}"
        )
        return (
            f"The face area has a few faint marks, but it still needs {need_phrase}."
        )
    return sentence


def _feature_instruction(part_name: str, user_prompt: str) -> Tuple[str, str]:
    """Return (target_region, artist phrase) for a non-final accept repair."""
    part = " ".join((part_name or "").strip().split())
    if part and _is_vague_target(part):
        if re.search(r"\b(face|facial)\b", part, re.IGNORECASE):
            part = "eyes"
        else:
            return _fallback_detail_target([], user_prompt)
    if part:
        return part, _instruction_for_part(part, user_prompt)
    return _fallback_detail_target([], user_prompt)


def _repair_nonfinal_accept(
    parsed: Dict[str, Any],
    user_prompt: str,
    iteration: int,
    max_iterations: int,
) -> Dict[str, Any]:
    """A non-final accept would make the next Artist pass restart from scratch.

    Treat it as a schema violation: the loop is fixed-length, so every non-final
    critique must provide one next feature. This guard should be rare because the
    prompt also forbids non-final accepts.
    """
    if iteration + 1 >= max_iterations or parsed.get("verdict") != "accept":
        return parsed

    part_name = _first_missing_part_name(parsed.get("part_status"))
    if not part_name:
        # Never hold before the final pass. If the model claims nothing is
        # missing, force one small extra detail anchored to a visible part.
        visual = _repair_visual_sentence(parsed, user_prompt)
        target, instruction = _fallback_detail_target(
            parsed.get("part_status"), user_prompt, visual=visual
        )
        visual = _visual_sentence_for_target(visual, target)
        parsed["verdict"] = "revise"
        parsed["action"] = "add"
        parsed["target_region"] = target
        parsed["feedback_for_artist"] = f"{visual.rstrip('.')}. {_suggestion_sentence(instruction)}"
        parsed["ui_message"] = (
            f"The main structure can stay, so the next pass adds {target}."
        )
        parsed["remaining_feedback"] = []
        return parsed
    target, instruction = _feature_instruction(part_name, user_prompt)
    parsed["verdict"] = "revise"
    parsed["action"] = "add"
    parsed["target_region"] = target
    parsed["feedback_for_artist"] = (
        f"The drawing is readable enough to keep, even if it is still sparse. "
        f"Keep the existing subject and try adding {instruction}."
    )
    parsed["ui_message"] = (
        f"The drawing is readable, so the next pass adds {target}."
    )
    parsed["remaining_feedback"] = []
    logger.warning(
        "critic returned accept on non-final iteration %d/%d; converted to revise target=%r",
        iteration + 1,
        max_iterations,
        target,
    )
    return parsed


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
    "padding": "padding",
    "connector": "connector",
    "connectors": "connector",
    "whisker": "whisker",
    "whiskers": "whisker",
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


def _feature_is_visible_status(status: str) -> bool:
    return status.strip().lower() in ("present", "weak")


def _artist_steps_support_feature(artist_steps: Optional[List[str]], feature: str) -> bool:
    if not isinstance(artist_steps, list) or not feature:
        return False
    return any(
        isinstance(step, str) and _features_overlap(step, feature)
        for step in artist_steps
    )


def _compact_attempt_labels(artist_steps: Optional[List[str]], limit: int = 4) -> List[str]:
    """Short, non-authoritative Artist intent labels for the Critic prompt."""
    if not isinstance(artist_steps, list):
        return []
    labels: List[str] = []
    for step in artist_steps:
        label = " ".join(str(step or "").split())
        if not label:
            continue
        if len(label) > 70:
            label = label[:67].rstrip() + "..."
        if label.lower() not in {x.lower() for x in labels}:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


_VISUAL_JUDGMENT_RE = re.compile(
    r"\b("
    r"visible|usable|readable|reads|present|clear|rough|lumpy|"
    r"large|small|oversized|undersized|faint|tiny|angular|"
    r"tall|wide|thin|low|high|curved|straight|arc|loop|box|"
    r"rectangular|trapezoid|centered|front|back|recognizable|recognisable|"
    r"good enough|a little|a bit|too big|too small"
    r")\b",
    re.IGNORECASE,
)

_BARE_COMMAND_RE = re.compile(
    r"^\s*(keep|add|draw|place|put|use|redraw|replace)\b",
    re.IGNORECASE,
)

_NEXT_ACTION_RE = re.compile(
    _ACTION_COMMAND_PATTERN,
    re.IGNORECASE,
)

_REFINE_WORDS_RE = re.compile(
    r"\b(refine|clearer|improve|improved|improving|fix|fixed|adjust|cleaner|"
    r"sharpen|smooth|better)\b",
    re.IGNORECASE,
)

_FINAL_COMMAND_RE = re.compile(
    r"\bconsider\s+(?:adding|drawing|placing|putting|redrawing|replacing|"
    r"refining|fixing|improving|adjusting)\b"
    r"|"
    + _ACTION_COMMAND_PATTERN,
    re.IGNORECASE,
)


def _feedback_has_visual_judgment(feedback: str) -> bool:
    text = " ".join(str(feedback or "").split())
    if not text:
        return False
    if _BARE_COMMAND_RE.search(text):
        return False
    return bool(_VISUAL_JUDGMENT_RE.search(text))


def _next_action_count(feedback: str) -> int:
    return len(_NEXT_ACTION_RE.findall(str(feedback or "")))


def _feedback_has_refinement_language(feedback: str) -> bool:
    return bool(_REFINE_WORDS_RE.search(str(feedback or "")))


def _feedback_has_final_command(feedback: str) -> bool:
    return bool(_FINAL_COMMAND_RE.search(str(feedback or "")))


def _repair_visual_sentence(
    parsed: Dict[str, Any],
    user_prompt: str,
    last_requested_feature: Optional[str] = None,
) -> str:
    """Find a concrete-looking sentence from the model's own visual fields."""
    candidates: List[str] = []
    observations = parsed.get("observations")
    if isinstance(observations, list):
        candidates.extend(str(obs).strip() for obs in observations if str(obs).strip())
    for field in ("reasoning", "ui_message", "feedback_for_artist"):
        value = str(parsed.get(field) or "").strip()
        if value:
            candidates.extend(c.strip() for c in re.findall(r"[^.!?]+[.!?]?", value))

    if last_requested_feature:
        for candidate in candidates:
            sentence = _first_sentence(candidate)
            if (
                sentence
                and not _BARE_COMMAND_RE.search(sentence)
                and _features_overlap(sentence, last_requested_feature)
                and _feedback_has_visual_judgment(sentence)
            ):
                return sentence

        status = _status_for_feature(parsed.get("part_status"), last_requested_feature)
        label = last_requested_feature.strip()
        if status == "missing" or not status:
            return f"The {label} is still not visible in the drawing."
        if status == "weak":
            return f"The {label} is faint but visible enough to use."
        if status == "malformed":
            return f"The {label} is visible but still reads as the wrong kind of shape."
        return f"The {label} is visible enough to use, even if it is rough."

    for candidate in candidates:
        sentence = _first_sentence(candidate)
        if (
            sentence
            and not _BARE_COMMAND_RE.search(sentence)
            and _feedback_has_visual_judgment(sentence)
        ):
            return sentence

    if last_requested_feature:
        return (
            f"The {last_requested_feature} is rough but usable enough to keep "
            "the drawing moving forward."
        )
    return "The main sketch is visible, but it still needs one clearer detail."


def _repair_loop_contract(
    parsed: Dict[str, Any],
    user_prompt: str,
    iteration: int,
    max_iterations: int,
    expected_next_feature: Optional[str] = None,
    last_requested_feature: Optional[str] = None,
    attempted_features: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Deterministic fallback when the model keeps violating the loop contract.

    This is intentionally only a fallback. The prompt should produce the real
    personalized critique; this guard prevents invalid generic retries from
    leaking to the UI/Artist.
    """
    if not isinstance(parsed, dict):
        parsed = {}

    is_final = iteration + 1 >= max_iterations
    visual = _repair_visual_sentence(parsed, user_prompt, last_requested_feature)

    parsed.setdefault("score", 5)
    parsed.setdefault("reasoning", visual)
    parsed.setdefault("observations", [visual])
    parsed["remaining_feedback"] = []
    last_status = _status_for_feature(parsed.get("part_status"), last_requested_feature or "")
    last_feature_locked = bool(
        last_requested_feature
        and any(
            _features_overlap(last_requested_feature, a)
            for a in (attempted_features or [])
        )
    )

    if is_final:
        final_last_missing = bool(
            last_requested_feature
            and last_status
            and not _feature_is_visible_status(last_status)
            and not last_feature_locked
        )
        if final_last_missing:
            parsed["verdict"] = "revise"
        parsed["verdict"] = (
            parsed.get("verdict")
            if parsed.get("verdict") in ("accept", "revise")
            else "revise"
        )
        parsed["feedback_for_artist"] = visual
        parsed["ui_message"] = visual
        parsed["target_region"] = ""
        parsed["action"] = "add"
        return parsed

    repeat_last = bool(
        last_requested_feature
        and not _feature_is_visible_status(last_status)
        and not last_feature_locked
    )
    expected = (expected_next_feature or "").strip()
    if (
        expected
        and last_requested_feature
        and _feature_is_visible_status(last_status)
        and _features_overlap(expected, last_requested_feature)
    ):
        expected = ""
    # Don't reuse the model's chosen target if it is a locked (already-present)
    # part (the fixation we are breaking) or a vague placeholder ("small
    # detail") — the loop must always name a concrete feature.
    _parsed_target = str(parsed.get("target_region") or "").strip()
    if _parsed_target and (
        _is_vague_target(_parsed_target)
        or (attempted_features and any(_features_overlap(_parsed_target, a) for a in attempted_features))
    ):
        _parsed_target = ""
    # Always advance to a genuinely NEW (unattempted) missing feature. NEVER
    # refine or re-request a present/locked part — that produced the same
    # feature ("whiskers") being asked for several passes in a row.
    _missing_next = _first_missing_part_name(parsed.get("part_status"), exclude=attempted_features)
    if repeat_last:
        planned = (last_requested_feature or "").strip()
    else:
        planned = expected or _parsed_target or _missing_next
    if not planned:
        # Never hold before the final pass. If the critic cannot find a missing
        # main part, force one small extra detail anchored to what is visible.
        target, instruction = _fallback_detail_target(
            parsed.get("part_status"), user_prompt, attempted_features, visual
        )
        visual = _visual_sentence_for_target(visual, target)
        parsed["verdict"] = "revise"
        parsed["action"] = "add"
        parsed["target_region"] = target
        parsed["feedback_for_artist"] = f"{visual.rstrip('.')}. {_suggestion_sentence(instruction)}"
        parsed["ui_message"] = (
            f"{visual.rstrip('.')}, so try adding {instruction}."
        )
        logger.warning(
            "critic loop-contract fallback: no missing feature; forced target=%r",
            target,
        )
        return parsed
    target, instruction = _feature_instruction(planned, user_prompt)
    if not target:
        target = planned
    visual = _visual_sentence_for_target(visual, target)

    parsed["verdict"] = "revise"
    parsed["action"] = "add"
    parsed["target_region"] = target
    parsed["feedback_for_artist"] = (
        f"{visual.rstrip('.')}. {_suggestion_sentence(instruction)}"
    )
    parsed["ui_message"] = (
        f"{visual.rstrip('.')}, so try adding {instruction}."
    )
    logger.warning(
        "critic loop-contract fallback synthesized feedback target=%r",
        target,
    )
    return parsed


def _validate_loop_contract(
    parsed: Dict[str, Any],
    iteration: int,
    max_iterations: int,
    last_requested_feature: Optional[str] = None,
    attempted_features: Optional[List[str]] = None,
    artist_steps: Optional[List[str]] = None,
    expected_next_feature: Optional[str] = None,
) -> List[str]:
    """Reject loop-level mistakes and ask the Critic to rewrite its own note."""
    errors: List[str] = []
    target = str(parsed.get("target_region") or "").strip()
    action = str(parsed.get("action") or "").strip()
    part_status = parsed.get("part_status") or []
    feedback = str(parsed.get("feedback_for_artist") or "").strip()
    is_final = iteration + 1 >= max_iterations
    target_status = _status_for_feature(part_status, target)
    last_status = _status_for_feature(part_status, last_requested_feature or "")
    # "Locked" = the orchestrator already recorded this feature as attempted
    # (the Artist drew geometry for it on a prior pass). Once locked we never
    # force the Critic to re-request it, even if the vision model can no longer
    # re-perceive it — that perception deadlock froze the loop on thin features.
    last_feature_locked = bool(
        last_requested_feature
        and any(
            _features_overlap(last_requested_feature, a)
            for a in (attempted_features or [])
        )
    )

    if not is_final and parsed.get("verdict") == "accept" and not is_clear_accept(parsed):
        errors.append(
            "Non-final accept must be high-confidence: score >= 9, no requested "
            "revision, and all listed main parts marked present."
        )
    if not is_final and _NONFINAL_COMPLETE_RE.search(
        f"{feedback} {parsed.get('ui_message') or ''}"
    ) and not is_clear_accept(parsed):
        errors.append(
            "Non-final completion language is allowed only with a clear accept; "
            "otherwise request one next feature."
        )

    if (
        is_final
        and parsed.get("verdict") == "accept"
        and last_requested_feature
        and last_status
        and not _feature_is_visible_status(last_status)
        and not last_feature_locked
    ):
        errors.append(
            f"Final iteration cannot accept while the last requested feature "
            f"{last_requested_feature!r} is still missing or not visible."
        )

    if parsed.get("verdict") == "revise" and action != "redraw_all":
        if not is_final and action in ("add", "redraw_element") and not target:
            errors.append(
                "Non-final revise must set target_region to one concrete feature "
                "and request that feature in feedback_for_artist."
            )
        if not is_final and action in ("add", "redraw_element") and _is_vague_target(target):
            errors.append(
                f"target_region={target!r} is too vague. Use an atomic visible feature "
                "such as eyes, nose, mouth, whiskers, handle, window, or leaf."
            )

        if not _feedback_has_visual_judgment(feedback):
            errors.append(
                "feedback_for_artist is too generic. Start with a specific visual "
                "judgment of the current/last feature, e.g. 'The wheels are a little "
                "large, but they clearly read as wheels, so they can stay.' Then ask for "
                "exactly one new feature."
            )
        if not is_final and _ROBOTIC_FEEDBACK_RE.search(feedback):
            errors.append(
                "feedback_for_artist uses broad or robotic wording. Do not say "
                "'face features', 'facial features', 'placed where it belongs', "
                "or 'small defining mark'; name one concrete visible feature instead."
            )

        if is_final:
            if (
                last_requested_feature
                and not _feature_is_visible_status(last_status)
                and not last_feature_locked
            ):
                if not _features_overlap(feedback, last_requested_feature):
                    errors.append(
                        f"Final-iteration feedback must mention that the last requested "
                        f"feature {last_requested_feature!r} is still missing or unclear."
                    )
            if _feedback_has_final_command(feedback):
                errors.append(
                    "Final-iteration feedback must be evaluative, not instructional. "
                    "Do not write 'add/consider adding/refine/fix X'; write 'X is "
                    "still missing' or 'X remains rough' instead."
                )
        else:
            if last_requested_feature:
                first_sentence = _first_sentence(feedback)
                if not _features_overlap(first_sentence, last_requested_feature):
                    errors.append(
                        f"Start feedback_for_artist by judging the last requested "
                        f"feature {last_requested_feature!r} by name, not by repeating "
                        "a whole-subject summary."
                    )
                if (
                    not _feature_is_visible_status(last_status)
                    and not last_feature_locked
                    and target
                    and not _features_overlap(target, last_requested_feature)
                ):
                    errors.append(
                        f"The last requested feature {last_requested_feature!r} is still "
                        "missing or not visible. Repeat that target with a clearer add "
                        "instruction instead of advancing."
                    )

            next_actions = _next_action_count(feedback)
            if next_actions != 1:
                errors.append(
                    f"feedback_for_artist must contain exactly one next action, but "
                    f"it appears to contain {next_actions}. Do not combine refinement "
                    "and addition; choose only the target_region feature."
                )

        if target and action in ("add", "redraw_element"):
            if not _features_overlap(feedback, target):
                errors.append(
                    f"feedback_for_artist does not match target_region={target!r}. "
                    "The prose instruction must ask for the same single feature as "
                    "target_region."
                )

        if _feedback_has_refinement_language(feedback):
            if not (action == "redraw_element" and target_status == "malformed"):
                errors.append(
                    "Do not use refinement language such as 'refine', 'clearer', "
                    "'improve', or 'fix' unless action=\"redraw_element\" and the "
                    "target part is tagged malformed. For rough-but-readable parts, "
                    "declare them usable and add the next missing feature."
                )

    if action == "redraw_element":
        if target_status != "malformed":
            errors.append(
                f"Do not redraw/refine {target!r}; it is not tagged malformed. "
                "Use action=\"add\" for the next missing feature instead."
            )

    if target and last_requested_feature and _features_overlap(target, last_requested_feature):
        status = _status_for_feature(part_status, last_requested_feature)
        if _feature_is_visible_status(status):
            errors.append(
                f"You repeated the previous target {last_requested_feature!r}. "
                "If it is visible, mark it present and choose the next planned feature."
            )

    for attempted in attempted_features or []:
        if target and _features_overlap(target, attempted):
            errors.append(
                f"You chose {target!r}, but that feature was already drawn on a "
                "previous pass and is locked. Treat it as present and advance to "
                "the next missing feature instead of re-requesting it."
            )
            break

    if expected_next_feature and target and not _features_overlap(target, expected_next_feature):
        status = _status_for_feature(part_status, target)
        if _feature_is_visible_status(status):
            errors.append(
                f"The feature plan expects {expected_next_feature!r} next, but you chose "
                f"already-visible {target!r}. Pick {expected_next_feature!r} unless the "
                "minimum scaffold is genuinely missing."
            )

    return errors


_VALID_ACTIONS = ("add", "redraw_element", "redraw_all")
_REDRAW_WORDS_RE = re.compile(
    r"\b(redraw|re-draw|replace|wrong[- ]category|unrecognizable|start over)\b",
    re.IGNORECASE,
)


def _normalize_action(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Guarantee `action` + `target_region` exist and are self-consistent.

    Runs on EVERY critique (after validate/repair), so a model that omits the
    fields still yields a usable lock signal without forcing a retry. Inference
    order when `action` is missing/invalid:
      - accept verdict           → "add" with empty target (nothing to draw)
      - feedback says start over and score is very low → "redraw_all"
      - feedback uses redraw/replace language         → "redraw_element"
      - otherwise                → "add" (the common additive-patch case)
    `target_region` falls back to the ui_message / first feedback sentence so
    the generator and the region schedule always have a label to work with.
    """
    verdict = parsed.get("verdict")
    action = parsed.get("action")
    feedback = parsed.get("feedback_for_artist", "") or ""
    try:
        score = int(parsed.get("score", 5))
    except (TypeError, ValueError):
        score = 5

    if action not in _VALID_ACTIONS:
        if verdict == "accept":
            action = "add"
        elif score <= 2 and _REDRAW_WORDS_RE.search(feedback):
            action = "redraw_all"
        elif _REDRAW_WORDS_RE.search(feedback):
            action = "redraw_element"
        else:
            action = "add"
    # A redraw_all only makes sense while the subject is unrecognizable.
    if action == "redraw_all" and score >= 5:
        action = "redraw_element" if _REDRAW_WORDS_RE.search(feedback) else "add"
    parsed["action"] = action

    target = parsed.get("target_region")
    if not isinstance(target, str):
        target = ""
    target = target.strip()
    if verdict == "accept":
        target = ""
    elif not target or _is_vague_target(target):
        # Prefer a concrete missing part the Critic itself named; otherwise leave
        # the target empty and let _apply_loop_policy decide (advance / polish /
        # hold). Never synthesize a placeholder feature here.
        target = _first_missing_part_name(parsed.get("part_status"))
    parsed["target_region"] = target
    return parsed


def _weakest_part(part_status: Any, exclude: Optional[List[str]] = None) -> str:
    """The existing part most in need of work: a 'malformed' one, else a 'weak' one."""
    if not isinstance(part_status, list):
        return ""
    exclude = exclude or []
    malformed = weak = ""
    for entry in part_status:
        if not isinstance(entry, str) or ":" not in entry:
            continue
        name, _, status = entry.partition(":")
        name, status = name.strip(), status.strip().lower()
        if not name or _is_vague_target(name):
            continue
        if any(_features_overlap(name, x) for x in exclude):
            continue
        if status == "malformed" and not malformed:
            malformed = name
        elif status == "weak" and not weak:
            weak = name
    return malformed or weak


def _the_part(feature: str) -> str:
    """"window" -> "the window"; tolerate an article the model already wrote."""
    f = re.sub(r"^\s*(a|an|the)\s+", "", (feature or "").strip(), flags=re.IGNORECASE)
    return f"the {f}" if f else "the next part"


def _critic_read(parsed: Dict[str, Any]) -> str:
    """The Critic's own one-line read of the current drawing, command words stripped."""
    for field in ("feedback_for_artist", "reasoning", "ui_message"):
        s = _first_sentence(str(parsed.get(field) or "")).strip()
        if len(s.split()) >= 3 and not _ACTION_COMMAND_RE.search(s):
            return s.rstrip(".")
    return ""


def _set_advance_feedback(parsed: Dict[str, Any], target: str) -> None:
    read = _critic_read(parsed)
    part = _the_part(target)
    parsed["feedback_for_artist"] = (
        f"{read}. Next, add {part}." if read else f"Good so far. Next, add {part}."
    )
    parsed["ui_message"] = f"Now adding {part}."


def _set_polish_feedback(parsed: Dict[str, Any], target: str) -> None:
    read = _critic_read(parsed)
    part = _the_part(target)
    tidy = f"{part[0].upper()}{part[1:]} is the roughest part now, so tidy it up."
    parsed["feedback_for_artist"] = f"{read}. {tidy}" if read else tidy
    parsed["ui_message"] = f"Tidying up {part}."


def _set_complete_feedback(parsed: Dict[str, Any], user_prompt: str) -> None:
    subj = _subject_label(user_prompt)
    parsed["feedback_for_artist"] = (
        f"All the main parts of the {subj} read clearly now, so the sketch looks complete."
    )
    parsed["ui_message"] = f"The {subj} looks complete."


# ---------------------------------------------------------------------------
# Critic class
# ---------------------------------------------------------------------------


class VisualCritic:
    """LM-Studio-backed Vision Critic. Grades a PNG and writes dual-channel feedback."""

    def __init__(self, client, model: str) -> None:
        if client is None:
            raise ValueError("client is required")
        if not model:
            raise ValueError("model is required")
        self.client = client
        self.model = model
        logger.info("VisualCritic ready: model=%s", model)

    def _build_user_message(
        self,
        user_prompt: str,
        iteration: int,
        max_iterations: int,
        stricter: bool = False,
        violations: Optional[List[str]] = None,
        last_requested_feature: Optional[str] = None,
        locked_features: Optional[List[str]] = None,
        banned_features: Optional[List[str]] = None,
        comparison_image: bool = False,
    ) -> str:
        is_final = iteration + 1 >= max_iterations
        lines = [
            f"Subject: {user_prompt}",
            f"Round {iteration + 1} of {max_iterations}.",
        ]
        if comparison_image:
            lines.append(
                "Three panels: previous, current, and the strokes just added. Judge the current drawing."
            )
        if is_final:
            lines.append("Final round: evaluate only. Accept only if all main parts are present.")
        else:
            lines.append(
                "Judge what is there. If the drawing is clearly complete, accept; "
                "otherwise request the one next part."
            )

        if last_requested_feature:
            lines.append(f"You asked for the {last_requested_feature} last round; check it first.")

        locked = [str(f).strip() for f in (locked_features or []) if str(f).strip()]
        if locked:
            lines.append("Already on the canvas, do not request again: " + ", ".join(locked[:8]) + ".")
        banned = [str(f).strip() for f in (banned_features or []) if str(f).strip()]
        if banned:
            lines.append("Asked for enough already, move past these: " + ", ".join(banned[:6]) + ".")

        if stricter:
            lines.append("Return valid JSON only.")
            if violations:
                lines.append("Fix: " + " | ".join(str(v) for v in violations[:3]))
        return "\n".join(lines)

    def _call_model(self, user_message: str, png_bytes: bytes) -> str:
        return self.client.chat_vision(
            model=self.model,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_message,
            image_bytes=png_bytes,
            image_format="png",
            temperature=CRITIC_TEMPERATURE,
            max_tokens=CRITIC_MAX_TOKENS,
            response_format={"type": "json_object"},
        )

    def _apply_loop_policy(
        self,
        parsed: Dict[str, Any],
        user_prompt: str,
        iteration: int,
        max_iterations: int,
        *,
        last_requested_feature: Optional[str] = None,
        locked_features: Optional[List[str]] = None,
        banned_features: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Enforce the loop rules on the Critic's own JSON, with no extra model call.

        The strong cloud Critic chooses the feature and writes the prose; this
        only steps in when its choice would break a rule the loop guarantees:
          - one change per round (region lock signal: action + target_region);
          - never re-request a part already on the canvas (``locked_features``)
            or asked for twice (``banned_features``);
          - a part that came out wrong gets exactly one redraw, then we move on;
          - accept is allowed before the final round only when it passes the
            strict clear-accept policy;
          - the final round is pure evaluation.
        When (and only when) we override the target, we write a short, plain
        sentence — never a canned template.
        """
        if not isinstance(parsed, dict):
            parsed = {}
        locked = [str(f).strip() for f in (locked_features or []) if str(f).strip()]
        banned = [str(f).strip() for f in (banned_features or []) if str(f).strip()]
        is_final = iteration + 1 >= max_iterations
        part_status = parsed.get("part_status") or []
        target = str(parsed.get("target_region") or "").strip()
        action = str(parsed.get("action") or "add").strip().lower()
        if action not in _VALID_ACTIONS:
            action = "add"

        # Final round: trust the Critic's evaluation; just keep the lock inert so
        # no stray redraw is requested.
        if is_final:
            parsed["action"] = "add" if action == "redraw_all" else action
            if parsed["action"] == "add":
                parsed["target_region"] = ""
            return parsed

        # Round 1 may legitimately redraw the whole canvas if nothing reads yet.
        if action == "redraw_all" and iteration == 0:
            parsed["verdict"] = "revise"
            parsed["target_region"] = ""
            return parsed

        def overlaps_any(feat: str, pool: List[str]) -> bool:
            return bool(feat) and any(_features_overlap(feat, x) for x in pool)

        skip = locked + banned
        target_status = _status_for_feature(part_status, target)

        # Decide whether to trust the Critic's chosen move or steer it.
        trust = True
        if parsed.get("verdict") == "accept":
            if is_clear_accept(parsed):
                parsed["action"] = "add"
                parsed["target_region"] = ""
                parsed["feedback_for_artist"] = ""
                parsed["remaining_feedback"] = []
                return parsed
            trust = False                       # ambiguous accept → keep improving
        elif not target or _is_vague_target(target):
            trust = False
        elif overlaps_any(target, banned):
            trust = False                       # asked for twice already → move on
        elif action == "redraw_all":
            trust = False                       # not round 1 → steer to a real part
        elif action == "add":
            if overlaps_any(target, locked) or target_status in ("present", "weak"):
                trust = False                   # already on the canvas → move on
            else:
                # Trust the named part. Only treat it as invented (and steer away)
                # if the Critic DID enumerate missing parts and this target is none
                # of them — that catches a stray "stripe/line" without rejecting a
                # real next part the Critic simply didn't list.
                missing_named = _first_missing_part_name(part_status, exclude=skip)
                if (
                    missing_named
                    and target_status != "missing"
                    and not _features_overlap(target, missing_named)
                ):
                    trust = False
        elif action == "redraw_element":
            # A redraw is only the one allowed fix of a part that came out wrong.
            is_fix_of_last = bool(
                last_requested_feature
                and _features_overlap(target, last_requested_feature)
            )
            is_polish = target_status in ("weak", "malformed")
            if not (is_fix_of_last or is_polish):
                trust = False

        if trust:
            parsed["action"] = action
            parsed["verdict"] = "revise"
            return parsed

        # --- Steer: advance to the next missing part, else polish, else hold. ---
        nxt = _first_missing_part_name(part_status, exclude=skip)
        if nxt:
            parsed["action"] = "add"
            parsed["target_region"] = nxt
            _set_advance_feedback(parsed, nxt)
        else:
            weak = _weakest_part(part_status, exclude=banned)
            if weak:
                parsed["action"] = "redraw_element"
                parsed["target_region"] = weak
                _set_polish_feedback(parsed, weak)
            else:
                # Everything reads and nothing needs work → hold the drawing.
                # The generator keeps it unchanged on an empty add-target.
                parsed["action"] = "add"
                parsed["target_region"] = ""
                _set_complete_feedback(parsed, user_prompt)
        parsed["verdict"] = "revise"
        return parsed

    def critique(
        self,
        user_prompt: str,
        rendered_png_bytes: bytes,
        iteration: int,
        max_iterations: int,
        previous_feedback: Optional[str] = None,
        last_requested_feature: Optional[str] = None,
        locked_features: Optional[List[str]] = None,
        banned_features: Optional[List[str]] = None,
        comparison_image: bool = False,
    ) -> Dict[str, Any]:
        if not user_prompt or not user_prompt.strip():
            raise ValueError("user_prompt is required")
        if not rendered_png_bytes:
            raise ValueError("rendered_png_bytes is required")
        if len(rendered_png_bytes) > MAX_PNG_BYTES:
            raise ValueError(
                f"rendered_png_bytes exceeds limit: {len(rendered_png_bytes)} > {MAX_PNG_BYTES}"
            )
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if iteration < 0:
            raise ValueError("iteration must be >= 0")

        logger.info(
            "critic.critique: iter=%d/%d png=%d bytes",
            iteration + 1, max_iterations, len(rendered_png_bytes),
        )

        self.client.ensure_model_loaded(self.model)

        user_message = self._build_user_message(
            user_prompt, iteration, max_iterations,
            last_requested_feature=last_requested_feature,
            locked_features=locked_features,
            banned_features=banned_features,
            comparison_image=comparison_image,
        )

        parsed: Any = {}
        ok = False
        errors: List[str] = []

        raw = self._call_model(user_message, rendered_png_bytes)
        if _DEBUG_CRITIC_IO:
            logger.warning("critic raw initial: %s", raw[:2500])
        try:
            parsed = parse_json_payload(raw)
            ok, errors = _validate_critique(parsed)
        except Exception as exc:
            ok = False
            errors = [f"JSON parse error: {exc}"]
            logger.warning("critic first JSON parse failed: %s", exc)

        # Spend a second round-trip only when the output is not even parseable
        # JSON. Schema slips (a missing field, wrong type) are repaired locally —
        # far cheaper than re-querying the vision model.
        if not ok and any("JSON parse error" in str(e) for e in errors):
            logger.warning("critic output unparseable; one stricter retry")
            retry_msg = self._build_user_message(
                user_prompt, iteration, max_iterations, stricter=True, violations=errors,
                last_requested_feature=last_requested_feature,
                locked_features=locked_features,
                banned_features=banned_features,
                comparison_image=comparison_image,
            )
            raw2 = self._call_model(retry_msg, rendered_png_bytes)
            if _DEBUG_CRITIC_IO:
                logger.warning("critic raw retry: %s", raw2[:2500])
            try:
                parsed = parse_json_payload(raw2)
                ok, errors = _validate_critique(parsed)
            except Exception as exc2:
                parsed, ok = (parsed if isinstance(parsed, dict) else {}), False
                logger.warning("critic retry JSON parse failed: %s", exc2)

        if not ok:
            logger.warning("critic auto-repairing invalid response: %s", errors)
            parsed = _auto_repair_critique(parsed)

        # Tidy the prose/voice, make the lock signal well-formed, then enforce the
        # loop rules locally — no extra model call, no canned phrasing.
        parsed = _compact_artist_feedback(parsed)
        parsed = _normalize_action(parsed)
        parsed = self._apply_loop_policy(
            parsed, user_prompt, iteration, max_iterations,
            last_requested_feature=last_requested_feature,
            locked_features=locked_features,
            banned_features=banned_features,
        )

        # The verdict is filtered, not blindly trusted. Only a high-confidence
        # accept with all listed parts present can stop the orchestrator early;
        # ambiguous accepts are converted into another concrete revision.

        _obs = parsed.get("observations") or []
        if isinstance(_obs, list):
            _obs = " | ".join(str(o) for o in _obs)
        _parts = parsed.get("part_status") or []
        if isinstance(_parts, list):
            _parts = " | ".join(str(p) for p in _parts)
        logger.warning(
            "CRITIC [%s] verdict=%s score=%s action=%s target=%r\n  part_status: %s\n  reasoning: %s\n  observations: %s\n  feedback: %s",
            self.model,
            parsed.get("verdict"),
            parsed.get("score"),
            parsed.get("action"),
            parsed.get("target_region"),
            str(_parts)[:400],
            (parsed.get("reasoning") or "")[:300],
            str(_obs)[:400],
            (parsed.get("feedback_for_artist") or parsed.get("ui_message") or "")[:300],
        )
        return parsed


if __name__ == "__main__":
    import os
    import sys
    import tempfile

    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    from backend.core.config import (
        CRITIC_MODEL,
        OLLAMA_BASE_URL,
        REQUEST_TIMEOUT_SECONDS,
    )

    print(f"Ollama:       {OLLAMA_BASE_URL}")
    print(f"Critic model: {CRITIC_MODEL}")
    print(f"Temperature:  {CRITIC_TEMPERATURE}  (lower = more consistent)")
    print()
    print(
        f"BEFORE CONTINUING: make sure the vision model is available "
        f"({CRITIC_MODEL}) and wait for it to finish loading."
    )
    print()

    from backend.core.config import OLLAMA_BASE_URL, OLLAMA_API_KEY
    from backend.core.ollama_client import OllamaClient
    client = OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=CRITIC_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    out_dir = tempfile.gettempdir()

    # Load the SVG produced by the generator smoke test.
    svg_candidates = [
        os.path.join(out_dir, "test_sun.svg"),
        "/tmp/test_sun.svg",
    ]
    svg_path: Optional[str] = next((p for p in svg_candidates if os.path.exists(p)), None)

    if svg_path:
        # Use the Critic-optimised render for the actual critique.
        from backend.core.renderer import render_svg_for_critic

        with open(svg_path, "r", encoding="utf-8") as f:
            svg_str = f.read()

        print(f"Loaded SVG: {svg_path}")
        print("Rendering Critic-optimised input (2048×2048, white bg, boosted strokes)...")
        png_bytes = render_svg_for_critic(svg_str)

        critic_input_path = os.path.join(out_dir, "test_sun_critic_input.png")
        with open(critic_input_path, "wb") as f:
            f.write(png_bytes)
        print(f"Saved Critic input: {critic_input_path} ({len(png_bytes):,} bytes)")
        print("Compare this against test_sun.png — the Critic input should be clearly more legible.")
    else:
        # Fall back to the pre-rendered PNG if SVG is unavailable.
        png_candidates = [
            os.path.join(out_dir, "test_sun.png"),
            "/tmp/test_sun.png",
        ]
        png_path: Optional[str] = next((p for p in png_candidates if os.path.exists(p)), None)
        if not png_path:
            print(
                "\nFAIL — no test_sun.svg or test_sun.png found. "
                "Run `python -m core.generator` first.",
                file=sys.stderr,
            )
            sys.exit(1)
        with open(png_path, "rb") as f:
            png_bytes = f.read()
        print(f"Loaded fallback PNG: {png_path} ({len(png_bytes):,} bytes)")
        print("NOTE: for better results, run `python -m core.generator` first so we can use")
        print("      the SVG to produce a Critic-optimised render.")

    print()

    critic = VisualCritic(client=client, model=CRITIC_MODEL)
    print("Critiquing against prompt: 'a smiling sun'")
    print("(local vision inference may take 10-40s on first call)\n")

    t0 = time.monotonic()
    try:
        result = critic.critique(
            user_prompt="a smiling sun",
            rendered_png_bytes=png_bytes,
            iteration=0,
            max_iterations=4,
        )
    except ModelBackendError as exc:
        print(f"\nFAIL — backend error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    except CritiqueError as exc:
        print(f"\nFAIL — CritiqueError: {exc}", file=sys.stderr)
        if exc.raw_response:
            print("\n--- raw response ---", file=sys.stderr)
            print(exc.raw_response[:1500], file=sys.stderr)
        sys.exit(2)
    elapsed = time.monotonic() - t0

    print("=" * 60)
    print(f"VERDICT: {result['verdict']}")
    print(f"SCORE:   {result['score']}/10")
    print(f"\nREASONING:\n  {result['reasoning']}")
    print("\nOBSERVATIONS:")
    for obs in result.get("observations", []):
        print(f"  - {obs}")
    print(f"\nUI MESSAGE (audience-facing):")
    print(f"  → {result['ui_message']}")
    print(f"\nFEEDBACK FOR ARTIST (detailed):")
    print(f"  {result['feedback_for_artist']}")
    print(f"\nElapsed: {elapsed:.2f}s")
    print()
    print("Validation checklist:")
    ui = result.get("ui_message", "")
    fb = result.get("feedback_for_artist", "")
    print(f"  {'✓' if _sentence_count(ui) == 1 else '✗'} ui_message is exactly one sentence")
    print(f"  {'✓' if 'step-' not in fb and 'd=' not in fb else '✗'} feedback_for_artist has no step-ids / SVG code")
    no_cross_category = not any(w in fb.lower() for w in ['whisker', 'petal', 'wheel', 'fin', 'wing', 'antenna'] if 'sun' in result.get('reasoning', '').lower())
    print(f"  {'✓' if no_cross_category else '✗'} no cross-category feature names (whiskers on a sun, etc.)")
    print(f"  {'✓' if result['verdict'] in ('accept', 'revise') else '✗'} verdict is valid")
    print(f"  {'?' } feedback_for_artist references sun-specific elements (check manually)")
