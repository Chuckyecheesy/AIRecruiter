"""Fiber: contact lookup (spec 02). READ.

Demo safety: every result's email is rewritten by `DemoInboxFiber` (applied in build_tools()),
so nothing real can ever be targeted.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import httpx

from api.tools.base import ToolError
from api.tools.models import ContactInfo
from api.tools.http import json_body, raise_for_status

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "fiber_contacts.json"
PLOID_FIXTURE = FIXTURE.parent / "ploid_candidates.json"


class MockFiberClient:
    def __init__(self, fixture: Path = FIXTURE):
        self._by_id: dict[str, dict] = json.loads(fixture.read_text())
        # resolve by name too (find_contact only gets name + links)
        rows = json.loads(PLOID_FIXTURE.read_text())
        # First row wins, so the original (pl_001) keeps its own contact, not the duplicate's.
        self._name_to_id: dict[str, str] = {}
        self._url_to_id: dict[str, str] = {}
        for r in rows:
            self._name_to_id.setdefault(_key(r["name"]), r["ploid_id"])
            for u in r["profile_urls"]:
                self._url_to_id.setdefault(u, r["ploid_id"])

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
        raise_for_status(r, "fiber")
        d = json_body(r, "fiber")
        if not isinstance(d, dict):
            raise ToolError("transient", "fiber: unexpected response shape")
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
        if info.email and _base(info.email) == _base(self._inbox):
            # Already the demo inbox itself (e.g. the team's own address): keep it exactly.
            return info.model_copy(update={"phone": None})
        return info.model_copy(update={"email": demo_address(self._inbox, name), "phone": None})


def demo_address(inbox: str, tag: str) -> str:
    """demo@example.com + "Sarah Chen" -> demo+sarah-chen@example.com (ASCII, never an empty tag).

    Lane A can pass the candidate_id instead of a name if they prefer `+{candidate_id}`.
    """
    local, at, domain = inbox.strip().partition("@")
    if not at or not local or not domain:
        raise ValueError(f"DEMO_INBOX must be an email address, got {inbox!r}")
    ascii_tag = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode().lower()
    slug = "-".join("".join(c if c.isalnum() else " " for c in ascii_tag).split())
    if not slug:  # e.g. "!!!" or a name in a non-Latin script: stable short hash instead
        slug = "c" + hashlib.sha256(tag.encode()).hexdigest()[:8]
    return f"{local}+{slug[:40]}@{domain}"


def _base(addr: str) -> str:
    local, _, domain = addr.strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


def _key(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())
