from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    db = tmp_path / "recruiter.db"
    monkeypatch.setenv("RECRUITER_DB", str(db))
    monkeypatch.setenv("DISPATCHER_ENABLED", "false")
    monkeypatch.setenv("LLM_MODE", "stub")
    monkeypatch.setenv("ADMIN_ENABLED", "true")

    from scripts.reset_demo import reset_demo

    job_id = reset_demo(seed=True, path=db)
    from api.db import repo

    repo.init_db(db)
    return job_id


@pytest.fixture()
def client(seeded):
    from api.main import create_app
    with TestClient(create_app()) as c:
        yield c
