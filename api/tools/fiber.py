"""Fiber: contact lookup (spec 02). READ.

Demo safety: every result's email is rewritten by `rewrite_to_demo_inbox` (applied in get_tools()),
so nothing real can ever be targeted.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from api.tools.base import ToolError
from api.tools.models import ContactInfo
from api.tools.ploid import _raise_for_status

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "fiber_contacts.json"
PLOID_FIXTURE = FIXTURE.parent / "ploid_candidates.json"


class MockFiberClient:
    def __init__(self, fixture: Path = FIXTURE):
        self._by_id: dict[str, dict] = json.loads(fixture.read_text())
        # resolve by name too (find_contact only gets name + links)
        rows = json.loads(PLOID_FIXTURE.read_text())
        self._name_to_id = {_key(r["name"]): r["ploid_id"] for r in rows}
        self._url_to_id = {u: r["ploid_id"] for r in rows for u in r["profile_urls"]}

    async def find_contact(self, name: str, links: dict[str, str], company_hint: str | None) -> ContactInfo:
        pid = next((self._url_to_id[u] for u in links.values() if u in self._url_to_id), None)
        pid = pid or self._name_to_id.get(_key(name))
        hit = self._by_id.get(pid) if pid else None
        if not hit:
            raise ToolError("not_found", f"no contact found for {name}")
        return ContactInfo(**hit)


class RealFiberClient:
    """TBD at the hackathon: endpoint/auth unconfirmed."""

    def __init__(self, api_key: str, base_url: str):
        self._http = httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20
        )

    async def find_contact(self, name: str, links: dict[str, str], company_hint: str | None) -> ContactInfo:
        try:
            r = await self._http.post(
                "/contact/lookup", json={"name": name, "links": links, "company": company_hint}
            )
        except httpx.TransportError as e:
            raise ToolError("transient", f"fiber network error: {e}") from e
        _raise_for_status(r, "fiber")
        d = r.json()
        if not d.get("email") and not d.get("phone"):
            raise ToolError("not_found", f"no contact found for {name}")
        return ContactInfo(
            email=d.get("email"), phone=d.get("phone"),
            confidence=float(d.get("confidence", 0.5)), source="fiber",
        )


class DemoInboxFiber:
    """Wraps any FiberClient: email -> DEMO_INBOX with a +tag, phone dropped."""

    def __init__(self, inner, demo_inbox: str):
        self._inner = inner
        self._inbox = demo_inbox

    async def find_contact(self, name: str, links: dict[str, str], company_hint: str | None) -> ContactInfo:
        info = await self._inner.find_contact(name, links, company_hint)
        return info.model_copy(update={"email": demo_address(self._inbox, name), "phone": None})


def demo_address(inbox: str, tag: str) -> str:
    """demo@example.com + "Sarah Chen" -> demo+sarah-chen@example.com.

    Lane A can call this with the candidate_id instead of a name if they prefer `+{candidate_id}`.
    """
    local, _, domain = inbox.partition("@")
    slug = "".join(c if c.isalnum() else "-" for c in tag.lower()).strip("-")
    slug = "-".join(p for p in slug.split("-") if p)
    return f"{local}+{slug}@{domain}"


def _key(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())
