"""The fixed vocabulary of the RFP workflow, taken from the briefing.

The briefing is the root `agentic-workflow-context.md` (the ticket calls it
`company-BRIEFING.md`). Department ids, owners, what each contributes, the
ticket statuses and the currency rule are its own words; a test parses its
tables so the two cannot drift. Parts 2 and 3 reuse these names.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal


class TicketStatus(StrEnum):
    """Briefing §2.3, "Ticket status by part" (same ticket across Parts 1-3)."""

    ANALYZING = "analyzing"
    DISCARDED = "discarded"
    INTAKE_COMPLETE = "intake_complete"
    DRAFTING = "drafting"
    UNDER_EVALUATION = "under_evaluation"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    DONE = "done"
    # Not in the briefing. A technical failure (unreadable PDF, model gateway
    # down, abandoned run) is not a classification: marking it `discarded`
    # would count it against the "correct classification rate" KPI (§3).
    FAILED = "failed"


BRIEFING_STATUSES: Final = frozenset(status for status in TicketStatus if status != "failed")

# Where a Part 1 run can end. A ticket in any of them can be read in full.
PART_1_OUTCOMES: Final = frozenset(
    {TicketStatus.DISCARDED, TicketStatus.INTAKE_COMPLETE, TicketStatus.FAILED}
)

DepartmentId = Literal["warehouse", "lastmile", "reverse"]
Service = Literal["warehousing", "last_mile", "reverse_logistics"]
Country = Literal["US", "ES"]
Currency = Literal["USD", "EUR"]


@dataclass(frozen=True)
class Department:
    """One row of briefing §2.1, plus the requested service that triggers it."""

    id: DepartmentId
    name: str
    owner: str
    contributes: str
    service: Service


# Briefing §2.1, verbatim. "Not every RFP needs all three departments."
DEPARTMENTS: Final[tuple[Department, ...]] = (
    Department(
        id="warehouse",
        name="Warehouse Operations",
        owner="Ana Whitfield",
        contributes="Storage capacity, cost per pallet/SKU, onboarding time",
        service="warehousing",
    ),
    Department(
        id="lastmile",
        name="Last Mile and Carrier Management",
        owner="Carlos Vega",
        contributes="Cost per shipment, available carriers by destination, delivery SLA",
        service="last_mile",
    ),
    Department(
        id="reverse",
        name="Reverse Logistics",
        owner="Sofía Ramos",
        contributes="Returns processing cost and turnaround time (if the client requests it)",
        service="reverse_logistics",
    ),
)

DEPARTMENT_IDS: Final[tuple[DepartmentId, ...]] = tuple(dept.id for dept in DEPARTMENTS)
DEPARTMENT_BY_ID: Final[dict[str, Department]] = {dept.id: dept for dept in DEPARTMENTS}
SERVICES: Final[tuple[Service, ...]] = tuple(dept.service for dept in DEPARTMENTS)
SERVICE_TO_DEPARTMENT: Final[dict[str, DepartmentId]] = {
    dept.service: dept.id for dept in DEPARTMENTS
}

SERVICE_LABELS: Final[dict[str, str]] = {
    "warehousing": "Warehousing",
    "last_mile": "Last mile delivery and carrier management",
    "reverse_logistics": "Reverse logistics (returns)",
}

# Briefing §2.2 and §5: "US or Spain — determines the currency".
COUNTRIES: Final[tuple[Country, ...]] = ("US", "ES")
COUNTRY_NAMES: Final[dict[str, str]] = {"US": "United States", "ES": "Spain"}
CURRENCY_BY_COUNTRY: Final[dict[str, Currency]] = {"US": "USD", "ES": "EUR"}

# Briefing §1: RFPs land on his desk; he is the Sales reader of every ticket.
COMMERCIAL_DIRECTOR: Final = "Miguel Torres"


def departments_for(services: Sequence[str]) -> list[DepartmentId]:
    """The departments a set of requested services needs, in the briefing's order."""
    wanted = {SERVICE_TO_DEPARTMENT[service] for service in services if service in SERVICES}
    return [dept_id for dept_id in DEPARTMENT_IDS if dept_id in wanted]
