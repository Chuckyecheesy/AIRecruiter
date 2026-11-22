"""Ploid: candidate discovery + enrichment (spec 02). READ."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from api.tools.base import ToolError
from api.tools.http import json_body, path_id, raise_for_status
from api.tools.models import EnrichedProfile, PloidCandidate, SearchCriteria

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "ploid_candidates.json"


class MockPloidClient:
    """Returns fixture candidates in fixture order (the duplicate is 3rd, so it's always in range).

    Lane A: request limit >= max_candidates + 1 so the duplicate is seen and skipped.
    """

    def __init__(self, fixture: Path = FIXTURE):
        self._rows = json.loads(fixture.read_text())
        self._by_id = {r["ploid_id"]: r for r in self._rows}

    async def search(self, criteria: SearchCriteria, limit: int) -> list[PloidCandidate]:
        if not criteria.title.strip():
            raise ToolError("invalid_input", "criteria.title is required")
        return [
            PloidCandidate(**{k: r[k] for k in ("ploid_id", "name", "headline", "location", "profile_urls")})
            for r in self._rows[:limit]
        ]

    async def enrich(self, ploid_id: str) -> EnrichedProfile:
        row = self._by_id.get(ploid_id)
        if row is None:
            raise ToolError("not_found", f"no Ploid profile {ploid_id}")
        return EnrichedProfile(**row["profile"])


class RealPloidClient:
    """TBD at the hackathon: endpoints/auth unconfirmed (spec 00 open items).

    Shape guess: POST {base}/search, GET {base}/profiles/{id}. Adjust once docs are in hand;
    keep the ToolError mapping.
    """

    def __init__(self, api_key: str, base_url: str):
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20
        )

    async def _req(self, method: str, url: str, **kw):
        try:
            r = await self._http.request(method, url, **kw)
        except httpx.TransportError as e:
            raise ToolError("transient", f"ploid network error: {e}") from e
        raise_for_status(r, "ploid")
        return json_body(r, "ploid")

    async def search(self, criteria: SearchCriteria, limit: int) -> list[PloidCandidate]:
        data = await self._req("POST", "/search", json={**criteria.model_dump(), "limit": limit})
        rows = data.get("results", []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ToolError("transient", "ploid: unexpected search response shape")
        return [PloidCandidate.model_validate(c) for c in rows]

    async def enrich(self, ploid_id: str) -> EnrichedProfile:
        data = await self._req("GET", f"/profiles/{path_id(ploid_id)}")
        if not isinstance(data, dict):
            raise ToolError("transient", "ploid: unexpected profile response shape")
        data.setdefault("ploid_id", ploid_id)
        data.setdefault("raw", dict(data))
        data.setdefault("completeness", _completeness(data))
        return EnrichedProfile.model_validate(data)


def _completeness(d: dict) -> float:
    fields = ["skills", "projects", "education", "experience", "links"]
    return sum(1 for f in fields if d.get(f)) / len(fields)


# Back-compat alias (tests and other adapters import it from here).
_raise_for_status = raise_for_status
