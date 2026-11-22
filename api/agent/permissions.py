"""Permission classes (specs/03-permissions.md). Policy/auto-approve logic lands in the next slice."""
from __future__ import annotations

from api.contracts import ActionName as A, PermissionClass

RESTRICTED_ACTIONS = frozenset(
    {A.send_outreach, A.send_followup, A.schedule_interview, A.reject_candidate_proposal})
_WRITE = frozenset({A.draft_outreach, A.redraft_outreach, A.draft_reply, A.shortlist,
                    A.mark_stale, A.process_reply, A.process_interview, A.evaluate_candidate})


def permission_class(action: A) -> PermissionClass:
    if action in RESTRICTED_ACTIONS:
        return PermissionClass.RESTRICTED
    return PermissionClass.WRITE if action in _WRITE else PermissionClass.READ
