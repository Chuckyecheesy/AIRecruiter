# Lane C. Windows without make: run the two commands under `dev` in two terminals.
PY ?= .venv/bin/python
ifeq ($(OS),Windows_NT)
PY = .venv/Scripts/python
endif

.PHONY: dev api web reset smoke test install

install:
	$(PY) -m pip install fastapi "uvicorn[standard]" pydantic pydantic-settings sse-starlette httpx pytest
	cd web && npm install

dev:
	$(MAKE) -j2 api web

api:
	$(PY) -m uvicorn api.main:app --reload --port 8000

web:
	cd web && npm run dev

reset:
	$(PY) scripts/reset_demo.py

smoke:
	$(PY) scripts/smoke.py

test:
	$(PY) -m pytest
