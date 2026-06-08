ARTIST ?= gemma4:26b
CRITIC ?= blaifa/InternVL3_5:8b
PORT ?= 8001
BACKEND_DIR := apps/backend

.PHONY: local

local:
	@test -d .venv || python3 -m venv .venv
	@. .venv/bin/activate && pip install -q -r $(BACKEND_DIR)/requirements.txt && cd $(BACKEND_DIR) && DEPLOYMENT_PROFILE=local OLLAMA_ARTIST_MODEL="$(ARTIST)" OLLAMA_CRITIC_MODEL="$(CRITIC)" PORT="$(PORT)" python -m api.sketch_stream
