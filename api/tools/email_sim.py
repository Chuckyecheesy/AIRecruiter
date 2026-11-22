"""Outbound email (spec 02). RESTRICTED.

Simulated by default. A REAL send happens only when ALLOW_REAL_EMAIL=true AND the recipient's base
address (plus-tag stripped: me+sarah@gmail.com -> me@gmail.com) is on EMAIL_ALLOWLIST AND SMTP is
configured. With DEMO_INBOX=me@gmail.com every candidate becomes me+<name>@gmail.com, so the demo can
email the team's own inbox and never a real candidate.

Pure: returns a SendReceipt. Lane A writes the `messages` row + contact_log and emits EmailSent.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage

from ulid import ULID

from api.tools.base import ToolError
from api.tools.models import SendReceipt

log = logging.getLogger(__name__)


def base_address(addr: str) -> str:
    local, _, domain = addr.strip().lower().partition("@")
    return f"{local.split('+', 1)[0]}@{domain}"


class EmailSimulator:
    def __init__(self, allow_real_email: bool = False, allowlist: set[str] | None = None,
                 smtp: dict | None = None):
        self._allow_real = allow_real_email
        self._allowlist = {base_address(a) for a in (allowlist or set())}
        self._smtp = smtp or {}

    def real_send_allowed(self, to: str) -> bool:
        return (self._allow_real and base_address(to) in self._allowlist
                and bool(self._smtp.get("host") and self._smtp.get("user") and self._smtp.get("password")))

    async def send(self, to: str, subject: str, body: str, thread_id: str) -> SendReceipt:
        if not to or "@" not in to:
            raise ToolError("invalid_input", f"bad recipient {to!r}")
        if not body.strip():
            raise ToolError("invalid_input", "empty body")
        if self.real_send_allowed(to):
            await asyncio.to_thread(self._smtp_send, to, subject, body)
            log.info("email: REAL send to %s (allowlisted)", to)
            return SendReceipt(message_id=f"smtp_{ULID()}", to=to, thread_id=thread_id, simulated=False,
                               sent_at=datetime.now(timezone.utc))
        return SendReceipt(
            message_id=f"sim_{ULID()}",
            to=to,
            thread_id=thread_id,
            simulated=True,
            sent_at=datetime.now(timezone.utc),
        )

    def _smtp_send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._smtp.get("from") or self._smtp["user"]
        msg["To"] = to
        msg["Subject"] = subject or "(no subject)"
        msg.set_content(body)
        try:
            with smtplib.SMTP(self._smtp["host"], int(self._smtp.get("port") or 587), timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(self._smtp["user"], self._smtp["password"])
                s.send_message(msg)
        except smtplib.SMTPAuthenticationError as e:
            raise ToolError("fatal", "smtp auth failed (use a Gmail app password)") from e
        except (smtplib.SMTPException, OSError) as e:
            raise ToolError("transient", f"smtp error: {type(e).__name__}") from e
