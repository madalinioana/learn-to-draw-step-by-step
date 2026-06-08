"""Artist module: turns a natural-language prompt into a hand-drawn ink-sketch SVG.

Backend: a model client (Gemini cloud or Ollama local) implementing the
shared model-client interface. The system prompt is structured for a code-tuned model
(numbered hard constraints, explicit JSON schema, two complete worked examples).

Pipeline per call:
    1. Confirm the artist model is loaded in LM Studio (sleeps for the swap
       grace period if so).
    2. Build the user message: a one-line "Draw: …" for fresh requests, or a
       full revision template (previous SVG + numbered critic edits) for
       follow-ups.
    3. Call `chat_text` with `response_format={"type": "json_object"}`. The
       LM Studio client translates that to a permissive json_schema under the
       hood since LM Studio's API doesn't accept the canonical OpenAI value.
    4. Parse the JSON response (with markdown-fence and brace-matching
       fallbacks), validate the payload shape.
    5. Security-sanitize the SVG (XXE-safe, script-free, viewBox/xmlns
       injected if missing).
    6. Style-validate against the sketch-aesthetic rules. On failure, retry
       once with a stricter prompt that lists the specific violations. If
       still failing, auto-repair what's repairable.
    7. If the auto-repaired SVG has zero <path> elements, raise GenerationError.
    8. Run a programmatic wobblify pass that perturbs every path coordinate
       by ±WOBBLE_NOISE_PX of seeded random noise. This is the second line
       of defence: even when Qwen ignores the prompt's wobble guidance and
       emits clean geometric paths, the post-processor guarantees the
       hand-drawn look. Deterministic per input via SVG-hash-derived seed.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from lxml import etree

from core._json_utils import parse_json_payload
from core.config import (
    ARTIST_MAX_TOKENS,
    ARTIST_REVISION_TEMPERATURE,
    ARTIST_TEMPERATURE,
    CANVAS_SIZE,
)
from core.errors import ModelBackendError


logger = logging.getLogger(__name__)

# Timing logger — separate name so callers can filter it independently.
_timing_log = logging.getLogger(__name__ + ".timing")
_FAST_INFERENCE = os.environ.get("FAST_INFERENCE", "1").strip() != "0"


SVG_NS = "http://www.w3.org/2000/svg"

_FORBIDDEN_TAGS = {
    "rect",
    "circle",
    "ellipse",
    "polygon",
    "line",
    "polyline",
    "text",
    "image",
    "use",
    "script",
    "foreignObject",
}

_FILTER_ID = "roughen"
_FILTER_REF = f"url(#{_FILTER_ID})"

MAX_SVG_BYTES = 256 * 1024

# Programmatic wobble applied AFTER generation. Even when Qwen ignores the
# prompt's wobble instructions and emits clean geometric paths, this pass
# perturbs every coordinate so the strokes at least look hand-drawn.
# Set to 0.0 to disable.
WOBBLE_NOISE_PX: float = 4.5

# Small added details (the Critic now requests >=40px features, but the Artist
# may still draw smaller) are dissolved by a full ±4.5px wobble — a 30px
# connector jittered ±4.5px loses its silhouette and the Critic re-requests it
# forever. Below SMALL_SHAPE_PX in its largest dimension, a path gets the
# gentler SMALL_SHAPE_WOBBLE_PX instead so it stays perceptible.
SMALL_SHAPE_PX: float = 55.0
SMALL_SHAPE_WOBBLE_PX: float = 1.8

# Minimum fraction of previous-iteration paths that must appear byte-for-byte
# in a revision. Below this threshold the orchestrator triggers ONE retry
# with an explicit preservation reminder. Below it again on the retry, we
# accept the degraded output but log loudly. Healthy operation should sit
# >= 0.6; consistent runs >= 0.8 indicate the safety belt is rarely needed.
PRESERVATION_RETRY_THRESHOLD: float = 0.2


_PROMPT_TEMPLATE = """\
You draw simple black-line sketches as SVG, like quick pencil studies. Reply with JSON only.

- Canvas viewBox 0 0 __CANVAS_SIZE__ __CANVAS_SIZE__. Use only <path> elements with stroke="#1a1a1a" and fill="none". No text, no colour, no filled shapes, no background.
- Every path has id="step-N" in drawing order; steps[] gives a short label for each path.
- Round 1: draw the subject's basic recognizable shape, centered — a few confident strokes, no small details yet.
- action "add": the "svg" field is a full <svg> document that contains ONLY the new path(s) for the named part, with ids continuing from the last step. Do not include any old paths inside it.
- action "redraw_element": the "svg" field is a full <svg> document with ALL paths; change only the named part and copy every other path unchanged.
- action "redraw_all": the "svg" field is a full <svg> document with a fresh simple drawing from scratch.

Work coarse to fine: the big shapes that identify the subject first, small details last.

JSON: {"reasoning":"","steps":["label"],"svg":"<svg viewBox='0 0 __CANVAS_SIZE__ __CANVAS_SIZE__' xmlns='http://www.w3.org/2000/svg'><g><path id='step-1' d='M...' fill='none' stroke='#1a1a1a'/></g></svg>"}
"""

_SYSTEM_PROMPT = _PROMPT_TEMPLATE.replace("__CANVAS_SIZE__", str(CANVAS_SIZE))


class GenerationError(RuntimeError):
    """Raised when the Artist cannot produce a parseable, usable SVG payload."""

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


# ---------------------------------------------------------------------------
# SVG sanitization, validation, and auto-repair (preserved from Prompt 8)
# ---------------------------------------------------------------------------


def _parse_xml(svg_text: str, recover: bool = False) -> etree._Element:
    parser = etree.XMLParser(
        remove_blank_text=False, resolve_entities=False, no_network=True, recover=recover
    )
    return etree.fromstring(svg_text.encode("utf-8"), parser=parser)


def _looks_remote(content: str) -> bool:
    return "@import" in content or "http://" in content or "https://" in content


def _sanitize_svg(svg_text: str) -> str:
    """Security sanitize only. Structural/aesthetic fixes are delegated to the
    style validator and the auto-repair stage.
    """
    if len(svg_text.encode("utf-8")) > MAX_SVG_BYTES:
        raise GenerationError(
            f"SVG payload exceeds {MAX_SVG_BYTES} bytes — refusing to render",
            raw_response=svg_text[:500],
        )

    try:
        root = _parse_xml(svg_text)
    except etree.XMLSyntaxError as exc:
        # Strict parse failed — local models sometimes emit unquoted attribute
        # values, stray characters, or other well-formedness violations. Try
        # lxml's recovery parser before giving up entirely: it patches the most
        # common structural errors and re-serializes a clean tree, so every
        # downstream stage (style validation, auto-repair, wobblify) sees
        # conformant XML regardless of what the model emitted.
        logger.warning(
            "strict XML parse failed (%s); attempting lxml recovery parse "
            "(malformed SVG from model — this is a reliability metric for the thesis)",
            exc,
        )
        try:
            root = _parse_xml(svg_text, recover=True)
            if root is None:
                raise etree.XMLSyntaxError(
                    "recovery parser returned None", None, None, None
                )
            logger.info("lxml recovery parse succeeded — SVG will be sanitized and re-serialized")
        except etree.XMLSyntaxError as exc2:
            raise GenerationError(
                f"SVG failed to parse as XML (strict and recovery both failed): {exc2}",
                raw_response=svg_text,
            ) from exc2

    local_name = etree.QName(root.tag).localname
    if local_name != "svg":
        raise GenerationError(
            f"root element is <{local_name}>, expected <svg>", raw_response=svg_text
        )

    for script in root.xpath(".//*[local-name()='script']"):
        parent = script.getparent()
        if parent is not None:
            parent.remove(script)

    for style in root.xpath(".//*[local-name()='style']"):
        if _looks_remote(style.text or ""):
            parent = style.getparent()
            if parent is not None:
                parent.remove(style)
                logger.warning("stripped <style> block with remote reference")

    if root.get("xmlns") is None and not root.nsmap.get(None):
        root.set("xmlns", SVG_NS)

    if not root.get("viewBox"):
        root.set("viewBox", f"0 0 {CANVAS_SIZE} {CANVAS_SIZE}")

    # Force the canonical sketch palette. The Artist is told to use only
    # stroke="#1a1a1a"/fill="none", but it occasionally leaks a colored stroke
    # (blue/green seen in mug/flower/sun). Normalize every path so no stray color
    # ever reaches the page; opacity/stroke-width variation is left untouched.
    for p in root.xpath(".//*[local-name()='path']"):
        p.set("stroke", "#1a1a1a")
        if (p.get("fill") or "").strip().lower() != "none":
            p.set("fill", "none")

    return etree.tostring(root, encoding="unicode")


def _find_roughen_groups(root: etree._Element) -> List[etree._Element]:
    out: List[etree._Element] = []
    for c in root:
        if not isinstance(c.tag, str):
            continue
        if etree.QName(c.tag).localname != "g":
            continue
        if (c.get("filter") or "").strip() == _FILTER_REF:
            out.append(c)
    return out


def _auto_wrap_orphan_paths(svg_string: str) -> Tuple[str, int]:
    """Move any <path> elements outside the roughen group into it.

    Returns `(svg_string, n_rewrapped)`. When `n_rewrapped > 0` a WARNING is
    logged — this is a per-iteration reliability metric for the thesis.
    Idempotent: paths already inside the group are untouched.
    """
    try:
        root = _parse_xml(svg_string)
    except etree.XMLSyntaxError:
        return svg_string, 0

    wrapping_gs = _find_roughen_groups(root)
    if wrapping_gs:
        wrapping_g = wrapping_gs[0]
    else:
        # No roughen group at all — create one so orphan paths land somewhere sane.
        wrapping_g = etree.SubElement(root, f"{{{SVG_NS}}}g")
        wrapping_g.set("filter", _FILTER_REF)
        logger.warning("_auto_wrap_orphan_paths: created missing <g filter=\"url(#roughen)\">")

    orphans: List[etree._Element] = []
    for p in root.xpath(".//*[local-name()='path']"):
        if p.getparent() is wrapping_g:
            continue
        orphans.append(p)

    for p in orphans:
        parent = p.getparent()
        if parent is not None:
            parent.remove(p)
        wrapping_g.append(p)

    if orphans:
        logger.warning(
            "auto_wrap_orphan_paths: rewrapped %d orphan path(s) into roughen group "
            "(model emitted structurally-flawed but recoverable SVG — thesis reliability metric)",
            len(orphans),
        )

    return etree.tostring(root, encoding="unicode"), len(orphans)


def _validate_sketch_style(svg_string: str) -> Tuple[bool, List[str]]:
    """Check the SVG against the sketch aesthetic rules.

    Returns `(ok, violations)`. Violation strings are short and specific so they
    can be fed verbatim into the retry prompt.
    """
    violations: List[str] = []

    try:
        root = _parse_xml(svg_string)
    except etree.XMLSyntaxError as exc:
        return False, [f"not well-formed XML: {exc}"]

    if etree.QName(root.tag).localname != "svg":
        return False, [
            f"root element is <{etree.QName(root.tag).localname}>, expected <svg>"
        ]

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el.tag).localname
        if local in _FORBIDDEN_TAGS:
            violations.append(f"forbidden tag <{local}>")

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        fill = el.get("fill")
        if fill is not None and fill.strip().lower() != "none":
            violations.append(
                f"<{etree.QName(el.tag).localname}> has fill={fill!r} — only fill='none' allowed"
            )

    filters = root.xpath(".//*[local-name()='filter' and @id='roughen']")
    if not filters:
        violations.append("missing <filter id='roughen'> — required inside <defs>")

    wrapping_gs = _find_roughen_groups(root)
    if not wrapping_gs:
        violations.append(
            'no <g filter="url(#roughen)"> child of <svg> wrapping the paths'
        )

    for g in wrapping_gs:
        for child in g:
            if not isinstance(child.tag, str):
                continue
            local = etree.QName(child.tag).localname
            if local != "path":
                violations.append(
                    f"non-path <{local}> inside the roughen group — only <path> allowed there"
                )
                continue
            cid = child.get("id")
            if not cid or not re.match(r"^step-\d+$", cid):
                violations.append(f"path missing step-N id (got id={cid!r})")

    # A path is "inside" iff its direct parent is a roughen group. Check that
    # structural property instead of comparing lxml proxy id()s across two
    # separate traversals: lxml hands out fresh proxy objects per query and may
    # GC the earlier ones, so the old id()-based check spuriously reported
    # "path outside group" once a drawing had ~5+ paths — forcing a wasted
    # style-retry (a full model round-trip) on essentially every real drawing.
    for p in root.xpath(".//*[local-name()='path']"):
        parent = p.getparent()
        if not (
            parent is not None
            and isinstance(parent.tag, str)
            and etree.QName(parent.tag).localname == "g"
            and (parent.get("filter") or "").strip() == _FILTER_REF
        ):
            violations.append(
                '<path> found outside the <g filter="url(#roughen)"> group'
            )

    seen: set = set()
    deduped: List[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            deduped.append(v)

    return (len(deduped) == 0), deduped


def _ensure_roughen_defs(root: etree._Element) -> None:
    defs_el = next(
        (
            c
            for c in root
            if isinstance(c.tag, str) and etree.QName(c.tag).localname == "defs"
        ),
        None,
    )
    if defs_el is None:
        defs_el = etree.Element(f"{{{SVG_NS}}}defs")
        root.insert(0, defs_el)

    has_filter = any(
        isinstance(c.tag, str)
        and etree.QName(c.tag).localname == "filter"
        and c.get("id") == _FILTER_ID
        for c in defs_el
    )
    if has_filter:
        return

    filter_xml = (
        f'<filter xmlns="{SVG_NS}" id="{_FILTER_ID}" '
        'x="-5%" y="-5%" width="110%" height="110%">'
        '<feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/>'
        '<feDisplacementMap in="SourceGraphic" scale="1.2"/>'
        "</filter>"
    )
    defs_el.append(etree.fromstring(filter_xml))
    logger.warning("auto-repair: injected missing <filter id='roughen'>")


def _auto_repair(svg_string: str) -> str:
    """Best-effort fix for common style violations. Idempotent."""
    try:
        root = _parse_xml(svg_string)
    except etree.XMLSyntaxError as exc:
        raise GenerationError(
            f"auto-repair cannot parse SVG: {exc}", raw_response=svg_string
        ) from exc

    if etree.QName(root.tag).localname != "svg":
        raise GenerationError(
            f"auto-repair: root <{etree.QName(root.tag).localname}> is not <svg>",
            raw_response=svg_string,
        )

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        local = etree.QName(el.tag).localname
        if local in _FORBIDDEN_TAGS:
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                logger.warning("auto-repair: stripped forbidden tag <%s>", local)

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        fill = el.get("fill")
        if fill is not None and fill.strip().lower() != "none":
            el.set("fill", "none")
            logger.warning(
                "auto-repair: nuked fill=%r on <%s>",
                fill,
                etree.QName(el.tag).localname,
            )

    _ensure_roughen_defs(root)

    wrapping_gs = _find_roughen_groups(root)
    if wrapping_gs:
        wrapping_g = wrapping_gs[0]
    else:
        wrapping_g = etree.SubElement(root, f"{{{SVG_NS}}}g")
        wrapping_g.set("filter", _FILTER_REF)
        logger.warning('auto-repair: created missing <g filter="url(#roughen)">')

    orphan_paths: List[etree._Element] = []
    for p in root.xpath(".//*[local-name()='path']"):
        parent = p.getparent()
        if parent is wrapping_g:
            continue
        orphan_paths.append(p)

    for p in orphan_paths:
        parent = p.getparent()
        if parent is not None:
            parent.remove(p)
        wrapping_g.append(p)

    if orphan_paths:
        logger.warning(
            "auto-repair: moved %d orphan <path>(s) into the roughen group",
            len(orphan_paths),
        )

    existing_ids = {
        p.get("id")
        for p in wrapping_g
        if isinstance(p.tag, str)
        and etree.QName(p.tag).localname == "path"
        and p.get("id")
        and re.match(r"^step-\d+$", p.get("id") or "")
    }
    next_step = 1
    assigned = 0
    for p in wrapping_g:
        if not isinstance(p.tag, str):
            continue
        if etree.QName(p.tag).localname != "path":
            continue
        cid = p.get("id")
        if cid and re.match(r"^step-\d+$", cid):
            continue
        while f"step-{next_step}" in existing_ids:
            next_step += 1
        new_id = f"step-{next_step}"
        p.set("id", new_id)
        existing_ids.add(new_id)
        next_step += 1
        assigned += 1
    if assigned:
        logger.warning("auto-repair: assigned step-N ids to %d path(s)", assigned)

    return etree.tostring(root, encoding="unicode")


def _has_any_path(svg_string: str) -> bool:
    try:
        root = _parse_xml(svg_string)
    except etree.XMLSyntaxError:
        return False
    return bool(root.xpath(".//*[local-name()='path']"))


def _validate_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Coerce the raw JSON dict into the expected shape."""
    if "svg" not in data or not isinstance(data["svg"], str) or not data["svg"].strip():
        raise ValueError("payload missing non-empty 'svg' string")

    reasoning = data.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    steps = data.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("payload 'steps' must be a list")
    steps = [str(s) for s in steps]

    style_notes = data.get("style_notes", "")
    if not isinstance(style_notes, str):
        style_notes = str(style_notes)

    return {
        "svg": data["svg"].strip(),
        "reasoning": reasoning.strip(),
        "steps": steps,
        "style_notes": style_notes.strip(),
    }


# ---------------------------------------------------------------------------
# Wobblify pass — programmatic insurance against geometric output
# ---------------------------------------------------------------------------
#
# Even with the loud anti-pattern instructions in the system prompt,
# Qwen2.5-Coder's training prior toward clean SVG icons sometimes wins.
# This pass parses every <path> in the generated SVG, perturbs every
# coordinate by ±WOBBLE_NOISE_PX of seeded random noise, and serializes
# back. The result is guaranteed visible wobble even when the model emits
# mathematically perfect geometry.
#
# Seed is derived from the SVG hash so the same SVG always wobblifies to
# the same output — consistent caching, consistent re-renders.

# Number of arguments per SVG path command. Same for upper/lower case.
_PATH_ARG_COUNT: Dict[str, int] = {
    "M": 2,
    "L": 2,
    "H": 1,
    "V": 1,
    "C": 6,
    "S": 4,
    "Q": 4,
    "T": 2,
    "A": 7,
    "Z": 0,
}

# Indices (within an arg group) that represent perturbable coordinates.
# Arc command 'A' has rotation + 2 flags at indices 2,3,4 — those must NOT
# be perturbed (rotation degrades shape, flags must remain 0/1).
_PATH_COORD_INDICES: Dict[str, List[int]] = {
    "M": [0, 1],
    "L": [0, 1],
    "H": [0],
    "V": [0],
    "C": [0, 1, 2, 3, 4, 5],
    "S": [0, 1, 2, 3],
    "Q": [0, 1, 2, 3],
    "T": [0, 1],
    "A": [0, 1, 5, 6],
    "Z": [],
}

_PATH_TOKEN_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


def _fmt_coord(x: float) -> str:
    """Compact float formatting for SVG path data."""
    if abs(x - round(x)) < 1e-3:
        return str(int(round(x)))
    return f"{x:.1f}"


def _subdivide_long_l_segments(d: str, max_len: float = 22.0) -> str:
    """Pre-wobblify pass: split long absolute-L segments into sub-L commands.

    The wobble pass jitters endpoints by ±WOBBLE_NOISE_PX. For polygon shapes
    (diamonds, stars, triangles, sails) made of long straight L edges, jittered
    endpoints leave straight edges between them — the shape looks like a clean
    geometric polygon with slightly offset corners. Real hand-drawn polygons
    have wavy edges, not just imperfect corners.

    By subdividing each long L segment into intermediate sub-L points BEFORE
    wobble, those intermediates get jittered too, producing the mid-line
    variation a notebook sketch actually has.

    Handles uppercase M and L (absolute coords) — the only commands the
    SHAPE VOCABULARY templates use. Other commands (C/Q/S/T/A/H/V and the
    lowercase relative variants) pass through with cursor tracking when
    possible, cursor invalidation otherwise (so we don't subdivide L
    commands after a cursor we can't trust).
    """
    if max_len <= 0 or not d.strip():
        return d

    tokens = _PATH_TOKEN_RE.findall(d)
    if not tokens:
        return d

    out: List[str] = []
    i = 0
    last_cmd: Optional[str] = None
    cx: Optional[float] = None
    cy: Optional[float] = None
    subpath_start: Optional[Tuple[float, float]] = None

    while i < len(tokens):
        tok = tokens[i]
        if len(tok) == 1 and tok.isalpha():
            out.append(tok)
            if tok in ("Z", "z") and subpath_start is not None:
                cx, cy = subpath_start
            last_cmd = tok
            i += 1
            continue

        if last_cmd is None:
            out.append(tok)
            i += 1
            continue

        cmd = last_cmd
        cmd_upper = cmd.upper()
        if cmd_upper not in _PATH_ARG_COUNT:
            out.append(tok)
            i += 1
            continue

        n_args = _PATH_ARG_COUNT[cmd_upper]
        if n_args == 0:
            i += 1
            continue

        group: List[float] = []
        for j in range(n_args):
            if i + j >= len(tokens):
                break
            tk = tokens[i + j]
            if len(tk) == 1 and tk.isalpha():
                break
            try:
                group.append(float(tk))
            except ValueError:
                break

        if len(group) != n_args:
            for g in group:
                out.append(_fmt_coord(g))
            i += len(group)
            continue

        if cmd == "L" and cx is not None and cy is not None:
            ex, ey = group[0], group[1]
            dx, dy = ex - cx, ey - cy
            seg_len = (dx * dx + dy * dy) ** 0.5
            if seg_len > max_len:
                n_sub = min(4, int(seg_len / max_len) + 1)
                for k in range(1, n_sub):
                    t = k / n_sub
                    out.append(_fmt_coord(cx + dx * t))
                    out.append(_fmt_coord(cy + dy * t))
            out.append(_fmt_coord(ex))
            out.append(_fmt_coord(ey))
            cx, cy = ex, ey
        else:
            for g in group:
                out.append(_fmt_coord(g))
            if cmd == "M":
                cx, cy = group[0], group[1]
                subpath_start = (cx, cy)
                last_cmd = "L"
                i += n_args
                continue
            elif cmd == "C":
                cx, cy = group[4], group[5]
            elif cmd in ("Q", "S"):
                cx, cy = group[2], group[3]
            elif cmd == "T":
                cx, cy = group[0], group[1]
            elif cmd == "H":
                cx = group[0]
            elif cmd == "V":
                cy = group[0]
            else:
                cx, cy = None, None

        i += n_args

    return " ".join(out)


def _path_points(d: str) -> List[Tuple[float, float]]:
    """Collect (x, y) coordinate pairs from a path 'd' (control points included).

    Walks command groups like the wobble pass. Not an exact geometric reading
    (H/V single-coords are skipped), but enough for size/position heuristics.
    """
    if not d.strip():
        return []
    tokens = _PATH_TOKEN_RE.findall(d)
    pts: List[Tuple[float, float]] = []
    i = 0
    last_cmd: Optional[str] = None
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) == 1 and tok.isalpha():
            last_cmd = tok.upper()
            i += 1
            continue
        if last_cmd is None or last_cmd not in _PATH_ARG_COUNT:
            i += 1
            continue
        n_args = _PATH_ARG_COUNT[last_cmd]
        if n_args == 0:
            i += 1
            continue
        group: List[float] = []
        for j in range(n_args):
            if i + j >= len(tokens):
                break
            tk = tokens[i + j]
            if len(tk) == 1 and tk.isalpha():
                break
            try:
                group.append(float(tk))
            except ValueError:
                break
        if len(group) != n_args:
            i += len(group)
            continue
        coord_idx = _PATH_COORD_INDICES[last_cmd]
        # Pair coordinate positions as (x, y); ignore a trailing lone coord (H/V).
        for k in range(0, len(coord_idx) - 1, 2):
            pts.append((group[coord_idx[k]], group[coord_idx[k + 1]]))
        i += n_args
    return pts


def _path_bbox_max_dim(d: str) -> Optional[float]:
    """Largest bounding-box dimension (px) of a path, or None if no points."""
    pts = _path_points(d)
    if not pts:
        return None
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _path_signature(d: str) -> Optional[Tuple[float, float, float, float, float, float]]:
    """A drift-tolerant fingerprint of a stroke: (start_x, start_y, end_x, end_y, width, height).

    Used to recognise when the Artist echoed an existing path (with the small
    coordinate paraphrase a model adds) rather than drawing a new one, without
    the lattice-boundary brittleness of snapping coordinates to a grid.

    The endpoint is part of the fingerprint so two *distinct* small strokes that
    happen to share a region and bounding box (e.g. an eye arc and a mouth curve
    drawn close together on the face) are not mistaken for one another — that
    false merge silently dropped legitimately-new features and wasted a whole
    iteration. A genuine echo re-states the same path, so its start, end, and
    size all coincide; a different feature differs in at least one.
    """
    pts = _path_points(d)
    if not pts:
        return None
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    return (pts[0][0], pts[0][1], pts[-1][0], pts[-1][1], max(xs) - min(xs), max(ys) - min(ys))


def _signatures_match(
    a: Tuple[float, float, float, float, float, float],
    b: Tuple[float, float, float, float, float, float],
    pos_tol: float = 18.0,
    size_tol: float = 28.0,
) -> bool:
    """True when two stroke signatures are close enough to be the same stroke.

    Requires the start point, the end point, AND the bounding box to all agree —
    so only a near-identical re-emission (a real echo) matches.
    """
    # Tiny details on the same face/cabin/body region can be legitimately close
    # together. A global 18px tolerance caused a new nose/mouth/window divider to
    # be mistaken for an echoed old stroke, producing byte-identical iterations.
    if max(a[4], a[5], b[4], b[5]) < 35.0:
        pos_tol = min(pos_tol, 7.0)
        size_tol = min(size_tol, 9.0)

    return (
        abs(a[0] - b[0]) <= pos_tol
        and abs(a[1] - b[1]) <= pos_tol
        and abs(a[2] - b[2]) <= pos_tol
        and abs(a[3] - b[3]) <= pos_tol
        and abs(a[4] - b[4]) <= size_tol
        and abs(a[5] - b[5]) <= size_tol
    )


def _wobblify_path_d(d: str, noise: float, rng: random.Random) -> str:
    """Perturb every coordinate in an SVG path 'd' attribute by ±noise pixels.

    Tokenizes the path, walks command-by-command, perturbs only the indices
    flagged as coordinates per command. Arc rotation and flag arguments are
    preserved exactly. Implicit-repeat groups (e.g., `C ... C-args C-args`)
    are handled by re-using the previous command's arg count.

    Returns a re-serialized 'd' string. On any unexpected input shape, the
    raw token is passed through.
    """
    if noise <= 0.0 or not d.strip():
        return d

    tokens = _PATH_TOKEN_RE.findall(d)
    out: List[str] = []
    i = 0
    last_cmd: Optional[str] = None

    while i < len(tokens):
        tok = tokens[i]
        if len(tok) == 1 and tok.isalpha():
            out.append(tok)
            last_cmd = tok.upper()
            i += 1
            continue

        if last_cmd is None or last_cmd not in _PATH_ARG_COUNT:
            out.append(tok)
            i += 1
            continue

        n_args = _PATH_ARG_COUNT[last_cmd]
        if n_args == 0:
            out.append(tok)
            i += 1
            continue

        # Collect one group of n_args numbers.
        group: List[float] = []
        for j in range(n_args):
            if i + j >= len(tokens):
                break
            tk = tokens[i + j]
            if len(tk) == 1 and tk.isalpha():
                break
            try:
                group.append(float(tk))
            except ValueError:
                break

        if len(group) != n_args:
            for g in group:
                out.append(_fmt_coord(g))
            i += len(group)
            continue

        coord_idx = _PATH_COORD_INDICES[last_cmd]
        for idx in coord_idx:
            group[idx] = group[idx] + rng.uniform(-noise, noise)

        for g in group:
            out.append(_fmt_coord(g))
        i += n_args

    return " ".join(out)


def _wobblify_svg(
    svg_string: str,
    noise: float = WOBBLE_NOISE_PX,
    preserve_ids: Optional[set] = None,
) -> str:
    """Walk every <path> in the SVG and perturb its `d` coordinates.

    Deterministic per input: the RNG seed is derived from the SVG content
    hash so re-rendering the same SVG produces the same wobble.

    `preserve_ids` is an optional set of step-N ids whose paths must NOT be
    re-perturbed. Used in revision mode: the artist preserves selected paths
    byte-for-byte from the previous iteration (which were already wobblified
    on their original generation), so we skip them here to avoid drift across
    iterations. Newly-added paths still get the wobble treatment.

    Tolerant: any parse failure or per-path exception falls back to leaving
    that element untouched.
    """
    if noise <= 0.0:
        return svg_string

    try:
        root = _parse_xml(svg_string)
    except etree.XMLSyntaxError:
        logger.warning("wobblify: cannot parse SVG, returning unchanged")
        return svg_string

    seed_hex = hashlib.md5(svg_string.encode("utf-8")).hexdigest()[:8]
    rng = random.Random(int(seed_hex, 16))

    preserve = preserve_ids or set()
    perturbed = 0
    skipped = 0
    for path in root.xpath(".//*[local-name()='path']"):
        pid = path.get("id") or ""
        if pid in preserve:
            skipped += 1
            continue
        d = path.get("d") or ""
        if not d.strip():
            continue
        try:
            # Small shapes get gentler wobble so the filter doesn't dissolve
            # them into invisible specks (which makes the Critic re-request them).
            path_noise = noise
            bbox_dim = _path_bbox_max_dim(d)
            if bbox_dim is not None and bbox_dim < SMALL_SHAPE_PX:
                path_noise = min(noise, SMALL_SHAPE_WOBBLE_PX)
            subdivided = _subdivide_long_l_segments(d, max_len=22.0)
            new_d = _wobblify_path_d(subdivided, noise=path_noise, rng=rng)
            path.set("d", new_d)
            perturbed += 1
        except Exception as exc:
            logger.warning("wobblify: failed on path d=%r: %s", d[:80], exc)

    if skipped:
        logger.info(
            "wobblify: perturbed %d path(s), preserved %d (revision lock)",
            perturbed, skipped,
        )
    else:
        logger.info("wobblify: perturbed %d path(s) by ±%.1fpx", perturbed, noise)
    return etree.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# Path preservation measurement (thesis evaluation metric)
# ---------------------------------------------------------------------------


def _extract_step_paths(svg_string: str) -> Dict[str, str]:
    """Return a `{step_id: d_attribute}` dict for every <path id="step-N"> in the tree.

    Returns `{}` on parse failure so callers don't have to guard.
    """
    if not svg_string:
        return {}
    try:
        root = _parse_xml(svg_string)
    except (etree.XMLSyntaxError, GenerationError):
        return {}

    paths: Dict[str, str] = {}
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el.tag).localname != "path":
            continue
        sid = el.get("id") or ""
        if not sid.startswith("step-"):
            continue
        paths[sid] = el.get("d") or ""
    return paths


def _round_path_d(d: str) -> str:
    """Round every numeric token in a d-attribute to the nearest integer.

    The Artist only needs to copy preserved d-strings verbatim back into its
    output. Sub-pixel precision in the round-trip is wasted: the wobblify
    pass perturbs every coord by ±4.5px on the new generation, so 1px
    rounding here is well inside the noise floor. Cuts revision payload
    size by ~30-50% on iterations 3+ where d-strings have accumulated
    decimals from prior wobblify passes.
    """
    if not d:
        return d
    out: List[str] = []
    for tok in _PATH_TOKEN_RE.findall(d):
        if len(tok) == 1 and tok.isalpha():
            out.append(tok)
            continue
        try:
            n = float(tok)
        except ValueError:
            out.append(tok)
            continue
        out.append(str(int(round(n))))
    return " ".join(out)


def _compact_paths_for_revision(
    svg_string: str,
    step_labels: Optional[List[str]] = None,
    geometry: str = "d",
) -> str:
    """Convert previous SVG to a compact JSON list per path.

    `step_labels` aligns 1:1 with the step-N ids (steps[0] is step-1). When
    provided, each entry includes a human-readable label so the Artist can
    identify what each preserved path depicts (e.g., "lighthouse tower",
    "left wave"). This is critical for the revision prompt to land — the
    Artist needs to know which path is which when the Critic says "add
    stripes to the lighthouse".

    `geometry="d"` includes rounded path data for redraw passes. `geometry="bbox"`
    includes only bounding boxes/endpoints for add passes, which is much shorter
    and enough for placing new strokes.

    Strips the per-path stroke/fill/opacity boilerplate (which is identical
    every call) and the <defs>/<filter> wrapper. Cuts ~800-1200 tokens vs
    embedding the full SVG.
    """
    import json
    paths = _extract_step_paths(svg_string)
    if not paths:
        return "[]"

    def step_num(sid: str) -> int:
        try:
            return int(sid.split("-", 1)[1])
        except (IndexError, ValueError):
            return 0

    ordered = sorted(paths.items(), key=lambda kv: step_num(kv[0]))

    labels = step_labels or []
    out = []
    for sid, d in ordered:
        idx = step_num(sid) - 1
        entry: Dict[str, Any] = {"id": sid}
        if 0 <= idx < len(labels) and labels[idx]:
            entry["label"] = labels[idx]
        if geometry == "bbox":
            pts = _path_points(d)
            if pts:
                xs = [x for x, _ in pts]
                ys = [y for _, y in pts]
                entry["box"] = [
                    int(round(min(xs))),
                    int(round(min(ys))),
                    int(round(max(xs))),
                    int(round(max(ys))),
                ]
                entry["ends"] = [
                    int(round(pts[0][0])),
                    int(round(pts[0][1])),
                    int(round(pts[-1][0])),
                    int(round(pts[-1][1])),
                ]
        else:
            entry["d"] = _round_path_d(d)
        if len(entry) > 1:
            out.append(entry)
        else:
            out.append({"id": sid})
    return json.dumps(out, separators=(",", ":"))


def _merge_dropped_paths(previous_svg: str, new_svg: str) -> Tuple[str, List[str]]:
    """Restore any step-N path from previous_svg that is missing in new_svg.

    The Artist must preserve every step-N id unless the Critic explicitly asked
    to remove it. When the Artist accidentally drops a path mid-revision, the
    drawing regresses (a previously-present element disappears) and the Critic's
    next score drops. This pass inserts the dropped path back into the roughen
    group, keeping document order in step-N sequence so animation playback is
    correct.

    Returns (merged_svg, restored_ids). On parse failure returns new_svg unchanged.
    """
    if not previous_svg or not new_svg:
        return new_svg, []

    try:
        prev_root = _parse_xml(previous_svg)
        new_root = _parse_xml(new_svg)
    except etree.XMLSyntaxError:
        return new_svg, []

    def step_num(sid: str) -> Optional[int]:
        if not sid or not sid.startswith("step-"):
            return None
        try:
            return int(sid.split("-", 1)[1])
        except (IndexError, ValueError):
            return None

    prev_paths: Dict[int, etree._Element] = {}
    for el in prev_root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el.tag).localname != "path":
            continue
        n = step_num(el.get("id") or "")
        if n is not None:
            prev_paths[n] = el

    roughen_groups = _find_roughen_groups(new_root)
    if not roughen_groups:
        return new_svg, []
    target_group = roughen_groups[0]

    new_step_nums: set = set()
    for el in target_group.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el.tag).localname != "path":
            continue
        n = step_num(el.get("id") or "")
        if n is not None:
            new_step_nums.add(n)

    dropped_nums = sorted(n for n in prev_paths.keys() if n not in new_step_nums)
    if not dropped_nums:
        return new_svg, []

    restored: List[str] = []
    for num in dropped_nums:
        prev_el = prev_paths[num]
        new_path = etree.SubElement(target_group, f"{{{SVG_NS}}}path")
        for attr in (
            "id", "d", "fill", "stroke", "stroke-width",
            "stroke-linecap", "stroke-linejoin", "opacity",
        ):
            val = prev_el.get(attr)
            if val is not None:
                new_path.set(attr, val)
        restored.append(f"step-{num}")

    def child_sort_key(child: etree._Element) -> int:
        if not isinstance(child.tag, str):
            return 10_000
        if etree.QName(child.tag).localname != "path":
            return 10_000
        n = step_num(child.get("id") or "")
        return n if n is not None else 10_000

    children = list(target_group)
    sorted_children = sorted(children, key=child_sort_key)
    for child in children:
        target_group.remove(child)
    for child in sorted_children:
        target_group.append(child)

    return etree.tostring(new_root, encoding="unicode"), restored


def _measure_preservation(previous_svg: str, new_svg: str) -> Dict[str, Any]:
    """Compare paths by step-N id and report counts of preserved/modified/removed/added.

    A path is "preserved" iff the same step-N id is present in both SVGs AND
    the `d` attribute matches byte-for-byte. "Modified" means the id matches
    but the `d` differs. "Removed" means the id was in previous but is gone.
    "Added" means a new step-N id appears in new_svg only.

    Returns a dict including `preservation_rate` = preserved / total_previous,
    or 1.0 if previous had zero paths. `error: True` is returned on parse
    failure so the caller can skip the safety belt and just log.
    """
    try:
        prev_paths = _extract_step_paths(previous_svg)
        new_paths = _extract_step_paths(new_svg)
    except Exception:
        return {
            "preserved": 0,
            "modified": 0,
            "removed": 0,
            "added": 0,
            "preservation_rate": 0.0,
            "error": True,
        }

    preserved = sum(
        1 for sid, d in prev_paths.items()
        if sid in new_paths and new_paths[sid] == d
    )
    modified = sum(
        1 for sid, d in prev_paths.items()
        if sid in new_paths and new_paths[sid] != d
    )
    removed = sum(1 for sid in prev_paths if sid not in new_paths)
    added = sum(1 for sid in new_paths if sid not in prev_paths)

    total_prev = len(prev_paths)
    preservation_rate = preserved / total_prev if total_prev > 0 else 1.0

    return {
        "preserved": preserved,
        "modified": modified,
        "removed": removed,
        "added": added,
        "preservation_rate": preservation_rate,
        "error": False,
    }


def _preserved_step_ids(previous_svg: str, new_svg: str) -> set:
    """Return the set of step-N ids whose `d` attribute matches byte-for-byte.

    Used to gate the wobblify pass so preserved paths stay frozen instead of
    drifting from re-application of random noise.
    """
    prev_paths = _extract_step_paths(previous_svg)
    new_paths = _extract_step_paths(new_svg)
    return {
        sid for sid, d in prev_paths.items()
        if sid in new_paths and new_paths[sid] == d
    }


def _is_redraw_intent(critic_feedback: str) -> bool:
    """Detect whether the Critic explicitly asks to redraw/replace existing elements.

    This is deliberately narrower than "the Critic wants an improvement". A
    patch such as "add whiskers" or "make the face clearer" should preserve
    existing paths; only explicit repair language should activate replacement
    instructions.
    """
    if not critic_feedback:
        return False
    feedback_lower = critic_feedback.lower()
    redraw_signals = [
        "redraw",
        "replace",
        "reshape",
        "change the shape",
        "wrong category",
        "unrecognizable",
        "not recognizable",
        "not identifiable",
        "does not read",
        "does not yet read",
        "doesn't read",
        "doesn't yet read",
        "instead of",
        "rather than",
    ]
    return any(signal in feedback_lower for signal in redraw_signals)


def _is_whole_subject_redraw_intent(critic_feedback: str) -> bool:
    """Return True when feedback asks for a broad subject-level rebuild.

    Local redraws should still preserve unrelated paths. Whole-subject repairs
    are reserved for cases where the image does not read as the requested
    category at all.
    """
    if not critic_feedback:
        return False
    feedback_lower = critic_feedback.lower()
    whole_subject_signals = [
        "redraw the whole",
        "redraw whole",
        "redraw the entire",
        "redraw this as",
        "redraw it as",
        "entire composition",
        "whole composition",
        "whole subject",
        "start over",
        "unrecognizable",
        "not recognizable",
        "not identifiable",
        "does not read as",
        "does not yet read as",
        "doesn't read as",
        "doesn't yet read as",
    ]
    return any(signal in feedback_lower for signal in whole_subject_signals)


def _extract_paths_by_id(svg_string: str) -> Dict[str, etree._Element]:
    """Return a {step_id: element} dict for every <path id="step-N"> in the tree.

    Returns {} on parse failure.
    """
    if not svg_string:
        return {}
    try:
        root = _parse_xml(svg_string)
    except (etree.XMLSyntaxError, GenerationError):
        return {}

    result: Dict[str, etree._Element] = {}
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el.tag).localname != "path":
            continue
        sid = el.get("id") or ""
        if sid.startswith("step-"):
            result[sid] = el
    return result


def _identify_targeted_paths(
    critic_feedback: str,
    previous_steps: List[str],
) -> set:
    """Heuristic: return step-N ids whose labels the Critic feedback appears to address.

    Strategy: for each step label, check if 3+ consecutive characters of the
    label (lowercased) appear in the feedback (lowercased). This is intentionally
    broad — a false positive (thinking a path is targeted when it isn't) is safe
    because it lets the Artist modify it freely; a false negative (thinking a
    path is NOT targeted when it is) would incorrectly lock the path.

    Returns a set of step-N ids (e.g., {"step-3", "step-5"}).
    Falls back to empty set on any error so the caller's preservation logic is
    skipped rather than erroneously applied.
    """
    if not critic_feedback or not previous_steps:
        return set()

    feedback_lower = critic_feedback.lower()
    targeted: set = set()

    for idx, label in enumerate(previous_steps):
        if not label:
            continue
        label_lower = label.lower().strip()
        words = label_lower.split()
        for word in words:
            # Skip very short words (articles, prepositions) to avoid false positives
            if len(word) < 4:
                continue
            if word in feedback_lower:
                targeted.add(f"step-{idx + 1}")
                break

    return targeted


def _enforce_path_preservation(
    previous_svg: str,
    new_svg: str,
    targeted_ids: set,
    critic_feedback: str = "",
    action: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Deterministically revert Artist modifications to paths the Critic did NOT target.

    This is the CODE-ENFORCED REGION LOCK. For each step-N path in previous_svg:
      - If its id is in targeted_ids → Artist was allowed to modify/replace it → keep new.
      - If its id is NOT in targeted_ids AND the Artist changed its `d` → revert to
        previous `d` (the Critic didn't ask for this change; it's drift).
      - If the path is missing in new_svg → restored by _merge_dropped_paths upstream;
        nothing to do here (we operate after the merge).
    Newly-added paths (ids absent from previous_svg) always pass through unchanged.

    When the Critic's explicit `action` is supplied it drives the lock directly —
    no fragile keyword matching:
      - "add"            → targeted_ids must be empty: EVERY existing path is locked,
                           only brand-new step ids may appear. This is the default
                           additive-patch case and the main fix for inter-iteration drift.
      - "redraw_element" → only the matched target path(s) may change; all else locked.
      - "redraw_all"     → enforcement skipped; the previous drawing is not trusted.
    When `action` is None (legacy / cached runs) the older keyword heuristics
    (`_is_redraw_intent` / `_is_whole_subject_redraw_intent`) decide skipping, so
    existing callers behave exactly as before.

    Returns (enforced_svg, report) where report contains counts of reverted/kept/targeted.
    Falls back to (new_svg, error_report) on parse failure.
    """
    if action is not None:
        if action == "redraw_all":
            logger.info(
                "enforce_path_preservation: SKIPPED — action=redraw_all (subject not trusted)",
            )
            return new_svg, {
                "error": False, "reverted": 0, "kept": 0,
                "targeted": len(targeted_ids), "skipped_reason": "redraw_all",
            }
        # action in ("add", "redraw_element"): always enforce the lock below.
        # No keyword early-returns — the Critic told us exactly what may change.
    else:
        # Legacy path: no explicit action signal, fall back to keyword heuristics.
        if _is_whole_subject_redraw_intent(critic_feedback):
            logger.info(
                "enforce_path_preservation: SKIPPED — Critic feedback asks for whole-subject repair "
                "(targeted=%d)",
                len(targeted_ids),
            )
            return new_svg, {
                "error": False, "reverted": 0, "kept": 0,
                "targeted": len(targeted_ids), "skipped_reason": "whole_subject_redraw",
            }
        if _is_redraw_intent(critic_feedback) and not targeted_ids:
            logger.info(
                "enforce_path_preservation: SKIPPED — local redraw requested but no target path "
                "could be identified",
            )
            return new_svg, {
                "error": False, "reverted": 0, "kept": 0,
                "targeted": 0, "skipped_reason": "redraw_no_targets",
            }

    if not previous_svg or not new_svg:
        return new_svg, {"error": True, "reverted": 0, "kept": 0, "targeted": 0}

    try:
        new_root = _parse_xml(new_svg)
    except etree.XMLSyntaxError:
        return new_svg, {"error": True, "reverted": 0, "kept": 0, "targeted": 0}

    prev_paths = _extract_step_paths(previous_svg)
    if not prev_paths:
        return new_svg, {"error": False, "reverted": 0, "kept": 0, "targeted": len(targeted_ids)}

    # IMPORTANT: extract the path elements FROM new_root (the tree we serialize
    # below), not via a second independent parse. A previous version parsed
    # new_svg twice — mutating one tree but serializing the other — so every
    # revert was silently discarded and this enforcement was a no-op.
    new_elements: Dict[str, etree._Element] = {}
    for el in new_root.iter():
        if not isinstance(el.tag, str):
            continue
        if etree.QName(el.tag).localname != "path":
            continue
        sid = el.get("id") or ""
        if sid.startswith("step-"):
            new_elements[sid] = el

    reverted = 0
    kept = 0

    for sid, prev_d in prev_paths.items():
        if sid in targeted_ids:
            kept += 1
            continue
        if sid not in new_elements:
            continue
        new_el = new_elements[sid]
        if new_el.get("d") != prev_d:
            new_el.set("d", prev_d)
            reverted += 1

    if reverted:
        logger.info(
            "enforce_path_preservation: reverted %d untargeted path(s) to previous d "
            "(targeted=%d kept=%d)",
            reverted, len(targeted_ids), kept,
        )
        new_svg = etree.tostring(new_root, encoding="unicode")

    return new_svg, {
        "error": False,
        "reverted": reverted,
        "kept": kept,
        "targeted": len(targeted_ids),
    }


def _step_num(sid: str) -> Optional[int]:
    """Parse the integer N from a 'step-N' id, or None."""
    if not sid or not sid.startswith("step-"):
        return None
    try:
        return int(sid.split("-", 1)[1])
    except (IndexError, ValueError):
        return None


def _extract_additive(
    previous_svg: str,
    model_svg: str,
    prev_max_id: int,
    model_steps: List[str],
) -> Tuple[str, List[str], List[str]]:
    """Composite an additive revision: previous drawing (verbatim) + only the
    Artist's NEW paths.

    In 'add' mode the Artist is asked to emit ONLY the new region's paths. We
    keep the previous drawing byte-for-byte (the lock) and append the Artist's
    paths that are NOT coarse-duplicates of an existing path, renumbered
    contiguously from step-(K+1). Filtering by geometry (not by id) makes this
    robust two ways:
      - if the Artist obeys and emits only new strokes (whatever it numbers
        them), they don't match any existing path → all kept;
      - if the Artist disobeys and re-emits the whole SVG, the echoed existing
        paths snap to the same lattice as the originals → dropped, so the
        drawing can never be doubled.
    A sane cap (8) guards against a runaway redraw being treated as additions.

    Returns (composite_svg, new_step_ids, new_labels). On parse failure or when
    nothing new was added, returns the previous SVG unchanged with empty lists.
    """
    try:
        prev_root = _parse_xml(previous_svg)
        model_root = _parse_xml(model_svg)
    except etree.XMLSyntaxError:
        return previous_svg, [], []

    group = None
    for el in prev_root.iter():
        if isinstance(el.tag, str) and etree.QName(el.tag).localname == "g":
            group = el
            break
    if group is None:
        return previous_svg, [], []

    prev_sigs = [
        sig for sig in (
            _path_signature(d) for d in _extract_step_paths(previous_svg).values()
        ) if sig is not None
    ]

    model_paths = [
        el for el in model_root.iter()
        if isinstance(el.tag, str) and etree.QName(el.tag).localname == "path"
    ]

    candidates: List[Tuple[etree._Element, str]] = []
    for pos, el in enumerate(model_paths):
        d = el.get("d") or ""
        if not d.strip():
            continue
        sig = _path_signature(d)
        # Drop paths that echo an existing stroke — previous is authoritative.
        if sig is not None and any(_signatures_match(sig, ps) for ps in prev_sigs):
            continue
        label = model_steps[pos] if pos < len(model_steps) else ""
        candidates.append((el, label))

    if len(candidates) > 8:  # one region shouldn't need more than this
        candidates = candidates[:8]
    if not candidates:
        return previous_svg, [], []

    new_ids: List[str] = []
    new_labels: List[str] = []
    nxt = prev_max_id + 1
    for el, label in candidates:
        added = copy.deepcopy(el)
        sid = f"step-{nxt}"
        added.set("id", sid)
        group.append(added)
        new_ids.append(sid)
        new_labels.append(label)
        nxt += 1

    return etree.tostring(prev_root, encoding="unicode"), new_ids, new_labels


# ---------------------------------------------------------------------------
# Generator class
# ---------------------------------------------------------------------------


class SVGGenerator:
    """LM-Studio-backed sketch Artist. Emits SVG + reasoning + steps + style notes."""

    def __init__(self, client, model: str) -> None:
        if client is None:
            raise ValueError("client is required")
        if not model:
            raise ValueError("model is required")
        self.client = client
        self.model = model
        self._last_call_seconds: float = 0.0
        self._last_call_tokens: int = 0
        logger.info("SVGGenerator (sketch mode) ready: model=%s", model)

    def _build_user_message(
        self,
        user_prompt: str,
        previous_svg: Optional[str],
        critic_feedback: Optional[str],
        stricter: bool = False,
        style_violations: Optional[List[str]] = None,
        preservation_warning: Optional[str] = None,
        previous_steps: Optional[List[str]] = None,
        iteration: int = 0,
        max_iterations: Optional[int] = None,
        critic_action: Optional[str] = None,
        critic_target: Optional[str] = None,
    ) -> str:
        total = max_iterations if max_iterations and max_iterations > 0 else None
        iter_label = (
            f"iteration {iteration + 1} of {total}"
            if total is not None
            else f"iteration {iteration + 1}"
        )
        is_final = total is not None and iteration + 1 >= total

        if previous_svg and critic_feedback:
            action = (critic_action or "add").strip().lower()
            if action not in ("add", "redraw_element", "redraw_all"):
                action = "redraw_element" if _is_redraw_intent(critic_feedback) else "add"
            compact = _compact_paths_for_revision(
                previous_svg,
                step_labels=previous_steps,
                geometry="bbox" if action == "add" else "d",
            )

            _max_step = 0
            for _sid in _extract_step_paths(previous_svg):
                try:
                    _max_step = max(_max_step, int(_sid.split("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
            next_id = _max_step + 1
            region = (critic_target or "the requested feature").strip()

            header = (
                f"Request: {user_prompt}\n"
                f"Pass: {iter_label}\n"
                f"Action: {action}\n"
                f"Target: {region}\n"
                f"Critic: {critic_feedback.strip()}\n"
            )

            if action == "add":
                base = (
                    f"{header}"
                    f"Existing paths (for reference only, do not repeat them): {compact}\n"
                    f"Your 'svg' field must be a complete <svg> document containing ONLY the new path(s) "
                    f"for '{region}', with ids starting at step-{next_id}. "
                    "1-3 strokes are enough; 4 only if the part truly needs it."
                )
            elif action == "redraw_element":
                base = (
                    f"{header}"
                    f"Existing paths: {compact}\n"
                    "Output the full SVG. Keep every old path unchanged except the target region. "
                    "Do not add duplicate outlines."
                )
            else:
                base = (
                    f"{header}"
                    "Ignore old paths. Output a new simple full SVG with a recognizable unfinished subject."
                )
        else:
            base = (
                f"Request: {user_prompt}\n"
                f"Pass: {iter_label}\n"
                "Draw the simplest recognizable unfinished subject. Use 3-6 paths: main form plus large identity parts. "
                "Skip small final details unless needed for recognition."
            )

        suffixes: List[str] = []

        if stricter:
            suffixes.append(
                "REMINDER: your previous response was not valid JSON. Return ONLY a JSON object "
                "with keys reasoning, steps, svg. No markdown fences, no prose, "
                "no commentary before or after."
            )

        if style_violations:
            lines = [
                "Fix these SVG issues and return JSON only:"
            ]
            for v in style_violations:
                lines.append(f"  - {v}")
            suffixes.append("\n".join(lines))

        if preservation_warning:
            suffixes.append(preservation_warning)

        if suffixes:
            return base + "\n\n" + "\n\n".join(suffixes)
        return base

    def _call_model(
        self,
        user_message: str,
        call_label: str = "api_call",
        temperature: Optional[float] = None,
    ) -> str:
        """Single chat_text round-trip. Returns raw response text.

        `temperature` overrides the default ARTIST_TEMPERATURE. Revisions pass
        ARTIST_REVISION_TEMPERATURE (lower) so the model follows the Critic's
        specific corrections instead of improvising.
        """
        t_send = time.perf_counter()
        raw = self.client.chat_text(
            model=self.model,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=user_message,
            temperature=ARTIST_TEMPERATURE if temperature is None else temperature,
            max_tokens=ARTIST_MAX_TOKENS,
            response_format={"type": "json_object"},
        )
        t_done = time.perf_counter()
        total_api = t_done - t_send
        n_chars = len(raw or "")
        # Approximate token count: ~4 chars/token for English/SVG mix.
        approx_tokens = max(1, n_chars // 4)
        tps = approx_tokens / total_api if total_api > 0 else 0
        _timing_log.info(
            "[%s] api_call=%.3fs  chars=%d  ~tokens=%d  ~tok/s=%.1f",
            call_label, total_api, n_chars, approx_tokens, tps,
        )
        # Store on instance for the caller to harvest into the phase report.
        self._last_call_seconds = total_api
        self._last_call_tokens = approx_tokens
        return raw

    @staticmethod
    def _check_response_complete(raw: str) -> None:
        """Raise GenerationError if the model's raw response looks truncated.

        We catch two failure modes the model exhibits when it hits the
        max_tokens cap mid-output:
          1. Trailing text doesn't end with `}` (the JSON object is unclosed).
          2. Opening braces outnumber closing braces (a string was truncated
             before its closing quote, causing all subsequent braces to be
             absorbed into the string and never matched).
        Both are ambiguous in isolation but together catch the common case.
        """
        text = (raw or "").strip()
        if not text:
            raise GenerationError(
                "model returned empty response — most likely a max_tokens limit hit",
                raw_response=raw,
            )
        if not text.endswith("}"):
            raise GenerationError(
                "model response does not end with '}' — output was truncated. "
                "Increase ARTIST_MAX_TOKENS in core/config.py or simplify the prompt.",
                raw_response=raw,
            )
        # Brace balance check — only fires when the imbalance is large, since
        # JSON strings legitimately contain unbalanced braces inside `d` paths
        # and reasoning text. We trip only on a clearly broken structure.
        open_braces = text.count("{")
        close_braces = text.count("}")
        if open_braces - close_braces > 1:
            raise GenerationError(
                f"model response has unbalanced braces "
                f"(open={open_braces}, close={close_braces}) — output was truncated. "
                f"Increase ARTIST_MAX_TOKENS in core/config.py or simplify the prompt.",
                raw_response=raw,
            )

    @staticmethod
    def _count_paths_in_svg(svg_text: str) -> int:
        """Count <path> elements in an SVG string via regex (no XML parse)."""
        return len(re.findall(r"<path\b", svg_text or ""))

    def _parse_and_sanitize(self, raw: str) -> Dict[str, Any]:
        """Parse JSON, validate shape, sanitize SVG (security only).

        Truncation is detected up-front so the caller's retry logic gets a
        more useful error message than the downstream JSON-parse failure.
        """
        self._check_response_complete(raw)
        parsed = parse_json_payload(raw)
        validated = _validate_payload(parsed)

        raw_path_count = self._count_paths_in_svg(validated["svg"])
        validated["svg"] = _sanitize_svg(validated["svg"])
        # Silently fix orphan paths before validation so they never trigger
        # a style-retry API call.  The WARNING in _auto_wrap_orphan_paths is
        # the thesis reliability metric; it fires on every call that needs it.
        validated["svg"], _rewrapped = _auto_wrap_orphan_paths(validated["svg"])
        sanitized_path_count = self._count_paths_in_svg(validated["svg"])
        steps_count = len(validated.get("steps") or [])

        # Cross-check: the LLM commits to a path count via the steps list.
        # If the actual SVG has materially fewer paths, something dropped them
        # (truncated body, recovery parser excision, malformed nesting).
        if raw_path_count and sanitized_path_count < raw_path_count:
            logger.warning(
                "sanitize dropped paths: raw=%d sanitized=%d (XML recovery probably skipped malformed elements)",
                raw_path_count, sanitized_path_count,
            )
        if steps_count and sanitized_path_count and abs(steps_count - sanitized_path_count) >= 3:
            logger.warning(
                "path/steps mismatch: SVG has %d paths but steps list has %d entries — "
                "model may have truncated the SVG before completing all promised paths",
                sanitized_path_count, steps_count,
            )

        return validated

    def generate(
        self,
        user_prompt: str,
        previous_svg: Optional[str] = None,
        critic_feedback: Optional[str] = None,
        previous_steps: Optional[List[str]] = None,
        iteration: int = 0,
        max_iterations: Optional[int] = None,
        critic_action: Optional[str] = None,
        critic_target: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not user_prompt or not user_prompt.strip():
            raise ValueError("user_prompt is required")

        revision = previous_svg is not None and bool(
            critic_feedback and critic_feedback.strip()
        )
        # Explicit Critic lock signal (None on initial gen / legacy cached runs).
        _action_this_iter = (critic_action or "").strip().lower() or None
        # Revisions need precision (implement Critic's specific corrections);
        # initial generations benefit from creative variety.
        call_temperature = ARTIST_REVISION_TEMPERATURE if revision else ARTIST_TEMPERATURE
        logger.info(
            "artist.generate: prompt_len=%d revision=%s feedback_len=%d temperature=%.2f",
            len(user_prompt),
            revision,
            len(critic_feedback or ""),
            call_temperature,
        )

        # Hold guard: when the Critic judges the subject complete it returns
        # action="add" with an EMPTY target_region (a "reads clearly; keep it"
        # note). There is nothing new to draw, so keep the previous drawing as-is
        # instead of prompting the Artist (which would trip the no-op retry and
        # risk a stray stroke). A normal feature pass always names a non-empty
        # target, so this only fires on the deliberate completion/hold case.
        if (
            revision
            and previous_svg is not None
            and (_action_this_iter in (None, "add"))
            and not (critic_target or "").strip()
        ):
            logger.info("artist.generate: hold (empty target) — keeping previous drawing unchanged")
            return {
                "svg": previous_svg,
                "reasoning": "Subject reads clearly; holding the drawing.",
                "steps": list(previous_steps or []),
                "style_notes": "",
                "preservation_report": {"error": False, "reverted": 0, "kept": 0, "targeted": 0},
                "redraw_intent": False,
                "_timing": {"total_seconds": 0.0, "phases": {}, "retries": []},
            }

        # ── Timing accumulators ──────────────────────────────────────────────
        _t_generate_start = time.perf_counter()
        _phase_times: Dict[str, float] = {}
        _retries: List[Dict[str, Any]] = []  # each entry: {reason, seconds}

        # ── Phase 1: setup ───────────────────────────────────────────────────
        _t_setup0 = time.perf_counter()
        _timing_log.info("[setup] model=%s  revision=%s", self.model, revision)

        # Confirm the model is loaded (and let it settle).
        self.client.ensure_model_loaded(self.model)

        user_message = self._build_user_message(
            user_prompt, previous_svg, critic_feedback, stricter=False,
            previous_steps=previous_steps, iteration=iteration,
            max_iterations=max_iterations,
            critic_action=critic_action, critic_target=critic_target,
        )
        _phase_times["setup"] = time.perf_counter() - _t_setup0
        _timing_log.info("[setup] done in %.3fs  user_msg_len=%d", _phase_times["setup"], len(user_message))

        # ── Phase 2: initial API call ────────────────────────────────────────
        _t_api0 = time.perf_counter()
        raw = self._call_model(user_message, call_label="initial", temperature=call_temperature)
        _phase_times["api_initial"] = time.perf_counter() - _t_api0

        # ── Phase 3: response processing ────────────────────────────────────
        _t_proc0 = time.perf_counter()
        try:
            validated = self._parse_and_sanitize(raw)
            _phase_times["response_processing"] = time.perf_counter() - _t_proc0
        except Exception as exc:
            _phase_times["response_processing"] = time.perf_counter() - _t_proc0
            _timing_log.warning(
                "[retry:json] trigger=JSON_PARSE_FAILED  reason=%s  after_api=%.3fs",
                type(exc).__name__, _phase_times["api_initial"],
            )
            logger.warning(
                "first JSON parse failed (%s); retrying with stricter prompt", exc
            )
            _t_retry0 = time.perf_counter()
            retry_message = self._build_user_message(
                user_prompt, previous_svg, critic_feedback, stricter=True,
                previous_steps=previous_steps, iteration=iteration,
                max_iterations=max_iterations,
                critic_action=critic_action, critic_target=critic_target,
            )
            raw2 = self._call_model(retry_message, call_label="json_retry", temperature=call_temperature)
            _t_parse2 = time.perf_counter()
            try:
                validated = self._parse_and_sanitize(raw2)
            except Exception as exc2:
                logger.error("second JSON parse failed: %s", exc2)
                _retries.append({
                    "reason": f"JSON_PARSE_FAILED:{type(exc).__name__}",
                    "seconds": time.perf_counter() - _t_retry0,
                })
                raise GenerationError(
                    f"could not parse Artist response as JSON after retry: {exc2}",
                    raw_response=raw2,
                ) from exc2
            _retries.append({
                "reason": f"JSON_PARSE_FAILED:{type(exc).__name__}",
                "seconds": time.perf_counter() - _t_retry0,
            })
            _phase_times["response_processing"] += time.perf_counter() - _t_parse2

        # ── Phase 4: style validation ────────────────────────────────────────
        _t_val0 = time.perf_counter()
        ok, violations = _validate_sketch_style(validated["svg"])
        _phase_times["style_validation"] = time.perf_counter() - _t_val0

        if not ok and not _FAST_INFERENCE:
            _timing_log.warning(
                "[retry:style] trigger=STYLE_VIOLATIONS  count=%d  violations=%s",
                len(violations), violations,
            )
            logger.warning(
                "sketch-style validation failed with %d violation(s); retrying with violation list: %s",
                len(violations),
                violations,
            )
            _t_sr0 = time.perf_counter()
            style_retry_msg = self._build_user_message(
                user_prompt,
                previous_svg,
                critic_feedback,
                stricter=False,
                style_violations=violations,
                previous_steps=previous_steps,
                iteration=iteration,
                max_iterations=max_iterations,
                critic_action=critic_action, critic_target=critic_target,
            )
            raw_style = self._call_model(style_retry_msg, call_label="style_retry", temperature=call_temperature)
            _t_sval = time.perf_counter()
            try:
                retry_validated = self._parse_and_sanitize(raw_style)
                ok_retry, retry_violations = _validate_sketch_style(
                    retry_validated["svg"]
                )
                if ok_retry:
                    logger.info("style retry succeeded")
                    validated = retry_validated
                    ok = True
                    violations = []
                else:
                    logger.warning(
                        "style retry still has %d violation(s): %s",
                        len(retry_violations),
                        retry_violations,
                    )
                    validated = retry_validated
                    violations = retry_violations
            except Exception as exc:
                logger.warning(
                    "style retry JSON parse failed (%s); continuing with original svg",
                    exc,
                )
            _retries.append({
                "reason": f"STYLE_VIOLATIONS:{len(violations)}",
                "seconds": time.perf_counter() - _t_sr0,
            })
            _phase_times["style_validation"] += time.perf_counter() - _t_sval

        # ── Phase 5: auto-repair ─────────────────────────────────────────────
        _t_repair0 = time.perf_counter()
        if not ok:
            logger.warning(
                "auto-repairing SVG (%d residual violations)", len(violations)
            )
            try:
                repaired = _auto_repair(validated["svg"])
                validated["svg"] = repaired
                ok_after, remaining = _validate_sketch_style(repaired)
                if not ok_after:
                    logger.warning(
                        "auto-repair left %d residual violation(s): %s",
                        len(remaining),
                        remaining,
                    )
                if not _has_any_path(repaired):
                    raise GenerationError(
                        "auto-repair produced an SVG with zero paths — unrecoverable",
                        raw_response=repaired,
                    )
            except etree.XMLSyntaxError as exc:
                raise GenerationError(
                    f"auto-repair failed to parse SVG: {exc}",
                    raw_response=validated["svg"],
                ) from exc
        _phase_times["auto_repair"] = time.perf_counter() - _t_repair0

        # ── Phase 5b: additive composite (action=add) ────────────────────────
        # In 'add' mode the Artist emits ONLY the new region's paths (a small
        # SVG) — a big latency win on slow local models. Composite them onto the
        # locked previous drawing here so every later phase sees the full SVG.
        # _extract_additive keeps the previous paths byte-for-byte and appends
        # only genuinely new geometry, so an echoed full SVG can't double the
        # drawing. If the Artist emits no usable new geometry, retry once with a
        # targeted no-op warning instead of silently accepting an identical frame.
        _add_ids: List[str] = []
        _add_labels: List[str] = []
        if revision and previous_svg is not None and _action_this_iter == "add":
            _prev_max = 0
            for _sid in _extract_step_paths(previous_svg):
                _n = _step_num(_sid)
                if _n is not None:
                    _prev_max = max(_prev_max, _n)
            _composite, _add_ids, _add_labels = _extract_additive(
                previous_svg, validated["svg"], _prev_max, validated.get("steps") or [],
            )
            if not _add_ids and not _FAST_INFERENCE:
                no_op_warning = (
                    f"Your previous response did not add any usable new path for "
                    f"'{critic_target or 'the requested part'}'. "
                    f"Try again. Your 'svg' field must be a complete <svg> document "
                    f"containing ONLY 1-3 new paths for that part, "
                    f"with ids starting at step-{_prev_max + 1}. "
                    "Do not include old paths. Place the new stroke in a clear empty "
                    "area so it reads as a separate, visible feature."
                )
                retry_msg = self._build_user_message(
                    user_prompt,
                    previous_svg,
                    critic_feedback,
                    stricter=False,
                    preservation_warning=no_op_warning,
                    previous_steps=previous_steps,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    critic_action=critic_action,
                    critic_target=critic_target,
                )
                _t_noop0 = time.perf_counter()
                try:
                    raw_noop = self._call_model(
                        retry_msg,
                        call_label="add_noop_retry",
                        temperature=call_temperature,
                    )
                    retry_validated = self._parse_and_sanitize(raw_noop)
                    ok_retry, retry_violations = _validate_sketch_style(retry_validated["svg"])
                    if not ok_retry:
                        try:
                            retry_validated["svg"] = _auto_repair(retry_validated["svg"])
                        except Exception:
                            logger.warning(
                                "add no-op retry auto-repair failed; keeping original no-op",
                                exc_info=True,
                            )
                    if _has_any_path(retry_validated["svg"]):
                        _retry_composite, _retry_add_ids, _retry_add_labels = _extract_additive(
                            previous_svg,
                            retry_validated["svg"],
                            _prev_max,
                            retry_validated.get("steps") or [],
                        )
                        if _retry_add_ids:
                            _composite = _retry_composite
                            _add_ids = _retry_add_ids
                            _add_labels = _retry_add_labels
                            logger.info(
                                "add no-op retry succeeded: appended %d new path(s) %s for region=%r",
                                len(_add_ids), _add_ids, critic_target,
                            )
                        else:
                            logger.warning(
                                "add no-op retry still produced no usable new paths for region=%r",
                                critic_target,
                            )
                    else:
                        logger.warning(
                            "add no-op retry produced empty SVG for region=%r",
                            critic_target,
                        )
                except Exception as exc:
                    logger.warning(
                        "add no-op retry failed (%s); keeping previous drawing",
                        exc,
                    )
                _retries.append({
                    "reason": f"ADD_NOOP:region={critic_target or ''}",
                    "seconds": time.perf_counter() - _t_noop0,
                })
            if _add_ids:
                validated["svg"] = _composite
                validated["steps"] = list(previous_steps or []) + _add_labels
                logger.info(
                    "additive composite: appended %d new path(s) %s for region=%r",
                    len(_add_ids), _add_ids, critic_target,
                )
            else:
                validated["svg"] = previous_svg
                validated["steps"] = list(previous_steps or [])
                logger.warning(
                    "additive composite: Artist added no new paths (region=%r); "
                    "keeping previous drawing unchanged this pass",
                    critic_target,
                )

        if not _has_any_path(validated["svg"]):
            raise GenerationError(
                "final SVG has zero paths",
                raw_response=validated["svg"],
            )

        # Preservation report and redraw flag — populated in Phase 6.7 for revisions only.
        _preservation_report: Dict[str, Any] = {"error": False, "reverted": 0, "kept": 0, "targeted": 0}
        _redraw_intent_this_iter: bool = False

        # ── Phase 6: preservation measurement + safety belt ──────────────────
        _t_pres0 = time.perf_counter()
        if revision and previous_svg is not None:
            metrics = _measure_preservation(previous_svg, validated["svg"])
            logger.info(
                "preservation: preserved=%d modified=%d removed=%d added=%d rate=%.2f",
                metrics["preserved"],
                metrics["modified"],
                metrics["removed"],
                metrics["added"],
                metrics["preservation_rate"],
            )

            if (
                not metrics["error"]
                and metrics["preservation_rate"] < PRESERVATION_RETRY_THRESHOLD
                # With an explicit lock action the code-enforced region lock
                # (Phase 6.7) restores preservation deterministically, so this
                # extra model round-trip would be wasted time — skip it.
                and _action_this_iter is None
            ):
                discarded_pct = int(round((1.0 - metrics["preservation_rate"]) * 100))
                _timing_log.warning(
                    "[retry:preservation] trigger=PRESERVATION_BELOW_THRESHOLD  rate=%.2f  threshold=%.2f",
                    metrics["preservation_rate"], PRESERVATION_RETRY_THRESHOLD,
                )
                logger.warning(
                    "preservation rate %.2f below threshold %.2f — retrying with reminder",
                    metrics["preservation_rate"],
                    PRESERVATION_RETRY_THRESHOLD,
                )
                preservation_warning = (
                    f"WARNING: Your previous output discarded {discarded_pct}% of the "
                    f"existing paths. This violates the preservation rule. Try again, "
                    f"and this time keep the existing paths byte-for-byte unless the "
                    f"critic specifically criticized them. Only add new paths or "
                    f"modify the specific elements the critic mentioned."
                )
                preservation_retry_msg = self._build_user_message(
                    user_prompt,
                    previous_svg,
                    critic_feedback,
                    stricter=False,
                    preservation_warning=preservation_warning,
                    previous_steps=previous_steps,
                    iteration=iteration,
                    max_iterations=max_iterations,
                    critic_action=critic_action, critic_target=critic_target,
                )
                _t_pr0 = time.perf_counter()
                try:
                    raw_pres = self._call_model(preservation_retry_msg, call_label="preservation_retry", temperature=call_temperature)
                    retry_validated = self._parse_and_sanitize(raw_pres)
                    ok_retry, _ = _validate_sketch_style(retry_validated["svg"])
                    if not ok_retry:
                        try:
                            retry_validated["svg"] = _auto_repair(
                                retry_validated["svg"]
                            )
                        except Exception:
                            pass
                    if not _has_any_path(retry_validated["svg"]):
                        logger.warning(
                            "preservation retry produced empty SVG; keeping original"
                        )
                    else:
                        retry_metrics = _measure_preservation(
                            previous_svg, retry_validated["svg"]
                        )
                        logger.info(
                            "preservation retry: preserved=%d rate=%.2f",
                            retry_metrics["preserved"],
                            retry_metrics["preservation_rate"],
                        )
                        if (
                            retry_metrics["preservation_rate"]
                            >= PRESERVATION_RETRY_THRESHOLD
                        ):
                            logger.info("preservation retry succeeded; using retry output")
                            validated = retry_validated
                        elif (
                            retry_metrics["preservation_rate"]
                            > metrics["preservation_rate"]
                        ):
                            logger.warning(
                                "preservation retry improved but still below threshold "
                                "(%.2f); accepting degraded output",
                                retry_metrics["preservation_rate"],
                            )
                            validated = retry_validated
                        else:
                            logger.warning(
                                "preservation retry did not improve "
                                "(%.2f); keeping original",
                                retry_metrics["preservation_rate"],
                            )
                except Exception as exc:
                    logger.warning(
                        "preservation retry failed (%s); accepting original output",
                        exc,
                    )
                _retries.append({
                    "reason": f"PRESERVATION_BELOW_THRESHOLD:rate={metrics['preservation_rate']:.2f}",
                    "seconds": time.perf_counter() - _t_pr0,
                })
        _phase_times["preservation"] = time.perf_counter() - _t_pres0

        # ── Phase 6.5: anti-regression merge ─────────────────────────────────
        # Even after the preservation retry the Artist may still have dropped
        # one or two step-N paths. Without intervention the next iteration will
        # render fewer elements than the previous (score regression). Restore
        # any dropped step-N paths byte-for-byte from previous_svg. This is the
        # hard guarantee that the drawing never loses elements between iters.
        # Skipped for redraw_all: there the previous drawing is untrusted and is
        # being replaced wholesale, so restoring its paths would be wrong.
        if revision and previous_svg is not None and _action_this_iter != "redraw_all":
            try:
                merged_svg, restored_ids = _merge_dropped_paths(
                    previous_svg, validated["svg"]
                )
                if restored_ids:
                    logger.info(
                        "anti-regression merge restored %d dropped path(s): %s",
                        len(restored_ids), restored_ids,
                    )
                    validated["svg"] = merged_svg
                    # Keep validated["steps"] in sync: extend with the restored
                    # labels from previous_steps when available, indexed by step-N.
                    if previous_steps:
                        new_steps = list(validated.get("steps") or [])
                        # Build a step-N → label map from existing new_steps
                        # (new_steps[0] is step-1's label). Then ensure each
                        # restored step-N has its previous label in the right slot.
                        for sid in restored_ids:
                            try:
                                idx = int(sid.split("-", 1)[1]) - 1
                            except (IndexError, ValueError):
                                continue
                            if 0 <= idx < len(previous_steps):
                                while len(new_steps) <= idx:
                                    new_steps.append("")
                                if not new_steps[idx]:
                                    new_steps[idx] = previous_steps[idx]
                        validated["steps"] = new_steps
            except Exception as exc:
                logger.warning("anti-regression merge failed (%s); continuing", exc)

        # ── Phase 6.7: deterministic path preservation enforcement ───────────
        # The CODE-ENFORCED REGION LOCK. After the anti-regression merge the
        # Artist has the full set of paths; now lock every path the Critic did
        # NOT name as the active region by reverting it to its previous `d`.
        # This is what makes the drawing improve monotonically: accepted
        # geometry cannot drift or degrade, so each iteration only adds/repairs
        # the one named region.
        #
        # The lock is driven by the Critic's explicit `action`/`target_region`
        # when present (no fragile keyword guessing):
        #   add            → targeted = {} → every existing path locked; only new
        #                     step ids (the additions) pass through.
        #   redraw_element → targeted = the path(s) matching target_region; the
        #                     rest stay locked.
        #   redraw_all     → enforcement skipped inside _enforce_path_preservation.
        # When no action is supplied (legacy/cached runs) we fall back to the old
        # keyword heuristic so those paths behave exactly as before.
        _redraw_intent_this_iter = (
            _action_this_iter in ("redraw_element", "redraw_all")
            if _action_this_iter
            else _is_redraw_intent(critic_feedback or "")
        )
        if revision and previous_svg is not None:
            try:
                if _action_this_iter == "add":
                    targeted: set = set()  # lock everything; additions are new ids
                    # Observability: did the Artist actually add any new path?
                    _prev_id_set = set(_extract_step_paths(previous_svg))
                    _new_id_set = set(_extract_step_paths(validated["svg"]))
                    _added = _new_id_set - _prev_id_set
                    if not _added:
                        logger.warning(
                            "iteration %d: action=add but the Artist emitted no new step ids "
                            "(region=%r) — this pass will be a no-op after the lock",
                            iteration, critic_target,
                        )
                    else:
                        logger.info(
                            "iteration %d: action=add, %d new path(s) %s added for region=%r",
                            iteration, len(_added), sorted(_added), critic_target,
                        )
                elif _action_this_iter == "redraw_element":
                    # Match the named region to existing step labels.
                    targeted = _identify_targeted_paths(
                        critic_target or "", previous_steps or [],
                    )
                    if not targeted:
                        # No label matched the target phrase; fall back to the
                        # feedback text so the requested repair isn't blocked.
                        targeted = _identify_targeted_paths(
                            critic_feedback or "", previous_steps or [],
                        )
                elif _action_this_iter == "redraw_all":
                    targeted = set()  # enforcement is skipped for redraw_all
                else:
                    targeted = _identify_targeted_paths(
                        critic_feedback or "", previous_steps or [],
                    )
                validated["svg"], _preservation_report = _enforce_path_preservation(
                    previous_svg, validated["svg"], targeted,
                    critic_feedback=critic_feedback or "",
                    action=_action_this_iter,
                )
            except Exception as exc:
                logger.warning("path preservation enforcement failed (%s); skipping", exc)

        # ── Phase 7: wobblify ────────────────────────────────────────────────
        _t_wob0 = time.perf_counter()
        preserve_ids: Optional[set] = None
        if revision and previous_svg is not None:
            try:
                preserve_ids = _preserved_step_ids(previous_svg, validated["svg"])
            except Exception:
                preserve_ids = None

        validated["svg"] = _wobblify_svg(
            validated["svg"],
            noise=WOBBLE_NOISE_PX,
            preserve_ids=preserve_ids,
        )
        _phase_times["wobblify"] = time.perf_counter() - _t_wob0

        # ── Structured timing report ─────────────────────────────────────────
        _total_generate = time.perf_counter() - _t_generate_start
        _total_api = sum(
            v for k, v in _phase_times.items() if k.startswith("api_")
        ) + sum(r["seconds"] for r in _retries)
        _retry_seconds = sum(r["seconds"] for r in _retries)

        _timing_log.info(
            "\n"
            "  ARTIST GENERATOR TIMING BREAKDOWN\n"
            "  ──────────────────────────────────\n"
            "  Total generate():         %6.3fs\n"
            "\n"
            "  Setup (model check +      %6.3fs\n"
            "    prompt construction):\n"
            "  Initial API call:         %6.3fs\n"
            "    (~tokens:              %6d  ~tok/s: %.1f)\n"
            "  Response processing:      %6.3fs\n"
            "    JSON parse + validate:\n"
            "  Style validation:         %6.3fs\n"
            "  Auto-repair:              %6.3fs\n"
            "  Preservation check:       %6.3fs\n"
            "  Wobblify:                 %6.3fs\n"
            "\n"
            "  Retries:  %d total  (%.3fs combined)\n"
            "%s",
            _total_generate,
            _phase_times.get("setup", 0.0),
            _phase_times.get("api_initial", 0.0),
            self._last_call_tokens, self._last_call_tokens / max(_phase_times.get("api_initial", 1e-9), 1e-9),
            _phase_times.get("response_processing", 0.0),
            _phase_times.get("style_validation", 0.0),
            _phase_times.get("auto_repair", 0.0),
            _phase_times.get("preservation", 0.0),
            _phase_times.get("wobblify", 0.0),
            len(_retries), _retry_seconds,
            "\n".join(
                f"    retry {j+1}: trigger={r['reason']}  time={r['seconds']:.3f}s"
                for j, r in enumerate(_retries)
            ) if _retries else "    (no retries)",
        )

        return {
            "svg": validated["svg"],
            "reasoning": validated["reasoning"],
            "steps": validated["steps"],
            "style_notes": validated.get("style_notes", ""),
            "preservation_report": _preservation_report,
            "redraw_intent": _redraw_intent_this_iter,
            "_timing": {
                "total_seconds": _total_generate,
                "phases": _phase_times,
                "retries": _retries,
            },
        }


if __name__ == "__main__":
    import os
    import sys
    import tempfile
    import time

    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()

    from core.config import (
        ARTIST_MODEL,
        OLLAMA_BASE_URL,
        REQUEST_TIMEOUT_SECONDS,
    )

    print(f"Connecting to Ollama at {OLLAMA_BASE_URL}")
    print(f"Artist model: {ARTIST_MODEL}")
    from core.config import OLLAMA_API_KEY
    from core.ollama_client import OllamaClient
    client = OllamaClient(
        base_url=OLLAMA_BASE_URL,
        api_key=OLLAMA_API_KEY,
        default_model=ARTIST_MODEL,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    gen = SVGGenerator(client=client, model=ARTIST_MODEL)
    out_dir = tempfile.gettempdir()

    # ── Step 1: initial generation ────────────────────────────────────────
    initial_prompt = "a sun"
    print(f"\n[1/2] Initial generation for: {initial_prompt!r}")
    print("(local inference may take 30-90s; the model warms up on first call)\n")

    t0 = time.monotonic()
    try:
        initial = gen.generate(initial_prompt)
    except ModelBackendError as exc:
        print(f"\nFAIL — backend error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    except GenerationError as exc:
        print(f"\nFAIL — GenerationError: {exc}", file=sys.stderr)
        if exc.raw_response:
            print("\n--- raw response (first 1500 chars) ---", file=sys.stderr)
            print(exc.raw_response[:1500], file=sys.stderr)
        sys.exit(2)
    initial_elapsed = time.monotonic() - t0

    print(f"  reasoning: {initial['reasoning']}")
    print(f"  steps:     {len(initial['steps'])} path(s)")
    print(f"  elapsed:   {initial_elapsed:.1f}s")

    initial_svg_path = os.path.join(out_dir, "sun_initial.svg")
    initial_png_path = os.path.join(out_dir, "sun_initial.png")

    with open(initial_svg_path, "w", encoding="utf-8") as f:
        f.write(initial["svg"])
    print(f"  wrote SVG: {initial_svg_path}")

    try:
        from core.renderer import render_svg_to_png

        png_bytes = render_svg_to_png(initial["svg"], size=CANVAS_SIZE)
        with open(initial_png_path, "wb") as f:
            f.write(png_bytes)
        print(f"  wrote PNG: {initial_png_path}")
    except Exception as exc:
        print(f"  PNG render skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    # ── Step 2: revision with synthetic critic feedback ───────────────────
    feedback = (
        "The sun outline and rays are well-drawn, but the user asked for a smiling "
        "sun and the face is missing. Please add a curved smile across the lower "
        "half of the circle and two small dots for eyes positioned in the upper "
        "portion. Keep all existing strokes unchanged."
    )
    print(f"\n[2/2] Revision with synthetic critic feedback")
    print(f"  feedback: {feedback}")
    print()

    t0 = time.monotonic()
    try:
        revised = gen.generate(
            initial_prompt,
            previous_svg=initial["svg"],
            critic_feedback=feedback,
        )
    except ModelBackendError as exc:
        print(f"\nFAIL — backend error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
    except GenerationError as exc:
        print(f"\nFAIL — GenerationError: {exc}", file=sys.stderr)
        if exc.raw_response:
            print("\n--- raw response (first 1500 chars) ---", file=sys.stderr)
            print(exc.raw_response[:1500], file=sys.stderr)
        sys.exit(2)
    revised_elapsed = time.monotonic() - t0

    print(f"  reasoning: {revised['reasoning']}")
    print(f"  steps:     {len(revised['steps'])} path(s)")
    print(f"  elapsed:   {revised_elapsed:.1f}s")

    revised_svg_path = os.path.join(out_dir, "sun_revised.svg")
    revised_png_path = os.path.join(out_dir, "sun_revised.png")

    with open(revised_svg_path, "w", encoding="utf-8") as f:
        f.write(revised["svg"])
    print(f"  wrote SVG: {revised_svg_path}")

    try:
        from core.renderer import render_svg_to_png

        png_bytes = render_svg_to_png(revised["svg"], size=CANVAS_SIZE)
        with open(revised_png_path, "wb") as f:
            f.write(png_bytes)
        print(f"  wrote PNG: {revised_png_path}")
    except Exception as exc:
        print(f"  PNG render skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    # ── Preservation metrics (the thesis-relevant measurement) ────────────
    metrics = _measure_preservation(initial["svg"], revised["svg"])
    total_prev = metrics["preserved"] + metrics["modified"] + metrics["removed"]
    rate = metrics["preservation_rate"]

    print()
    print("=" * 60)
    print("PRESERVATION METRICS")
    print("=" * 60)
    print(f"  previous paths total: {total_prev}")
    print(f"  preserved (byte-for-byte): {metrics['preserved']}  ({rate * 100:.1f}%)")
    print(f"  modified (id same, d differs): {metrics['modified']}")
    print(f"  removed (id missing in revision): {metrics['removed']}")
    print(f"  added (new step-N ids): {metrics['added']}")
    print(f"  preservation_rate: {rate:.3f}")
    print()

    if metrics["error"]:
        print("  NOTE: preservation measurement hit a parse error (results unreliable)")
        rating = "(unmeasurable)"
    elif rate >= 0.8:
        rating = "✓ excellent (>=0.8)"
    elif rate >= 0.6:
        rating = "✓ healthy (>=0.6)"
    elif rate >= 0.4:
        rating = "⚠ degraded (>=0.4) — safety belt did not fire (or fired and didn't help)"
    else:
        rating = "✗ wholesale regeneration (<0.4) — safety belt should have fired"
    print(f"  rating: {rating}")
    print()
    print(f"Open {initial_png_path}")
    print(f"and  {revised_png_path}")
    print(
        "side by side. The revised version should preserve all rays and the circle "
        "outline, with new strokes added for the smile and eyes."
    )
    print()
    print(f"Total elapsed: {initial_elapsed + revised_elapsed:.1f}s "
          f"(initial {initial_elapsed:.1f}s + revision {revised_elapsed:.1f}s)")

    # Exit code reflects validation gate from Prompt 16d.
    if metrics["error"]:
        sys.exit(3)
    if rate < 0.4:
        sys.exit(4)  # wholesale regeneration — safety belt failed
    sys.exit(0)
