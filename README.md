# Learn to Draw, Step by Step

Artist-Critic loop for SVG sketches: an **Artist** writes SVG, a **Critic**
looks at the rendered image, and the drawing is revised step by step.

## Repository layout

```text
apps/backend/   FastAPI service, model clients, renderer, Dockerfile
apps/frontend/  Static thesis interface deployed on Vercel
docs/thesis/    Bachelor thesis sources, figures, and experiment scripts
scripts/        Build helpers
```

## Local

Prerequisite: Ollama is running and the two models below are pulled.

```bash
make local
```

Open <http://127.0.0.1:8001>.

To use different local models:

```bash
make local ARTIST=gemma3:27b CRITIC=blaifa/InternVL3_5:8b
```

If port `8001` is busy:

```bash
make local PORT=8002
```

If rendering fails because Cairo is missing, install the native packages from
`apps/backend/packages.txt`. On macOS:

```bash
brew install cairo pango
```

## Hosted

### Render backend

Create a Render **Free Web Service** from this repository and select Docker.
Set the Render root directory to `apps/backend`; Render will use
`apps/backend/Dockerfile`.

Set these environment variables on Render:

```env
DEPLOYMENT_PROFILE=hosted
CORS_ALLOW_ORIGINS=https://your-vercel-app.vercel.app
GEMINI_API_KEY=...
GEMINI_ARTIST_MODEL=gemini-3.1-flash-lite
GEMINI_CRITIC_MODEL=gemini-3.1-flash-lite
```

The container starts:

```bash
uvicorn api.sketch_stream:app --host 0.0.0.0 --port $PORT
```

### Vercel frontend

Deploy the repository to Vercel with the project root set to `apps/frontend`.
Vercel will use `apps/frontend/vercel.json`. Set:

```env
SKETCH_API_BASE=https://your-render-service.onrender.com
```

The Vercel build writes this value into `dist/config.js`. The browser pings
`/health` as soon as the page opens, so a sleeping Render service starts waking
up before the user begins a live run.

Hosted mode exposes only live cloud runs and recorded runs. Local mode exposes
only the local backend and the loaded Ollama models.
