"""Permission layer (specs/03-permissions.md).

Capability matrix + per-job policy decide whether a RESTRICTED action needs the CEO,
is auto-approved, or is allowed outright. `execute_approved` is the only way a restricted
action runs: it needs an `approved` row, claims it atomically (approved -> executed) so it
runs at most once, and uses the STORED payload.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional

from api.contracts import ActionName as A, Approval, PermissionClass
from api.db import repo

RESTRICTED_ACTIONS = frozenset(
    {A.send_outreach, A.send_followup, A.schedule_interview, A.reject_candidate_proposal})
_WRITE = frozenset({A.draft_outreach, A.redraft_outreach, A.draft_reply, A.shortlist,
                    A.mark_stale, A.process_reply, A.process_interview, A.evaluate_candidate})


def permission_class(action: A) -> PermissionClass:
    if action in RESTRICTED_ACTIONS:
        return PermissionClass.RESTRICTED
    return PermissionClass.WRITE if action in _WRITE else PermissionClass.READ


class Verdict(str, Enum):
    allow = "allow"
    auto_approve = "auto_approve"
    require_approval = "require_approval"


class PermissionDenied(Exception):
    """A restricted action was attempted without a valid, unexecuted approval."""


def check_permission(action: A, policy: Mapping[str, Any] | repo.PermissionPolicy | None) -> Verdict:
    action = A(action)
    if permission_class(action) != PermissionClass.RESTRICTED:
        return Verdict.allow
    pol = policy if isinstance(policy, repo.PermissionPolicy) else repo.PermissionPolicy(**(policy or {}))
    if action not in pol.restricted_actions:
        return Verdict.allow
    if pol.auto_approve:
        listed = action in pol.auto_approve_actions
        if action == A.reject_candidate_proposal:
            # Irreversible and sensitive: only when explicitly listed.
            return Verdict.auto_approve if listed else Verdict.require_approval
        if listed or not pol.auto_approve_actions:
            return Verdict.auto_approve
    return Verdict.require_approval


def assert_approved(approval: Optional[Approval], action: A) -> None:
    """Defense in depth: restricted execution requires an approved row for this very action."""
    if approval is None:
        raise PermissionDenied(f"{A(action).value}: no approval")
    if approval.status != "approved":
        raise PermissionDenied(f"{A(action).value}: approval {approval.id} is {approval.status}")
    if approval.action != A(action):
        raise PermissionDenied(
            f"approval {approval.id} is for {approval.action.value}, not {A(action).value}")


async def execute_approved(approval_id: str, fn: Callable[[dict], Awaitable[Any]]) -> Any:
    """Run `fn(stored_payload)` at most once for an approved approval.

    The claim (approved -> executed) is atomic. If `fn` fails, the claim is released so a
    retry can still send: a send that never happened must not look executed.
    """
    approval = repo.get_approval(approval_id)
    if approval is None:
        raise PermissionDenied(f"approval {approval_id} not found")
    assert_approved(approval, approval.action)
    if not repo.mark_approval_executed(approval_id):
        raise PermissionDenied(f"approval {approval_id} was already executed")
    try:
        return await fn(approval.payload)
    except BaseException:
        repo.release_approval_claim(approval_id)
        raise
