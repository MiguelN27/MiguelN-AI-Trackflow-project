"""What an owner is asked, what they answer, and the checks an answer must pass.

`ApprovalRequest` is the `interrupt` payload of a department's branch: the
section under review, its round, the draft's SHA-256 and the actions still
allowed. `ApprovalDecision` is the value the graph is resumed with.

`check_decision` is the one rule set, used twice (ticket: "validate the human
response on resume ... before letting it back into the graph"):
- by the API, before a decision is stored (`runner.submit_decision`), so the
  owner gets a clear refusal at once;
- by the graph's `validate_decision` node, on the value it is resumed with,
  because a resume can also come from the CLI or from code.

A decision answers one request: same department, same round, same draft. If
the section moved on (revised, or decided already), the decision is stale.
"""

import hashlib
from datetime import datetime
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from data.pipelines.rfp_intake.approval import approvers
from data.pipelines.rfp_intake.briefing import DepartmentId
from data.pipelines.rfp_intake.config import RfpIntakeSettings

Action = Literal["approve", "request_changes", "reject"]
ACTIONS: Final[tuple[Action, ...]] = ("approve", "request_changes", "reject")
# Actions that need a comment: the generator (or Sales) must know why.
COMMENT_REQUIRED: Final = frozenset({"request_changes", "reject"})
COMMENT_MAX_CHARS: Final = 2000

ACTION_LABELS: Final[dict[str, str]] = {
    "approve": "approved",
    "request_changes": "requested changes",
    "reject": "rejected",
    "withdrawn": "withdrawn",
}


def draft_sha256(markdown: str | None) -> str:
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()


def allowed_actions(revisions_used: int, max_revision_rounds: int) -> list[Action]:
    """Every action, except request_changes once the section has used its revisions."""
    if revisions_used >= max_revision_rounds:
        return ["approve", "reject"]
    return list(ACTIONS)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalRequest(_Contract):
    """The interrupt payload: one department's section, waiting for its owner."""

    kind: Literal["department_approval"] = "department_approval"
    ticket_id: UUID
    section_id: UUID
    department_id: DepartmentId
    department_name: str
    owner: str
    approver_configured: bool
    # 1 for Part 2's draft, then one more per Part 3 revision.
    review_round: int = Field(ge=1)
    revisions_used: int = Field(ge=0)
    max_revision_rounds: int = Field(ge=1)
    allowed_actions: list[Action]
    draft_sha256: str
    draft_status: str | None
    needs_human_review: bool
    # The arbitration ruling or owner request this round answers, if any.
    reason: str | None = None
    requested_at: datetime
    # Why the previous answer to this request was refused, if it was.
    error: str | None = None


class Decider(_Contract):
    source: Literal["owner", "system"]
    email: str | None = None
    name: str


class ApprovalDecision(_Contract):
    """The value a department's branch is resumed with."""

    decision_id: UUID
    department_id: DepartmentId
    review_round: int = Field(ge=1)
    draft_sha256: str
    action: Literal["approve", "request_changes", "reject", "withdrawn"]
    comment: str | None = Field(default=None, max_length=COMMENT_MAX_CHARS)
    decided_by: Decider
    decided_at: datetime


class DecisionRefused(ValueError):
    """A decision that cannot be taken. `code` maps to an HTTP status; the message is safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# code -> HTTP status, for the API.
REFUSAL_STATUS: Final[dict[str, int]] = {
    "not_found": 404,
    "not_owner": 403,
    "not_waiting": 409,
    "stale": 409,
    "already_decided": 409,
    "limit": 409,
    "comment_required": 422,
    "invalid": 422,
}


def clean_comment(comment: str | None) -> str | None:
    text = " ".join((comment or "").split())
    return text or None


def check_decision(
    *,
    settings: RfpIntakeSettings,
    department_id: str,
    review_round: int,
    draft_sha: str,
    revisions_used: int,
    action: str,
    comment: str | None,
    source: str,
    decided_by_email: str | None,
    expected_round: int,
    expected_sha: str,
) -> None:
    """Raise `DecisionRefused` unless this decision may answer the open request."""
    if source == "system":
        if action != "withdrawn":
            raise DecisionRefused("invalid", "The system can only withdraw a request.")
    else:
        if action not in ACTIONS:
            raise DecisionRefused("invalid", "Choose approve, request changes or reject.")
        if not approvers.is_owner(settings, department_id, decided_by_email):
            owner = approvers.owner_of(department_id)
            if approvers.approver_email(settings, department_id) is None:
                raise DecisionRefused(
                    "not_owner",
                    f"No account is configured for {owner}, so nobody can decide on this "
                    "section yet (RFP_APPROVERS).",
                )
            raise DecisionRefused(
                "not_owner", f"Only {owner} can decide on this section, from their own account."
            )
    if review_round != expected_round or draft_sha != expected_sha:
        raise DecisionRefused(
            "stale",
            "This section changed since you opened it (it was revised or decided). "
            "Reload it and review the current draft.",
        )
    if action in COMMENT_REQUIRED and not clean_comment(comment):
        verb = "requesting changes" if action == "request_changes" else "rejecting"
        raise DecisionRefused(
            "comment_required", f"Say what needs to change: a comment is required when {verb}."
        )
    if action == "request_changes" and revisions_used >= settings.max_revision_rounds:
        raise DecisionRefused(
            "limit",
            f"This section has been revised {revisions_used} time(s), the limit "
            f"({settings.max_revision_rounds}). Approve it or reject it.",
        )


def parse_decision(value: Any) -> ApprovalDecision:
    """The resume value as an `ApprovalDecision`, or `DecisionRefused('invalid')`."""
    try:
        return ApprovalDecision.model_validate(value)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise DecisionRefused(
            "invalid", "The decision is malformed: " + ", ".join(fields or ["value"])
        ) from None
