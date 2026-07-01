# Learn to Draw, Step by Step

Artist-Critic loop for SVG sketches: an **Artist** writes SVG, a **Critic**
looks at the rendered image, and the drawing is revised step by step.

## Repository layout

```text
backend/    FastAPI service, model clients, renderer
frontend/   Static web interface
```

## Requirements

- **Python 3.10+**
- **[Ollama](https://ollama.com)** running locally, with the two models pulled:
  ```bash
  ollama pull gemma4:26b
  ollama pull blaifa/InternVL3_5:8b
  ```
- **Cairo & Pango** native libraries (needed to render SVG to PNG). On macOS:
  ```bash
  brew install cairo pango
  ```
  On Debian/Ubuntu install the packages listed in `backend/packages.txt`.

Python dependencies are listed in `backend/requirements.txt` and are installed
automatically by `make local` into a local `.venv`.

## Run locally

```bash
make local
```

Then open <http://127.0.0.1:8001>.

### Options

Use different Ollama models:

```bash
make local ARTIST=gemma3:27b CRITIC=blaifa/InternVL3_5:8b
```

Use a different port if `8001` is busy:

```bash
make local PORT=8002
```
