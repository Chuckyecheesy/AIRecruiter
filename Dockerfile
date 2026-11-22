# recruiter-api: FastAPI + LangGraph agent + dispatcher + lane B tools (spec 07).
#
# Local:     docker compose up --build          (or, by hand:)
#            docker build -t airecruiter-api .
#            docker run --rm -p 8000:8000 --env-file .env -e DATA_DIR=/data -e CHECKPOINT_DB=/data/checkpoints.db \
#                       -v airecruiter-data:/data airecruiter-api
#            (.env's DATA_DIR=./data would otherwise override /data and skip the volume)
# Maritime:  maritime create airecruiter-api --repo https://github.com/Chuckyecheesy/AIRecruiter --public
#            (Maritime builds this file from the repo root, injects $PORT, and persists only /data.)
# Browser tool (stretch): docker build --build-arg WITH_BROWSER=true ...
FROM python:3.12-slim

ARG WITH_BROWSER=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DATA_DIR=/data \
    CHECKPOINT_DB=/data/checkpoints.db \
    PORT=8000

WORKDIR /app

# Dependencies first (cached layer): read them straight from pyproject.toml. The app itself is NOT
# pip-installed -- fixtures/ and schema.sql are loaded relative to the source tree in /app.
COPY pyproject.toml ./
RUN python -c "import tomllib; d = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(d['dependencies'] + (d.get('optional-dependencies', {}).get('browser', []) if '${WITH_BROWSER}' == 'true' else [])))" \
      > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && if [ "$WITH_BROWSER" = "true" ]; then python -m playwright install --with-deps chromium; fi

COPY api ./api
COPY fixtures ./fixtures
COPY scripts ./scripts

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)" || exit 1

# Shell form so $PORT expands (Maritime injects it; never hardcode 8080 there).
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
