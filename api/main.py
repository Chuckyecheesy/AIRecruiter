"""FastAPI app factory (lane C)."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routes import admin, approvals, candidates, chat, events, jobs
from api.routes.deps import start_dispatcher, stop_dispatcher

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("DISPATCHER_ENABLED", "true").lower() not in {"0", "false", "no"}:
        await start_dispatcher()
    yield
    await stop_dispatcher()


def create_app() -> FastAPI:
    app = FastAPI(title="AIRecruiter", lifespan=lifespan)

    origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    demo_token = os.environ.get("DEMO_TOKEN")
    if demo_token:

        @app.middleware("http")
        async def require_demo_token(request: Request, call_next):
            path = request.url.path
            exempt = request.method == "OPTIONS" or path in {"/health", "/api/health"} or path.startswith("/api/webhooks")
            # EventSource can't set headers, so the stream also accepts ?token=
            token = request.headers.get("X-Demo-Token") or request.query_params.get("token")
            if not exempt and token != demo_token:
                return JSONResponse({"detail": "missing or bad X-Demo-Token"}, status_code=401)
            return await call_next(request)

    for r in (jobs, candidates, approvals, events, chat, admin):
        app.include_router(r.router, prefix="/api")

    try:  # lane B's ElevenLabs webhook router
        from api.routes import webhooks  # type: ignore

        app.include_router(webhooks.router, prefix="/api")
    except ImportError:
        logging.getLogger("api").info("webhooks router not available yet (lane B)")

    # Unprefixed /health for the Docker healthcheck (spec 07).
    app.add_api_route("/health", admin.health, methods=["GET"])
    return app


app = create_app()
