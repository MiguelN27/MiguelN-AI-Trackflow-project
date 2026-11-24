"""Who approves each department's section: its owner (briefing §2.1), and only them.

The owner's *name* comes from the briefing (`briefing.DEPARTMENTS`). Which
signed-in account is that owner comes from configuration (`RFP_APPROVERS`, a
department -> email map), decided with the developer on 2026-11-23: the user
accounts carry no department, and an approval must come from the owner's own
account. Nobody decides on another owner's behalf, admins included, because
TrackFlow has peer department owners only (briefing §6: no approval ladder).
"""

from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID
from data.pipelines.rfp_intake.config import RfpIntakeSettings


def owner_of(department_id: str) -> str:
    """The briefing's owner of a department, who approves its section."""
    return DEPARTMENT_BY_ID[department_id].owner


def approver_email(settings: RfpIntakeSettings, department_id: str) -> str | None:
    """The account configured for the department's owner, if any."""
    return settings.approvers.get(department_id)


def is_owner(settings: RfpIntakeSettings, department_id: str, email: str | None) -> bool:
    configured = approver_email(settings, department_id)
    return bool(configured and email and email.strip().lower() == configured)


def departments_of(settings: RfpIntakeSettings, email: str | None) -> list[str]:
    """The departments whose owner signs in with this account (normally one)."""
    if not email:
        return []
    wanted = email.strip().lower()
    return [dept for dept, configured in settings.approvers.items() if configured == wanted]
