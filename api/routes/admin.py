"""Admin + health routes (lane C)."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from api.db import queries
from api.routes.deps import tools_status

router = APIRouter(tags=["admin"])


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


@router.post("/admin/reset")
def reset(seed: bool = True):
    if not _truthy(os.environ.get("ADMIN_ENABLED")):
        raise HTTPException(403, "admin disabled (set ADMIN_ENABLED=true)")
    from scripts.reset_demo import reset_demo

    job_id = reset_demo(seed=seed)
    return {"ok": True, "jobId": job_id}


def health_payload() -> dict:
    db = "ok"
    try:
        conn = queries.connect()
        conn.execute("SELECT 1 FROM jobs LIMIT 1")
        conn.close()
    except Exception as e:
        db = f"error: {e}"
    return {
        "status": "ok",
        "db": db,
        "toolsMode": os.environ.get("TOOLS_MODE", "mock"),
        "llmMode": os.environ.get("LLM_MODE", "real"),
        "tools": tools_status(),
    }


@router.get("/health")
def health():
    return health_payload()
