"use strict";

// ── Demo mode: pre-baked runs, no server needed ───────────────────────────
// Each entry has { prompt, iterations[] }, where each iteration has the same
// shape as the live SSE event payloads consumed by handleEvent().

const DEMO_PROMPTS = [

  // ── 1. a flower — petals, center, stem; leaves added on the refinement pass.
  {
    prompt: "a flower",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g/><g filter="url(#roughen)"><path id="step-1" d="M 254.4 450.4 C 259.9 346.8 237.5 302.9 254.9 218.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 241.5 203.8 A 14.7 20.4 0 1 0 267.7 203.1 A 19.2 19.1 0 1 0 240.6 200.4" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 254.4 185.6 Q 281.3 126.5 256.5 109.3 Q 236.0 132.2 256.2 181.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 255.6 213.9 Q 279.5 268.4 259.3 292.4 Q 232.3 272.5 252.9 214.4" fill="none" stroke="#1a1a1a"/><path id="step-5" d="M 236.7 199.1 Q 187.7 177.0 173.6 197.0 Q 186.7 222.3 243.5 200.0" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 271.5 197.0 Q 323.2 180.0 337.2 202.7 Q 321.3 215.5 270.5 197.9" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["stem", "center", "top petal", "bottom petal", "left petal", "right petal"],
        reasoning: "I will start by drawing the basic structure of a flower: a central stem, a circular center, and four simple petals to establish the overall form and identity of the subject.",
        verdict: "revise",
        score: 6,
        ui_message: "Add some leaves to the stem.",
        feedback_for_artist: "You have the basic shape of the flower and the stem down. Now let's add some leaves to the stem to make it look more natural.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-7" d="M 254.4 351.3 Q 209.6 340.7 210.9 311.1 Q 254.0 309.2 255.0 351.6" fill="none" stroke="#1a1a1a"/><path id="step-8" d="M 256.4 380.0 Q 300.6 369.6 298.8 339.8 Q 255.6 339.0 254.4 379.3" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 254.4 450.4 C 259.9 346.8 237.5 302.9 254.9 218.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 241.5 203.8 A 14.7 20.4 0 1 0 267.7 203.1 A 19.2 19.1 0 1 0 240.6 200.4" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 254.4 185.6 Q 281.3 126.5 256.5 109.3 Q 236.0 132.2 256.2 181.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 255.6 213.9 Q 279.5 268.4 259.3 292.4 Q 232.3 272.5 252.9 214.4" fill="none" stroke="#1a1a1a"/><path id="step-5" d="M 236.7 199.1 Q 187.7 177.0 173.6 197.0 Q 186.7 222.3 243.5 200.0" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 271.5 197.0 Q 323.2 180.0 337.2 202.7 Q 321.3 215.5 270.5 197.9" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["stem", "center", "top petal", "bottom petal", "left petal", "right petal", "left leaf", "right leaf"],
        reasoning: "Adding two simple leaves to the stem to enhance the natural look of the flower. One leaf extends to the left and one to the right, using quadratic Bézier curves to create a leaf-like shape attached to the existing stem path.",
        verdict: "accept",
        score: 9,
        ui_message: "The drawing is complete!",
        feedback_for_artist: "",
      },
    ],
  },

  // ── 2. an owl on a branch — body first, then beak, wings, talons, perch.
  {
    prompt: "an owl on a branch",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g/><g filter="url(#roughen)"><path id="step-1" d="M 202.3 375.9 C 158.7 376.2 158.0 183.4 253.9 176.1 C 356.4 176.6 348.1 377.4 315.4 383.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 232.2 240.3 A 10.7 18.0 0 1 1 228.1 268.7 A 18.9 10.6 0 1 1 230.6 240.7" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 279.1 239.1 A 18.5 14.8 0 1 1 279.5 267.3 A 16.2 16.6 0 1 1 283.7 241.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 99.9 420.3 Q 255.0 382.3 413.0 420.4" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["owl body and head", "left eye", "right eye", "branch"],
        reasoning: "I will create the basic silhouette of an owl perched on a branch. This includes a rounded body/head shape, two large circular eyes to establish the owl's identity, and a simple curved line for the branch.",
        verdict: "revise",
        score: 3,
        ui_message: "Add a beak to the face.",
        feedback_for_artist: "You have the basic shape and eyes down. Now let's add a beak to make it look like a bird.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 253.8 254.0 L 258.5 274.3 L 263.3 256.7" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 202.3 375.9 C 158.7 376.2 158.0 183.4 253.9 176.1 C 356.4 176.6 348.1 377.4 315.4 383.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 232.2 240.3 A 10.7 18.0 0 1 1 228.1 268.7 A 18.9 10.6 0 1 1 230.6 240.7" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 279.1 239.1 A 18.5 14.8 0 1 1 279.5 267.3 A 16.2 16.6 0 1 1 283.7 241.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 99.9 420.3 Q 255.0 382.3 413.0 420.4" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["owl body and head", "left eye", "right eye", "branch", "beak"],
        reasoning: "Adding a small, downward-pointing beak between the eyes to clearly identify the subject as a bird.",
        verdict: "revise",
        score: 5,
        ui_message: "Add the wings to the sides of the owl.",
        feedback_for_artist: "I see the body, eyes, and beak are all there. Now let's add the wings to the sides of the body.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 253.8 254.0 L 258.5 274.3 L 263.3 256.7" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 198.6 237.4 Q 150.0 304.1 213.5 363.9" stroke="#1a1a1a" fill="none"/><path id="step-7" d="M 309.8 238.7 Q 358.6 299.8 299.1 356.9" stroke="#1a1a1a" fill="none"/></g><g filter="url(#roughen)"><path id="step-1" d="M 202.3 375.9 C 158.7 376.2 158.0 183.4 253.9 176.1 C 356.4 176.6 348.1 377.4 315.4 383.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 232.2 240.3 A 10.7 18.0 0 1 1 228.1 268.7 A 18.9 10.6 0 1 1 230.6 240.7" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 279.1 239.1 A 18.5 14.8 0 1 1 279.5 267.3 A 16.2 16.6 0 1 1 283.7 241.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 99.9 420.3 Q 255.0 382.3 413.0 420.4" fill="none" stroke="#1a1a1a"/><path id="step-5" d="M 253.8 254.0 L 258.5 274.3 L 263.3 256.7" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["owl body and head", "left eye", "right eye", "branch", "beak", "left wing", "right wing"],
        reasoning: "Adding simple curved lines to represent the wings on either side of the owl's body to give it a more complete silhouette.",
        verdict: "revise",
        score: 7,
        ui_message: "Add the talons gripping the branch.",
        feedback_for_artist: "You've got the main shape and the wings down. Now let's add the talons gripping the branch.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 253.8 254.0 L 258.5 274.3 L 263.3 256.7" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 198.6 237.4 Q 150.0 304.1 213.5 363.9" stroke="#1a1a1a" fill="none"/><path id="step-7" d="M 309.8 238.7 Q 358.6 299.8 299.1 356.9" stroke="#1a1a1a" fill="none"/><path id="step-8" d="M 224.4 384.1 q -5.2 14.1 3.7 13.1 M 233.2 385.9 q 7.5 12.4 -6.0 9.7" fill="none" stroke="#1a1a1a"/><path id="step-9" d="M 278.6 381.9 q -6.3 15.9 0.5 13.7 M 288.3 382.2 q 6.6 8.7 -5.0 11.4" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 202.3 375.9 C 158.7 376.2 158.0 183.4 253.9 176.1 C 356.4 176.6 348.1 377.4 315.4 383.5" fill="none" stroke="#1a1a1a"/><path id="step-2" d="M 232.2 240.3 A 10.7 18.0 0 1 1 228.1 268.7 A 18.9 10.6 0 1 1 230.6 240.7" fill="none" stroke="#1a1a1a"/><path id="step-3" d="M 279.1 239.1 A 18.5 14.8 0 1 1 279.5 267.3 A 16.2 16.6 0 1 1 283.7 241.1" fill="none" stroke="#1a1a1a"/><path id="step-4" d="M 99.9 420.3 Q 255.0 382.3 413.0 420.4" fill="none" stroke="#1a1a1a"/><path id="step-5" d="M 253.8 254.0 L 258.5 274.3 L 263.3 256.7" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 198.6 237.4 Q 150.0 304.1 213.5 363.9" fill="none" stroke="#1a1a1a"/><path id="step-7" d="M 309.8 238.7 Q 358.6 299.8 299.1 356.9" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["owl body and head", "left eye", "right eye", "branch", "beak", "left wing", "right wing", "left talons", "right talons"],
        reasoning: "Adding small curved lines at the base of the owl's body to represent talons gripping the branch, ensuring they overlap the branch path for a realistic grip.",
        verdict: "accept",
        score: 9,
        ui_message: "Great job!",
        feedback_for_artist: "",
      },
    ],
  },

  // ── 3. a hot air balloon — envelope, basket, ropes, panel lines, burner.
  {
    prompt: "a hot air balloon",
    iterations: [
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g/><g filter="url(#roughen)"><path id="step-1" d="M 253.3 102.5 C 160.9 98.1 143.7 252.6 176.8 298.6 C 201.8 321.9 313.8 318.4 335.9 296.8 C 372.2 247.4 347.9 97.9 260.3 101.6 Z" stroke="#1a1a1a" fill="none"/><path id="step-2" d="M 184.3 296.4 Q 252.6 321.7 332.7 298.9" stroke="#1a1a1a" fill="none"/><path id="step-3" d="M 198.7 312.6 L 207.7 320.7 213.1 342.7 226.7 358.6 229.0 373.7 M 310.8 309.6 L 302.2 322.4 297.0 338.8 290.9 352.1 284.8 372.0" stroke="#1a1a1a" fill="none"/><path id="step-4" d="M 225.5 367.5 L 247.5 369.4 267.2 365.8 286.1 368.2 L 283.7 388.7 283.4 401.9 289.9 423.8 L 271.0 420.9 250.5 423.2 241.5 419.4 216.4 420.9 Z" stroke="#1a1a1a" fill="none"/></g></svg>`,
        steps: ["balloon envelope", "balloon mouth", "connecting ropes", "basket"],
        reasoning: "Starting with the most basic structural elements of a hot air balloon: the large envelope (balloon part), the opening at the bottom, the connecting lines, and the simple basket shape.",
        verdict: "revise",
        score: 6,
        ui_message: "Add the burner.",
        feedback_for_artist: "You have the main shape and the basket down. Now let's add the burner between the balloon and the basket.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 245.5 323.2 L 266.4 320.8 L 267.6 334.2 L 244.3 336.4 Z" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 257.6 320.4 L 254.3 313.8" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 253.3 102.5 C 160.9 98.1 143.7 252.6 176.8 298.6 C 201.8 321.9 313.8 318.4 335.9 296.8 C 372.2 247.4 347.9 97.9 260.3 101.6 Z" stroke="#1a1a1a" fill="none"/><path id="step-2" d="M 184.3 296.4 Q 252.6 321.7 332.7 298.9" stroke="#1a1a1a" fill="none"/><path id="step-3" d="M 198.7 312.6 L 207.7 320.7 213.1 342.7 226.7 358.6 229.0 373.7 M 310.8 309.6 L 302.2 322.4 297.0 338.8 290.9 352.1 284.8 372.0" stroke="#1a1a1a" fill="none"/><path id="step-4" d="M 225.5 367.5 L 247.5 369.4 267.2 365.8 286.1 368.2 L 283.7 388.7 283.4 401.9 289.9 423.8 L 271.0 420.9 250.5 423.2 241.5 419.4 216.4 420.9 Z" stroke="#1a1a1a" fill="none"/></g></svg>`,
        steps: ["balloon envelope", "balloon mouth", "connecting ropes", "basket", "burner body", "burner flame"],
        reasoning: "Adding the burner unit and a small flame indicator between the balloon envelope and the basket to complete the functional assembly.",
        verdict: "revise",
        score: 6,
        ui_message: "The hot air balloon looks complete.",
        feedback_for_artist: "All the main parts of the hot air balloon read clearly now, so the sketch looks complete.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 245.5 323.2 L 266.4 320.8 L 267.6 334.2 L 244.3 336.4 Z" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 257.6 320.4 L 254.3 313.8" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 253.3 102.5 C 160.9 98.1 143.7 252.6 176.8 298.6 C 201.8 321.9 313.8 318.4 335.9 296.8 C 372.2 247.4 347.9 97.9 260.3 101.6 Z" stroke="#1a1a1a" fill="none"/><path id="step-2" d="M 184.3 296.4 Q 252.6 321.7 332.7 298.9" stroke="#1a1a1a" fill="none"/><path id="step-3" d="M 198.7 312.6 L 207.7 320.7 213.1 342.7 226.7 358.6 229.0 373.7 M 310.8 309.6 L 302.2 322.4 297.0 338.8 290.9 352.1 284.8 372.0" stroke="#1a1a1a" fill="none"/><path id="step-4" d="M 225.5 367.5 L 247.5 369.4 267.2 365.8 286.1 368.2 L 283.7 388.7 283.4 401.9 289.9 423.8 L 271.0 420.9 250.5 423.2 241.5 419.4 216.4 420.9 Z" stroke="#1a1a1a" fill="none"/></g></svg>`,
        steps: ["balloon envelope", "balloon mouth", "connecting ropes", "basket", "burner body", "burner flame"],
        reasoning: "Subject reads clearly; holding the drawing.",
        verdict: "revise",
        score: 7,
        ui_message: "Add some vertical panel lines to the balloon.",
        feedback_for_artist: "You have all the main components in place. Now, let's add some vertical panel lines to the envelope to give it that classic balloon look.",
      },
      {
        svg: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><defs><filter id="roughen" x="-5%" y="-5%" width="110%" height="110%"><feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="2" seed="42"/><feDisplacementMap in="SourceGraphic" scale="1.2"/></filter></defs><g><path id="step-5" d="M 245.5 323.2 L 266.4 320.8 L 267.6 334.2 L 244.3 336.4 Z" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 257.6 320.4 L 254.3 313.8" fill="none" stroke="#1a1a1a"/><path id="step-7" d="M 206.3 127.7 Q 217.0 202.7 212.0 296.8 M 256.5 100.9 L 258.0 152.8 254.3 198.7 256.7 245.3 253.9 293.8 M 305.7 128.5 Q 292.2 203.0 302.9 297.9" fill="none" stroke="#1a1a1a"/></g><g filter="url(#roughen)"><path id="step-1" d="M 253.3 102.5 C 160.9 98.1 143.7 252.6 176.8 298.6 C 201.8 321.9 313.8 318.4 335.9 296.8 C 372.2 247.4 347.9 97.9 260.3 101.6 Z" stroke="#1a1a1a" fill="none"/><path id="step-2" d="M 184.3 296.4 Q 252.6 321.7 332.7 298.9" stroke="#1a1a1a" fill="none"/><path id="step-3" d="M 198.7 312.6 L 207.7 320.7 213.1 342.7 226.7 358.6 229.0 373.7 M 310.8 309.6 L 302.2 322.4 297.0 338.8 290.9 352.1 284.8 372.0" stroke="#1a1a1a" fill="none"/><path id="step-4" d="M 225.5 367.5 L 247.5 369.4 267.2 365.8 286.1 368.2 L 283.7 388.7 283.4 401.9 289.9 423.8 L 271.0 420.9 250.5 423.2 241.5 419.4 216.4 420.9 Z" stroke="#1a1a1a" fill="none"/><path id="step-5" d="M 245.5 323.2 L 266.4 320.8 L 267.6 334.2 L 244.3 336.4 Z" fill="none" stroke="#1a1a1a"/><path id="step-6" d="M 257.6 320.4 L 254.3 313.8" fill="none" stroke="#1a1a1a"/></g></svg>`,
        steps: ["balloon envelope", "balloon mouth", "connecting ropes", "basket", "burner body", "burner flame", "panel lines"],
        reasoning: "To give the hot air balloon envelope a classic segmented look, I will add three curved vertical lines (panel lines) that follow the contour of the balloon, extending from the top curve down to the mouth of the envelope.",
        verdict: "accept",
        score: 10,
        ui_message: "Great job!",
        feedback_for_artist: "",
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
