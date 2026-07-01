# Learn to Draw, Step by Step

Artist-Critic loop for SVG sketches: an **Artist** writes SVG, a **Critic**
looks at the rendered image, and the drawing is revised step by step.

## Run locally

Requirements:

- [Ollama](https://ollama.com) running, with the models pulled:
  ```bash
  ollama pull gemma4:26b
  ollama pull blaifa/InternVL3_5:8b
  ```
- Cairo & Pango (to render SVG). On macOS: `brew install cairo pango`

Install dependencies and start:

```bash
pip install -r backend/requirements.txt
make local
```

Then open <http://127.0.0.1:8001>.
