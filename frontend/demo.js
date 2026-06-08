"use strict";

// ── Demo mode: pre-baked runs, no server needed ───────────────────────────
// Each entry has { prompt, iterations[] }, where each iteration has the same
// shape as the live SSE event payloads consumed by handleEvent().

const DEMO_PROMPTS = [

  // ── 1. a cat ─────────────────────────────────────────────────────────────
  // Same loose doodle vocabulary as the landing-page qualitative example:
  // tall curled tail, long simple body, small eared head, dot eyes and whiskers.
  {
    prompt: "a cat",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M152 290 C148 250 146 212 152 188 C156 170 178 168 188 182 C196 193 188 208 176 205 C169 203 168 193 173 188" stroke-width="1.75"/>
    <path id="step-2" d="M152 290 C200 270 252 266 300 270" stroke-width="1.75"/>
    <path id="step-3" d="M334 344 C280 351 205 351 158 344" stroke-width="1.75"/>
    <path id="step-4" d="M158 344 C152 322 150 305 152 290" stroke-width="1.75"/>
  </g>
</svg>`,
        steps: ["curled tail", "back line", "belly line", "rear line"],
        reasoning: "Starting with the long body and tall curled tail, but the drawing still lacks the cat's head and details.",
        verdict: "revise",
        score: 2,
        ui_message: "body and tail only, needs head and features",
        feedback_for_artist: "The curled tail and long body suggest a cat, but there is no head, no ears, no face, and no legs. Add the small head and a few identifying details before refining proportions.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M152 290 C148 250 146 212 152 188 C156 170 178 168 188 182 C196 193 188 208 176 205 C169 203 168 193 173 188" stroke-width="1.75"/>
    <path id="step-2" d="M152 290 C200 270 252 266 300 270" stroke-width="1.75"/>
    <path id="step-3" d="M300 270 C298 242 314 224 338 224 C362 224 378 242 376 266 C374 290 358 302 336 300" stroke-width="1.75"/>
    <path id="step-4" d="M316 228 L312 206 L330 222" stroke-width="1.75"/>
    <path id="step-5" d="M350 222 L366 204 L368 228" stroke-width="1.75"/>
    <path id="step-6" d="M336 300 C337 316 336 332 334 344" stroke-width="1.75"/>
    <path id="step-7" d="M334 344 C280 351 205 351 158 344" stroke-width="1.75"/>
    <path id="step-8" d="M158 344 C152 322 150 305 152 290" stroke-width="1.75"/>
  </g>
</svg>`,
        steps: ["curled tail", "back line", "head outline", "left ear", "right ear", "chest", "belly line", "rear line"],
        reasoning: "Added the eared head on the right end of the body, matching the sparse doodle style.",
        verdict: "revise",
        score: 4,
        ui_message: "head and ears added, still blank",
        feedback_for_artist: "Good progress — the eared head and tall tail now make the subject read as a cat. The face is blank and there are no legs, so the drawing still needs eyes, nose, whiskers, and short legs.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M152 290 C148 250 146 212 152 188 C156 170 178 168 188 182 C196 193 188 208 176 205 C169 203 168 193 173 188" stroke-width="1.75"/>
    <path id="step-2" d="M152 290 C200 270 252 266 300 270" stroke-width="1.75"/>
    <path id="step-3" d="M300 270 C298 242 314 224 338 224 C362 224 378 242 376 266 C374 290 358 302 336 300" stroke-width="1.75"/>
    <path id="step-4" d="M316 228 L312 206 L330 222" stroke-width="1.75"/>
    <path id="step-5" d="M350 222 L366 204 L368 228" stroke-width="1.75"/>
    <path id="step-6" d="M336 300 C337 316 336 332 334 344" stroke-width="1.75"/>
    <path id="step-7" d="M334 344 C280 351 205 351 158 344" stroke-width="1.75"/>
    <path id="step-8" d="M158 344 C152 322 150 305 152 290" stroke-width="1.75"/>
    <path id="step-9" d="M196 345 C196 353 196 361 198 367" stroke-width="1.75"/>
    <path id="step-10" d="M224 346 C224 354 224 361 226 367" stroke-width="1.75"/>
    <path id="step-11" d="M300 346 C300 354 300 361 302 367" stroke-width="1.75"/>
    <path id="step-12" d="M328 345 C328 353 328 360 330 366" stroke-width="1.75"/>
    <path id="step-13" d="M328 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0" stroke-width="1.75"/>
    <path id="step-14" d="M352 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0" stroke-width="1.75"/>
    <path id="step-15" d="M340 268 C343 272 349 272 352 268" stroke-width="1.75"/>
    <path id="step-16" d="M372 262 L396 258 M374 270 L396 271 M373 278 L395 283" stroke-width="1.75"/>
  </g>
</svg>`,
        steps: ["curled tail", "back line", "head outline", "left ear", "right ear", "chest", "belly line", "rear line", "leg 1", "leg 2", "leg 3", "leg 4", "left eye", "right eye", "nose", "whiskers"],
        reasoning: "Added the short legs, dot eyes, mouth and whiskers, giving the cat the same sparse hand-drawn look as the paper example.",
        verdict: "revise",
        score: 8,
        ui_message: "clear cat, proportions need one pass",
        feedback_for_artist: "This is a charming doodle cat: the tall curled tail, the long simple body, the small eared head, the dot eyes and whiskers, and the short legs all read clearly with a confident hand-drawn line. The main remaining issue is proportion — the head sits a touch high and the four legs could be spaced more evenly along the belly.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M152 290 C148 250 146 212 152 188 C156 170 178 168 188 182 C196 193 188 208 176 205 C169 203 168 193 173 188" stroke-width="1.75"/>
    <path id="step-2" d="M152 290 C200 270 252 266 300 270" stroke-width="1.75"/>
    <path id="step-3" d="M300 270 C298 244 315 226 338 226 C361 226 376 243 375 266 C373 288 357 300 336 298" stroke-width="1.75"/>
    <path id="step-4" d="M316 229 L313 209 L330 223" stroke-width="1.75"/>
    <path id="step-5" d="M350 223 L365 207 L367 229" stroke-width="1.75"/>
    <path id="step-6" d="M336 298 C337 314 336 330 334 344" stroke-width="1.75"/>
    <path id="step-7" d="M334 344 C280 351 205 351 158 344" stroke-width="1.75"/>
    <path id="step-8" d="M158 344 C152 322 150 305 152 290" stroke-width="1.75"/>
    <path id="step-9" d="M186 345 C187 354 189 361 192 366" stroke-width="1.75"/>
    <path id="step-10" d="M232 346 C233 354 235 361 238 366" stroke-width="1.75"/>
    <path id="step-11" d="M286 346 C285 354 283 361 280 366" stroke-width="1.75"/>
    <path id="step-12" d="M326 345 C324 353 321 360 318 365" stroke-width="1.75"/>
    <path id="step-13" d="M328 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0" stroke-width="1.75"/>
    <path id="step-14" d="M352 256 m-2.6 0 a2.6 2.6 0 1 0 5.2 0 a2.6 2.6 0 1 0 -5.2 0" stroke-width="1.75"/>
    <path id="step-15" d="M340 268 C343 272 349 272 352 268" stroke-width="1.75"/>
    <path id="step-16" d="M372 262 L396 258 M374 270 L396 271 M373 278 L395 283" stroke-width="1.75"/>
  </g>
</svg>`,
        steps: ["curled tail", "back line", "head outline", "left ear", "right ear", "chest", "belly line", "rear line", "leg 1", "leg 2", "leg 3", "leg 4", "left eye", "right eye", "nose", "whiskers"],
        reasoning: "Lowered the head slightly and spaced the short legs more evenly while preserving the same doodle-cat style.",
        verdict: "accept",
        score: 9,
        ui_message: "a complete doodle cat",
        feedback_for_artist: "The cat is fully realised as a spare academic doodle: a tall curled tail, long body, compact eared head, dot eyes, simple mouth, whiskers, and evenly spaced short legs. The line is confident and the drawing reads clearly.",
      },
    ],
  },

  // ── 2. a lighthouse ──────────────────────────────────────────────────────
  {
    prompt: "a lighthouse",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 240 380 L 238 300 L 240 200 L 272 200 L 274 300 L 272 380 Z" stroke-width="2.2" opacity="0.90"/>
  </g>
</svg>`,
        steps: ["tower body"],
        reasoning: "Basic rectangular tower shape.",
        verdict: "revise",
        score: 2,
        ui_message: "just a rectangle — needs taper, lantern, detail",
        feedback_for_artist: "This is a plain rectangle, which could be anything. A lighthouse has a tapered silhouette (wider at base, narrower at top), a distinct lantern room at the summit, and horizontal stripe bands across the body. None of those are present.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 234 378 C 232 345,228 305,226 268 C 224 231,228 212,236 202 C 242 194,270 194,276 202 C 284 212,288 231,286 268 C 284 305,280 345,278 378 Z" stroke-width="2.2" opacity="0.92"/>
    <path id="step-2" d="M 224 378 L 288 378" stroke-width="2.2" opacity="0.90"/>
    <path id="step-3" d="M 236 202 C 236 193,240 186,244 181 C 248 176,256 173,256 173 C 256 173,264 176,268 181 C 272 186,276 193,276 202" stroke-width="2.0" opacity="0.88"/>
    <path id="step-4" d="M 244 181 C 244 177,248 173,256 171 C 264 173,268 177,268 181" stroke-width="1.8" opacity="0.85"/>
  </g>
</svg>`,
        steps: ["tower body", "base line", "lantern housing", "lantern roof"],
        reasoning: "Added taper to the tower and a small lantern room with a pointed cap.",
        verdict: "revise",
        score: 5,
        ui_message: "needs stripe bands and coastal rocks",
        feedback_for_artist: "Much better — the silhouette is now recognisably a lighthouse. Two horizontal stripe bands across the mid-section and a rough rocky base would make this unambiguously coastal.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 234 378 C 232 345,228 305,226 268 C 224 231,228 212,236 202 C 242 194,270 194,276 202 C 284 212,288 231,286 268 C 284 305,280 345,278 378 Z" stroke-width="2.2" opacity="0.92"/>
    <path id="step-2" d="M 224 378 L 288 378" stroke-width="2.2" opacity="0.90"/>
    <path id="step-3" d="M 236 202 C 236 193,240 186,244 181 C 248 176,256 173,256 173 C 256 173,264 176,268 181 C 272 186,276 193,276 202" stroke-width="2.0" opacity="0.88"/>
    <path id="step-4" d="M 244 181 C 244 177,248 173,256 171 C 264 173,268 177,268 181" stroke-width="1.8" opacity="0.85"/>
    <path id="step-5" d="M 227 298 C 231 296,235 295,240 296 C 248 297,256 297,264 296 C 269 295,273 296,277 298" stroke-width="1.6" opacity="0.80"/>
    <path id="step-6" d="M 229 338 L 275 338" stroke-width="1.6" opacity="0.80"/>
    <path id="step-7" d="M 218 382 C 212 376,204 374,196 378 C 206 378,210 384,220 386 C 226 383,228 380,226 378 M 294 382 C 300 376,308 374,316 378 C 306 378,302 384,292 386 C 286 383,284 380,286 378" stroke-width="1.8" opacity="0.82"/>
  </g>
</svg>`,
        steps: ["tower body", "base line", "lantern housing", "lantern roof", "upper band", "lower band", "rocks"],
        reasoning: "Added two horizontal stripe bands across the tower and rough rock shapes flanking the base.",
        verdict: "revise",
        score: 7,
        ui_message: "light beams would complete the scene",
        feedback_for_artist: "The lighthouse reads very clearly now with the banded pattern and rocky base. The one missing element is light beams radiating from the lantern — even two or three diverging lines would complete the scene and make it unmistakably functional.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 234 378 C 232 345,228 305,226 268 C 224 231,228 212,236 202 C 242 194,270 194,276 202 C 284 212,288 231,286 268 C 284 305,280 345,278 378 Z" stroke-width="2.2" opacity="0.92"/>
    <path id="step-2" d="M 224 378 L 288 378" stroke-width="2.2" opacity="0.90"/>
    <path id="step-3" d="M 236 202 C 236 193,240 186,244 181 C 248 176,256 173,256 173 C 256 173,264 176,268 181 C 272 186,276 193,276 202" stroke-width="2.0" opacity="0.88"/>
    <path id="step-4" d="M 244 181 C 244 177,248 173,256 171 C 264 173,268 177,268 181" stroke-width="1.8" opacity="0.85"/>
    <path id="step-5" d="M 227 298 C 231 296,235 295,240 296 C 248 297,256 297,264 296 C 269 295,273 296,277 298" stroke-width="1.6" opacity="0.80"/>
    <path id="step-6" d="M 229 338 L 275 338" stroke-width="1.6" opacity="0.80"/>
    <path id="step-7" d="M 218 382 C 212 376,204 374,196 378 C 206 378,210 384,220 386 C 226 383,228 380,226 378 M 294 382 C 300 376,308 374,316 378 C 306 378,302 384,292 386 C 286 383,284 380,286 378" stroke-width="1.8" opacity="0.82"/>
    <path id="step-8" d="M 256 174 C 248 161,234 148,218 138 M 256 174 C 258 159,261 143,269 129 M 256 174 C 264 162,278 150,294 141" stroke-width="1.4" opacity="0.68"/>
  </g>
</svg>`,
        steps: ["tower body", "base line", "lantern housing", "lantern roof", "upper band", "lower band", "rocks", "light beams"],
        reasoning: "Added three diverging light beams radiating from the lantern into the upper sky.",
        verdict: "revise",
        score: 8,
        ui_message: "a door would add the final touch",
        feedback_for_artist: "Excellent — the beams make the lighthouse feel alive and functional. The only small refinement would be a simple arched door at the base of the tower, a classic lighthouse detail that completes the character.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 234 378 C 232 345,228 305,226 268 C 224 231,228 212,236 202 C 242 194,270 194,276 202 C 284 212,288 231,286 268 C 284 305,280 345,278 378 Z" stroke-width="2.2" opacity="0.92"/>
    <path id="step-2" d="M 224 378 L 288 378" stroke-width="2.2" opacity="0.90"/>
    <path id="step-3" d="M 236 202 C 236 193,240 186,244 181 C 248 176,256 173,256 173 C 256 173,264 176,268 181 C 272 186,276 193,276 202" stroke-width="2.0" opacity="0.88"/>
    <path id="step-4" d="M 244 181 C 244 177,248 173,256 171 C 264 173,268 177,268 181" stroke-width="1.8" opacity="0.85"/>
    <path id="step-5" d="M 227 298 C 231 296,235 295,240 296 C 248 297,256 297,264 296 C 269 295,273 296,277 298" stroke-width="1.6" opacity="0.80"/>
    <path id="step-6" d="M 229 338 L 275 338" stroke-width="1.6" opacity="0.80"/>
    <path id="step-7" d="M 218 382 C 212 376,204 374,196 378 C 206 378,210 384,220 386 C 226 383,228 380,226 378 M 294 382 C 300 376,308 374,316 378 C 306 378,302 384,292 386 C 286 383,284 380,286 378" stroke-width="1.8" opacity="0.82"/>
    <path id="step-8" d="M 256 174 C 248 161,234 148,218 138 M 256 174 C 258 159,261 143,269 129 M 256 174 C 264 162,278 150,294 141" stroke-width="1.4" opacity="0.68"/>
    <path id="step-9" d="M 248 378 L 248 358 C 248 352,264 352,264 358 L 264 378" stroke-width="1.6" opacity="0.80"/>
  </g>
</svg>`,
        steps: ["tower body", "base line", "lantern housing", "lantern roof", "upper band", "lower band", "rocks", "light beams", "door"],
        reasoning: "Added a simple arched door at the base of the tower.",
        verdict: "accept",
        score: 9,
        ui_message: "a complete lighthouse",
        feedback_for_artist: "The drawing is fully resolved — tapered tower, lantern room, stripe bands, rocky base, radiating light beams, and a small door. Every element reads clearly and the composition is well balanced.",
      },
    ],
  },

  // ── 3. a smiling sun ─────────────────────────────────────────────────────
  {
    prompt: "a smiling sun",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 256 340 C 212 340,176 304,176 258 C 176 212,212 176,256 176 C 300 176,336 212,336 258 C 336 304,300 340,256 340" stroke-width="2.4" opacity="0.92"/>
  </g>
</svg>`,
        steps: ["sun disc"],
        reasoning: "Just the circular disc so far.",
        verdict: "revise",
        score: 2,
        ui_message: "only a circle — needs face and rays",
        feedback_for_artist: "This is just a bare circle. A smiling sun needs at minimum a curved smile on the face, eyes, and rays radiating outward to distinguish it from any other circular subject.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 256 340 C 212 340,176 304,176 258 C 176 212,212 176,256 176 C 300 176,336 212,336 258 C 336 304,300 340,256 340" stroke-width="2.4" opacity="0.92"/>
    <path id="step-2" d="M 230 244 C 234 236,244 234,250 240 C 244 242,236 250,230 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-3" d="M 262 244 C 266 236,276 234,282 240 C 276 242,268 250,262 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-4" d="M 224 282 C 234 298,278 298,288 282" stroke-width="2.0" opacity="0.90"/>
  </g>
</svg>`,
        steps: ["sun disc", "left eye", "right eye", "smile"],
        reasoning: "Added almond eyes and a wide smile arc.",
        verdict: "revise",
        score: 5,
        ui_message: "no rays — reads as a face not a sun",
        feedback_for_artist: "The face is warm and expressive but without rays the circle looks like a generic face floating in space. Add 8 rays at cardinal and diagonal positions to make it unambiguously a sun.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 256 340 C 212 340,176 304,176 258 C 176 212,212 176,256 176 C 300 176,336 212,336 258 C 336 304,300 340,256 340" stroke-width="2.4" opacity="0.92"/>
    <path id="step-2" d="M 230 244 C 234 236,244 234,250 240 C 244 242,236 250,230 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-3" d="M 262 244 C 266 236,276 234,282 240 C 276 242,268 250,262 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-4" d="M 224 282 C 234 298,278 298,288 282" stroke-width="2.0" opacity="0.90"/>
    <path id="step-5" d="M 256 154 C 255 142,256 130,257 120" stroke-width="1.8" opacity="0.82"/>
    <path id="step-6" d="M 256 362 C 255 374,256 386,257 396" stroke-width="1.8" opacity="0.82"/>
    <path id="step-7" d="M 154 258 C 142 257,130 258,120 259" stroke-width="1.8" opacity="0.82"/>
    <path id="step-8" d="M 358 258 C 370 257,382 258,392 259" stroke-width="1.8" opacity="0.82"/>
    <path id="step-9" d="M 184 188 C 176 180,168 170,161 162" stroke-width="1.8" opacity="0.82"/>
    <path id="step-10" d="M 328 188 C 336 180,344 170,351 162" stroke-width="1.8" opacity="0.82"/>
    <path id="step-11" d="M 184 328 C 176 336,168 346,161 354" stroke-width="1.8" opacity="0.82"/>
    <path id="step-12" d="M 328 328 C 336 336,344 346,351 354" stroke-width="1.8" opacity="0.82"/>
  </g>
</svg>`,
        steps: ["sun disc", "left eye", "right eye", "smile", "top ray", "bottom ray", "left ray", "right ray", "top-left ray", "top-right ray", "bottom-left ray", "bottom-right ray"],
        reasoning: "Added eight rays at cardinal and diagonal directions.",
        verdict: "revise",
        score: 7,
        ui_message: "rays are thin — vary their length for warmth",
        feedback_for_artist: "The sun now reads correctly with all eight rays. To push the warmth further, try making the cardinal rays slightly longer than the diagonal ones — alternating lengths give the sun a livelier, more energetic feel.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 256 340 C 212 340,176 304,176 258 C 176 212,212 176,256 176 C 300 176,336 212,336 258 C 336 304,300 340,256 340" stroke-width="2.4" opacity="0.92"/>
    <path id="step-2" d="M 230 244 C 234 236,244 234,250 240 C 244 242,236 250,230 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-3" d="M 262 244 C 266 236,276 234,282 240 C 276 242,268 250,262 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-4" d="M 224 282 C 234 298,278 298,288 282" stroke-width="2.0" opacity="0.90"/>
    <path id="step-5" d="M 256 152 C 255 138,256 124,257 112" stroke-width="2.0" opacity="0.84"/>
    <path id="step-6" d="M 256 364 C 255 378,256 392,257 404" stroke-width="2.0" opacity="0.84"/>
    <path id="step-7" d="M 152 258 C 138 257,124 258,112 259" stroke-width="2.0" opacity="0.84"/>
    <path id="step-8" d="M 360 258 C 374 257,388 258,400 259" stroke-width="2.0" opacity="0.84"/>
    <path id="step-9" d="M 186 188 C 179 181,172 172,165 164" stroke-width="1.6" opacity="0.76"/>
    <path id="step-10" d="M 326 188 C 333 181,340 172,347 164" stroke-width="1.6" opacity="0.76"/>
    <path id="step-11" d="M 186 328 C 179 335,172 344,165 352" stroke-width="1.6" opacity="0.76"/>
    <path id="step-12" d="M 326 328 C 333 335,340 344,347 352" stroke-width="1.6" opacity="0.76"/>
  </g>
</svg>`,
        steps: ["sun disc", "left eye", "right eye", "smile", "top ray", "bottom ray", "left ray", "right ray", "top-left ray", "top-right ray", "bottom-left ray", "bottom-right ray"],
        reasoning: "Made cardinal rays longer and slightly heavier than the diagonal ones for a livelier alternating rhythm.",
        verdict: "revise",
        score: 8,
        ui_message: "rosy cheeks would add personality",
        feedback_for_artist: "The alternating ray lengths give the sun a much more dynamic character. One small addition that would add warmth and personality: a small arc or dot on each cheek to suggest a rosy blush.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <g fill="none" stroke="#1a1a1a" stroke-linecap="round" stroke-linejoin="round">
    <path id="step-1" d="M 256 340 C 212 340,176 304,176 258 C 176 212,212 176,256 176 C 300 176,336 212,336 258 C 336 304,300 340,256 340" stroke-width="2.4" opacity="0.92"/>
    <path id="step-2" d="M 230 244 C 234 236,244 234,250 240 C 244 242,236 250,230 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-3" d="M 262 244 C 266 236,276 234,282 240 C 276 242,268 250,262 244 Z" stroke-width="1.8" opacity="0.88"/>
    <path id="step-4" d="M 224 282 C 234 298,278 298,288 282" stroke-width="2.0" opacity="0.90"/>
    <path id="step-5" d="M 256 152 C 255 138,256 124,257 112" stroke-width="2.0" opacity="0.84"/>
    <path id="step-6" d="M 256 364 C 255 378,256 392,257 404" stroke-width="2.0" opacity="0.84"/>
    <path id="step-7" d="M 152 258 C 138 257,124 258,112 259" stroke-width="2.0" opacity="0.84"/>
    <path id="step-8" d="M 360 258 C 374 257,388 258,400 259" stroke-width="2.0" opacity="0.84"/>
    <path id="step-9" d="M 186 188 C 179 181,172 172,165 164" stroke-width="1.6" opacity="0.76"/>
    <path id="step-10" d="M 326 188 C 333 181,340 172,347 164" stroke-width="1.6" opacity="0.76"/>
    <path id="step-11" d="M 186 328 C 179 335,172 344,165 352" stroke-width="1.6" opacity="0.76"/>
    <path id="step-12" d="M 326 328 C 333 335,340 344,347 352" stroke-width="1.6" opacity="0.76"/>
    <path id="step-13" d="M 210 274 C 214 270,222 270,224 274 C 222 278,214 278,210 274" stroke-width="1.4" opacity="0.65"/>
    <path id="step-14" d="M 288 274 C 292 270,300 270,302 274 C 300 278,292 278,288 274" stroke-width="1.4" opacity="0.65"/>
  </g>
</svg>`,
        steps: ["sun disc", "left eye", "right eye", "smile", "top ray", "bottom ray", "left ray", "right ray", "top-left ray", "top-right ray", "bottom-left ray", "bottom-right ray", "left cheek", "right cheek"],
        reasoning: "Added small oval blush marks on each cheek for warmth and personality.",
        verdict: "accept",
        score: 9,
        ui_message: "warm, sunny, and full of character",
        feedback_for_artist: "The sun is now fully expressive — lively alternating rays, a broad smile, and blushing cheeks. The composition is balanced and the subject is immediately readable. A very satisfying result.",
      },
    ],
  },
];

// ── Playback engine ───────────────────────────────────────────────────────
// Fires handleEvent() calls with realistic inter-event delays, so every
// piece of UI logic (thinking sketch, stroke animator, critic annotation,
// acceptance moment) runs exactly as it would on a live run.

const DEMO_DELAYS = {
  afterStart:        800,   // iteration_start → start of thinking pause
  generationThink:  7000,   // artist generation pause — about 7–8 seconds total
  afterGeneration:   400,   // generation_done → render_done
  afterRender:       300,   // render_done → critique_start
  critiqueThink:    3500,   // critic feedback pause — about 3–4 seconds
  afterCritique:     300,   // critique_start → critique_done
  betweenIter:       400,   // critique_done → next iteration_start (user clicks continue)
};

let _demoAbortController = null;

function abortDemo() {
  if (_demoAbortController) { _demoAbortController.abort(); _demoAbortController = null; }
}

async function runDemo(promptText) {
  abortDemo();
  const ac = new AbortController();
  _demoAbortController = ac;
  const sig = ac.signal;

  // Match the prompt (case-insensitive prefix/contains match)
  const needle = promptText.trim().toLowerCase();
  const entry  = DEMO_PROMPTS.find(e =>
    e.prompt.toLowerCase() === needle ||
    e.prompt.toLowerCase().includes(needle) ||
    needle.includes(e.prompt.toLowerCase())
  ) || DEMO_PROMPTS[0]; // fallback to first

  const total = entry.iterations.length;

  const sleep = ms => new Promise((res, rej) => {
    const t = setTimeout(res, ms);
    sig.addEventListener("abort", () => { clearTimeout(t); rej(new DOMException("aborted","AbortError")); }, {once:true});
  });

  try {
    for (let i = 0; i < total; i++) {
      const iter = entry.iterations[i];

      handleEvent({ event: "iteration_start", payload: { index: i, total, recorded: true } });
      await sleep(DEMO_DELAYS.afterStart);
      if (sig.aborted) return;

      // Simulate the artist "thinking" — thinking sketch is already running
      await sleep(DEMO_DELAYS.generationThink);
      if (sig.aborted) return;

      handleEvent({ event: "generation_done", payload: {
        svg:       iter.svg,
        steps:     iter.steps,
        reasoning: iter.reasoning,
        style_notes: "",
        elapsed_seconds: 2.1,
        recorded: true,
      }});
      await sleep(DEMO_DELAYS.afterGeneration);
      if (sig.aborted) return;

      handleEvent({ event: "render_done", payload: {} });
      await sleep(DEMO_DELAYS.afterRender);
      if (sig.aborted) return;

      handleEvent({ event: "critique_start", payload: { recorded: true } });
      await sleep(DEMO_DELAYS.critiqueThink);
      if (sig.aborted) return;

      handleEvent({ event: "critique_done", payload: {
        verdict:             iter.verdict,
        score:               iter.score,
        ui_message:          iter.ui_message,
        feedback_for_artist: iter.feedback_for_artist,
        reasoning:           iter.feedback_for_artist,
        elapsed_seconds:     1.4,
        recorded: true,
      }});

      if (iter.verdict === "accept") {
        // Wait for critique_done chain: gaze(400) + delay(600) + typewriter(~2s for both lines)
        // + _showAccept shows 2000ms then fades 600ms + showAcceptanceMoment brightness(3000ms)
        // = ~9s total. Fire loop_complete into the chain; queues after all prior items.
        await sleep(8000);
        if (sig.aborted) return;
        handleEvent({ event: "loop_complete", payload: {
          final_svg:   iter.svg,
          total_iterations: i + 1,
        }});
        return;
      }

      await sleep(DEMO_DELAYS.betweenIter);
      if (sig.aborted) return;
    }

    // Fallback: if last iteration wasn't accept, close anyway
    const lastSVG = entry.iterations[total - 1].svg;
    await sleep(2000);
    if (sig.aborted) return;
    handleEvent({ event: "loop_complete", payload: { final_svg: lastSVG, total_iterations: total } });

  } catch (e) {
    if (e.name !== "AbortError") console.error("[demo]", e);
  }
}
