"""Route dependencies + the single seam to other lanes (lane C).

Every call from lane C into lane A (repo writes, dispatcher) or lane B (rubric, tools_status)
goes through this module. When their signatures land or change, only this file changes.

Expected lane A interface (api/db/repo.py), per specs/team/person-a-agent-core.md:
    create_job(form: dict, rubric: dict | None) -> str
    update_job(job_id: str, **fields) -> None
    set_job_state(job_id: str, state: JobState) -> None
    emit_event(job_id: str, candidate_id: str | None, type: EventType, payload: dict) -> int
    decide_approval(approval_id: str, approved: bool, *, payload: dict | None = None,
                    feedback: str | None = None, decided_by: str = "ceo") -> None
    resume_candidate(candidate_id: str) -> None   # NEEDS_HUMAN -> prev_state, emits HumanResolved
    skip_candidate(candidate_id: str) -> None     # -> REJECTED, logged
    set_demo_disposition(candidate_id: str, disposition: str | None) -> None
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from types import ModuleType
from typing import Any

from fastapi import HTTPException

from api.contracts import JobForm, Rubric, RubricCriterion
from api.db import queries

log = logging.getLogger("api.deps")


# ───────────────────────────── DB (reads) ─────────────────────────────


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = queries.connect()
    try:
        yield conn
    finally:
        conn.close()


# ───────────────────────────── lane A: repo ─────────────────────────────

_repo_override: ModuleType | Any | None = None


def set_repo(repo: Any | None) -> None:
    """Tests (and the dev fake) can inject a repo implementation."""
    global _repo_override
    _repo_override = repo


def get_repo() -> Any:
    if _repo_override is not None:
        return _repo_override
    if os.environ.get("REPO_IMPL") == "fake":  # dev only, until lane A's repo lands
        from scripts import dev_fake_repo

        return dev_fake_repo
    try:
        from api.db import repo  # type: ignore[attr-defined]
    except ImportError as e:  # lane A not merged yet
        raise HTTPException(503, f"repo not available yet (lane A): {e}") from e
    return repo


def call_repo(name: str, *args: Any, **kwargs: Any) -> Any:
    repo = get_repo()
    fn = getattr(repo, name, None)
    if fn is None:
        raise HTTPException(503, f"repo.{name} not implemented yet (lane A)")
    try:
        return fn(*args, **kwargs)
    except NotImplementedError as e:
        raise HTTPException(503, f"repo.{name} not implemented yet (lane A)") from e


# ───────────────────────────── lane A: dispatcher ─────────────────────────────


async def start_dispatcher() -> None:
    try:
        from api.agent.dispatcher import start_dispatcher as _start  # type: ignore
    except ImportError:
        log.warning("api.agent.dispatcher not available yet; agent loop is not running")
        return
    res = _start()
    if hasattr(res, "__await__"):
        await res


async def stop_dispatcher() -> None:
    try:
        from api.agent.dispatcher import stop_dispatcher as _stop  # type: ignore
    except ImportError:
        return
    res = _stop()
    if hasattr(res, "__await__"):
        await res


# ───────────────────────────── lane B: tools ─────────────────────────────


def stub_rubric(form: JobForm) -> Rubric:
    """Deterministic rubric used until B's RubricGenerator lands (or if it fails)."""
    must = form.must_haves[:4] or [form.title]
    nice = form.nice_to_haves[:2]
    n_must, n_nice = len(must), len(nice)
    must_w = 0.75 if nice else 1.0
    criteria = [RubricCriterion(name=m, weight=round(must_w / n_must, 3), description=f"Must have: {m}") for m in must]
    criteria += [RubricCriterion(name=n, weight=round(0.25 / n_nice, 3), description=f"Nice to have: {n}") for n in nice]
    themes = [f"Walk through a project using {m}" for m in must[:3]] + ["Why this role, why now"]
    return Rubric(criteria=criteria, interview_themes=themes)


async def generate_rubric(form: JobForm) -> Rubric:
    if os.environ.get("LLM_MODE") == "stub":
        return stub_rubric(form)
    try:
        from api.tools.rubric import RubricGenerator  # type: ignore
    except ImportError:
        log.info("RubricGenerator not available yet; using stub rubric")
        return stub_rubric(form)
    try:
        gen = RubricGenerator()
        res = gen.generate(form)
        if hasattr(res, "__await__"):
            res = await res
        return res if isinstance(res, Rubric) else Rubric.model_validate(res.model_dump() if hasattr(res, "model_dump") else res)
    except Exception as e:  # never block role creation on the rubric
        log.warning("RubricGenerator failed (%s); using stub rubric", e)
        return stub_rubric(form)


def tools_status() -> dict[str, str]:
    try:
        from api.tools import tools_status as _ts  # type: ignore
    except ImportError:
        return {"tools": "unavailable (lane B not merged)"}
    try:
        return dict(_ts())
    except Exception as e:
        return {"tools": f"error: {e}"}
