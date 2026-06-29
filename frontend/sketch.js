"use strict";

// ── Config ────────────────────────────────────────────────────────────────
function normalizeApiBase(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function configuredApiBase() {
  const params = new URLSearchParams(window.location ? window.location.search : "");
  return normalizeApiBase(
    params.get("api") ||
    (window.SKETCH_API_BASE || "")
  );
}

const CONFIGURED_API_BASE = configuredApiBase();
const IS_LOCAL_PAGE =
  !window.location ||
  window.location.protocol === "file:" ||
  ["localhost", "127.0.0.1", ""].includes(window.location.hostname);
const SAME_ORIGIN_API_BASE =
  window.location && window.location.protocol.startsWith("http")
    ? window.location.origin
    : null;
const API_BASE_CANDIDATES = [
  CONFIGURED_API_BASE,
  SAME_ORIGIN_API_BASE,
  ...(IS_LOCAL_PAGE ? ["http://127.0.0.1:8001", "http://localhost:8001"] : []),
].filter(Boolean).filter((base, index, all) => all.indexOf(base) === index);
let API_BASE = API_BASE_CANDIDATES[0];

function prewarmApi(base) {
  if (!base) return;
  fetch(`${base}/health`, {
    mode: "cors",
    cache: "no-store",
    keepalive: true,
  }).catch(() => {});
}

prewarmApi(CONFIGURED_API_BASE || SAME_ORIGIN_API_BASE);

const SVG_NATIVE = 512;
const PENCIL     = "#666666";  // neutral pencil tone for all annotation marks
const ART_STROKE = "#000000";
const ART_STROKE_SCREEN_WIDTH = 2.5;

// ── DOM refs ──────────────────────────────────────────────────────────────
const canvas          = document.getElementById("sketch");
const ctx             = canvas.getContext("2d");
const promptUI        = document.getElementById("prompt-ui");
const promptInput     = document.getElementById("prompt-input");
const stepLabel       = document.getElementById("step-label");
const doneLabel       = document.getElementById("done-label");
const errorToast      = document.getElementById("error-toast");
const annotationLayer = document.getElementById("annotation-layer");
const demoChips       = document.getElementById("demo-chips");

// Legacy controls (bottom iteration dots, continue button, demo toggle) were
// removed in favour of the run inspector panel. `_stub` keeps the old call sites
// harmless without ripping them all out.
const _stub = () => document.createElement("div");
const iterDots      = document.getElementById("iter-dots")   || _stub();
const iterNav       = document.getElementById("iter-nav")    || _stub();
const demoToggleBtn = document.getElementById("demo-toggle") || _stub();
const iterPrev      = document.getElementById("iter-prev")   || _stub();
const iterNext      = document.getElementById("iter-next")   || _stub();
const nextIterBtn   = document.getElementById("next-iter-btn") || _stub();

function cancelNextClick() {}

// ── Demo mode state ───────────────────────────────────────────────────────
let _demoMode = false;

// Build chips once from DEMO_PROMPTS (defined in demo.js, loaded before sketch.js)
const _demoLabel = document.createElement("span");
_demoLabel.className = "demo-label";
_demoLabel.textContent = "choose a subject";
demoChips.appendChild(_demoLabel);

DEMO_PROMPTS.forEach((entry) => {
  const chip = document.createElement("span");
  chip.className = "demo-chip";
  chip.textContent = entry.prompt;
  chip.addEventListener("click", () => {
    if (!_demoMode) return;
    // Dim all other chips, hold the selected one dark, then fade the whole card out.
    Array.from(demoChips.querySelectorAll(".demo-chip")).forEach(c => {
      c.style.transition = "color 220ms ease, opacity 220ms ease";
      if (c === chip) { c.style.color = "#111"; }
      else            { c.style.opacity = "0.18"; }
    });
    setTimeout(() => {
      demoChips.classList.remove("visible");
      setTimeout(() => {
        demoChips.classList.add("hidden");
        // Reset inline styles for next time
        Array.from(demoChips.querySelectorAll(".demo-chip")).forEach(c => {
          c.style.color = ""; c.style.opacity = ""; c.style.transition = "";
        });
        startDemoGeneration(entry.prompt);
      }, 400);
    }, 320);
  });
  demoChips.appendChild(chip);
});

function _showDemoChips() {
  demoChips.classList.remove("hidden");
  requestAnimationFrame(() => requestAnimationFrame(() => demoChips.classList.add("visible")));
}

function _hideDemoChips() {
  demoChips.classList.remove("visible");
  setTimeout(() => demoChips.classList.add("hidden"), 420);
}

const inputWrapper = document.getElementById("input-wrapper");

// ── State ─────────────────────────────────────────────────────────────────
const state = {
  phase:             "idle",   // idle|drawing|critiquing|preparing|complete
  currentIteration:  0,
  totalIterations:   4,
  svgBounds:         null,
  currentSVG:        null,
  previousSVG:       null,
  finalSVG:          null,
  ghostSnapshot:     null,
  activeAnimator:    null,
  currentGaze:       null,
  currentAnnotation: null,
  stepBoxes:         [],
  iterHistory:       [],      // SVGs saved per completed iteration
  iterRecords:       [],      // full per-iteration detail (score, verdict, timings, feedback)
  viewingIter:       0,       // which iteration is currently displayed
  runPrompt:         "",      // the prompt for the active run
  runStartMs:        0,       // performance.now() when the run began (for total elapsed)
};

// ── Per-iteration record helpers ──────────────────────────────────────────
function _iterRec(idx) {
  if (!state.iterRecords[idx]) {
    state.iterRecords[idx] = {
      index: idx, svg: null, score: null, verdict: null,
      feedback: "", uiMessage: "", reasoning: "", observations: [],
      artistSeconds: null, renderSeconds: null, criticSeconds: null,
      swapSeconds: null, totalSeconds: null, phase: "drawing",
      timerStartMs: null, timerEndMs: null,
    };
  }
  return state.iterRecords[idx];
}

function transition(p) { console.log(`[state] ${state.phase}→${p}`); state.phase = p; }

function _updateIterArrows() {
  const n = state.iterHistory.length;
  const show = n > 1 && state.phase === "complete";
  const canPrev = show && state.viewingIter > 0;
  const canNext = show && state.viewingIter < n - 1;
  iterPrev.classList.toggle("visible", canPrev);
  iterNext.classList.toggle("visible", canNext);
}

function _hideIterArrows() {
  iterPrev.classList.remove("visible");
  iterNext.classList.remove("visible");
}

async function _jumpToIter(idx) {
  const svg = state.iterHistory[idx]; if (!svg) return;
  state.viewingIter = idx;
  // highlight the dot
  Array.from(document.querySelectorAll(".iter-dot")).forEach((d,i)=>{
    d.classList.remove("active","waiting");
    d.classList.toggle("done", i !== idx);
    if (i === idx) { d.classList.remove("done"); d.classList.add("active"); }
  });
  // Ensure drawing is centred (no critique shift) when browsing history.
  document.documentElement.style.setProperty("--canvas-shift", "0px");
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const bounds = computeSVGBounds();
  state.svgBounds = bounds; window.svgBounds = bounds;
  const anim = new StrokeAnimator(svg, [], bounds, {});
  state.activeAnimator = anim;
  await anim.play();
  state.activeAnimator = null;
  // restore done styling
  Array.from(document.querySelectorAll(".iter-dot")).forEach((d,i)=>{
    d.classList.remove("active","waiting");
    d.classList.add("done");
  });
  _updateIterArrows();
}

iterPrev.addEventListener("click", () => { if (state.viewingIter > 0) _jumpToIter(state.viewingIter - 1); });
iterNext.addEventListener("click", () => { if (state.viewingIter < state.iterHistory.length - 1) _jumpToIter(state.viewingIter + 1); });

// ── Run inspector panel ─────────────────────────────────────────────────────
const RP = {
  panel:    document.getElementById("run-panel"),
  statusDot:document.getElementById("rp-status-dot"),
  statusTxt:document.getElementById("rp-status-text"),
  figLabel: document.getElementById("rp-figure-label"),
  prompt:   document.getElementById("rp-prompt"),
  elapsed:  document.getElementById("rp-elapsed"),
  track:    document.getElementById("rp-iter-track"),
  continueSep: document.getElementById("rp-continue-sep"),
  continueBtn: document.getElementById("rp-continue"),
  returnBtn: document.getElementById("rp-return"),
  iterId:   document.getElementById("rp-iter-id"),
  phase:    document.getElementById("rp-phase"),
  scoreKey: document.getElementById("rp-score-key"),
  score:    document.getElementById("rp-score"),
  verdictKey:document.getElementById("rp-verdict-key"),
  verdict:  document.getElementById("rp-verdict"),
  feedback: document.getElementById("rp-feedback"),
  tArtist:  document.getElementById("rp-t-artist"),
  tCritic:  document.getElementById("rp-t-critic"),
  tTotal:   document.getElementById("rp-t-total"),
  barArtist:document.getElementById("rp-bar-artist"),
  barCritic:document.getElementById("rp-bar-critic"),
};

let _panelTicker = null;   // interval id for the live elapsed clock
let _panelBrowse = false;  // true once the run is complete
let _panelSel    = 0;      // which iteration the panel/canvas is showing
let _maxReady    = -1;     // highest iteration index the loop has finished computing
let _shownAny    = false;  // has the canvas auto-shown the first iteration yet
let _panelViewBusy = false;
let _panelPendingTarget = null; // iteration selected before its SVG has arrived
let _panelChangeTimer = null;
let _feedbackRevealTimer = null;
let _metaRevealTimer = null;
let _feedbackStreamToken = 0;
let _lastFeedbackSig = "";
let _feedbackControlsSig = "";
let _panelViewed = new Set();
let _panelDrawingReveal = null;
let _panelInspectionTimer = null;
let _panelInspectionToken = 0;
let _panelRevealWatchdog = null;
let _panelDisplayTimer = null;
let _panelDrawWaitStart = 0;  // when the user began waiting for the current draw

if (RP.continueBtn) {
  RP.continueBtn.addEventListener("click", () => {
    const next = _panelSel + 1;
    if (_panelViewBusy) return;
    if (!_panelFeedbackReady(_panelSel)) return;
    if (next >= _panelTotalIterations()) return;
    _requestIter(next);
  });
}

if (RP.returnBtn) {
  RP.returnBtn.addEventListener("click", () => {
    panelReturnToPaper();
  });
}

const _fmtS = v => (v == null ? "—" : `${Math.max(0, Math.round(Number(v)))}s`);
const _fmtElapsed = v => String(Math.max(0, Math.floor(Number(v) || 0)));

function _panelIterElapsed(rec) {
  if (!rec) return 0;
  if (rec.timerStartMs != null) {
    const end = rec.timerEndMs != null ? rec.timerEndMs : performance.now();
    return (end - rec.timerStartMs) / 1000;
  }
  if (rec.totalSeconds != null) return Number(rec.totalSeconds);
  return 0;
}

function _panelDisplayElapsed(rec) {
  if (_panelDisplayTimer && _panelDisplayTimer.idx === _panelSel) {
    const end = _panelDisplayTimer.endMs != null ? _panelDisplayTimer.endMs : performance.now();
    return (end - _panelDisplayTimer.startMs) / 1000;
  }
  return _panelIterElapsed(rec);
}

function _panelStartDisplayTimer(idx, originMs = performance.now()) {
  _panelDisplayTimer = { idx, startMs: originMs, endMs: null };
  if (idx === _panelSel && RP.elapsed) {
    RP.elapsed.textContent = _fmtElapsed((performance.now() - originMs) / 1000);
  }
  _panelEnsureClock();
}

function _panelStopDisplayTimer(idx) {
  if (!_panelDisplayTimer || _panelDisplayTimer.idx !== idx) return;
  if (_panelDisplayTimer.endMs == null) _panelDisplayTimer.endMs = performance.now();
  if (idx === _panelSel) _panelUpdateTimer();
}

function _panelClearDisplayTimer(idx = null) {
  if (idx == null || (_panelDisplayTimer && _panelDisplayTimer.idx === idx)) {
    _panelDisplayTimer = null;
  }
}

function _panelUpdateTimer() {
  if (!RP.panel) return;
  const rec = state.iterRecords[_panelSel];
  RP.elapsed.textContent = _fmtElapsed(_panelDisplayElapsed(rec));
  if (_panelDisplayTimer && _panelDisplayTimer.idx === _panelSel && _panelDisplayTimer.endMs == null) {
    return;
  }
  if (!rec || rec.phase === "done" || rec.timerEndMs != null || rec.timerStartMs == null) {
    _panelStopClock();
  }
}

function _panelEnsureClock() {
  if (_panelTicker) return;
  _panelTicker = setInterval(_panelUpdateTimer, 250);
}

function _panelSyncTimer() {
  const rec = state.iterRecords[_panelSel];
  _panelUpdateTimer();
  if (_panelDisplayTimer && _panelDisplayTimer.idx === _panelSel && _panelDisplayTimer.endMs == null) {
    _panelEnsureClock();
    return;
  }
  if (rec && rec.timerStartMs != null && rec.timerEndMs == null && rec.phase !== "done") {
    _panelEnsureClock();
  }
}

function _panelStartIterTimer(idx, reset = false) {
  const rec = _iterRec(idx);
  if (reset || rec.timerStartMs == null || rec.timerEndMs != null) {
    rec.timerStartMs = performance.now();
    rec.timerEndMs = null;
  }
  if (idx === _panelSel) _panelSyncTimer();
}

function _panelStopIterTimer(idx) {
  const rec = state.iterRecords[idx];
  if (!rec) return;
  if (rec.timerStartMs != null && rec.timerEndMs == null) {
    rec.timerEndMs = performance.now();
  }
  if (idx === _panelSel) _panelSyncTimer();
}

function _panelAnimateChange() {
  if (!RP.panel) return;
  const paper = RP.panel.querySelector(".rp-paper");
  if (!paper) return;
  if (_panelChangeTimer) clearTimeout(_panelChangeTimer);
  paper.classList.remove("iteration-changing");
  void paper.offsetWidth;
  paper.classList.add("iteration-changing");
  _panelChangeTimer = setTimeout(() => {
    paper.classList.remove("iteration-changing");
    _panelChangeTimer = null;
  }, 900);
}

function _panelSetStatusCenter(centered) {
  if (!RP.panel) return;
  RP.panel.classList.toggle("status-center", Boolean(centered));
  if (centered) {
    RP.panel.style.top = Math.round(window.innerHeight / 2) + 'px';
  } else {
    RP.panel.style.top = '';
  }
}

function _panelSetFeedbackShown(shown) {
  if (!RP.panel) return;
  const wasShown = RP.panel.classList.contains("feedback-shown");
  // FLIP the elapsed-time row: when the score / iteration track / continue button
  // appear, the flex row recentres and the timer would otherwise jump sideways.
  // Capture its position first, then ease it from the old spot to the new one.
  const stateEl = RP.panel.querySelector(".rp-state");
  const flip = !_panelReducedMotion() && stateEl && wasShown !== Boolean(shown);
  const beforeRect = flip ? stateEl.getBoundingClientRect() : null;

  RP.panel.classList.toggle("feedback-shown", Boolean(shown));

  if (flip) {
    const afterRect = stateEl.getBoundingClientRect();
    const dx = beforeRect.left - afterRect.left;
    if (Math.abs(dx) > 0.5) {
      stateEl.animate(
        [{ transform: `translateX(${dx}px)` }, { transform: "translateX(0)" }],
        { duration: 620, easing: "cubic-bezier(0.16, 1, 0.3, 1)" }
      );
    }
  }
  if (_metaRevealTimer) {
    clearTimeout(_metaRevealTimer);
    _metaRevealTimer = null;
  }
  RP.panel.classList.remove("meta-entering");
  if (!shown) return;
  if (wasShown || _panelReducedMotion()) return;
  void RP.panel.offsetWidth;
  RP.panel.classList.add("meta-entering");
  _metaRevealTimer = setTimeout(() => {
    RP.panel.classList.remove("meta-entering");
    _metaRevealTimer = null;
  }, 1900);
}

function _panelStopFeedbackReveal() {
  if (!RP.feedback) return;
  _feedbackStreamToken += 1;
  if (_feedbackRevealTimer) {
    clearTimeout(_feedbackRevealTimer);
    _feedbackRevealTimer = null;
  }
  RP.feedback.classList.remove("typesetting", "status-entering");
  if (RP.panel) RP.panel.classList.remove("feedback-typesetting");
}

function _panelClearInspectionPause() {
  _panelInspectionToken += 1;
  if (_panelInspectionTimer) {
    clearTimeout(_panelInspectionTimer);
    _panelInspectionTimer = null;
  }
}

function _panelClearRevealWatchdog() {
  if (_panelRevealWatchdog) {
    clearTimeout(_panelRevealWatchdog);
    _panelRevealWatchdog = null;
  }
}

function _panelExpectedRevealMs(anim) {
  // Only the strokes that are actually drawn count toward the duration. Each can
  // take up to ~2200ms to draw + 300ms erase (for redraws) + 150ms gap, plus the
  // animator's trailing delay(1000). Use a generous per-stroke budget and a wide
  // buffer so this watchdog only ever fires for a genuinely stalled animation —
  // normal completion is handled by awaiting anim.play().
  const count = Math.max(1, anim && anim.drawableCount ? anim.drawableCount() : 1);
  return Math.min(40000, count * 2900 + 4000);
}

function _panelDisplayFeedback(rec) {
  if (!rec) return "";
  const text = (rec.feedback || rec.uiMessage || "").trim();
  if (text) return text;
  if (rec.verdict === "accept") return "The drawing matches the request.";
  return "";
}

function _panelRecordedDelay(idx, stage) {
  const seed = Math.sin((idx + 1) * (stage === "artist" ? 17.31 : 29.73)) * 10000;
  const jitter = seed - Math.floor(seed);
  // Keep the recorded artist wait at/under the 3s floor so a replayed run starts
  // drawing about as fast as a live cloud run (~3s), instead of a long fake pause.
  if (stage === "artist") return 2000 + Math.round(jitter * 600);
  return 3000 + Math.round(jitter * 1000);
}

// Minimum perceived wait so the loop never feels instantaneous even when the
// model responds very fast: the artist "thinks" for >= 3s before drawing, the
// critic "inspects" for >= 2s before its feedback is revealed.
const _ARTIST_MIN_WAIT_MS = 3000;
const _CRITIC_MIN_WAIT_MS = 2000;

function _panelArtistDelay(idx, fallbackMs = 650) {
  const rec = state.iterRecords[idx];
  const base = rec && rec.recorded ? _panelRecordedDelay(idx, "artist") : fallbackMs;
  return Math.max(_ARTIST_MIN_WAIT_MS, base);
}

function _panelCriticDelay(idx, fallbackMs = 1750) {
  const rec = state.iterRecords[idx];
  const base = rec && rec.recorded ? _panelRecordedDelay(idx, "critic") : fallbackMs;
  return Math.max(_CRITIC_MIN_WAIT_MS, base);
}

function _panelShowInspection(idx, holdMs = 1750, revealFeedback = true) {
  const rec = state.iterRecords[idx];
  if (!RP.panel || !rec || _panelSel !== idx) return false;
  const displayFeedback = _panelDisplayFeedback(rec);
  if (_panelDrawingReveal === idx) _panelDrawingReveal = null;
  _panelSetStatusCenter(false);
  _panelClearInspectionPause();
  _panelSetFeedbackShown(false);
  const inspectSig = `${idx}:inspect:${displayFeedback || rec.phase || "pending"}`;
  _panelSetFeedbackStatus("The critic is inspecting the drawing...", inspectSig);
  _lastFeedbackSig = inspectSig;
  if (revealFeedback && displayFeedback) {
    const token = ++_panelInspectionToken;
    _panelInspectionTimer = setTimeout(() => {
      if (token !== _panelInspectionToken || _panelSel !== idx) return;
      _panelInspectionTimer = null;
      _panelRenderDetail(idx);
    }, holdMs);
  }
  return true;
}

function _panelReducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function _panelAnimateNoteMutation(mutate) {
  const note = RP.feedback ? RP.feedback.closest(".rp-note") : null;
  const noteLabel = note ? note.querySelector(".rp-note-label") : null;
  const tracked = [
    note,
    noteLabel,
  ].filter(Boolean);
  const before = !_panelReducedMotion()
    ? tracked.map(el => [el, el.getBoundingClientRect()])
    : [];
  mutate();

  before.forEach(([el, rect]) => {
    const after = el.getBoundingClientRect();
    const dx = rect.left - after.left;
    const dy = rect.top - after.top;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;

    el.animate(
      [
        { transform: `translate(${dx}px, ${dy}px)`, opacity: 0.72 },
        { transform: "translate(0, 0)", opacity: 1 },
      ],
      { duration: 1250, easing: "cubic-bezier(0.16, 1, 0.3, 1)" }
    );
  });
}

function _panelBuildFeedbackWords(text, className = "rp-feedback-word") {
  const parts = text.split(/(\s+)/).filter(Boolean);
  let wordIndex = 0;
  const fragment = document.createDocumentFragment();
  parts.forEach(part => {
    if (/^\s+$/.test(part)) {
      fragment.appendChild(document.createTextNode(part));
      return;
    }
    const span = document.createElement("span");
    span.className = className;
    span.textContent = part;
    span.style.setProperty("--word-delay", `${wordIndex * 76}ms`);
    fragment.appendChild(span);
    wordIndex += 1;
  });
  return { fragment, wordIndex };
}

function _panelSetFeedbackStatus(text, sig) {
  if (!RP.feedback) return;
  if (sig === _lastFeedbackSig && RP.feedback.textContent === text) return;
  const isCriticInspection = /\bcritic is inspecting\b/i.test(text);
  // Both the artist's "drawing" wait and the critic's "inspecting" wait use the
  // same sweeping beam animation, so the two waiting states feel consistent.
  const hasBeamStatus = /\bartist is drawing\b/i.test(text) || isCriticInspection;
  _panelStopFeedbackReveal();
  _panelSetFeedbackShown(false);
  _panelAnimateNoteMutation(() => {
    RP.feedback.textContent = text;
    RP.feedback.className = hasBeamStatus ? "rp-feedback muted drawing-beam" : "rp-feedback muted";
  });
  if (!_panelReducedMotion()) {
    void RP.feedback.offsetWidth;
    RP.feedback.classList.add("status-entering");
    _feedbackRevealTimer = setTimeout(() => {
      RP.feedback.classList.remove("status-entering");
      _feedbackRevealTimer = null;
    }, 1050);
  }
}

function _panelTypesetFeedback(text, sig = "") {
  if (!RP.feedback) return;
  _panelStopFeedbackReveal();
  _feedbackControlsSig = "";
  _panelSetFeedbackShown(false);
  if (_panelReducedMotion()) {
    RP.feedback.textContent = text;
    RP.feedback.className = "rp-feedback";
    _feedbackControlsSig = sig;
    _panelStopDisplayTimer(_panelSel);
    _panelSetFeedbackShown(true);
    return;
  }

  const token = ++_feedbackStreamToken;
  const { fragment, wordIndex } = _panelBuildFeedbackWords(text);

  _panelAnimateNoteMutation(() => {
    if (RP.panel) RP.panel.classList.add("feedback-typesetting");
    RP.feedback.textContent = "";
    RP.feedback.className = "rp-feedback typesetting";
    RP.feedback.appendChild(fragment);
  });

  const duration = 640 + wordIndex * 76;
  _feedbackRevealTimer = setTimeout(() => {
    if (token !== _feedbackStreamToken) return;
    if (RP.panel) RP.panel.classList.remove("feedback-typesetting");
    RP.feedback.classList.remove("typesetting");
    _feedbackRevealTimer = null;
    if (!sig || sig === _lastFeedbackSig) {
      _feedbackControlsSig = sig;
      _panelStopDisplayTimer(_panelSel);
      _panelSetFeedbackShown(true);
      _panelRenderChips();
    }
  }, duration);
}

function panelStartRun(prompt) {
  if (!RP.panel) return;
  _panelBrowse = false;
  _panelSel = 0;
  _maxReady = -1;
  _shownAny = false;
  _panelViewBusy = false;
  _panelPendingTarget = null;
  _lastFeedbackSig = "";
  _feedbackControlsSig = "";
  _panelViewed = new Set();
  _panelDrawingReveal = null;
  _panelDrawWaitStart = performance.now();
  _panelClearDisplayTimer();
  _panelClearRevealWatchdog();
  _panelClearInspectionPause();
  _panelStopFeedbackReveal();
  RP.panel.classList.remove("run-complete");
  const first = _iterRec(0);
  first.phase = "drawing";
  first.timerStartMs = performance.now();
  first.timerEndMs = null;
  RP.prompt.textContent = prompt;
  RP.statusDot.className = "rp-status-dot live";
  RP.statusTxt.textContent = "running";
  RP.elapsed.textContent = "0";
  RP.track.innerHTML = "";
  document.body.classList.add("run-panel-open");
  _panelSetStatusCenter(true);
  _panelSetFeedbackShown(false);
  RP.panel.classList.remove("hidden");
  // next frame so the transform transition plays
  requestAnimationFrame(() => RP.panel.classList.add("visible"));
  // set --rp-top / --fig-offset up front so the figure + caption compose
  // immediately (no jump when the first stroke renders)
  computeSVGBounds();
  _panelStopClock();
  _panelRenderChips();
  _panelRenderDetail(0);
}

function _panelStopClock() {
  if (_panelTicker) { clearInterval(_panelTicker); _panelTicker = null; }
}

function panelHide() {
  if (!RP.panel) return;
  _panelStopClock();
  _panelClearDisplayTimer();
  _panelDrawingReveal = null;
  _panelClearRevealWatchdog();
  _panelClearInspectionPause();
  _panelStopFeedbackReveal();
  _panelSetStatusCenter(false);
  _panelSetFeedbackShown(false);
  RP.panel.classList.remove("run-complete");
  RP.panel.classList.remove("visible");
  document.body.classList.remove("run-panel-open");
  setTimeout(() => RP.panel.classList.add("hidden"), 560);
}

async function _fadeOutCanvas(duration = 460) {
  if (_panelReducedMotion()) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  const fade = canvas.animate(
    [{ opacity: 1 }, { opacity: 0 }],
    { duration, easing: "cubic-bezier(0.4, 0, 0.2, 1)", fill: "forwards" }
  );
  try { await fade.finished; } catch (_) {}
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  canvas.style.opacity = "";
}

function panelReturnToPaper() {
  panelHide();
  if (state.activeAnimator) {
    state.activeAnimator.cancel();
    state.activeAnimator = null;
  }
  stopThinking();
  _fadeOutCanvas();
  const screen = document.getElementById("thesis-landing");
  if (!screen) return;
  screen.classList.remove("gone");
  screen.classList.add("fade-out");
  screen.scrollTop = 0;
  if (typeof screen.scrollTo === "function") screen.scrollTo({ top: 0, left: 0, behavior: "auto" });
  if (window.history && window.location.hash) {
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  requestAnimationFrame(() => {
    screen.scrollTop = 0;
    screen.classList.remove("fade-out");
  });
}

// Refresh the minimal iteration selector.
function _panelTotalIterations() {
  if (_panelBrowse || state.phase === "complete") {
    return Math.max(1, _maxReady + 1);
  }
  return Math.max(1, state.totalIterations, state.iterRecords.length, _maxReady + 1);
}

function _panelFeedbackReady(idx) {
  const rec = state.iterRecords[idx];
  if (!rec || rec.phase !== "done") return false;
  return Boolean(rec.feedback || rec.verdict || rec.score != null);
}

function _panelRenderChips() {
  if (!RP.panel) return;
  const total = _panelTotalIterations();
  const runComplete = _panelBrowse || state.phase === "complete";
  const allViewed = runComplete && total > 0 && Array.from({ length: total }, (_, i) => _panelViewed.has(i)).every(Boolean);
  RP.panel.classList.toggle("run-complete", runComplete);
  RP.panel.classList.toggle("iterations-all-viewed", allViewed);
  const canAdvance = !allViewed && !_panelViewBusy && _panelFeedbackReady(_panelSel) && _panelSel < total - 1;
  if (RP.continueBtn) {
    // During an iteration transition (_panelViewBusy), keep the button's current
    // visibility so it doesn't collapse and re-expand (which looks like a teleport).
    // Only the enabled/disabled state changes; visibility settles in the finally block.
    const currentlyVisible = RP.continueBtn.classList.contains("visible");
    const showContinue = _panelViewBusy ? currentlyVisible : canAdvance;
    RP.continueBtn.disabled = !canAdvance;
    RP.continueBtn.classList.toggle("visible", showContinue);
    RP.continueBtn.setAttribute("aria-hidden", showContinue ? "false" : "true");
    RP.continueBtn.setAttribute("aria-label", "continue to next iteration");
    if (RP.continueSep) {
      RP.continueSep.classList.toggle("visible", showContinue);
      RP.continueSep.setAttribute("aria-hidden", showContinue ? "false" : "true");
    }
  }
  RP.track.innerHTML = "";
  for (let i = 0; i < total; i += 1) {
    const rec = state.iterRecords[i];
    const hasSvg = Boolean((rec && rec.svg) || state.iterHistory[i]);
    const current = i === _panelSel;
    const previouslyViewed = _panelViewed.has(i);
    // Iterations already stepped through are clickable to revisit at any time.
    // Forward jumps are blocked until all iterations have been viewed — only
    // after viewing the last one (allViewed) does full free navigation unlock.
    const enabled = !current && !_panelViewBusy && hasSvg &&
                    (allViewed || previouslyViewed);

    const item = document.createElement("button");
    item.type = "button";
    item.className = "rp-iter-choice";
    item.textContent = String(i + 1);
    item.disabled = !enabled;
    item.setAttribute("aria-label", `show iteration ${i + 1}`);
    if (current) {
      item.classList.add("current");
      item.setAttribute("aria-current", "step");
    } else if (runComplete && hasSvg) {
      item.classList.add("complete");
      if (!previouslyViewed) item.classList.add("pending");
    } else if (previouslyViewed) {
      item.classList.add("viewed");
    } else {
      item.classList.add("pending");
    }
    if (enabled) {
      item.addEventListener("click", () => {
        _viewIter(i, { fade: true });
      });
    }
    RP.track.appendChild(item);
  }
}

// Render one iteration's full detail into the body.
function _panelRenderDetail(i, opts = {}) {
  if (!RP.panel) return;
  const rec = state.iterRecords[i];
  const hasSvg = Boolean((rec && rec.svg) || state.iterHistory[i]);
  const centerStatus = Boolean(opts.centerStatus) || !rec || (rec.phase === "drawing" && !hasSvg);
  const drawingRevealActive = _panelDrawingReveal === i && !opts.staticFeedback;
  _panelSetStatusCenter(centerStatus);
  if (RP.figLabel) RP.figLabel.textContent = `Figure A${i + 1}.`;
  RP.iterId.textContent = `Iteration ${i + 1}`;
  if (RP.scoreKey) RP.scoreKey.innerHTML = `r<sub>${i + 1}</sub> =`;
  if (RP.verdictKey) RP.verdictKey.innerHTML = `v<sub>${i + 1}</sub> =`;

  if (!rec) {
    _panelSetFeedbackShown(false);
    RP.phase.className = "rp-phase";
    RP.phase.textContent = "pending";
    RP.statusTxt.textContent = "waiting";
    RP.elapsed.textContent = "0";
    _panelStopClock();
    RP.score.textContent = "—"; RP.score.className = "rp-metric-val";
    RP.verdict.textContent = "—"; RP.verdict.className = "rp-metric-val";
    const pendingSig = `${i}:pending`;
    _panelSetFeedbackStatus("Not reached yet.", pendingSig);
    _lastFeedbackSig = pendingSig;
    [RP.tArtist,RP.tCritic,RP.tTotal].forEach(e => e.textContent = "—");
    [RP.barArtist,RP.barCritic].forEach(b => b.style.width = "0%");
    return;
  }

  const phaseLabels = {
    drawing: "artist drawing",
    critiquing: "critic reading",
    done: "rendered and critiqued",
  };
  const revealStillDrawing = drawingRevealActive && rec.phase === "drawing";
  const displayPhase = (rec.phase === "drawing" && hasSvg && !drawingRevealActive)
    ? "critiquing"
    : rec.phase;
  const visiblePhase = revealStillDrawing ? "drawing" : displayPhase;
  RP.phase.className = "rp-phase " + (visiblePhase || "");
  RP.phase.textContent = phaseLabels[visiblePhase] || visiblePhase || "";
  const statusLabels = {
    drawing: "running",
    critiquing: "reading",
    done: "critiqued",
  };
  RP.statusTxt.textContent = statusLabels[visiblePhase] || "running";

  RP.score.textContent = rec.score == null ? "—" : `${rec.score}/10`;
  RP.score.className = "rp-metric-val";
  RP.verdict.textContent = rec.verdict || "—";
  RP.verdict.className = "rp-metric-val" + (rec.verdict === "accept" ? " accept" : "");

  const statusText =
    visiblePhase === "critiquing" ? "The critic is inspecting the drawing..."
    : visiblePhase === "drawing" ? "The artist is drawing..."
    : rec.verdict === "accept" ? "The drawing matches the request."
    : "No feedback recorded.";
  const displayFeedback = _panelDisplayFeedback(rec);
  const feedbackSig = displayFeedback ? `${i}:feedback:${displayFeedback}` : `${i}:${rec.phase || "empty"}:${statusText}`;
  if (opts.loadingStatus || revealStillDrawing) {
    _panelSetFeedbackShown(false);
    const loadingSig = `${i}:loading:drawing`;
    _panelSetFeedbackStatus("The artist is drawing...", loadingSig);
    _lastFeedbackSig = loadingSig;
  } else if (displayFeedback) {
    if (opts.staticFeedback) {
      _panelStopFeedbackReveal();
      RP.feedback.textContent = displayFeedback;
      RP.feedback.className = "rp-feedback";
      _feedbackControlsSig = feedbackSig;
      _panelSetFeedbackShown(true);
    } else if (feedbackSig !== _lastFeedbackSig) {
      _panelTypesetFeedback(displayFeedback, feedbackSig);
    } else if (!RP.feedback.classList.contains("typesetting")) {
      RP.feedback.textContent = displayFeedback;
      RP.feedback.className = "rp-feedback";
      _feedbackControlsSig = feedbackSig;
      _panelSetFeedbackShown(true);
    } else {
      _panelSetFeedbackShown(_feedbackControlsSig === feedbackSig);
    }
  } else {
    _panelSetFeedbackShown(false);
    _panelSetFeedbackStatus(statusText, feedbackSig);
  }
  if (!opts.loadingStatus && !revealStillDrawing) _lastFeedbackSig = feedbackSig;

  // timings + proportional bars (scaled to the largest stage in this iteration)
  const stages = [
    [RP.tArtist, RP.barArtist, rec.artistSeconds],
    [RP.tCritic, RP.barCritic, rec.criticSeconds],
  ];
  const maxS = Math.max(0.0001, ...stages.map(s => s[2] || 0));
  stages.forEach(([valEl, barEl, v]) => {
    valEl.textContent = _fmtS(v);
    barEl.style.width = v ? `${Math.round((v / maxS) * 100)}%` : "0%";
  });
  const total = rec.totalSeconds != null ? rec.totalSeconds
    : ((rec.artistSeconds||0)+(rec.renderSeconds||0)+(rec.criticSeconds||0)+(rec.swapSeconds||0)) || null;
  RP.tTotal.textContent = _fmtS(total);

  _panelSyncTimer();
}

// Live update: events populate records in the background. The detail view only
// changes for the iteration the user is currently viewing (no auto-advance).
function panelSetLive(idx, phase) {
  if (!RP.panel) return;
  _panelRenderChips();
  if (idx !== _panelSel) return;
  // While the drawing animation is in progress don't touch feedback display —
  // _viewIter will show it after anim.play() resolves.
  if (_panelDrawingReveal === idx) return;

  const rec = state.iterRecords[idx];
  const currentFeedback = RP.feedback ? RP.feedback.textContent : "";
  if (
    phase === "done" &&
    rec &&
    _panelDisplayFeedback(rec) &&
    /\bartist is drawing\b/i.test(currentFeedback)
  ) {
    _panelShowInspection(idx, _panelCriticDelay(idx, 1400), true);
    return;
  }
  if (phase === "critiquing") {
    _panelShowInspection(idx, 0, false);
    return;
  }
  _panelRenderDetail(_panelSel);
}

// Run finished: freeze the clock, mark complete. (Per-iteration scrubbing and
// the action buttons are already available; nothing auto-advances.)
function panelEnterBrowse() {
  if (!RP.panel) return;
  _panelStopClock();
  _panelBrowse = true;
  RP.panel.classList.add("run-complete");
  state.phase = "complete";
  const hadPendingMissing = _panelPendingTarget != null && _panelPendingTarget > _maxReady;
  if (hadPendingMissing) {
    _panelPendingTarget = null;
    _panelViewBusy = false;
    _panelSel = Math.max(0, _maxReady);
    state.viewingIter = _panelSel;
    stopThinking();
  }
  state.iterRecords.forEach((rec, idx) => {
    if (idx <= _maxReady && rec && rec.svg && rec.phase !== "done") rec.phase = "done";
  });
  RP.statusDot.className = "rp-status-dot done";
  // Fixed-iteration loop: the run always ends by completing its schedule, not
  // by a Critic "accept", so the end state is reported neutrally.
  RP.statusTxt.textContent = "complete";
  _panelRenderChips();
  _panelRenderDetail(_panelSel);
  if (hadPendingMissing && _maxReady >= 0) _viewIter(_panelSel, { force: true });
}

async function _requestIter(idx) {
  let rec = state.iterRecords[idx];
  const svg = (rec && rec.svg) || state.iterHistory[idx];
  const revisiting = _panelViewed.has(idx);
  if (!revisiting) {
    const viewRec = _iterRec(idx);
    viewRec.phase = viewRec.phase || "drawing";
    _panelDrawWaitStart = performance.now();
    _panelStartDisplayTimer(idx);
  } else {
    _panelClearDisplayTimer(idx);
  }
  if (!_panelFeedbackReady(idx)) {
    rec = _iterRec(idx);
    rec.phase = rec.phase || "drawing";
    _panelStartIterTimer(idx, true);
  }
  if (svg) {
    _viewIter(idx, { loading: !revisiting, transition: !revisiting, fade: revisiting });
    return;
  }

  _panelPendingTarget = idx;
  _panelViewBusy = true;
  _panelSel = idx;
  state.viewingIter = idx;
  _panelAnimateChange();
  _panelRenderChips();
  _panelRenderDetail(idx);

  if (state.activeAnimator) { state.activeAnimator.cancel(); state.activeAnimator = null; }
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

// Animate iteration `idx` on the canvas (drawing it stroke by stroke) and sync
// the panel to it. The loop keeps computing other iterations in the background;
// this only changes what the user is looking at.
async function _viewIter(idx, opts = {}) {
  if (_panelViewBusy && !opts.force) return;
  _panelClearInspectionPause();
  _panelClearRevealWatchdog();
  const rec = state.iterRecords[idx];
  const svg = (rec && rec.svg) || state.iterHistory[idx];
  if (!svg) return;
  _panelViewBusy = true;
  _panelPendingTarget = null;
  _panelSel = idx;
  state.viewingIter = idx;
  const useFade = opts.fade || (_panelViewed.has(idx) && !opts.force);
  if (!useFade) _panelDrawingReveal = idx;
  const centerWhileLoading = Boolean(opts.loading && !useFade);
  if (centerWhileLoading) {
    _panelSetStatusCenter(true);
    if (state.activeAnimator) { state.activeAnimator.cancel(); state.activeAnimator = null; }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }
  if ((useFade || opts.transition || opts.loading || opts.force) && !opts.initial) _panelAnimateChange();
  _panelRenderChips();
  _panelRenderDetail(idx, {
    staticFeedback: useFade,
    centerStatus: centerWhileLoading,
    loadingStatus: centerWhileLoading,
  });
  try {
    if (state.activeAnimator) { state.activeAnimator.cancel(); state.activeAnimator = null; }
    if (useFade) {
      _panelSetStatusCenter(false);
      await _fadeToIter(svg);
      return;
    }
    // Floor the artist wait: the drawing never starts sooner than the minimum
    // (>= 3s) after the user began waiting for this iteration, no matter how fast
    // the model returned. On slow responses the elapsed wait already covers it,
    // so nothing extra is added.
    if (!_panelViewed.has(idx)) {
      if (opts.loading) ctx.clearRect(0, 0, canvas.width, canvas.height);
      const target = _panelArtistDelay(idx, 650);
      const waited = _panelDrawWaitStart ? performance.now() - _panelDrawWaitStart : target;
      const remaining = target - waited;
      if (remaining > 0) {
        await delay(remaining);
        if (_panelSel !== idx) return;
      }
    }
    _panelSetStatusCenter(false);
    await stopThinking();
    document.documentElement.style.setProperty("--canvas-shift", "0px");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const bounds = computeSVGBounds();
    state.svgBounds = bounds; window.svgBounds = bounds;

    // For iteration N > 0: fade the previous iteration onto the canvas as a
    // static background, then animate only new / changed paths on top.
    let prevDMap = {};
    if (idx > 0) {
      const prevRec = state.iterRecords[idx - 1];
      const prevSVG = (prevRec && prevRec.svg) || state.iterHistory[idx - 1];
      if (prevSVG) {
        prevDMap = buildPathDMap(prevSVG);
        // Background holds ONLY the paths that are unchanged from the previous
        // iteration (same id, same d). Changed / new paths are animated fresh on
        // top, and removed paths simply aren't drawn — so there's no rectangular
        // erase that could wipe a neighbouring shape mid-draw.
        const curDMap = buildPathDMap(svg);
        const sw = artStrokeWidth(bounds);
        const prevEl = parseSVG(prevSVG);
        const prevPaths = (prevEl ? extractStepPaths(prevEl) : [])
          .filter(({id, dAttr}) => curDMap[id] === dAttr);
        const pathsHTML = prevPaths.map(({el}) => {
          const cl = el.cloneNode(true);
          cl.setAttribute("stroke", ART_STROKE);
          cl.setAttribute("stroke-width", sw);
          cl.setAttribute("fill", "none");
          cl.setAttribute("stroke-opacity", "1");
          cl.setAttribute("opacity", "1");
          cl.removeAttribute("style");
          cl.removeAttribute("filter");
          return cl.outerHTML;
        }).join("");
        const bgSVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SVG_NATIVE} ${SVG_NATIVE}" width="${SVG_NATIVE}" height="${SVG_NATIVE}"><g fill="none" stroke-linecap="round" stroke-linejoin="round">${pathsHTML}</g></svg>`;
        const prevUrl = URL.createObjectURL(new Blob([bgSVG], {type:"image/svg+xml"}));
        const prevImg = await new Promise((res, rej) => {
          const img = new Image();
          img.onload = () => { URL.revokeObjectURL(prevUrl); res(img); };
          img.onerror = () => { URL.revokeObjectURL(prevUrl); rej(new Error("SVG load")); };
          img.src = prevUrl;
        });
        if (_panelSel !== idx) return;
        await new Promise(resolve => {
          const t0 = performance.now(), dur = 560;
          const tick = now => {
            const t = Math.min((now - t0) / dur, 1);
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            ctx.save();
            ctx.globalAlpha = ease(t);
            ctx.drawImage(prevImg, bounds.x, bounds.y, bounds.width, bounds.height);
            ctx.restore();
            if (t < 1) requestAnimationFrame(tick); else resolve();
          };
          requestAnimationFrame(tick);
        });
        if (_panelSel !== idx) return;
        await delay(240);
        if (_panelSel !== idx) return;
      }
    }

    const anim = new StrokeAnimator(svg, (rec && rec.steps) || [], bounds, prevDMap);
    state.activeAnimator = anim;
    const armWatchdog = () => {
      _panelClearRevealWatchdog();
      _panelRevealWatchdog = setTimeout(() => {
        _panelRevealWatchdog = null;
        const liveRec = state.iterRecords[idx];
        if (_panelSel !== idx || _panelDrawingReveal !== idx || !liveRec) return;
        // Never preempt an animation that is still drawing — only reveal once the
        // strokes have finished (or genuinely stalled). Re-arm and check later.
        if (state.activeAnimator === anim && anim.getProgress() < 1) {
          armWatchdog();
          return;
        }
        if (liveRec.phase === "critiquing" || (liveRec.phase === "drawing" && liveRec.svg)) {
          _panelShowInspection(idx, 0, false);
        } else if (liveRec.phase === "done" && _panelDisplayFeedback(liveRec)) {
          _panelShowInspection(idx, _panelCriticDelay(idx, 1400), true);
        }
      }, _panelExpectedRevealMs(anim));
    };
    armWatchdog();
    await anim.play();
    _panelClearRevealWatchdog();
    if (state.activeAnimator === anim) state.activeAnimator = null;
    if (_panelDrawingReveal === idx) _panelDrawingReveal = null;
    _panelViewed.add(idx);
    if (rec && _panelDisplayFeedback(rec)) {
      _panelShowInspection(idx, _panelCriticDelay(idx, 1750), true);
    } else if (rec && (rec.phase === "critiquing" || (rec.phase === "drawing" && rec.svg))) {
      _panelShowInspection(idx, 0, false);
    }
  } finally {
    _panelClearRevealWatchdog();
    if (_panelDrawingReveal === idx) _panelDrawingReveal = null;
    _panelViewBusy = false;
    _panelRenderChips();
  }
}

async function _fadeToIter(svg) {
  await stopThinking();
  document.documentElement.style.setProperty("--canvas-shift", "0px");
  const fadeOut = canvas.animate(
    [{ opacity: 1 }, { opacity: 0.18 }],
    { duration: 220, easing: "cubic-bezier(0.4, 0, 1, 1)", fill: "forwards" }
  );
  try { await fadeOut.finished; } catch (_) {}
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  await renderSVGToCanvas(svg);
  const fadeIn = canvas.animate(
    [{ opacity: 0.18 }, { opacity: 1 }],
    { duration: 520, easing: "cubic-bezier(0.16, 1, 0.3, 1)", fill: "forwards" }
  );
  try { await fadeIn.finished; } catch (_) {}
  canvas.style.opacity = "";
  _panelViewed.add(_panelSel);
}

// ── Utility ───────────────────────────────────────────────────────────────
const delay = ms => new Promise(r => setTimeout(r, ms));
const lerp  = (a, b, t) => a + (b - a) * t;

// cubic-bezier(0.4, 0, 0.2, 1) approximation
function ease(t) {
  const p1y = 0.0, p2y = 1.0;
  return 3*p1y*t*(1-t)**2 + 3*p2y*t**2*(1-t) + t**3;
}

function doodleStrokeEase(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

function doodleDrawDuration(len) {
  return Math.max(660, Math.min(2200, len * 7.4));
}

function artStrokeWidth(bounds) {
  const scale = bounds && bounds.scale ? bounds.scale : 1;
  return String(Math.max(2.0, ART_STROKE_SCREEN_WIDTH / scale));
}

function hexToRGB(hex) {
  const c = hex.replace("#","");
  if (c.length===3)
    return [parseInt(c[0]+c[0],16), parseInt(c[1]+c[1],16), parseInt(c[2]+c[2],16)];
  return [parseInt(c.slice(0,2),16), parseInt(c.slice(2,4),16), parseInt(c.slice(4,6),16)];
}

function lerpColor(a, b, t) {
  let ra; try { ra = hexToRGB(a); } catch(_){ ra=[51,51,51]; }
  const rb = hexToRGB(b);
  return `rgb(${Math.round(lerp(ra[0],rb[0],t))},${Math.round(lerp(ra[1],rb[1],t))},${Math.round(lerp(ra[2],rb[2],t))})`;
}

// ── Canvas ────────────────────────────────────────────────────────────────
// Scale backing store by DPR so drawings are sharp on Retina / mobile HiDPI.
const DPR = Math.round(window.devicePixelRatio || 1);

function _resizeCanvas() {
  canvas.width  = Math.round(window.innerWidth  * DPR);
  canvas.height = Math.round(window.innerHeight * DPR);
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
}
_resizeCanvas();

window.addEventListener("resize", () => {
  _resizeCanvas();
  computeSVGBounds();
  if (RP.panel && RP.panel.classList.contains("status-center")) {
    RP.panel.style.top = Math.round(window.innerHeight / 2) + 'px';
  }
});

function computeSVGBounds() {
  const cw = canvas.width / DPR, ch = canvas.height / DPR;
  const MAX_FIG = 460;
  const open = document.body.classList.contains("run-panel-open")
            && RP.panel && !RP.panel.classList.contains("hidden");
  const root = document.documentElement.style;

  if (!open) {
    const s = Math.min(cw / SVG_NATIVE, ch / SVG_NATIVE, MAX_FIG / SVG_NATIVE);
    const w = SVG_NATIVE * s, h = SVG_NATIVE * s;
    return { x:(cw - w) / 2, y:(ch - h) / 2, width:w, height:h, scale:s };
  }

  const isMobile = cw <= 560;
  const reserve = isMobile ? Math.min(ch * 0.30, 170) : Math.min(ch * 0.26, 190);
  const gap = isMobile ? 24 : 28;
  const margin = isMobile ? 48 : 30;
  // On mobile cap drawing height at 52% of viewport so there's breathing room top and bottom.
  const maxFig = isMobile ? Math.min(MAX_FIG, Math.round(ch * 0.52)) : MAX_FIG;
  const availH = Math.max(180, ch - reserve - gap - margin * 2);
  const s = Math.min(cw / SVG_NATIVE, availH / SVG_NATIVE, maxFig / SVG_NATIVE);
  const w = SVG_NATIVE * s, h = SVG_NATIVE * s;
  const blockH = h + gap + reserve;
  const y = Math.max(margin, (ch - blockH) / 2);
  const x = (cw - w) / 2;
  const ink = computeSVGInkBox(activePanelSVG());
  // Position the caption just below the actual ink, not the full figure box.
  // ink.y1 is now an exact getBBox measurement; a small pad accounts for stroke
  // width and gives the caption a little breathing room below the drawing.
  const INK_PAD = 16;
  const inkBottom = ink ? y + (Math.min(SVG_NATIVE, ink.y1 + INK_PAD) / SVG_NATIVE) * h : y + h * 0.58;
  const desiredCaptionTop = inkBottom + gap;
  const captionTop = Math.max(y + h * 0.30, Math.min(y + h + gap * 1.6, desiredCaptionTop));

  root.setProperty("--rp-top", captionTop + "px");
  root.setProperty("--fig-dx", "0px");
  root.setProperty("--fig-dy", ((y + h / 2) - ch / 2) + "px");
  root.removeProperty("--rp-cap-left");
  root.removeProperty("--rp-cap-top");
  root.removeProperty("--rp-cap-w");
  root.removeProperty("--rp-margin-left");
  root.removeProperty("--rp-margin-top");
  root.removeProperty("--rp-margin-w");

  return { x, y, width: w, height: h, scale: s };
}

function svgToViewport(sx, sy, b) {
  b = b || state.svgBounds || computeSVGBounds();
  return { x: b.x+(sx/SVG_NATIVE)*b.width, y: b.y+(sy/SVG_NATIVE)*b.height };
}

function canvasCenter() { return { x:canvas.width/(2*DPR), y:canvas.height/(2*DPR) }; }

function normalizeSVG(svgStr, bounds) {
  const strokeWidth = artStrokeWidth(bounds);
  return svgStr
    .replace(/\bfilter="[^"]*"/g, "")
    .replace(/\bfill="(?!none)[^"]*"/g, 'fill="none"')
    .replace(/\bstroke="(?!none)[^"]*"/g, `stroke="${ART_STROKE}"`)
    .replace(/\bstroke-width="[^"]*"/g, `stroke-width="${strokeWidth}"`)
    .replace(/\bstroke-opacity="[^"]*"/g, 'stroke-opacity="1"')
    .replace(/\bopacity="[^"]*"/g, 'opacity="1"')
    // Paths without an explicit stroke-width keep the browser default (1 px) and
    // appear thin against paths that were explicitly set. Add the attribute to any
    // <path> element that the regex above did not already touch.
    .replace(/<path\b(?![^>]*\bstroke-width\b)/g, `<path stroke-width="${strokeWidth}"`);
}

function renderSVGToCanvas(svgStr) {
  return new Promise((resolve,reject) => {
    const b   = computeSVGBounds();
    const url = URL.createObjectURL(new Blob([normalizeSVG(svgStr, b)],{type:"image/svg+xml"}));
    const img = new Image();
    img.onload = () => {
      ctx.drawImage(img, b.x, b.y, b.width, b.height);
      URL.revokeObjectURL(url);
      state.svgBounds = b; window.svgBounds = b;
      resolve(b);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("SVG load")); };
    img.src = url;
  });
}

// ── Canvas brightness (acceptance moment) ─────────────────────────────────
// Uses CSS filter on the canvas element so it doesn't disturb pixel data.
function setBrightness(value) { canvas.style.filter = value === 1 ? "" : `brightness(${value})`; }

async function animateBrightness(from, to, duration) {
  const start = performance.now();
  await new Promise(resolve => {
    const tick = now => {
      const t = Math.min((now-start)/duration, 1);
      // ease-in-out: smoothstep
      const s = t*t*(3-2*t);
      setBrightness(lerp(from, to, s));
      if (t<1) requestAnimationFrame(tick); else resolve();
    };
    requestAnimationFrame(tick);
  });
}

// ── SVG helpers ───────────────────────────────────────────────────────────
function parseSVG(s) {
  const doc = new DOMParser().parseFromString(s, "image/svg+xml");
  return doc.querySelector("parsererror") ? null : doc.documentElement;
}

function extractStepPaths(svgEl) {
  return Array.from(svgEl.querySelectorAll("path[id]"))
    .map(el => { const m=el.id.match(/^step-(\d+)$/); return m ? {id:el.id,n:parseInt(m[1],10),el,dAttr:el.getAttribute("d")||""} : null; })
    .filter(Boolean).sort((a,b)=>a.n-b.n);
}

// Split a path's `d` into its subpaths (each starting with a moveto). Returns
// null when the path is a single subpath, or when any subpath after the first
// uses a relative moveto (`m`) — those depend on the previous subpath's end
// point and can't be rendered independently, so we animate the whole path.
function splitSubpaths(d) {
  const subs = d.match(/[Mm][^Mm]*/g);
  if (!subs || subs.length <= 1) return null;
  for (let i = 1; i < subs.length; i++) {
    if (/^\s*m/.test(subs[i])) return null;
  }
  return subs;
}

function buildPathDMap(svgStr) {
  if (!svgStr) return {};
  const el = parseSVG(svgStr); if (!el) return {};
  const m = {};
  for (const {id,dAttr} of extractStepPaths(el)) m[id]=dAttr;
  return m;
}

function approxBBox(d) {
  const nums=[], re=/[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?/g; let m;
  while((m=re.exec(d))!==null) nums.push(parseFloat(m[0]));
  if (nums.length<2) return null;
  const xs=[], ys=[];
  for (let i=0; i+1<nums.length; i+=2){ xs.push(nums[i]); ys.push(nums[i+1]); }
  if (!xs.length) return null;
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  return {cx:(x0+x1)/2, cy:(y0+y1)/2, width:x1-x0, height:y1-y0};
}

function activePanelSVG() {
  const rec = state.iterRecords[state.viewingIter];
  return (rec && rec.svg) || state.iterHistory[state.viewingIter] || state.currentSVG || "";
}

// Hidden off-screen SVG used to measure true geometry via getBBox(). Reused
// across calls so we don't thrash the DOM.
let _inkMeasureSVG = null;
function _getInkMeasureSVG() {
  if (_inkMeasureSVG) return _inkMeasureSVG;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${SVG_NATIVE} ${SVG_NATIVE}`);
  svg.setAttribute("width", String(SVG_NATIVE));
  svg.setAttribute("height", String(SVG_NATIVE));
  svg.style.cssText = "position:absolute;left:-9999px;top:-9999px;width:" +
    SVG_NATIVE + "px;height:" + SVG_NATIVE + "px;pointer-events:none;opacity:0;";
  document.body.appendChild(svg);
  _inkMeasureSVG = svg;
  return svg;
}

// True ink bounds of a drawing, measured by the browser's geometry engine.
// getBBox() honours every path command (relative moveto, arc flags, beziers),
// so it is exact where the old number-scraping heuristic was not. Falls back to
// the heuristic if measurement is unavailable.
function computeSVGInkBox(svgStr) {
  if (!svgStr) return null;
  const el = parseSVG(svgStr); if (!el) return null;
  const inner = el.querySelector("g") || el;
  const host = _getInkMeasureSVG();
  let box = null;
  try {
    host.replaceChildren(document.importNode(inner, true));
    const g = host.firstElementChild;
    if (g && typeof g.getBBox === "function") {
      const bb = g.getBBox();
      if (bb && bb.width >= 0 && bb.height >= 0 && Number.isFinite(bb.x) && Number.isFinite(bb.y)) {
        box = { x0: bb.x, y0: bb.y, x1: bb.x + bb.width, y1: bb.y + bb.height,
                width: bb.width, height: bb.height };
      }
    }
    host.replaceChildren();
  } catch (_) { /* fall through to heuristic */ }
  if (box) return box;

  // Fallback: heuristic number-scraping (legacy).
  const boxes = Array.from(el.querySelectorAll("path"))
    .map(path => approxBBox(path.getAttribute("d") || ""))
    .filter(Boolean);
  if (!boxes.length) return null;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const b of boxes) {
    x0 = Math.min(x0, b.cx - b.width / 2);
    y0 = Math.min(y0, b.cy - b.height / 2);
    x1 = Math.max(x1, b.cx + b.width / 2);
    y1 = Math.max(y1, b.cy + b.height / 2);
  }
  if (![x0, y0, x1, y1].every(Number.isFinite)) return null;
  return { x0, y0, x1, y1, width: x1 - x0, height: y1 - y0 };
}

function computeStepBoundingBoxes(svgStr) {
  if (!svgStr) return [];
  const el=parseSVG(svgStr); if (!el) return [];
  return extractStepPaths(el).map(({id,dAttr})=>{const b=approxBBox(dAttr);return b?{stepId:id,...b}:null;}).filter(Boolean);
}

function estimateTargetRegion(text, boxes) {
  if (!boxes||!boxes.length) return null;
  const t = text.toLowerCase();
  const scored = boxes.map(b => {
    let s=0;
    if (/\btop\b|\bupper\b/.test(t))              s += b.cy < SVG_NATIVE*0.4 ? 3:-1;
    if (/\bbottom\b|\blower\b/.test(t))            s += b.cy > SVG_NATIVE*0.6 ? 3:-1;
    if (/\bleft\b/.test(t))                        s += b.cx < SVG_NATIVE*0.4 ? 3:-1;
    if (/\bright\b/.test(t))                       s += b.cx > SVG_NATIVE*0.6 ? 3:-1;
    if (/\bcenter\b|\bmiddle\b/.test(t))           s += (Math.abs(b.cx-256)<80&&Math.abs(b.cy-256)<80)?3:-1;
    if (/\bhead\b|\bface\b|\beye\b|\bear\b/.test(t)) s += (b.cy<SVG_NATIVE*0.45&&b.width<200)?3:0;
    if (/\bbody\b|\btrunk\b|\btorso\b/.test(t))    s += b.width>100?2:0;
    if (/\btail\b/.test(t))                        s += (b.cx>SVG_NATIVE*0.6||b.cy>SVG_NATIVE*0.6)?2:0;
    if (/\bleg\b|\bfeet\b|\bfoot\b|\bpaw\b/.test(t))  s += b.cy>SVG_NATIVE*0.55?2:0;
    if (/\bnose\b|\bmouth\b|\bwhisker\b/.test(t))  s += (b.cy<SVG_NATIVE*0.55&&b.width<150)?2:0;
    return {b,s};
  }).sort((a,b2)=>b2.s-a.s);
  return scored[0].s>0 ? scored[0].b : null;
}

// ── Iteration dots ────────────────────────────────────────────────────────
function buildIterationDots(total) {
  iterDots.innerHTML="";
  for (let i=0;i<total;i++){
    const d=document.createElement("div");
    d.className="iter-dot"; d.dataset.index=i;
    iterDots.appendChild(d);
  }
}

function updateIterationDots(active, total) {
  if (iterDots.children.length!==total) buildIterationDots(total);
  Array.from(iterDots.children).forEach((d,i)=>{
    d.classList.remove("active","done","waiting");
    if (i<active) d.classList.add("done");
    else if (i===active) d.classList.add("active");
  });
}

function markDotWaiting(index) {
  Array.from(iterDots.children).forEach((d, i) => {
    if (i < index) {
      d.classList.remove("active","waiting"); d.classList.add("done");
    } else if (i === index) {
      d.classList.remove("active","done"); d.classList.add("waiting");
    } else {
      d.classList.remove("active","done","waiting");
    }
  });
}

function markDotActive(index) {
  Array.from(iterDots.children).forEach((d, i) => {
    if (i < index) {
      d.classList.remove("active","waiting"); d.classList.add("done");
    } else if (i === index) {
      d.classList.remove("waiting","done"); d.classList.add("active");
    } else {
      d.classList.remove("active","done","waiting");
    }
  });
}

function markDotDone(index) {
  Array.from(iterDots.children).forEach((d, i) => {
    if (i <= index) {
      d.classList.remove("active","waiting"); d.classList.add("done");
    } else {
      d.classList.remove("active","done","waiting");
    }
  });
}

// ── CritiqueVerbs — cycling analysis text shown while critic scans ────────
const CRITIQUE_VERBS = [
  "reading lines",
  "checking proportions",
  "tracing edges",
  "studying shapes",
  "measuring balance",
  "looking for gaps",
  "comparing forms",
  "scanning details",
  "noting structure",
  "reviewing strokes",
];

// ── ScanningGaze — minimal scan dot + cycling critique verbs ──────────────
class ScanningGaze {
  constructor(bounds, stepBoxes) {
    this.bounds    = bounds || computeSVGBounds();
    this.stepBoxes = stepBoxes || [];
    this._running  = false;
    this._dot      = null;   // tiny scan reticle on the drawing
    this._verbEl   = null;   // text below the drawing, same style as thinking-verb
    this._verbIdx  = Math.floor(Math.random() * CRITIQUE_VERBS.length);
  }

  start() {
    // Tiny scan dot — just a small circle that drifts over the drawing
    const dot = document.createElement("div");
    dot.className = "scan-dot";
    const c = canvasCenter();
    dot.style.left = c.x + "px";
    dot.style.top  = c.y + "px";
    document.body.appendChild(dot);
    this._dot = dot;
    requestAnimationFrame(() => requestAnimationFrame(() => { dot.style.opacity = "1"; }));

    // Verb label — same font/size as #thinking-verb
    const verb = document.createElement("div");
    verb.className = "critique-verb";
    document.body.appendChild(verb);
    this._verbEl = verb;
    requestAnimationFrame(() => requestAnimationFrame(() => { verb.style.opacity = "1"; }));

    this._running = true;
    this._driftLoop();
    this._verbLoop();
  }

  async _driftLoop() {
    while (this._running) {
      const t = this._pickTarget();
      this._dot.style.left = t.x + "px";
      this._dot.style.top  = t.y + "px";
      await delay(500 + Math.random() * 900);
    }
  }

  async _verbLoop() {
    while (this._running) {
      const word = CRITIQUE_VERBS[this._verbIdx % CRITIQUE_VERBS.length];
      this._verbIdx++;
      await this._typeVerb(word);
      if (!this._running) break;
      await delay(600);
      await this._fadeVerb();
      if (!this._running) break;
      await delay(200);
    }
  }

  async _typeVerb(word) {
    if (!this._verbEl) return;
    this._verbEl.textContent = "";
    const interval = Math.min(90, Math.max(45, 1200 / (word.length + 2)));
    for (let i = 0; i <= word.length; i++) {
      if (!this._running) return;
      this._verbEl.textContent = word.slice(0, i) + (i < word.length ? "_" : "");
      if (i < word.length) await delay(interval);
    }
    // Hold with trailing "..."
    for (const dot of [".", ".", "."]) {
      if (!this._running) return;
      this._verbEl.textContent += dot;
      await delay(interval);
    }
    // Hold fully typed — in short chunks so stop() isn't delayed too long
    const holdMs = 900 + Math.random() * 600;
    const steps = 6;
    for (let s = 0; s < steps; s++) {
      if (!this._running) return;
      await delay(holdMs / steps);
    }
  }

  async _fadeVerb() {
    if (!this._verbEl) return;
    const el = this._verbEl;
    const t0 = performance.now();
    const dur = 300;
    await new Promise(resolve => {
      const tick = now => {
        const t = Math.min((now - t0) / dur, 1);
        el.style.opacity = String(1 - t);
        if (t < 1) requestAnimationFrame(tick);
        else { el.textContent = ""; el.style.opacity = "1"; resolve(); }
      };
      requestAnimationFrame(tick);
    });
  }

  _pickTarget() {
    const b = this.bounds;
    if (this.stepBoxes.length && Math.random() < 0.72) {
      const box = this.stepBoxes[Math.floor(Math.random() * this.stepBoxes.length)];
      return svgToViewport(box.cx, box.cy, b);
    }
    return {
      x: b.x + (0.12 + Math.random() * 0.76) * b.width,
      y: b.y + (0.10 + Math.random() * 0.70) * b.height,
    };
  }

  async stop(focusStepId) {
    this._running = false;
    if (this._dot) {
      if (focusStepId) {
        const box = this.stepBoxes.find(b => b.stepId === focusStepId);
        if (box) {
          const v = svgToViewport(box.cx, box.cy, this.bounds);
          this._dot.style.left = v.x + "px";
          this._dot.style.top  = v.y + "px";
          await delay(400);
        }
      }
      this._dot.style.opacity = "0";
      await delay(400);
      if (this._dot.parentNode) this._dot.parentNode.removeChild(this._dot);
      this._dot = null;
    }
    if (this._verbEl) {
      this._verbEl.style.transition = "opacity 0.3s ease";
      this._verbEl.style.opacity = "0";
      await delay(350);
      if (this._verbEl.parentNode) this._verbEl.parentNode.removeChild(this._verbEl);
      this._verbEl = null;
    }
  }
}

// ── CriticAnnotation ──────────────────────────────────────────────────────
const _criticFeedbackEl = document.getElementById("critic-feedback");

class CriticAnnotation {
  constructor(layerEl, bounds) {
    this.layerEl = layerEl;
    this.bounds  = bounds || computeSVGBounds();
    this._els    = [];
    this._active = true;
  }

  async show(uiMessage, feedbackType, centroid, detailText) {
    if (!this._active) return;
    if (feedbackType === "accept") { await this._showAccept(uiMessage); return; }
    // Critic feedback for revisions now lives in the run inspector panel, so we
    // no longer type it onto the canvas (it would duplicate the panel). Hold a
    // short beat so the scan-to-redraw transition still feels paced.
    await delay(900);
  }

  async hide() {
    this._active = false;
    for (const el of this._els) {
      el.style.transition = "opacity 0.5s ease";
      el.style.opacity = "0";
    }
    await delay(550);
    for (const el of this._els) { if (el.parentNode) el.parentNode.removeChild(el); }
    this._els = [];
  }

  async _showAccept(msg) {
    const el = document.createElement("div");
    el.className = "annotation-accept";
    el.textContent = msg || "looks good.";
    _criticFeedbackEl.appendChild(el); this._els.push(el);
    await delay(20); el.classList.add("visible");
    await delay(2000); el.classList.remove("visible"); await delay(550);
  }

  async _typeMessage(msg, detail) {
    if (!msg) return;

    // ── Line 1: ui_message — typewriter with _ cursor
    const el = document.createElement("div");
    el.className = "annotation-text";
    el.textContent = "";
    _criticFeedbackEl.appendChild(el); this._els.push(el);
    el.classList.add("visible");

    const interval1 = Math.min(110, Math.max(55, 2000 / (msg.length + 2)));
    for (let i = 0; i <= msg.length; i++) {
      if (!this._active) break;
      el.textContent = msg.slice(0, i) + (i < msg.length ? "_" : "");
      if (i < msg.length) await delay(interval1);
    }
    if (!this._active) return;

    // ── Line 2: feedback_for_artist — slower, no cursor
    if (detail && detail !== msg) {
      await delay(700);
      if (!this._active) return;
      const det = document.createElement("div");
      det.className = "annotation-detail";
      det.textContent = "";
      _criticFeedbackEl.appendChild(det); this._els.push(det);
      det.classList.add("visible");

      const interval2 = Math.min(80, Math.max(38, 6000 / (detail.length + 2)));
      for (let i = 0; i <= detail.length; i++) {
        if (!this._active) break;
        det.textContent = detail.slice(0, i);
        if (i < detail.length) await delay(interval2);
      }
    }
  }
}

// ── StrokeAnimator ────────────────────────────────────────────────────────
class StrokeAnimator {
  constructor(svgStr, steps, bounds, prevDMap={}) {
    this.svgStr    = svgStr;
    this.steps     = steps||[];
    this.bounds    = bounds;
    this.prevDMap  = prevDMap;
    this._progress = 0;
    this._cancelled= false;
    this.svgEl     = parseSVG(svgStr);
    this.stepPaths = this.svgEl ? extractStepPaths(this.svgEl) : [];
  }

  getProgress() { return this._progress; }
  cancel()       { this._cancelled=true; }

  // Count paths that will actually be animated (new or changed); unchanged ones
  // are skipped because they arrive via the static background fade.
  drawableCount() {
    const hasPrev = Object.keys(this.prevDMap).length > 0;
    if (!hasPrev) return this.stepPaths.length;
    let n = 0;
    for (const {id,dAttr} of this.stepPaths) {
      if (this.prevDMap[id] === dAttr) continue;   // unchanged → skipped
      n += 1;
    }
    return n;
  }

  async play() {
    if (!this.svgEl||!this.stepPaths.length) { await renderSVGToCanvas(this.svgStr); return; }
    const total=this.stepPaths.length;
    const hasPrev=Object.keys(this.prevDMap).length>0;
    for (let i=0;i<total;i++){
      if (this._cancelled) break;
      const {id,el,dAttr,n}=this.stepPaths[i];
      const label   =this.steps[i]||this.steps[n-1]||"";
      const prevD   =this.prevDMap[id];
      // Unchanged paths are already on canvas from the background fade — skip them.
      // Changed / new paths animate fresh on top; the old version of a changed
      // path was excluded from the background, so no erase is needed.
      if (hasPrev && prevD===dAttr) { this._progress=(i+1)/total; continue; }
      this._setLabel(label);
      await this._animStroke(el);
      this._progress=(i+1)/total;
      if (i<total-1&&!this._cancelled) await delay(150);
    }
    await delay(1000);
    this._setLabel("");
  }

  // Erase redraw: white rect at 0→0.85 opacity over the path's approx region,
  // leaving the ghost visible at ~15%.
  async _erasePath(stepId) {
    const bb = approxBBox(this.prevDMap[stepId]);
    if (!bb) return;
    const b   = this.bounds;
    const pad = 8;                             // a little breathing room
    const rx  = b.x + (bb.cx-bb.width/2-pad)/SVG_NATIVE*b.width;
    const ry  = b.y + (bb.cy-bb.height/2-pad)/SVG_NATIVE*b.height;
    const rw  = (bb.width+pad*2)/SVG_NATIVE*b.width;
    const rh  = (bb.height+pad*2)/SVG_NATIVE*b.height;

    const snap=ctx.getImageData(0,0,canvas.width,canvas.height);
    const t0=performance.now();
    await new Promise(resolve=>{
      const tick=now=>{
        const t=Math.min((now-t0)/300,1);
        ctx.putImageData(snap,0,0);
        ctx.save();
        ctx.globalAlpha=lerp(0,1.0,ease(t));
        ctx.fillStyle="#ffffff";
        ctx.fillRect(rx,ry,rw,rh);
        ctx.restore();
        if(t<1) requestAnimationFrame(tick); else resolve();
      };
      requestAnimationFrame(tick);
    });
  }

  // A single <path> from the model often packs several distinct shapes into one
  // element (e.g. both wheels as two "M …" subpaths). Animate each subpath in
  // turn so shapes are drawn one at a time instead of all at once.
  async _animStroke(pathEl) {
    const dAttr = pathEl.getAttribute("d") || "";
    const subs = splitSubpaths(dAttr);
    if (!subs) { await this._animSubStroke(pathEl, dAttr); return; }
    for (let i = 0; i < subs.length; i++) {
      if (this._cancelled) return;
      await this._animSubStroke(pathEl, subs[i]);
      if (i < subs.length - 1 && !this._cancelled) await delay(120);
    }
  }

  async _animSubStroke(pathEl, dStr) {
    let len=200;
    try {
      const ns="http://www.w3.org/2000/svg", s=document.createElementNS(ns,"svg");
      s.setAttribute("viewBox",`0 0 ${SVG_NATIVE} ${SVG_NATIVE}`);
      s.style.cssText="position:absolute;top:-9999px;left:-9999px;visibility:hidden;width:0;height:0";
      const cl=pathEl.cloneNode(true); cl.setAttribute("d", dStr);
      s.appendChild(cl); document.body.appendChild(s);
      len=cl.getTotalLength(); document.body.removeChild(s);
    } catch(_){}

    const dur=doodleDrawDuration(len);
    const t0 =performance.now();
    const b  =this.bounds;

    await new Promise(resolve=>{
      const frame=now=>{
        if(this._cancelled){resolve();return;}
        const rawT=Math.min((now-t0)/dur,1);
        const off =lerp(len,0,doodleStrokeEase(rawT));
        const svg =this._singlePathSVG(pathEl,len,off,dStr);
        const url =URL.createObjectURL(new Blob([svg],{type:"image/svg+xml"}));
        const img =new Image();
        img.onload=()=>{
          if(rawT===0||this._needsSnap){
            this._snap=ctx.getImageData(0,0,canvas.width,canvas.height);
            this._needsSnap=false;
          }
          if(this._snap) ctx.putImageData(this._snap,0,0);
          ctx.drawImage(img,b.x,b.y,b.width,b.height);
          URL.revokeObjectURL(url);
          if(rawT<1) requestAnimationFrame(frame); else resolve();
        };
        img.onerror=()=>{URL.revokeObjectURL(url);resolve();};
        img.src=url;
      };
      this._needsSnap=true; requestAnimationFrame(frame);
    });
  }

  _singlePathSVG(pathEl, len, off, dOverride) {
    const cl=pathEl.cloneNode(true);
    if (dOverride != null) cl.setAttribute("d", dOverride);
    const strokeWidth = artStrokeWidth(this.bounds);
    cl.setAttribute("stroke-dasharray",String(len));
    cl.setAttribute("stroke-dashoffset",String(off));
    cl.setAttribute("stroke", ART_STROKE);
    cl.setAttribute("stroke-width", strokeWidth);
    cl.setAttribute("fill","none");
    cl.setAttribute("stroke-opacity","1");
    cl.setAttribute("opacity","1");
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SVG_NATIVE} ${SVG_NATIVE}" width="${SVG_NATIVE}" height="${SVG_NATIVE}"><g fill="none" stroke-linecap="round" stroke-linejoin="round">${cl.outerHTML}</g></svg>`;
  }

  _setLabel(_text) {
    // No-op: step label is reserved for the iteration counter only
  }
}

// ── Step label (iteration counter) ───────────────────────────────────────
function setStateLabel(text) {
  stepLabel.textContent = text || "";
  stepLabel.classList.toggle("visible", !!text);
}

// ── Thinking overlay: sketch subjects ────────────────────────────────────
// Each subject: { verb, paths: [d-string, ...] }
// All drawn in a 180×180 viewBox, centered at (90,90).
const THINKING_SUBJECTS = [
  {
    verb: "sketching",
    paths: [
      // circle
      "M 90,45 C 115,43 132,60 132,85 C 132,110 115,127 90,127 C 65,127 48,110 48,85 C 48,60 65,43 90,45",
      // top ray
      "M 90,10 C 89,20 91,32 90,38",
      // top-right ray
      "M 126,21 C 120,30 114,38 110,44",
      // right ray
      "M 150,85 C 140,84 130,85 125,85",
      // bottom-right ray
      "M 145,112 C 136,108 128,103 124,97",
      // left ray
      "M 35,85 C 45,84 55,85 58,85",
      // smile
      "M 72,88 C 78,100 102,100 108,88",
      // left eye
      "M 74,76 C 77,72 82,72 84,76",
      // right eye
      "M 96,76 C 99,72 104,72 106,76",
    ],
  },
  {
    verb: "imagining",
    paths: [
      // head outline
      "M 90,34 C 116,33 134,52 133,78 C 132,104 114,122 90,122 C 66,122 48,104 47,78 C 46,52 64,35 90,34",
      // left ear triangle top
      "M 68,42 L 58,22 L 78,36",
      // right ear triangle
      "M 112,42 L 122,22 L 102,36",
      // left eye (arc)
      "M 72,72 Q 75,66 80,72",
      // right eye (arc)
      "M 100,72 Q 105,66 108,72",
      // nose
      "M 88,86 L 90,82 L 92,86",
      // left whiskers
      "M 34,90 L 60,94 M 34,98 L 60,100 M 34,106 L 60,104",
      // right whiskers
      "M 120,94 L 146,90 M 120,100 L 146,98 M 120,104 L 146,106",
    ],
  },
  {
    verb: "drafting",
    paths: [
      // left wall
      "M 30,140 L 30,82",
      // right wall
      "M 150,140 L 150,82",
      // base / floor line
      "M 22,140 L 158,140",
      // roof left slope
      "M 22,82 L 90,32",
      // roof right slope
      "M 90,32 L 158,82",
      // door
      "M 70,140 L 70,108 Q 90,98 110,108 L 110,140",
      // left window
      "M 38,100 L 58,100 L 58,120 L 38,120 Z",
      // window cross
      "M 48,100 L 48,120 M 38,110 L 58,110",
      // chimney
      "M 108,60 L 108,42 L 122,42 L 122,54",
    ],
  },
  {
    verb: "conjuring",
    paths: [
      // left petal
      "M 90,82 Q 74,62 82,42 Q 90,62 90,82",
      // top-right petal
      "M 90,82 Q 110,68 128,76 Q 108,84 90,82",
      // bottom-right petal
      "M 90,90 Q 112,100 118,120 Q 96,110 90,90",
      // bottom-left petal
      "M 90,90 Q 70,104 62,122 Q 82,108 90,90",
      // top-left petal
      "M 90,82 Q 68,70 52,76 Q 72,86 90,82",
      // center circle
      "M 90,72 C 100,72 108,80 108,90 C 108,100 100,108 90,108 C 80,108 72,100 72,90 C 72,80 80,72 90,72",
      // stem
      "M 90,108 Q 88,132 84,148",
    ],
  },
];

// ── ThinkingSketch ────────────────────────────────────────────────────────
class ThinkingSketch {
  constructor() {
    this._stopped   = false;
    this._overlay   = document.getElementById("thinking-overlay");
    this._block     = document.getElementById("thinking-block");
    this._verbEl    = document.getElementById("thinking-verb");
    this._svgEl     = document.getElementById("thinking-svg");
    this._subjectIdx = 0;
  }

  async start() {
    this._stopped = false;
    this._subjectIdx = 0;
    this._block.classList.remove("mini");

    this._verbEl.textContent = "";
    this._svgEl.style.opacity = "1";
    this._overlay.classList.remove("hidden");

    this._runLoop();
  }

  async _runLoop() {
    while (!this._stopped) {
      const subj = THINKING_SUBJECTS[this._subjectIdx % THINKING_SUBJECTS.length];
      this._subjectIdx++;

      // Build fresh path elements for this subject
      const ns = "http://www.w3.org/2000/svg";
      this._svgEl.innerHTML = "";
      const pathEls = subj.paths.map(d => {
        const p = document.createElementNS(ns, "path");
        p.setAttribute("d", d);
        this._svgEl.appendChild(p);
        return p;
      });

      // Measure lengths (paths are in DOM now)
      const lens = pathEls.map(p => { try { return p.getTotalLength(); } catch(_) { return 200; } });

      // Hide all
      pathEls.forEach((p, i) => {
        p.style.strokeDasharray  = String(lens[i]);
        p.style.strokeDashoffset = String(lens[i]);
      });

      // Start typewriter in sync with draw duration
      const drawDuration = pathEls.length * (520 + 28);
      this._typeVerb(subj.verb, drawDuration);

      // Draw each path
      for (let i = 0; i < pathEls.length; i++) {
        if (this._stopped) return;
        await this._animPath(pathEls[i], lens[i], true, 520);
        if (!this._stopped && i < pathEls.length - 1) await delay(28);
      }
      if (this._stopped) return;

      // Hold fully drawn
      await delay(1800);
      if (this._stopped) return;

      // Fade out verb
      this._fadeVerb();
      await delay(250);

      // Undraw in reverse
      for (let i = pathEls.length - 1; i >= 0; i--) {
        if (this._stopped) return;
        await this._animPath(pathEls[i], lens[i], false, 340);
        if (!this._stopped && i > 0) await delay(16);
      }
      if (this._stopped) return;

      // Brief blank pause before next subject
      await delay(500);
    }
  }

  // Typewrite the verb in sync with the drawing — one char per ~(drawDuration/wordLen) ms
  async _typeVerb(word, drawDuration) {
    this._verbEl.textContent = "";
    // start typing as soon as the first stroke begins
    await delay(80);
    const charInterval = Math.min(80, Math.max(30, drawDuration / (word.length + 2)));
    for (let i = 0; i <= word.length; i++) {
      if (this._stopped) return;
      // underscore cursor while typing, gone when done
      this._verbEl.textContent = word.slice(0, i) + (i < word.length ? "_" : "");
      await delay(charInterval);
    }
    for (const dot of [".", ".", "."]) {
      if (this._stopped) return;
      this._verbEl.textContent += dot;
      await delay(charInterval);
    }
  }

  _fadeVerb() {
    const el = this._verbEl;
    const t0 = performance.now();
    const dur = 280;
    const tick = now => {
      const t = Math.min((now - t0) / dur, 1);
      el.style.opacity = String(1 - t);
      if (t < 1) requestAnimationFrame(tick);
      else { el.textContent = ""; el.style.opacity = "1"; }
    };
    requestAnimationFrame(tick);
  }

  _animPath(pathEl, len, draw, duration) {
    return new Promise(resolve => {
      const from = draw ? len : 0;
      const to   = draw ? 0   : len;
      const t0 = performance.now();
      const tick = now => {
        if (this._stopped) { resolve(); return; }
        const t = Math.min((now - t0) / duration, 1);
        const s = t * t * (3 - 2 * t);
        pathEl.style.strokeDashoffset = String(lerp(from, to, s));
        if (t < 1) requestAnimationFrame(tick); else resolve();
      };
      requestAnimationFrame(tick);
    });
  }

  async stop() {
    this._stopped = true;
    // Fade svg + verb out together
    const svgEl  = this._svgEl;
    const verbEl = this._verbEl;
    const t0 = performance.now();
    const dur = 350;
    await new Promise(resolve => {
      const tick = now => {
        const t = Math.min((now - t0) / dur, 1);
        const a = 1 - t * t * (3 - 2 * t);
        svgEl.style.opacity  = String(a);
        verbEl.style.opacity = String(a);
        if (t < 1) requestAnimationFrame(tick); else resolve();
      };
      requestAnimationFrame(tick);
    });
    this._overlay.classList.add("hidden");
    this._block.classList.remove("mini");
    svgEl.style.opacity  = "1";
    verbEl.style.opacity = "1";
    verbEl.textContent   = "";
    svgEl.innerHTML      = "";
  }
}

// ── Singleton ─────────────────────────────────────────────────────────────
const _thinking = new ThinkingSketch();

// Waiting state: a pen drawing a flourish on a loop (a nib rides the stroke
// front as it's drawn, then the line retracts and redraws). Hairline black to
// match the rest of the design — it reads as "the artist is actively sketching".
let _waitRAF = null;

async function startThinking() {
  const ov = _thinking._overlay, svg = _thinking._svgEl, verb = _thinking._verbEl;
  if (_waitRAF) { cancelAnimationFrame(_waitRAF); _waitRAF = null; }

  svg.setAttribute("viewBox", "0 0 280 100");
  svg.setAttribute("width", "280");
  svg.setAttribute("height", "100");
  svg.style.overflow = "visible";
  svg.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M 18 58 C 48 14 78 14 96 50 C 112 82 140 84 158 52 C 176 20 206 18 226 48 C 240 68 256 64 262 44");
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "#111");
  path.setAttribute("stroke-width", "1.4");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);
  const nib = document.createElementNS(ns, "circle");
  nib.setAttribute("r", "2.8");
  nib.setAttribute("fill", "#111");
  nib.style.opacity = "0";
  svg.appendChild(nib);

  const len = path.getTotalLength();
  path.style.strokeDasharray = String(len);
  path.style.strokeDashoffset = String(len);

  verb.textContent = "sketching";
  ov.classList.remove("hidden");

  const DRAW = 1600, HOLD = 650, ERASE = 1050, GAP = 350;
  const CYCLE = DRAW + HOLD + ERASE + GAP;
  const ease = t => t * t * (3 - 2 * t);
  const t0 = performance.now();
  const tick = (now) => {
    const e = (now - t0) % CYCLE;
    let drawn, showNib = false;
    if (e < DRAW)                    { drawn = ease(e / DRAW) * len; showNib = true; }
    else if (e < DRAW + HOLD)        { drawn = len; }
    else if (e < DRAW + HOLD + ERASE){ drawn = (1 - ease((e - DRAW - HOLD) / ERASE)) * len; }
    else                             { drawn = 0; }
    path.style.strokeDashoffset = String(len - drawn);
    if (showNib && drawn > 0.5) {
      const p = path.getPointAtLength(drawn);
      nib.setAttribute("cx", p.x); nib.setAttribute("cy", p.y);
      nib.style.opacity = "1";
    } else {
      nib.style.opacity = "0";
    }
    _waitRAF = requestAnimationFrame(tick);
  };
  _waitRAF = requestAnimationFrame(tick);
}

async function stopThinking() {
  if (_waitRAF) { cancelAnimationFrame(_waitRAF); _waitRAF = null; }
  _thinking._overlay.classList.add("hidden");
}

// ── Acceptance moment ─────────────────────────────────────────────────────
async function showAcceptanceMoment(finalSVG) {
  transition("complete");

  // Reset canvas shift — drawing should be centred for the final display.
  document.documentElement.style.setProperty("--canvas-shift", "0px");

  // 800ms silence after last stroke.
  await delay(800);

  // Subtle brightness lift over 3 seconds.
  await animateBrightness(1.0, 1.08, 3000);

  // "done." fades in — position is fixed via CSS.
  doneLabel.textContent = "done.";
  await delay(20);
  doneLabel.classList.add("visible");

  _updateIterArrows();

  // In demo mode stay on the drawing — user navigates via chips/arrows.
  if (_demoMode) return;

  // After 4 seconds of stillness, ghost the drawing and show prompt.
  await delay(4000);

  // Snapshot the current canvas for the ghost.
  state.ghostSnapshot = ctx.getImageData(0,0,canvas.width,canvas.height);

  // Fade the canvas content to 15% — paint white overlay over the snapshot.
  const snap=state.ghostSnapshot;
  const t0=performance.now();
  await new Promise(resolve=>{
    const tick=now=>{
      const t=Math.min((now-t0)/2000,1);
      ctx.putImageData(snap,0,0);
      ctx.save();
      ctx.globalAlpha=lerp(0,0.85,t);   // 0.85 white → 15% drawing visible
      ctx.fillStyle="#ffffff";
      ctx.fillRect(0,0,canvas.width,canvas.height);
      ctx.restore();
      if(t<1) requestAnimationFrame(tick); else resolve();
    };
    requestAnimationFrame(tick);
  });

  // Reset brightness before prompt appears.
  setBrightness(1.0);

  doneLabel.classList.remove("visible");
  showPromptUIAnimated();
}

// ── Input arc draw-on animation ───────────────────────────────────────────
function drawInputFrame() {
  const arc = document.getElementById("frame-bottom");
  if (!arc) return;

  let len;
  try { len = arc.getTotalLength(); } catch(_) { len = 500; }
  arc.style.strokeDasharray  = String(len);
  arc.style.strokeDashoffset = String(len);

  async function drawArc() {
    await delay(200);
    const dur = 700;
    const t0 = performance.now();
    return new Promise(resolve => {
      const tick = now => {
        const t = Math.min((now - t0) / dur, 1);
        const s = t * t * (3 - 2 * t);
        arc.style.strokeDashoffset = String(lerp(len, 0, s));
        if (t < 1) requestAnimationFrame(tick); else resolve();
      };
      requestAnimationFrame(tick);
    });
  }
  drawArc();
}

// ── Landing + prompt UI helpers ───────────────────────────────────────────
let _landingGone = false;

// Animate a single doodle path in reverse (undraw), starting from its current offset.
function _undrawPath(pathEl, len, duration) {
  const fromOffset = parseFloat(pathEl.style.strokeDashoffset || "0");
  return new Promise(resolve => {
    const t0 = performance.now();
    const tick = now => {
      const t = Math.min((now - t0) / duration, 1);
      const s = t * t * (3 - 2 * t);
      pathEl.style.strokeDashoffset = String(lerp(fromOffset, len, s));
      if (t < 1) requestAnimationFrame(tick); else resolve();
    };
    requestAnimationFrame(tick);
  });
}

// Ripple-dismiss: doodles undraw from center outward, then landing fades.
// Returns ms until the landing layer is fully hidden (so caller can time what comes next).
// keepPromptUI=true: skip fading/hiding promptUI (used when demo chips need to stay visible).
function landingFadeOut(keepPromptUI = false) {
  const landingEl = document.getElementById("landing");

  if (!_landingGone && landingEl) {
    _landingGone = true;                   // bail all waitOrBail loops immediately

    // 1. Input: fade out only if we're not keeping it for demo chips.
    if (!keepPromptUI) {
      promptUI.classList.remove("landing-in", "fading-in");
      void promptUI.offsetWidth;
      promptUI.style.transition = "opacity 600ms ease";
      promptUI.style.opacity    = "0";
    }

    // 2. Collect doodles that have at least one visible path (dashoffset < full length).
    const doodleEls = Array.from(document.querySelectorAll(".doodle"));
    const vw = window.innerWidth / 2, vh = window.innerHeight / 2;
    const visible = doodleEls
      .map(el => {
        const paths = Array.from(el.querySelectorAll(".doodle-path"));
        const drawn = paths.filter(p => {
          const off = parseFloat(p.style.strokeDashoffset || "9999");
          const arr = parseFloat(p.style.strokeDasharray  || "0");
          return arr > 0 && off / arr < 0.85; // at least 15% drawn
        });
        if (!drawn.length) return null;
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2 - vw;
        const cy = rect.top  + rect.height / 2 - vh;
        const dist = Math.sqrt(cx * cx + cy * cy);
        return { el, paths: drawn, dist };  // only undraw the drawn paths
      })
      .filter(Boolean)
      .sort((a, b) => a.dist - b.dist);   // near → far

    // 3. Stagger undraw: 60ms between each doodle (ripple outward).
    const STAGGER   = 60;
    const UNDRAW_MS = 700;
    const lastStart = visible.length * STAGGER;

    visible.forEach(({ el, paths }, i) => {
      setTimeout(() => {
        // Undraw paths in reverse order, overlapping
        const reversedPaths = [...paths].reverse();
        reversedPaths.forEach((p, j) => {
          setTimeout(() => {
            const len = parseFloat(p.style.strokeDasharray || "0") || 400;
            _undrawPath(p, len, UNDRAW_MS);
          }, j * 40);
        });
      }, i * STAGGER);
    });

    // 4. Fade the whole landing layer once the last undraw has started + a beat.
    const fadeDelay = lastStart + 200;
    setTimeout(() => {
      landingEl.style.transition = "opacity 400ms ease";
      landingEl.style.opacity    = "0";
    }, fadeDelay);

    // 5. Hide everything after fade completes.
    const totalMs = fadeDelay + 420;
    setTimeout(() => {
      landingEl.style.display = "none";
      if (!keepPromptUI) {
        promptUI.style.transition = "";
        promptUI.classList.add("hidden");
        promptUI.style.opacity = "";
      }
    }, totalMs);

    return totalMs;

  } else {
    // Already past landing — fade prompt out then hide
    promptUI.style.transition = "opacity 300ms ease";
    promptUI.style.opacity    = "0";
    setTimeout(() => {
      promptUI.classList.add("hidden");
      promptUI.classList.remove("fading-in", "landing-in");
      promptUI.style.opacity    = "";
      promptUI.style.transition = "";
    }, 320);
    return 320;
  }
}

function showPromptUI() {
  promptInput.value="";
  promptInput.placeholder = "what should I draw?";
  if (_demoMode) { inputWrapper.classList.add("hidden"); promptUI.classList.add("demo-mode"); }
  else { inputWrapper.classList.remove("hidden"); promptUI.classList.remove("demo-mode"); }
  promptUI.classList.remove("hidden","fading-in","landing-in");
  if (_demoMode) _showDemoChips();
  else promptInput.focus();
  transition("idle");
}

function showPromptUIAnimated() {
  promptInput.value="";
  promptInput.placeholder = "what should I draw?";
  if (_demoMode) { inputWrapper.classList.add("hidden"); promptUI.classList.add("demo-mode"); }
  else { inputWrapper.classList.remove("hidden"); promptUI.classList.remove("demo-mode"); }
  promptUI.classList.remove("hidden","landing-in");
  promptUI.classList.add("fading-in");
  if (_demoMode) _showDemoChips();
  else promptInput.focus();
  transition("idle");
}

function hidePromptUI() {
  promptUI.style.opacity    = "";
  promptUI.style.transition = "";
  promptUI.classList.add("hidden");
  promptUI.classList.remove("fading-in", "landing-in");
}

let _toastTimer = null;
function showErrorToast(errorType, errorMessage) {
  const LABELS = {
    "RateLimitError":    "rate limit",
    "QuotaExceeded":     "quota exceeded",
    "GeminiError":       "api error",
    "LMStudioError":     "model error",
    "GenerationError":   "generation failed",
    "ModelSwapFailed":   "model swap failed",
    "ConnectionError":   "connection lost",
  };
  const label = LABELS[errorType] || errorType?.toLowerCase().replace("error","").trim() || "error";
  const detail = errorMessage
    ? String(errorMessage).slice(0, 80).toLowerCase().replace(/\.$/, "")
    : "";
  errorToast.textContent = detail ? `${label} — ${detail}` : label;
  errorToast.classList.add("visible");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { errorToast.classList.remove("visible"); }, 6000);
}

function showError(msg) {
  if(state.activeAnimator)   { state.activeAnimator.cancel();  state.activeAnimator=null; }
  if(state.currentGaze)      { state.currentGaze.stop(null);   state.currentGaze=null; }
  if(state.currentAnnotation){ state.currentAnnotation.hide(); state.currentAnnotation=null; }
  stopThinking();
  setStateLabel("");

  // Fade canvas to 30%.
  const snap=ctx.getImageData(0,0,canvas.width,canvas.height);
  ctx.putImageData(snap,0,0);
  ctx.save();
  ctx.globalAlpha=0.70; ctx.fillStyle="#ffffff";
  ctx.fillRect(0,0,canvas.width,canvas.height);
  ctx.restore();

  doneLabel.classList.remove("visible");
  cancelNextClick();
  _hideIterArrows();
  iterNav.classList.remove("visible");
  setBrightness(1.0);
  document.documentElement.style.setProperty("--canvas-shift", "0px");
  promptInput.placeholder = msg || "what should I draw?"; promptInput.value="";
  showPromptUI();
}

// ── SSE event wiring ──────────────────────────────────────────────────────
let _chain = Promise.resolve();

function handleEvent(ev) {
  const {event:name, payload} = ev;
  console.log(`[evt] ${name}`, payload);

  switch(name) {

    case "iteration_start": {
      const idx = payload.index??0;
      state.currentIteration=idx;
      state.totalIterations =payload.total??state.totalIterations;
      const rec = _iterRec(idx);
      rec.phase = "drawing";
      if (payload.recorded) rec.recorded = true;
      _panelStartIterTimer(idx);
      panelSetLive(idx, "drawing");
      break;
    }

    // Events only POPULATE per-iteration records (the loop runs to completion in
    // the background). The canvas shows whichever iteration the user is viewing;
    // it is never auto-advanced — see _viewIter / the panel arrows.
    case "generation_done": {
      const svg=payload.svg??null, steps=payload.steps??[], idx=state.currentIteration;
      const rec=_iterRec(idx);
      rec.svg=svg; rec.steps=steps; rec.artistSeconds=payload.elapsed_seconds??rec.artistSeconds;
      if (payload.recorded) rec.recorded = true;
      if (!svg) break;
      state.iterHistory[idx] = svg;
      _maxReady = Math.max(_maxReady, idx);
      _panelRenderChips();
      if (_panelPendingTarget === idx) {
        _panelPendingTarget = null;
        _viewIter(idx, { force: true });
        break;
      }
      // Auto-show the very first iteration so something appears without a click.
      // loading:true gives it the same artist "thinking" wait (>= 3s) and the
      // "artist is drawing" beam; initial:true suppresses the iteration-change
      // flash (the panel has only just appeared, so there's nothing to flip from).
      if (!_shownAny) {
        _shownAny = true;
        // Continue the clock from run start (no reset to 0) so it counts smoothly
        // through the artist wait, drawing and critique until feedback appears.
        _panelStartDisplayTimer(0, _panelDrawWaitStart || performance.now());
        _viewIter(0, { loading: true, initial: true });
      }
      else if (idx === _panelSel) _panelRenderDetail(idx);
      break;
    }

    case "render_done": break;

    case "critique_start": {
      const idx=state.currentIteration;
      const rec = _iterRec(idx);
      rec.phase="critiquing";
      if (payload.recorded) rec.recorded = true;
      panelSetLive(idx,"critiquing");
      break;
    }

    case "critique_done": {
      const {feedback_for_artist,verdict}=payload;
      const idx=state.currentIteration, rec=_iterRec(idx);
      rec.score=payload.score??rec.score;
      rec.verdict=verdict??rec.verdict;
      rec.uiMessage=payload.ui_message||rec.uiMessage;
      rec.feedback=feedback_for_artist||rec.feedback;
      rec.reasoning=payload.reasoning||rec.reasoning;
      rec.observations=payload.observations||rec.observations;
      rec.criticSeconds=payload.elapsed_seconds??rec.criticSeconds;
      if (payload.recorded) rec.recorded = true;
      rec.phase="done";
      _maxReady = Math.max(_maxReady, idx);
      _panelStopIterTimer(idx);
      panelSetLive(idx,"done");
      break;
    }

    case "iteration_end": {
      const d = payload.iteration_dict || {};
      const idx = d.index ?? state.currentIteration;
      const rec = _iterRec(idx);
      if (d.svg) { rec.svg = d.svg; state.iterHistory[idx] = d.svg; }
      if (d.critic_score != null) rec.score = d.critic_score;
      if (d.critic_verdict) rec.verdict = d.critic_verdict;
      if (d.critic_ui_message || d.ui_message) rec.uiMessage = d.critic_ui_message || d.ui_message;
      if (d.critic_feedback) rec.feedback = d.critic_feedback;
      if (d.critic_reasoning) rec.reasoning = d.critic_reasoning;
      if (d.critic_observations) rec.observations = d.critic_observations;
      if (d.artist_seconds != null) rec.artistSeconds = d.artist_seconds;
      if (d.critic_seconds != null) rec.criticSeconds = d.critic_seconds;
      if (d.elapsed_seconds != null) rec.totalSeconds = d.elapsed_seconds;
      if (payload.recorded || d.recorded) rec.recorded = true;
      rec.phase = "done";
      _maxReady = Math.max(_maxReady, idx);
      _panelStopIterTimer(idx);
      if (_panelPendingTarget === idx && rec.svg) {
        _panelPendingTarget = null;
        _viewIter(idx, { force: true });
        break;
      }
      panelSetLive(idx, "done");
      break;
    }

    case "loop_complete": {
      state.finalSVG=payload.final_svg??state.currentSVG;
      panelEnterBrowse();
      break;
    }

    case "iteration_error":
      console.error("[iteration_error]",payload);
      showErrorToast(payload?.error_type, payload?.error_message);
      break;

    case "stream_error":
      console.error("[stream_error]",payload);
      _panelPendingTarget = null;
      _panelViewBusy = false;
      _panelRenderChips();
      _chain=_chain.then(async()=>{ await stopThinking(); setStateLabel(""); showErrorToast(payload?.error_type || "stream_error", payload?.message); showError("something went wrong"); });
      break;
  }
}

// ── SSE connection ────────────────────────────────────────────────────────
function startGeneration(prompt) {
  abortDemo();
  hidePromptUI();
  const dismissMs = landingFadeOut();
  transition("preparing");
  state.currentIteration=0; state.currentSVG=null; state.previousSVG=null;
  state.finalSVG=null; state.stepBoxes=[]; state.currentGaze=null;
  state.currentAnnotation=null; state.ghostSnapshot=null;
  state.iterHistory=[]; state.viewingIter=0;
  state.iterRecords=[]; state.runPrompt=prompt; state.runStartMs=performance.now();
  _hideIterArrows();
  panelStartRun(prompt);
  _chain=Promise.resolve();

  // If re-generating over a ghost, fade it out first.
  const snap=ctx.getImageData(0,0,canvas.width,canvas.height);
  const t0=performance.now();
  const fadeOut=(now)=>{
    const t=Math.min((now-t0)/1000,1);
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.save(); ctx.globalAlpha=1-t; ctx.putImageData(snap,0,0); ctx.restore();
    if(t<1) requestAnimationFrame(fadeOut);
  };
  requestAnimationFrame(fadeOut);

  setBrightness(1.0);
  document.documentElement.style.setProperty("--canvas-shift", "0px");
  doneLabel.classList.remove("visible");
  cancelNextClick();
  errorToast.classList.remove("visible");
  if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
  iterNav.classList.remove("visible");
  buildIterationDots(state.totalIterations);

  const params = new URLSearchParams({
    prompt,
    max_iterations: String(SELECTED_ITERATIONS),
    backend: SELECTED_BACKEND,
  });
  const src=new EventSource(`${API_BASE}/generate?${params.toString()}`);
  src.onmessage=msg=>{
    let p; try{p=JSON.parse(msg.data);}catch(e){return;}
    handleEvent(p);
    if(p.event==="loop_complete"||p.event==="stream_error") src.close();
  };
  src.onerror=()=>{
    src.close();
    _chain=_chain.then(()=>showError("connection lost"));
  };
}

// ── Input ─────────────────────────────────────────────────────────────────
function submitPrompt() {
  const t = promptInput.value.trim(); if (!t) return;
  if (_demoMode) {
    startDemoGeneration(t);
  } else {
    startGeneration(t);
  }
}

function startDemoGeneration(prompt) {
  const dismissMs = landingFadeOut();
  transition("preparing");
  state.currentIteration=0; state.currentSVG=null; state.previousSVG=null;
  state.finalSVG=null; state.stepBoxes=[]; state.currentGaze=null;
  state.currentAnnotation=null; state.ghostSnapshot=null;
  state.iterHistory=[]; state.viewingIter=0;
  state.iterRecords=[]; state.runPrompt=prompt; state.runStartMs=performance.now();
  _hideIterArrows();
  panelStartRun(prompt);
  _chain=Promise.resolve();

  const snap=ctx.getImageData(0,0,canvas.width,canvas.height);
  const t0=performance.now();
  const fadeOut=(now)=>{
    const t=Math.min((now-t0)/1000,1);
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.save(); ctx.globalAlpha=1-t; ctx.putImageData(snap,0,0); ctx.restore();
    if(t<1) requestAnimationFrame(fadeOut);
  };
  requestAnimationFrame(fadeOut);

  setBrightness(1.0);
  document.documentElement.style.setProperty("--canvas-shift", "0px");
  doneLabel.classList.remove("visible");
  cancelNextClick();
  errorToast.classList.remove("visible");
  if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
  iterNav.classList.remove("visible");
  buildIterationDots(5); // will be corrected to actual total by first iteration_start

  delay(dismissMs + 300).then(() => runDemo(prompt));
}

promptInput.addEventListener("keydown", e=>{ if(e.key==="Enter") submitPrompt(); });

// ── Landing doodle animation (each doodle independent, zone-based placement) ─
function initLandingDoodles() {
  if (!document.getElementById("landing")) return;

  const doodleEls = Array.from(document.querySelectorAll(".doodle"));

  const doodleData = doodleEls.map(el => {
    const paths = Array.from(el.querySelectorAll(".doodle-path"));
    const lens  = paths.map(p => { try { return p.getTotalLength(); } catch(_) { return 400; } });
    const svgEl = el.querySelector("svg");
    const w = svgEl ? svgEl.width.baseVal.value  : 120;
    const h = svgEl ? svgEl.height.baseVal.value : 120;
    return { el, paths, lens, w, h };
  });

  // Hide all paths initially
  doodleData.forEach(({ paths, lens }) =>
    paths.forEach((p, i) => {
      p.style.strokeDasharray  = String(lens[i]);
      p.style.strokeDashoffset = String(lens[i]);
    })
  );

  // Animate one path
  function animPath(pathEl, len, draw, duration) {
    return new Promise(resolve => {
      const from = draw ? len : 0, to = draw ? 0 : len;
      const t0 = performance.now();
      const tick = now => {
        if (_landingGone) { resolve(); return; }
        const t = Math.min((now - t0) / duration, 1);
        const s = t * t * (3 - 2 * t);
        pathEl.style.strokeDashoffset = String(lerp(from, to, s));
        if (t < 1) requestAnimationFrame(tick); else resolve();
      };
      requestAnimationFrame(tick);
    });
  }

  // 20 hand-placed anchors. Every pair is ≥0.26 apart in weighted (dx, dy*0.45) distance.
  // Avoids the center band (cx 0.28–0.72, cy 0.36–0.64) where the input lives.
  const ANCHORS = [
    { cx: 0.06, cy: 0.08 },  //  0 — top-left
    { cx: 0.36, cy: 0.07 },  //  1 — top, left-of-center
    { cx: 0.68, cy: 0.09 },  //  2 — top, right-of-center
    { cx: 0.93, cy: 0.10 },  //  3 — top-right
    { cx: 0.18, cy: 0.28 },
    { cx: 0.52, cy: 0.22 },
    { cx: 0.80, cy: 0.26 },
    { cx: 0.05, cy: 0.52 },
    { cx: 0.93, cy: 0.48 },
    { cx: 0.22, cy: 0.72 },
    { cx: 0.52, cy: 0.78 },
    { cx: 0.80, cy: 0.72 },
    { cx: 0.07, cy: 0.85 },
    { cx: 0.38, cy: 0.90 },
    { cx: 0.68, cy: 0.88 },
    { cx: 0.93, cy: 0.84 },
    { cx: 0.27, cy: 0.46 },
    { cx: 0.75, cy: 0.44 },
    { cx: 0.14, cy: 0.62 },
    { cx: 0.86, cy: 0.66 },
    // Extra slots so doodles wander — ~10 always free, giving each cycle a new location
    { cx: 0.42, cy: 0.12 },
    { cx: 0.72, cy: 0.14 },
    { cx: 0.08, cy: 0.20 },
    { cx: 0.62, cy: 0.34 },
    { cx: 0.30, cy: 0.78 },
    { cx: 0.88, cy: 0.30 },
    { cx: 0.10, cy: 0.38 },
    { cx: 0.90, cy: 0.18 },
    { cx: 0.48, cy: 0.92 },
    { cx: 0.20, cy: 0.10 },
    { cx: 0.78, cy: 0.90 },
    { cx: 0.55, cy: 0.68 },
    { cx: 0.25, cy: 0.90 },
    { cx: 0.92, cy: 0.62 },
  ];

  // Each slot is either free or occupied. A doodle must claim a free slot before drawing.
  const freeSlots = new Set(ANCHORS.map((_, i) => i));

  function slotPosition({ w, h }, slotIdx) {
    const anchor = ANCHORS[slotIdx];
    const vw = window.innerWidth, vh = window.innerHeight;
    const pad = 20;
    const jx = (Math.random() * 2 - 1) * 0.025 * vw;
    const jy = (Math.random() * 2 - 1) * 0.025 * vh;
    const left = Math.max(pad, Math.min(vw - w - pad, anchor.cx * vw - w / 2 + jx));
    const top  = Math.max(pad, Math.min(vh - h - pad, anchor.cy * vh - h / 2 + jy));
    const rot  = (Math.random() * 22 - 11).toFixed(1);
    return { left, top, rot };
  }

  // Resolve after ms or immediately if _landingGone
  function waitOrBail(ms) {
    return new Promise(resolve => {
      if (_landingGone) { resolve(); return; }
      const id = setTimeout(resolve, ms);
      const poll = setInterval(() => {
        if (_landingGone) { clearTimeout(id); clearInterval(poll); resolve(); }
      }, 40);
      setTimeout(() => clearInterval(poll), ms + 60);
    });
  }

  let inputShown = false;

  // Per-doodle speed/timing variation so cycles never synchronise
  const SPEED_FACTORS = [0.7, 1.0, 1.3, 0.85, 1.15, 0.6, 1.25, 0.95, 1.1, 0.75, 1.4, 0.9, 0.65, 1.2, 0.8, 1.05, 0.72, 1.18, 0.88, 1.35, 0.78, 1.08, 0.92, 1.22, 0.68, 1.12, 0.82, 1.16, 0.74, 1.28, 0.96, 0.66, 1.20, 0.84];
  const HOLD_OFFSETS  = [0, 400, 800, 200, 1100, 550, 150, 950, 350, 750, 125, 900, 300, 1050, 475, 650, 225, 850, 425, 1000, 275, 700, 375, 975, 175, 800, 450, 600, 250, 900, 500, 350, 700, 150];
  const PAUSE_OFFSETS = [0, 300, 100, 700, 250, 500, 400, 150, 550, 225, 450, 75, 375, 175, 625, 325, 460, 90, 525, 285, 210, 440, 120, 550, 340, 160, 480, 220, 560, 280, 400, 130, 490, 230];

  async function doodleLoop(d, idx) {
    const spd   = SPEED_FACTORS[idx] ?? 1.0;
    const holdX = HOLD_OFFSETS[idx]  ?? 0;
    const pauX  = PAUSE_OFFSETS[idx] ?? 0;

    while (!_landingGone) {
      // Wait until a free slot is available
      while (freeSlots.size === 0 && !_landingGone) await waitOrBail(200);
      if (_landingGone) return;

      // Claim a random free slot (avoid center zone)
      const candidates = [...freeSlots].filter(i => {
        const a = ANCHORS[i];
        return !(a.cx > 0.28 && a.cx < 0.72 && a.cy > 0.36 && a.cy < 0.64);
      });
      if (candidates.length === 0) { await waitOrBail(300); continue; }
      const slotIdx = candidates[Math.floor(Math.random() * candidates.length)];
      freeSlots.delete(slotIdx); // claim it — no other doodle can use this slot now

      const pos = slotPosition(d, slotIdx);
      d.el.style.left      = pos.left + "px";
      d.el.style.top       = pos.top  + "px";
      d.el.style.transform = `rotate(${pos.rot}deg)`;

      // Draw
      const pathDur = (1800 + Math.random() * 1200) * spd;
      for (let i = 0; i < d.paths.length; i++) {
        if (_landingGone) { freeSlots.add(slotIdx); return; }
        await animPath(d.paths[i], d.lens[i], true, pathDur);
        if (i < d.paths.length - 1 && !_landingGone) await waitOrBail(60 + Math.random() * 80);
      }
      if (_landingGone) { freeSlots.add(slotIdx); return; }

      // Reveal input after first doodle completes
      if (!inputShown) {
        inputShown = true;
        promptUI.classList.remove("hidden");
        promptUI.classList.add("landing-in");
        promptInput.focus();
        drawInputFrame();
      }

      // Hold
      await waitOrBail(200 + Math.random() * 400 + holdX * 0.05);
      if (_landingGone) { freeSlots.add(slotIdx); return; }

      // Erase
      for (let i = d.paths.length - 1; i >= 0; i--) {
        if (_landingGone) { freeSlots.add(slotIdx); return; }
        const dur = (600 + Math.random() * 400) * spd;
        await animPath(d.paths[i], d.lens[i], false, dur);
        if (i > 0 && !_landingGone) await waitOrBail(10 + Math.random() * 20);
      }
      if (_landingGone) { freeSlots.add(slotIdx); return; }

      // Release slot — another doodle can now use this location
      freeSlots.add(slotIdx);

      // Pause before next cycle
      await waitOrBail(300 + Math.random() * 900 + pauX * 0.15);
    }
  }

  // Map each doodle (by DOM order) to an anchor that's spread across the whole screen.
  // Burst doodles (first 8, launching 0–2s) get one anchor per screen region so they
  // fill all four quadrants immediately. Remaining 12 fill the gaps.
  //
  // Anchor index layout reminder (cx, cy fractions):
  //  0=TL  1=TC-L  2=TC-R  3=TR    — top strip
  //  4=UL  5=UM    6=UR    7=R-up  — upper-mid
  //  8=L-m 9=LC   10=RC  11=R-lo  — mid (flanking input)
  // 12=L-b 13=BL  14=BM  15=BR   16=bot-L 17=bot-CL 18=bot-CR 19=bot-R
  //
  // Burst 8 picks: TL, TR, lower-left, lower-right, upper-mid, bottom-center, left-mid, right-mid
  // Burst 18 for instant full-screen population; remaining 16 trickle in.
  // All 34 doodles launch immediately with a tiny stagger. The freeSlots set is the
  // sole gating mechanism — a doodle that can't find a free slot simply waits.
  doodleData.forEach((d, i) => {
    delay(i * 80).then(() => { if (!_landingGone) doodleLoop(d, i); });
  });
}

// ── Thesis landing controller ──────────────────────────────────────────────
// Selected iteration count from the landing stepper, sent to /generate.
let SELECTED_ITERATIONS = 4;
let DEPLOYMENT_PROFILE = (window.DEPLOYMENT_PROFILE || "local");
// Seed the backend/profile-dependent defaults from the deployment profile so the
// first paint (before /config resolves) already matches hosted vs local. Otherwise
// a hosted page briefly shows local-only UI (the "Ollama runtime" model box) during
// the Render cold start until /config comes back.
let SELECTED_BACKEND = DEPLOYMENT_PROFILE === "hosted" ? "gemini" : "local";

(function initThesisLanding() {
  const screen      = document.getElementById("thesis-landing");
  if (!screen) { initLandingDoodles(); return; }

  const lobbyIntro  = document.getElementById("tl-lobby-intro");
  const iterValueEl = document.getElementById("tl-iter-value");
  const iterMetaEl  = document.getElementById("tl-meta-iterations");
  const backendMetaEl = document.getElementById("tl-meta-backend");
  const modeMetaEl  = document.getElementById("tl-meta-mode");
  const iterMinus   = document.getElementById("tl-iter-minus");
  const iterPlus    = document.getElementById("tl-iter-plus");
  const beginBtn    = document.getElementById("tl-begin");
  const promptField = document.getElementById("tl-prompt");
  const demoOptions = document.getElementById("tl-demo-options");
  const demoOptList = document.getElementById("tl-demo-options-list");
  const statusDot   = document.getElementById("tl-status-dot");
  const statusText  = document.getElementById("tl-status-text");
  const backendParam = document.getElementById("tl-param-backend");
  const liveModeBtn     = document.getElementById("tl-mode-live");
  const recordedModeBtn = document.getElementById("tl-mode-recorded");
  const livePanel       = document.getElementById("tl-live-panel");
  const recordedPanel   = document.getElementById("tl-recorded-panel");
  const modelStatus = document.getElementById("tl-model-status");
  const setupText = document.getElementById("tl-repro-setup");
  const setupCode = document.getElementById("tl-repro-code");
  const setupMore = document.getElementById("tl-repro-more");
  const reproNote = document.querySelector(".tl-repro-note");
  const backendButtons = Array.from(document.querySelectorAll(".tl-backend-option"));
  const bridgeState = document.getElementById("tl-bridge-state");
  const bridgeStatus = document.getElementById("tl-bridge-status");
  const bridgeDetail = document.getElementById("tl-bridge-detail");
  const bridgeRetry = document.getElementById("tl-bridge-retry");

  let iterMin = 1, iterMax = 8;
  let backendOptions = {
    local:  { id: "local", label: "local", available: false, reason: "checking local bridge" },
    gemini: { id: "gemini", label: "cloud", available: false, reason: "waiting for API config" },
  };
  let featureFlags = {
    live: true,
    recorded: false,
    backend_picker: false,
    // Model names (the "Ollama runtime" box) are a local-only affordance. Default
    // to the profile so a hosted page never flashes it during the cold start.
    show_model_names: DEPLOYMENT_PROFILE === "local",
  };
  let runtimeInfo = {};
  let cloudModelName = "";

  function initPaperMotion() {
    const reduceMotion = window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // Looping in-view animations. The first diagram can be partly visible on
    // initial load, so it uses a lighter threshold than later sections.
    const diagrams = Array.from(document.querySelectorAll(".tl-diagram"));
    if (diagrams.length && ("IntersectionObserver" in window)) {
      // threshold 0 so a tall diagram appears the moment any part of it enters the
      // viewport (16% of a large diagram may never fit on screen at once).
      const diagramObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          entry.target.classList.toggle("is-visible", entry.isIntersecting);
        });
      }, { threshold: 0, rootMargin: "0px" });
      diagrams.forEach(el => {
        diagramObserver.observe(el);
        // Reveal immediately if it's already on screen at load — the observer's
        // first callback can otherwise leave an in-view diagram hidden until the
        // first scroll nudges it.
        const rect = el.getBoundingClientRect();
        if (rect.top < window.innerHeight && rect.bottom > 0) el.classList.add("is-visible");
      });
    } else {
      diagrams.forEach(el => el.classList.add("is-visible"));
    }

    const algorithms = Array.from(document.querySelectorAll(".tl-algorithm"));
    if (algorithms.length && ("IntersectionObserver" in window)) {
      const algorithmObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          entry.target.classList.toggle("is-visible", entry.isIntersecting);
        });
      }, { threshold: 0.5, rootMargin: "0px 0px -20% 0px" });
      algorithms.forEach(el => algorithmObserver.observe(el));
    } else {
      algorithms.forEach(el => el.classList.add("is-visible"));
    }

    const evalPlots = Array.from(document.querySelectorAll(".tl-eval-plot-wrap"));
    if (evalPlots.length && ("IntersectionObserver" in window)) {
      const evalObserver = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          const figure = entry.target.closest(".tl-eval-figure");
          if (!figure) return;

          if (entry.isIntersecting) {
            figure.classList.remove("is-visible");
            void figure.offsetWidth;
            figure.classList.add("is-visible");
          } else {
            figure.classList.remove("is-visible");
          }
        });
      }, { threshold: 0.52, rootMargin: "0px 0px -22% 0px" });
      evalPlots.forEach(el => evalObserver.observe(el));
    } else {
      document.querySelectorAll(".tl-eval-figure").forEach(el => el.classList.add("is-visible"));
    }

    const systemDiagrams = Array.from(document.querySelectorAll(".tl-diagram--system"));
    if (!reduceMotion && systemDiagrams.length) {
      systemDiagrams.forEach((diagram, index) => {
        let phase = (index % 4) + 1;
        diagram.dataset.phase = String(phase);
        window.setInterval(() => {
          if (!diagram.classList.contains("is-visible")) return;
          phase = phase >= 4 ? 1 : phase + 1;
          diagram.dataset.phase = String(phase);
        }, 2200);
      });
    }

    // One-shot scroll-reveal: each block rises into view as it's scrolled to.
    // The hero is left alone — the sheet itself animates in around it.
    const revealSel = [
      ".tl-section-label", ".tl-agents", ".tl-loop", ".tl-example-lead",
      ".tl-example-figure", ".tl-result", ".tl-references", ".tl-lobby",
      ".tl-live-cell", ".tl-listing",
      ".tl-figure-note",
    ].join(",");
    const blocks = Array.from(document.querySelectorAll(revealSel));
    if (!blocks.length) return;
    if (reduceMotion || !("IntersectionObserver" in window)) return; // visible by default

    blocks.forEach(el => el.classList.add("tl-reveal"));
    const revealObs = new IntersectionObserver((entries, obs) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-revealed");
          obs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.24, rootMargin: "0px 0px -16% 0px" });
    blocks.forEach(el => {
      // Reveal immediately if already in the visible zone at load time.
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight * 0.84) {
        el.classList.add("is-revealed");
      } else {
        revealObs.observe(el);
      }
    });
  }
  initPaperMotion();

  // ── Worked example (section 3): draw a representative iteration stroke by stroke
  //    on a loop, with critic feedback beside it. ────────────────────────────
  let _doodleAnimStop = false;
  (function animateExample() {
    const exSvg = document.getElementById("tl-ex-svg");
    if (!exSvg || typeof DEMO_PROMPTS === "undefined") return;

    // A curated mid-loop state for the landing illustration. It keeps the
    // output thesis-like while still being a subject the Artist would draw.
    const iter = {
      score: 8,
      verdict: "revise",
      feedback_for_artist:
        "This is a charming doodle cat: the tall curled tail, the long simple body, " +
        "the small eared head, the dot eyes and whiskers, and the short legs all read " +
        "clearly with a confident hand-drawn line. The main remaining issue is " +
        "proportion — the head sits a touch high and the four legs could be spaced " +
        "more evenly along the belly.",
      svg:
        '<svg viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">' +
        // reference-style doodle cat: tall curled tail on the left, long organic
        // body, small eared head on the right, dot eyes + whiskers, short legs.
        // tail — tall hook curling over at the top
        '<path d="M152 290 C148 250 146 212 152 188 C156 170 178 168 188 182 ' +
        'C196 193 188 208 176 205 C169 203 168 193 173 188"/>' +
        // back — top line running to the head
        '<path d="M152 290 C200 270 252 266 300 270"/>' +
        // head outline
        '<path d="M300 270 C298 242 314 224 338 224 C362 224 378 242 376 266 ' +
        'C374 290 358 302 336 300"/>' +
        // ears
        '<path d="M316 228 L312 206 L330 222"/>' +
        '<path d="M350 222 L366 204 L368 228"/>' +
        // chest down to belly
        '<path d="M336 300 C337 316 336 332 334 344"/>' +
        // belly
        '<path d="M334 344 C280 351 205 351 158 344"/>' +
        // rear, closing up to the tail base
        '<path d="M158 344 C152 322 150 305 152 290"/>' +
        // four short legs
        '<path d="M196 345 C196 353 196 361 198 367"/>' +
        '<path d="M224 346 C224 354 224 361 226 367"/>' +
        '<path d="M300 346 C300 354 300 361 302 367"/>' +
        '<path d="M328 345 C328 353 328 360 330 366"/>' +
        // dot eyes
        '<path d="M328 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0"/>' +
        '<path d="M352 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0"/>' +
        // nose / mouth
        '<path d="M340 268 C343 272 349 272 352 268"/>' +
        // whiskers
        '<path d="M372 262 L396 258 M374 270 L396 271 M373 278 L395 283"/>' +
        '</svg>',
    };

    document.getElementById("tl-ex-score").textContent   = String(iter.score);
    document.getElementById("tl-ex-verdict").textContent = iter.verdict;
    document.getElementById("tl-ex-feedback").textContent = iter.feedback_for_artist;

    // Pull the path elements out of the curated SVG string into our inline svg.
    const doc = new DOMParser().parseFromString(iter.svg, "image/svg+xml");
    const srcPaths = Array.from(doc.querySelectorAll("path"));
    const srcLabels = Array.from(doc.querySelectorAll("text"));
    exSvg.innerHTML = "";
    exSvg.setAttribute("viewBox", "120 164 300 210");
    srcLabels.forEach(label => exSvg.appendChild(label.cloneNode(true)));
    const artworkTransform = "translate(0 0)";
    const paths = srcPaths.map(sp => {
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("class", "tl-example-stroke");
      p.setAttribute("d", sp.getAttribute("d"));
      p.setAttribute("transform", artworkTransform);
      exSvg.appendChild(p);
      return p;
    });
    const lens = paths.map(p => { try { return p.getTotalLength(); } catch (_) { return 200; } });
    paths.forEach((p, i) => { p.style.strokeDasharray = String(lens[i]); p.style.strokeDashoffset = String(lens[i]); });

    let exampleRunning = false;
    let exampleToken = 0;
    const lerp = (a, b, t) => a + (b - a) * t;
    const wait = ms => new Promise(r => setTimeout(r, ms));
    function resetExample() {
      paths.forEach((p, i) => { p.style.strokeDashoffset = String(lens[i]); });
    }
    function animPath(p, len, draw, duration, token) {
      return new Promise(resolve => {
        const from = draw ? len : 0, to = draw ? 0 : len, t0 = performance.now();
        const tick = now => {
          if (_doodleAnimStop || !exampleRunning || token !== exampleToken) { resolve(); return; }
          const t = Math.min((now - t0) / duration, 1);
          const s = t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
          p.style.strokeDashoffset = String(lerp(from, to, s));
          if (t < 1) requestAnimationFrame(tick); else resolve();
        };
        requestAnimationFrame(tick);
      });
    }
    async function loop(token) {
      while (!_doodleAnimStop && exampleRunning && token === exampleToken) {
        // Draw stroke by stroke with restrained figure-like pacing.
        for (let i = 0; i < paths.length; i++) {
          const drawMs = Math.max(660, Math.min(2200, lens[i] * 7.4));
          await animPath(paths[i], lens[i], true, drawMs, token);
          if (!exampleRunning || token !== exampleToken) return;
          if (i < paths.length - 1) await wait(150);
        }
        await wait(1500);
        if (_doodleAnimStop || !exampleRunning || token !== exampleToken) return;
        // Undraw in reverse, still slow enough to read as a plotted trace.
        for (let i = paths.length - 1; i >= 0; i--) {
          await animPath(paths[i], lens[i], false, Math.max(460, Math.min(1200, lens[i] * 4.3)), token);
          if (!exampleRunning || token !== exampleToken) return;
          if (i > 0) await wait(74);
        }
        await wait(320);
      }
    }

    let _exStartTimer = null;

    function startExample() {
      if (_exStartTimer) return;
      _exStartTimer = setTimeout(() => {
        _exStartTimer = null;
        if (exampleRunning) return;
        exampleRunning = true;
        exampleToken++;
        resetExample();
        loop(exampleToken);
      }, 1000);
    }

    function stopExample() {
      if (_exStartTimer) { clearTimeout(_exStartTimer); _exStartTimer = null; }
      exampleRunning = false;
      exampleToken++;
      resetExample();
    }

    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) startExample();
          else stopExample();
        });
      }, { threshold: 0.5, rootMargin: "0px 0px 0px 0px" });
      observer.observe(exSvg);
    } else {
      startExample();
    }
  })();

  function renderProfileCopy() {
    if (DEPLOYMENT_PROFILE === "hosted") {
      if (lobbyIntro) {
        lobbyIntro.innerHTML =
          'This page also serves as a small reproduction cell. <em>Recorded</em> runs ' +
          'replay instantly with no setup; <em>live</em> runs stream stroke by stroke ' +
          'from the inference endpoint.';
      }
      if (setupText) setupText.classList.add("hidden");
      if (setupCode) setupCode.classList.add("hidden");
      if (setupMore) {
        const model = cloudModelName ? "<code>" + htmlEscape(cloudModelName) + "</code>" : "a lightweight cloud model";
        setupMore.innerHTML =
          'Live cloud runs use ' + model + ', chosen for fast inference, so the drawings ' +
          'are noticeably weaker than the larger models used in the thesis&#8217;s local ' +
          'experiments. For full-quality local inference, see the ' +
          '<a href="https://github.com/madalinioana/learn-to-draw-step-by-step" target="_blank" rel="noreferrer">setup notes&nbsp;&nearr;</a>.';
        setupMore.classList.remove("hidden");
      }
      if (reproNote) reproNote.classList.add("hidden");
    } else {
      if (lobbyIntro) {
        lobbyIntro.innerHTML =
          'This page also serves as a local reproduction cell. <em>Live</em> runs ' +
          'stream stroke by stroke from your local Ollama backend.';
      }
      if (setupText) {
        setupText.textContent = "Local reproduction command:";
        setupText.classList.remove("hidden");
      }
      if (setupCode) {
        setupCode.innerHTML =
          'make local ARTIST=gemma3:27b CRITIC=blaifa/InternVL3_5:8b';
        setupCode.classList.remove("hidden");
      }
      if (setupMore) {
        setupMore.innerHTML =
          'Full setup is in the ' +
          '<a href="https://github.com/madalinioana/learn-to-draw-step-by-step" target="_blank" rel="noreferrer">setup notes&nbsp;&nearr;</a>.';
        setupMore.classList.remove("hidden");
      }
      if (reproNote) reproNote.classList.remove("hidden");
    }
  }

  function renderFeatureFlags() {
    if (backendParam) backendParam.classList.toggle("hidden", !featureFlags.backend_picker);
    if (recordedModeBtn) recordedModeBtn.classList.toggle("hidden", !featureFlags.recorded);
    if (!featureFlags.recorded) {
      setMode("live");
      if (modeMetaEl) modeMetaEl.textContent = "live";
    }
    renderProfileCopy();
  }

  function htmlEscape(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function localRuntimeModels() {
    return {
      artist: runtimeInfo.artist_model || (backendOptions.local && backendOptions.local.artist && backendOptions.local.artist.model) || "not configured",
      critic: runtimeInfo.critic_model || (backendOptions.local && backendOptions.local.critic && backendOptions.local.critic.model) || "not configured",
    };
  }

  function renderLocalCommand() {
    if (!setupCode) return;
    const models = localRuntimeModels();
    setupCode.innerHTML =
      `make local ARTIST=${htmlEscape(models.artist)} CRITIC=${htmlEscape(models.critic)}`;
  }

  function renderLocalModelStatus() {
    if (!modelStatus) return;
    if (!featureFlags.show_model_names || SELECTED_BACKEND !== "local") {
      modelStatus.classList.add("hidden");
      modelStatus.innerHTML = "";
      return;
    }

    const { artist, critic } = localRuntimeModels();
    const loaded = Array.isArray(runtimeInfo.loaded_models) ? runtimeInfo.loaded_models : [];
    const selected = selectedBackendOption();
    const status = selected && selected.available === false && selected.reason
      ? `<div class="tl-runtime-loaded">local backend note: ${htmlEscape(selected.reason)}</div>`
      : "";
    const loadedSummary = loaded.length
      ? `${loaded.length} model${loaded.length === 1 ? "" : "s"} available`
      : "no models reported";
    const listId = "tl-runtime-list";

    modelStatus.innerHTML = `
      <div class="tl-runtime">
        <p class="tl-runtime-title">Ollama runtime</p>
        <pre class="tl-runtime-code">"artist": "${htmlEscape(artist)}",
"critic": "${htmlEscape(critic)}"</pre>
        <div class="tl-runtime-loaded">
          ${htmlEscape(loadedSummary)}
          ${loaded.length ? `<button class="tl-model-toggle" type="button" aria-expanded="false" aria-controls="${listId}">show list</button>` : ""}
        </div>
        ${loaded.length ? `<div class="tl-runtime-list hidden" id="${listId}">${loaded.map(htmlEscape).join(", ")}</div>` : ""}
        ${status}
      </div>
    `;
    const toggle = modelStatus.querySelector(".tl-model-toggle");
    const list = modelStatus.querySelector(".tl-runtime-list");
    if (toggle && list) {
      toggle.addEventListener("click", () => {
        const opening = list.classList.contains("hidden");
        list.classList.toggle("hidden", !opening);
        if (opening) {
          list.classList.add("revealing");
          window.setTimeout(() => list.classList.remove("revealing"), 420);
        }
        toggle.textContent = opening ? "hide list" : "show list";
        toggle.setAttribute("aria-expanded", opening ? "true" : "false");
      });
    }
    modelStatus.classList.remove("hidden");
    renderLocalCommand();
  }

  function renderIter() {
    iterValueEl.textContent = String(SELECTED_ITERATIONS);
    if (iterMetaEl) iterMetaEl.textContent = String(SELECTED_ITERATIONS);
    iterMinus.disabled = SELECTED_ITERATIONS <= iterMin;
    iterPlus.disabled  = SELECTED_ITERATIONS >= iterMax;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function selectedBackendOption() {
    return backendOptions[SELECTED_BACKEND] || backendOptions.local || Object.values(backendOptions)[0] || null;
  }

  function firstAvailableBackend() {
    return Object.keys(backendOptions).find(id => {
      const opt = backendOptions[id];
      return opt && opt.available !== false;
    }) || Object.keys(backendOptions)[0] || "local";
  }

  function hasLiveBackend() {
    return Object.values(backendOptions).some(opt => opt && opt.available !== false);
  }

  function backendLabel(id) {
    return (backendOptions[id] && backendOptions[id].label) || (id === "gemini" ? "cloud" : "local");
  }

  function renderModelCards() {
    // The paper view intentionally describes roles instead of provider/model ids.
  }

  function renderBackend() {
    renderFeatureFlags();
    let selected = selectedBackendOption();
    if (!selected || selected.available === false) SELECTED_BACKEND = firstAvailableBackend();
    selected = selectedBackendOption();

    backendButtons.forEach(btn => {
      const id = btn.dataset.backend;
      const opt = backendOptions[id] || {};
      const active = id === SELECTED_BACKEND && opt.available !== false;
      const unavailable = opt.available === false;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-checked", active ? "true" : "false");
      btn.disabled = unavailable;
      if (unavailable) btn.title = opt.reason || "Backend unavailable";
      else btn.removeAttribute("title");
    });

    if (backendMetaEl) backendMetaEl.textContent = hasLiveBackend() ? backendLabel(SELECTED_BACKEND) : "unavailable";
    if (beginBtn) beginBtn.disabled = !hasLiveBackend();
    renderModelCards();
    renderLocalModelStatus();
  }

  backendButtons.forEach(btn => {
    btn.addEventListener("click", () => {
      if (!featureFlags.backend_picker) return;
      const id = btn.dataset.backend;
      const opt = backendOptions[id];
      if (!id || (opt && opt.available === false)) return;
      SELECTED_BACKEND = id;
      renderBackend();
    });
  });
  iterMinus.addEventListener("click", () => {
    if (SELECTED_ITERATIONS > iterMin) { SELECTED_ITERATIONS--; renderIter(); }
  });
  iterPlus.addEventListener("click", () => {
    if (SELECTED_ITERATIONS < iterMax) { SELECTED_ITERATIONS++; renderIter(); }
  });

  // Build the recorded-run subject options from DEMO_PROMPTS.
  DEMO_PROMPTS.forEach((entry) => {
    const opt = document.createElement("button");
    opt.className = "tl-demo-opt";
    opt.textContent = entry.prompt;
    opt.addEventListener("click", () => dismiss(() => startDemoGeneration(entry.prompt)));
    demoOptList.appendChild(opt);
  });

  function setMode(mode) {
    const toLive = mode === "live";
    if (liveModeBtn) liveModeBtn.classList.toggle("active", toLive);
    if (recordedModeBtn) recordedModeBtn.classList.toggle("active", !toLive);
    if (livePanel) livePanel.classList.toggle("hidden", !toLive);
    if (recordedPanel) recordedPanel.classList.toggle("hidden", toLive);
    if (modeMetaEl) modeMetaEl.textContent = toLive ? "live" : "recorded";
  }

  if (liveModeBtn) liveModeBtn.addEventListener("click", () => setMode("live"));
  if (recordedModeBtn) recordedModeBtn.addEventListener("click", () => {
    if (!featureFlags.recorded) return;
    setMode("recorded");
  });

  // Fade the thesis landing away, then start the run. We skip the old scattered
  // doodle intro screen entirely — generation begins immediately.
  function dismiss(then) {
    _doodleAnimStop = true;          // stop the two side doodles
    _landingGone = true;             // ensure the canvas-screen doodle loops never start
    screen.classList.add("fade-out");
    setTimeout(() => screen.classList.add("gone"), 650);
    if (typeof then === "function") then();
  }

  function runTypedPrompt() {
    const p = (promptField.value || "").trim();
    if (!p) { promptField.focus(); return; }
    if (!hasLiveBackend() || selectedBackendOption().available === false) {
      if (bridgeStatus) bridgeStatus.textContent = "not connected";
      if (bridgeDetail) {
        bridgeDetail.textContent = DEPLOYMENT_PROFILE === "local"
          ? "Start the local server with make local and make sure the configured Ollama models are loaded."
          : "The live endpoint is not available; recorded runs remain available.";
      }
      return;
    }
    if (modeMetaEl) modeMetaEl.textContent = "live";
    dismiss(() => startGeneration(p));
  }

  beginBtn.addEventListener("click", runTypedPrompt);
  promptField.addEventListener("keydown", (e) => { if (e.key === "Enter") runTypedPrompt(); });

  function setBridgeState(kind, label, detail) {
    if (bridgeState) {
      bridgeState.classList.remove("checking", "ok", "bad");
      bridgeState.classList.add(kind);
    }
    if (bridgeStatus) bridgeStatus.textContent = label;
    if (bridgeDetail) bridgeDetail.textContent = detail;
    if (beginBtn) {
      if (kind === "checking") {
        beginBtn.classList.add("tl-begin--loading");
        beginBtn.disabled = true;
      } else {
        beginBtn.classList.remove("tl-begin--loading");
      }
    }
  }

  function fetchConfigFrom(base) {
    const controller = new AbortController();
    // Hosted runs on Render's free tier, which spins down when idle. The first
    // request after idle is held open while the instance wakes (can take ~30-60s),
    // so give the hosted profile a long timeout to ride out the cold start rather
    // than aborting early and falling back to the "not ready" state.
    const timeoutMs = DEPLOYMENT_PROFILE === "hosted" ? 60000 : 5000;
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    return fetch(`${base}/config`, {
      signal: controller.signal,
      mode: "cors",
      cache: "no-store",
    })
      .then(r => {
        clearTimeout(timer);
        if (!r.ok) throw new Error("config " + r.status);
        return r.json();
      })
      .catch(err => {
        clearTimeout(timer);
        throw err;
      });
  }

  function applyLiveConfig(cfg, base) {
    API_BASE = base;
    document.body.dataset.deploymentProfile = DEPLOYMENT_PROFILE;
    featureFlags = Object.assign({
      live: true,
      recorded: false,
      backend_picker: false,
      show_model_names: DEPLOYMENT_PROFILE === "local",
    }, cfg.features || {});
    runtimeInfo = cfg.runtime || {};
    cloudModelName = cfg.cloud_model || cloudModelName;
    iterMin = cfg.iterations_min ?? 1;
    iterMax = cfg.iterations_max ?? 8;
    SELECTED_ITERATIONS = Math.min(Math.max(cfg.max_iterations ?? 4, iterMin), iterMax);
    renderIter();

    const options = (cfg.backends && Array.isArray(cfg.backends.options)) ? cfg.backends.options : [];
    backendOptions = {};
    options.forEach(opt => { if (opt && opt.id) backendOptions[opt.id] = opt; });
    if (!Object.keys(backendOptions).length) {
      backendOptions[DEPLOYMENT_PROFILE === "hosted" ? "gemini" : "local"] = {
        id: DEPLOYMENT_PROFILE === "hosted" ? "gemini" : "local",
        label: DEPLOYMENT_PROFILE === "hosted" ? "cloud" : "local",
        available: DEPLOYMENT_PROFILE === "hosted",
        reason: "not reported by API",
      };
    }
    // Hosted: enable button whenever backend is reachable; missing API key surfaces as an error toast at run time.
    if (DEPLOYMENT_PROFILE === "hosted" && backendOptions["gemini"]) {
      backendOptions["gemini"] = Object.assign({}, backendOptions["gemini"], { available: true });
    }
    const configuredDefault =
      (cfg.runtime && cfg.runtime.default_backend) ||
      (cfg.backends && cfg.backends.default) ||
      (DEPLOYMENT_PROFILE === "hosted" ? "gemini" : "local");
    SELECTED_BACKEND = backendOptions[configuredDefault] ? configuredDefault : firstAvailableBackend();
    renderBackend();
    if (statusDot) {
      statusDot.classList.remove("bad");
      statusDot.classList.add("ok");
    }
    if (statusText) {
      statusText.textContent = DEPLOYMENT_PROFILE === "local"
        ? "local backend · ready"
        : "inference endpoint · ready";
    }
    const selected = selectedBackendOption();
    if (selected && selected.available === false) {
      const detail = DEPLOYMENT_PROFILE === "local"
        ? "backend: not ready"
        : (selected.reason || "Backend is not ready.");
      setBridgeState("bad", "not ready", detail);
    } else {
      const detail = DEPLOYMENT_PROFILE === "local"
        ? "backend: connected"
        : `Connected to ${base}`;
      setBridgeState("ok", "connected", detail);
    }
  }

  function applyOfflineConfig() {
    document.body.dataset.deploymentProfile = DEPLOYMENT_PROFILE;
    featureFlags = {
      live: true,
      recorded: false,
      backend_picker: false,
      show_model_names: DEPLOYMENT_PROFILE === "local",
    };
    runtimeInfo = {};
    backendOptions = {
      local:  {
        id: "local",
        label: "local",
        available: false,
        reason: "local bridge is not running",
      },
      gemini: {
        id: "gemini",
        label: "cloud",
        available: false,
        reason: "API config unavailable",
      },
    };
    SELECTED_BACKEND = "local";
    renderIter();
    renderBackend();
    if (statusDot) {
      statusDot.classList.remove("ok");
      statusDot.classList.add("bad");
    }
    if (statusText) statusText.textContent = "local backend unavailable";
    setBridgeState(
      "bad",
      "not running",
      "backend: not running · Ollama"
    );
  }

  const COLD_START_DETAIL =
    "Waking the inference backend — the first request after the instance has been idle can take up to a minute.";

  async function loadLiveConfig() {
    setBridgeState(
      "checking",
      DEPLOYMENT_PROFILE === "hosted" ? "starting" : "checking",
      DEPLOYMENT_PROFILE === "hosted" ? COLD_START_DETAIL : "Looking for the inference backend.",
    );
    for (const base of API_BASE_CANDIDATES) {
      try {
        const cfg = await fetchConfigFrom(base);
        applyLiveConfig(cfg, base);
        return true;
      } catch (err) {
        console.debug("[loadLiveConfig] %s failed: %s", base, err && err.message || err);
      }
    }
    // Hosted: an unreachable backend on load almost always means the Render
    // instance is still cold-starting. Keep the "backend is starting" loading
    // state (the draw button stays disabled) and let scheduleRetry keep polling,
    // instead of dropping to the offline state that shows a clickable-looking but
    // dead "draw" button before the backend is actually ready.
    if (DEPLOYMENT_PROFILE === "hosted") {
      setBridgeState("checking", "starting", COLD_START_DETAIL);
      return false;
    }
    applyOfflineConfig();
    return false;
  }

  if (bridgeRetry) bridgeRetry.addEventListener("click", loadLiveConfig);

  renderIter();
  renderBackend();
  loadLiveConfig();

  (function scheduleRetry() {
    // While a hosted backend is still waking, poll quickly so "draw" unlocks the
    // moment it's ready; once live (or on local) fall back to a slow heartbeat.
    const delay = DEPLOYMENT_PROFILE === "hosted" && !hasLiveBackend() ? 3000 : 10000;
    setTimeout(() => {
      if (!hasLiveBackend()) loadLiveConfig().then(scheduleRetry).catch(scheduleRetry);
    }, delay);
  })();
})();

// ── Appendix A: typewriter placeholder ──────────────────────────────────────
// The prompt field idly types through the recorded subjects, with a blinking
// caret — a quiet, on-theme cue that any subject can be reproduced. It parks on
// a static placeholder the moment the field is focused or holds text, and is
// disabled entirely under reduced-motion.
(function reproPlaceholder() {
  const input = document.getElementById("tl-prompt");
  if (!input || typeof DEMO_PROMPTS === "undefined") return;

  const subjects = DEMO_PROMPTS.map(e => e.prompt).filter(Boolean);
  if (!subjects.length) return;

  const reduce = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce) { input.placeholder = subjects[0]; return; }

  const CARET = "▏";              // ▏ thin caret
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const idle  = () => document.activeElement !== input && !input.value;

  (async function run() {
    await sleep(700);
    let i = 0;
    while (true) {
      // Wait for the field to be idle (unfocused and empty).
      while (!idle()) { input.placeholder = subjects[0]; await sleep(240); }

      const word = subjects[i % subjects.length];
      i++;

      // Type in.
      for (let c = 1; c <= word.length && idle(); c++) {
        input.placeholder = word.slice(0, c) + CARET;
        await sleep(70 + Math.random() * 46);
      }
      // Hold with a blinking caret.
      for (let b = 0; b < 5 && idle(); b++) {
        input.placeholder = word + (b % 2 ? " " : CARET);
        await sleep(440);
      }
      // Erase.
      for (let c = word.length - 1; c >= 0 && idle(); c--) {
        input.placeholder = word.slice(0, c) + CARET;
        await sleep(34);
      }
      if (idle()) { input.placeholder = CARET; await sleep(360); }
    }
  })();
})();
