"""SVG → PNG rendering for the Critic and progressive frame generation for the UI.

Three rendering paths:
    - `render_svg_to_png`: single rasterization. When called with
      `critic_render=True`, applies legibility boosts for the vision model:
      1024×1024 output, white background, stroke widths ×1.4, displacement
      filter scale halved from 1.2 → 0.6 (still wobbly, just less fuzzy).
    - `render_svg_for_critic`: convenience entry point that calls
      render_svg_to_png with critic_render=True.
    - `render_svg_progressive`: produces N PNGs for the UI animation. UNCHANGED
      from the non-critic path — progressive frames stay in the original
      aesthetic (transparent, 512×512, original strokes).

Progressive frames are built by deep-copying the full tree and *removing*
elements with step-index > K, agnostic to nesting depth.

Both single-image paths share viewBox / width / height normalization so
cairosvg never has to guess the output size.
"""

from __future__ import annotations

import copy
import io
import logging
import os as _os
import re
import time
from typing import List, Optional

import cairosvg
from lxml import etree
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

from backend.core.config import CANVAS_SIZE


logger = logging.getLogger(__name__)


SVG_NS = "http://www.w3.org/2000/svg"
SVG_NSMAP = {"svg": SVG_NS}

# Stroke-width multiplier applied in Critic-optimised renders.
_CRITIC_STROKE_BOOST: float = 1.4
# Displacement scale used in Critic-optimised renders (original is 1.2). Set to
# 0 so the Critic sees CLEAN strokes: the hand-drawn wobble is cosmetic and only
# hurts the model's judgment of proportions and shape (e.g. tail vs loop). The UI
# render still uses the full wobble for the audience-facing aesthetic.
_CRITIC_DISPLACEMENT_SCALE: str = "0.0"
# Output resolution for Critic-optimised renders.
_CRITIC_RENDER_SIZE: int = int(_os.environ.get("CRITIC_RENDER_SIZE", "768"))


class RenderError(RuntimeError):
    """Raised when SVG rasterization fails. Includes a preview of the offending SVG."""

    def __init__(self, message: str, svg_preview: str = "") -> None:
        super().__init__(message)
        self.svg_preview = svg_preview

    def __str__(self) -> str:
        base = super().__str__()
        if self.svg_preview:
            return f"{base}\n--- svg preview (first 200 chars) ---\n{self.svg_preview}"
        return base


def _parse_svg(svg_string: str) -> etree._Element:
    parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(svg_string.encode("utf-8"), parser=parser)
    except etree.XMLSyntaxError as exc:
        raise RenderError(f"SVG failed to parse as XML: {exc}", svg_preview=svg_string[:200]) from exc

    if etree.QName(root.tag).localname != "svg":
        raise RenderError(
            f"root element is <{etree.QName(root.tag).localname}>, expected <svg>",
            svg_preview=svg_string[:200],
        )
    return root


def _ensure_size_attrs(root: etree._Element, size: int) -> None:
    """Inject width/height/viewBox/xmlns on the root if missing."""
    if not root.get("viewBox"):
        root.set("viewBox", f"0 0 {size} {size}")
    if not root.get("width"):
        root.set("width", str(size))
    if not root.get("height"):
        root.set("height", str(size))
    if not root.get("xmlns") and not root.nsmap.get(None):
        root.set("xmlns", SVG_NS)


def _serialize(root: etree._Element) -> str:
    return etree.tostring(root, encoding="unicode")


def _rasterize(
    svg_text: str,
    size: int,
    preview_source: str,
    white_background: bool = False,
) -> bytes:
    """Rasterize SVG text to PNG bytes via cairosvg.

    `white_background=True` requests a white canvas. Tried first via cairosvg's
    `background_color` keyword argument (supported in cairosvg ≥ 2.7); falls
    back silently if the installed version doesn't accept that argument.
    """
    t0 = time.monotonic()
    kwargs = {
        "bytestring": svg_text.encode("utf-8"),
        "output_width": size,
        "output_height": size,
    }
    if white_background:
        kwargs["background_color"] = "white"

    try:
        out = cairosvg.svg2png(**kwargs)
    except TypeError:
        # Installed cairosvg version doesn't support background_color — remove it
        # and fall back to a transparent background.
        kwargs.pop("background_color", None)
        try:
            out = cairosvg.svg2png(**kwargs)
        except Exception as exc:
            logger.error("cairosvg rasterize failed: %s", exc)
            raise RenderError(f"cairosvg failed: {exc}", svg_preview=preview_source[:200]) from exc
    except Exception as exc:
        logger.error("cairosvg rasterize failed: %s", exc)
        raise RenderError(f"cairosvg failed: {exc}", svg_preview=preview_source[:200]) from exc

    logger.debug(
        "rasterized %d-byte SVG → %d-byte PNG at %dpx in %.3fs",
        len(svg_text), len(out), size, time.monotonic() - t0,
    )
    return out


def _step_index(element: etree._Element) -> Optional[int]:
    """Return N if the element has id='step-N', else None."""
    cid = element.get("id")
    if not cid:
        return None
    m = re.match(r"^step-(\d+)$", cid)
    if not m:
        return None
    return int(m.group(1))


def _strip_filter_refs(root: etree._Element) -> int:
    """Remove `filter="…"` attributes from every element in the tree."""
    removed = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if "filter" in el.attrib:
            del el.attrib["filter"]
            removed += 1
    return removed


# Set DEBUG_RENDER=1 in the environment to dump every render output to a
# diagnostic directory for post-mortem inspection. Off by default.
_DEBUG_RENDER = _os.environ.get("DEBUG_RENDER", "").strip() == "1"
_DEBUG_RENDER_DIR = _os.path.join(
    _os.environ.get("TMPDIR") or _os.environ.get("TEMP") or "/tmp",
    "sketch_debug",
)


def _dump_debug(name: str, data: bytes) -> None:
    if not _DEBUG_RENDER:
        return
    try:
        _os.makedirs(_DEBUG_RENDER_DIR, exist_ok=True)
        full = _os.path.join(_DEBUG_RENDER_DIR, name)
        with open(full, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("utf-8"))
    except Exception as exc:  # pragma: no cover
        logger.warning("DEBUG_RENDER dump failed for %s: %s", name, exc)


_STROKE_WIDTH_RE = re.compile(r'(stroke-width)\s*=\s*"([0-9]*\.?[0-9]+)"')
_DISPLACEMENT_SCALE_RE = re.compile(
    r'(<feDisplacementMap\b[^>]*?\bscale\s*=\s*")[0-9]*\.?[0-9]+(")'
)


def _boost_for_critic(svg_string: str) -> str:
    """Modify SVG via pure string substitution for Critic-optimised rendering.

    Two changes, both targeted at making thin ink strokes legible to a vision
    model that struggles with low-contrast input:

    1. Multiply every quoted `stroke-width` value by `_CRITIC_STROKE_BOOST` (1.4).
    2. Set `<feDisplacementMap scale>` to `_CRITIC_DISPLACEMENT_SCALE` (0.6).

    Implementation note: this used to do an lxml parse → mutate → serialize
    round-trip, which is theoretically robust but adds a third XML
    parse/serialize step downstream of `_sanitize_svg` and `_wobblify_svg`.
    Each round-trip is a chance for some structural detail to be lost on
    pathological model output. Pure regex substitution preserves the SVG
    string byte-for-byte except at the matched attribute values, which is
    safer for an Artist whose output may include nested groups, comments, or
    other non-canonical structure.
    """
    boost = _CRITIC_STROKE_BOOST

    def _replace_stroke_width(m: "re.Match[str]") -> str:
        attr = m.group(1)
        try:
            val = float(m.group(2))
        except (TypeError, ValueError):
            return m.group(0)
        return f'{attr}="{val * boost:.2f}"'

    boosted_svg = _STROKE_WIDTH_RE.sub(_replace_stroke_width, svg_string)
    boosted_svg = _DISPLACEMENT_SCALE_RE.sub(
        lambda m: f"{m.group(1)}{_CRITIC_DISPLACEMENT_SCALE}{m.group(2)}",
        boosted_svg,
    )

    logger.debug("_boost_for_critic: applied stroke ×%.1f, displacement scale → %s",
                 boost, _CRITIC_DISPLACEMENT_SCALE)
    return boosted_svg


def render_svg_to_png(
    svg_string: str,
    size: int = CANVAS_SIZE,
    white_background: bool = False,
    disable_filter: bool = False,
    critic_render: bool = False,
) -> bytes:
    """Rasterize a full SVG string to PNG bytes.

    Arguments:
        size:             Output width/height in pixels (square). Overridden to
                          `_CRITIC_RENDER_SIZE` when critic_render=True.
        white_background: Fill the canvas with white before rendering. Overridden
                          to True when critic_render=True.
        disable_filter:   Strip all `filter="…"` attributes before rasterizing.
                          Independent of critic_render.
        critic_render:    When True, enables the Critic-optimised pipeline:
                          - Forces size = 1024 and white_background = True.
                          - Multiplies every path stroke-width by 1.4.
                          - Reduces feDisplacementMap scale from 1.2 → 0.6.
                          The UI animation path leaves this False.
    """
    if not svg_string or not svg_string.strip():
        raise RenderError("empty SVG string", svg_preview="")

    if critic_render:
        white_background = True
        size = _CRITIC_RENDER_SIZE
        svg_string = _boost_for_critic(svg_string)
        logger.info(
            "render_svg_to_png: critic_render mode — size=%d, stroke boost ×%.1f, "
            "displacement scale → %s",
            size, _CRITIC_STROKE_BOOST, _CRITIC_DISPLACEMENT_SCALE,
        )

    root = _parse_svg(svg_string)
    _ensure_size_attrs(root, size)

    if disable_filter:
        n = _strip_filter_refs(root)
        if n:
            logger.info("render_svg_to_png: stripped %d filter attribute(s) pre-rasterize", n)

    normalized = _serialize(root)
    out = _rasterize(normalized, size, preview_source=normalized, white_background=white_background)

    if _DEBUG_RENDER:
        import time as _time
        suffix = "critic" if critic_render else "ui"
        ts = _time.strftime("%Y%m%d_%H%M%S")
        _dump_debug(f"render_{suffix}_{ts}.svg", normalized.encode("utf-8"))
        _dump_debug(f"render_{suffix}_{ts}.png", out)

    return out


def render_svg_for_critic(svg_string: str) -> bytes:
    """Render SVG optimised for vision-model perception.

    Produces a 1024×1024 PNG with a white background, strokes boosted ×1.4,
    and the displacement filter scale halved (clearer strokes, less fuzz).

    This is the clean entry point the orchestrator should call when producing
    the image to send to VisualCritic.critique(). The UI animation continues
    to use render_svg_to_png() with default arguments.
    """
    return render_svg_to_png(svg_string, critic_render=True)


def _to_rgb_on_white(img: Image.Image) -> Image.Image:
    """Return an RGB image composited on white."""
    if img.mode == "RGB":
        return img.copy()
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").getchannel("A"))
        return bg
    return img.convert("RGB")


def _fit_panel(img: Image.Image, size: int) -> Image.Image:
    """Fit an image into a square white panel without cropping."""
    panel = Image.new("RGB", (size, size), "white")
    fitted = ImageOps.contain(_to_rgb_on_white(img), (size, size), method=Image.Resampling.LANCZOS)
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    panel.paste(fitted, (x, y))
    return panel


def _new_strokes_panel(previous: Image.Image, current: Image.Image, size: int) -> Image.Image:
    """Visual diff panel: black pixels show where the current render changed."""
    prev = _fit_panel(previous, size).convert("L")
    cur = _fit_panel(current, size).convert("L")
    diff = ImageChops.difference(prev, cur)
    mask = diff.point(lambda p: 255 if p > 18 else 0)
    # Make tiny differences easier for the vision model to see.
    mask = mask.filter(ImageFilter.MaxFilter(3))
    panel = Image.new("RGB", (size, size), "white")
    ink = Image.new("RGB", (size, size), "#111111")
    panel.paste(ink, mask=mask)
    return panel


def _draw_labeled_panel(
    canvas: Image.Image,
    panel: Image.Image,
    label: str,
    x: int,
    y: int,
    label_h: int,
) -> None:
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((x, y + 8), label, fill="#222222", font=font)
    px = x
    py = y + label_h
    canvas.paste(panel, (px, py))
    draw.rectangle(
        [px, py, px + panel.width - 1, py + panel.height - 1],
        outline="#bdbdbd",
        width=2,
    )


def render_critic_comparison(previous_svg: str, current_svg: str) -> bytes:
    """Render a side-by-side critic sheet for revision critiques.

    The critic still receives a single PNG, which keeps all client backends
    compatible. The sheet gives it visual memory:
      left   = previous drawing before the last requested feature,
      middle = current drawing after the Artist's attempt,
      right  = computed visual difference / new strokes.
    """
    if not previous_svg or not previous_svg.strip():
        return render_svg_for_critic(current_svg)

    previous_png = render_svg_for_critic(previous_svg)
    current_png = render_svg_for_critic(current_svg)
    previous_img = png_bytes_to_pil(previous_png)
    current_img = png_bytes_to_pil(current_png)

    panel_size = int(_os.environ.get("CRITIC_COMPARISON_PANEL_SIZE", "512"))
    label_h = 34
    margin = 24
    gap = 18
    labels = ("previous", "current", "new strokes")
    panels = [
        _fit_panel(previous_img, panel_size),
        _fit_panel(current_img, panel_size),
        _new_strokes_panel(previous_img, current_img, panel_size),
    ]
    width = margin * 2 + panel_size * 3 + gap * 2
    height = margin * 2 + label_h + panel_size
    sheet = Image.new("RGB", (width, height), "white")
    x = margin
    for label, panel in zip(labels, panels):
        _draw_labeled_panel(sheet, panel, label, x, margin, label_h)
        x += panel_size + gap

    out = io.BytesIO()
    sheet.save(out, format="PNG", optimize=True)
    data = out.getvalue()
    logger.info(
        "render_critic_comparison: previous/current/new-strokes sheet %dx%d, %d bytes",
        width,
        height,
        len(data),
    )
    return data


def png_bytes_to_pil(png_bytes: bytes) -> Image.Image:
    """Decode PNG bytes into a PIL Image."""
    if not png_bytes:
        raise RenderError("empty PNG bytes", svg_preview="")
    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        return img
    except Exception as exc:
        raise RenderError(f"PIL could not decode PNG: {exc}", svg_preview="") from exc


def count_steps(svg_string: str) -> int:
    """Count elements whose id matches `step-\\d+` ANYWHERE in the tree."""
    root = _parse_svg(svg_string)
    count = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if _step_index(el) is not None:
            count += 1
    return count


def render_svg_progressive(
    svg_string: str,
    size: int = CANVAS_SIZE,
    disable_filter: bool = False,
) -> List[bytes]:
    """Produce one PNG per step, cumulatively revealing the drawing.

    UNCHANGED from the non-critic path — progressive frames stay in the
    original UI aesthetic (transparent background, original stroke widths,
    original displacement scale, 512×512).

    Frame K contains `step-1` … `step-K`. Every structural element (`<defs>`,
    `<filter>`, the `<g filter=…>` wrapper) is preserved on every frame.
    """
    root = _parse_svg(svg_string)
    _ensure_size_attrs(root, size)

    stepped_numbers: List[int] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        n = _step_index(el)
        if n is not None:
            stepped_numbers.append(n)

    stepped_numbers.sort()

    if not stepped_numbers:
        logger.warning("render_svg_progressive: no step-N elements; returning single full frame")
        if disable_filter:
            _strip_filter_refs(root)
        full = _serialize(root)
        return [_rasterize(full, size, preview_source=full)]

    logger.info("render_svg_progressive: %d frames", len(stepped_numbers))

    frames: List[bytes] = []
    for k in range(1, len(stepped_numbers) + 1):
        kept = set(stepped_numbers[:k])
        frame_root = copy.deepcopy(root)

        for el in list(frame_root.iter()):
            if not isinstance(el.tag, str):
                continue
            n = _step_index(el)
            if n is None:
                continue
            if n not in kept:
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)

        if disable_filter:
            _strip_filter_refs(frame_root)

        frame_svg = _serialize(frame_root)
        frames.append(_rasterize(frame_svg, size, preview_source=frame_svg))

    if _DEBUG_RENDER:
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M%S")
        for i, fb in enumerate(frames):
            _dump_debug(f"progressive_{ts}_frame{i + 1:02d}_of_{len(frames):02d}.png", fb)

    return frames


if __name__ == "__main__":
    import os
    import sys
    import tempfile

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    test_svg = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <filter id="roughen" x="-5%" y="-5%" width="110%" height="110%">
      <feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/>
      <feDisplacementMap in="SourceGraphic" scale="1.2"/>
    </filter>
  </defs>
  <g filter="url(#roughen)">
    <path id="step-1"
          d="M 263 158 C 318 152 364 211 358 264 C 366 314 308 367 248 358 C 192 365 149 308 158 256 C 142 207 207 145 268 162"
          fill="none" stroke="#1a1a1a" stroke-width="2.8"
          stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/>
    <path id="step-2"
          d="M 222 234 Q 229 222 241 232"
          fill="none" stroke="#1a1a1a" stroke-width="2.3"
          stroke-linecap="round" stroke-linejoin="round" opacity="0.98"/>
    <path id="step-3"
          d="M 218 286 Q 234 318 256 327 Q 281 322 297 287"
          fill="none" stroke="#1a1a1a" stroke-width="3.1"
          stroke-linecap="round" stroke-linejoin="round" opacity="0.97"/>
  </g>
</svg>"""

    out_dir = tempfile.gettempdir()
    print(f"Output directory: {out_dir}")

    try:
        # ── UI render (original aesthetic, transparent, 512×512) ──────────────
        ui_png = render_svg_to_png(test_svg, size=512)
        ui_path = os.path.join(out_dir, "test_render_ui.png")
        with open(ui_path, "wb") as f:
            f.write(ui_png)
        print(f"wrote UI render:     {ui_path} ({len(ui_png):,} bytes)  512×512, transparent bg")

        # ── Critic render (boosted strokes, white bg, 2048×2048) ─────────────
        critic_png = render_svg_for_critic(test_svg)
        critic_path = os.path.join(out_dir, "test_render_critic.png")
        with open(critic_path, "wb") as f:
            f.write(critic_png)
        print(f"wrote Critic render: {critic_path} ({len(critic_png):,} bytes)  2048×2048, white bg")

        print()
        print("Open both PNGs side-by-side. The critic version should be clearly more legible:")
        print("  - black strokes on white background (not transparent grey)")
        print("  - larger canvas (2048 vs 512) so strokes are thicker in absolute pixels")
        print("  - stroke widths boosted ×1.4 from the original")
        print("  - displacement jitter visibly reduced (scale 0.6 vs 1.2)")

        # Verify dimensions via PIL.
        ui_img = png_bytes_to_pil(ui_png)
        critic_img = png_bytes_to_pil(critic_png)
        print()
        print(f"UI render size:     {ui_img.size}")
        print(f"Critic render size: {critic_img.size}")
        assert ui_img.size == (512, 512), f"expected (512,512) got {ui_img.size}"
        assert critic_img.size == (2048, 2048), f"expected (2048,2048) got {critic_img.size}"

        # Progressive frames are unchanged (original aesthetic).
        frames = render_svg_progressive(test_svg, size=512)
        assert len(frames) == 3, f"expected 3 frames, got {len(frames)}"
        for i, frame in enumerate(frames, 1):
            p = os.path.join(out_dir, f"test_render_frame_{i}.png")
            with open(p, "wb") as f:
                f.write(frame)
        print(f"\nProgressive frames: {len(frames)} written (original aesthetic, step-by-step)")

        print("\nOK — all assertions passed.")

    except (RenderError, AssertionError) as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
