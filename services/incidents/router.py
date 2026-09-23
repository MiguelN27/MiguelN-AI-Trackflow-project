"""HTTP surface for the incident manager. No business rules live here.

Route order matters in one place: `/summary` is declared before `/{incident_id}`
because an incident id is an arbitrary string, so the parameterised route would
otherwise swallow the literal path and try to look up an incident called
"summary".

Failures leave this module as exceptions - `IncidentNotFound` from here,
`ValidationFailed` from the service layer - and `services.core.http_errors`
decides what they become on the wire.
"""

from fastapi import APIRouter, Query, status

from services.core.errors import IncidentNotFound
from services.incidents import service
from services.incidents.models import (
    IncidentBranch,
    IncidentCategory,
    IncidentCreate,
    IncidentInDB,
    IncidentOrigin,
    IncidentResponse,
    IncidentStatus,
    IncidentStatusUpdate,
    IncidentSummary,
)

router = APIRouter(prefix="/api/incidents", tags=["incidents"])


def to_response(incident: IncidentInDB) -> IncidentResponse:
    """Drop the seed-only bookkeeping before the incident goes on the wire."""
    return IncidentResponse(**incident.model_dump(exclude={"source_incident_id"}))


@router.post("", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_incident(payload: IncidentCreate) -> IncidentResponse:
    """Register a new incident.

    A missing or out-of-range field never reaches this function: the payload
    fails to validate and the handler in `services.main` answers 400 naming the
    field.
    """
    return to_response(service.create_incident(payload))


@router.get("", response_model=list[IncidentResponse])
def list_incidents(
    status: IncidentStatus | None = Query(default=None),
    origin: IncidentOrigin | None = Query(default=None),
    branch: IncidentBranch | None = Query(default=None),
    category: IncidentCategory | None = Query(default=None),
) -> list[IncidentResponse]:
    """List incidents, optionally filtered. Filters combine with AND."""
    incidents = service.list_incidents(
        status=status, origin=origin, branch=branch, category=category
    )
    return [to_response(incident) for incident in incidents]


@router.get("/summary", response_model=IncidentSummary)
def get_summary() -> IncidentSummary:
    """Aggregated metrics by status, category, origin and branch."""
    return service.summarize()


@router.get("/{incident_id}", response_model=IncidentResponse)
def get_incident(incident_id: str) -> IncidentResponse:
    """One incident in full."""
    incident = service.get_incident(incident_id)
    if incident is None:
        raise IncidentNotFound(incident_id)
    return to_response(incident)


@router.patch("/{incident_id}/status", response_model=IncidentResponse)
def update_incident_status(incident_id: str, payload: IncidentStatusUpdate) -> IncidentResponse:
    """Advance an incident along its lifecycle.

    `open` leads to `in_progress` or `discarded`; `in_progress` leads to
    `resolved` or `discarded`; `resolved` and `discarded` are final. Anything
    else is a 400 that says which moves were available.
    """
    return to_response(service.update_status(incident_id, payload.status))
