"""RFP routes: upload a PDF, list and read tickets, owners' decisions, the final document,
and the live notification stream.

HTTP only (briefing §2.4: "Routers import and trigger; they do not own agent
logic"). The upload validates and stores the PDF under `data/raw/rfp/`,
creates the ticket (`analyzing`, with its `rfp_ticket_created` announcement in
the same transaction) and answers 202 at once. Background tasks then run in this
same API process, in order:
- the live stream's wake-up (`notifications.wake`), so every connected
  dashboard shows the new ticket at once;
- the `rfp_intake` graph (`runner.run_intake`);
- with `RFP_AUTO_DRAFT` on, the proposal drafting queue
  (`proposal.runner.run_pending`), which picks up the handoff the intake has
  just queued;
- with `RFP_AUTO_REQUEST_APPROVAL` on, the approval queue
  (`approval.runner.start_pending`), which opens one request per department
  owner and pauses the graph until they decide.

An owner's decision (`POST .../sections/{department}/decision`) is validated
and recorded at once (202); the graph is resumed with it in the background
(`approval.runner.apply_decisions`). None of the background tasks ever
raises. The UI follows a ticket's progress by polling it; new tickets reach it
through the live stream (`GET /notifications/stream`, also served here as
`/rfp/notifications/stream`; see `services/notifications/`), never by polling.

Staff only (admin or manager): RFPs carry client budgets and volumes, and a
self-registered account is a customer. Database failures are a 503 that names
nothing; the log keeps the error class only.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Annotated, Final, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Response, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from sse_starlette import EventSourceResponse

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.approval import detail as approval_detail
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.approval import trace as approval_trace
from data.pipelines.rfp_intake.approval.decisions import REFUSAL_STATUS, DecisionRefused
from data.pipelines.rfp_intake.config import ROOT_DIR, get_rfp_settings
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from services.auth.dependencies import StaffUser
from services.inventory.database import get_engine
from services.notifications import stream as notifications
from services.notifications.router import STREAM_RESPONSES, stream_notifications
from services.rfp.models import (
    RfpApprovalDecision,
    RfpDecisionRequest,
    RfpFinalDocument,
    RfpTicketAccepted,
    RfpTicketDetail,
    RfpTicketList,
    RfpTicketSummary,
    RfpTrace,
    RfpTraceStep,
)

logger = logging.getLogger("api.rfp")

router = APIRouter(prefix="/rfp", tags=["rfp"])

PDF_SIGNATURE: Final = b"%PDF-"
# The PDF header may follow a few bytes of junk; readers look in the first 1 KiB.
SIGNATURE_WINDOW: Final = 1024
CHUNK_BYTES: Final = 1024 * 1024
PDF_CONTENT_TYPES: Final = frozenset({"application/pdf", "application/x-pdf"})
DepartmentPath = Literal["warehouse", "lastmile", "reverse"]
NO_DOCUMENT: Final = (
    "The final document is generated once every department owner has approved their section."
)


def _unavailable(error: Exception, what: str) -> HTTPException:
    logger.error("%s failed: %s", what, type(error).__name__)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "RFP storage unavailable")


def _display_name(filename: str | None) -> str:
    name = Path((filename or "").replace("\\", "/")).name.strip()
    return (name or "upload.pdf")[:255]


def _read_limited(upload: UploadFile, limit: int) -> bytes:
    data = bytearray()
    while chunk := upload.file.read(CHUNK_BYTES):
        data += chunk
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"The PDF is larger than {limit // (1024 * 1024)} MB.",
            )
    return bytes(data)


def _stored_path(path: Path) -> str:
    """Relative to the repository when inside it ("data/raw/rfp/<id>.pdf")."""
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return str(path)


def _save(directory: Path, ticket_id: UUID, data: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{ticket_id}.pdf"
    partial = target.with_suffix(".pdf.partial")
    partial.write_bytes(data)
    os.replace(partial, target)
    return target


@router.post(
    "/tickets",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RfpTicketAccepted,
)
def upload_rfp(
    user: StaffUser,
    background: BackgroundTasks,
    file: Annotated[UploadFile, File(description="The RFP, as a PDF")],
) -> RfpTicketAccepted:
    """Create one ticket per uploaded PDF and start its analysis in the background.

    415 for anything that is not a PDF, 413 over the size limit
    (`RFP_MAX_UPLOAD_MB`), 422 for an empty file, 503 when storage fails.
    """
    settings = get_rfp_settings()
    filename = _display_name(file.filename)
    declared_pdf = (
        filename.lower().endswith(".pdf") or (file.content_type or "").lower() in PDF_CONTENT_TYPES
    )
    if not declared_pdf:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload the RFP as a PDF file.")
    data = _read_limited(file, settings.max_upload_bytes)
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The uploaded file is empty.")
    if PDF_SIGNATURE not in data[:SIGNATURE_WINDOW]:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "The uploaded file is not a valid PDF."
        )

    ticket_id = uuid4()
    try:
        path = _save(settings.upload_dir, ticket_id, data)
    except OSError as error:
        raise _unavailable(error, "rfp pdf storage") from None

    engine = get_engine()
    try:
        ticket = store.create_ticket(
            engine,
            ticket_id=ticket_id,
            original_filename=filename,
            raw_pdf_path=_stored_path(path),
            pdf_sha256=hashlib.sha256(data).hexdigest(),
            pdf_bytes=len(data),
            uploaded_by=user.id,
        )
    except SQLAlchemyError as error:
        path.unlink(missing_ok=True)
        raise _unavailable(error, "rfp ticket creation") from None

    logger.info("rfp ticket %s created (%d bytes); analysis scheduled", ticket_id, len(data))
    # First, right after the 202: push the new ticket to every connected dashboard.
    background.add_task(notifications.wake)
    background.add_task(runner.run_intake, ticket_id, engine)
    if settings.auto_draft:
        # Runs after the intake: drafting starts from the handoff it queues.
        background.add_task(proposal_runner.run_pending, engine)
    if settings.auto_request_approval:
        # Runs after drafting: each owner is asked, and the graph pauses for them.
        background.add_task(approval_runner.start_pending, engine)
    return RfpTicketAccepted(
        ticket_id=ticket.id,
        rfp_id=ticket.rfp_id,
        status=ticket.status,
        original_filename=ticket.original_filename,
    )


@router.get("/tickets", response_model=RfpTicketList)
def list_rfp_tickets(user: StaffUser) -> RfpTicketList:
    """The 50 newest tickets. Tickets whose run died with its process are closed first."""
    engine = get_engine()
    try:
        runner.sweep(engine)
        rows = store.list_tickets(engine)
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp ticket list") from None
    return RfpTicketList(tickets=[RfpTicketSummary.model_validate(row) for row in rows])


# The live stream shipped under this route; it is now the backoffice's shared
# `GET /notifications/stream` (`services/notifications/`), served here unchanged.
router.add_api_route(
    "/notifications/stream",
    stream_notifications,
    methods=["GET"],
    response_class=EventSourceResponse,
    responses=STREAM_RESPONSES,
    summary="Live notifications (alias of GET /notifications/stream)",
)


@router.get("/tickets/{ticket_id}", response_model=RfpTicketDetail)
def get_rfp_ticket(ticket_id: UUID, user: StaffUser) -> RfpTicketDetail:
    """Everything Sales needs from one ticket, and what the signed-in owner may decide."""
    try:
        detail = approval_detail.ticket_detail(
            get_engine(), ticket_id, viewer_email=user.email, settings=get_rfp_settings()
        )
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp ticket read") from None
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFP ticket not found")
    return RfpTicketDetail.model_validate(detail)


@router.post(
    "/tickets/{ticket_id}/sections/{department_id}/decision",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RfpApprovalDecision,
)
def decide_section(
    ticket_id: UUID,
    department_id: DepartmentPath,
    body: RfpDecisionRequest,
    user: StaffUser,
    background: BackgroundTasks,
) -> RfpApprovalDecision:
    """An owner approves, rejects or requests changes on their department's section.

    Only the owner's own account (`RFP_APPROVERS`) may decide: 403 otherwise.
    409 when the section is not waiting for a decision, the round is stale, or
    its revisions are used up; 422 for a missing comment. 202 once recorded:
    the graph is resumed with the decision in the background.
    """
    engine = get_engine()
    try:
        decision = approval_runner.submit_decision(
            engine,
            ticket_id,
            department_id,
            action=body.action,
            comment=body.comment,
            review_round=body.review_round,
            decided_by_email=user.email,
            settings=get_rfp_settings(),
        )
    except store.TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFP ticket not found") from None
    except DecisionRefused as refused:
        raise HTTPException(REFUSAL_STATUS[refused.code], refused.message) from None
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp decision") from None
    logger.info(
        "rfp ticket %s: %s decision recorded (%s); resume scheduled",
        ticket_id,
        department_id,
        body.action,
    )
    background.add_task(approval_runner.apply_decisions, ticket_id, engine)
    return RfpApprovalDecision.model_validate(decision)


def _document(ticket_id: UUID) -> RfpFinalDocument:
    try:
        document = approval_detail.document_fields(get_engine(), ticket_id)
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp document read") from None
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NO_DOCUMENT)
    return RfpFinalDocument.model_validate(document)


@router.get("/tickets/{ticket_id}/document", response_model=RfpFinalDocument)
def get_final_document(ticket_id: UUID, user: StaffUser) -> RfpFinalDocument:
    """The final document (briefing §2.3). 404 until every owner has approved."""
    return _document(ticket_id)


@router.get("/tickets/{ticket_id}/document.md", response_class=Response)
def download_final_document(ticket_id: UUID, user: StaffUser) -> Response:
    """The final document as a Markdown file, for Sales to send."""
    document = _document(ticket_id)
    slug = "-".join((document.client_name or "client").lower().split())
    safe = "".join(ch for ch in slug if ch.isalnum() or ch == "-")[:60] or "client"
    return Response(
        content=document.content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="trackflow-proposal-{safe}.md"'},
    )


@router.get("/tickets/{ticket_id}/trace", response_model=RfpTrace)
def get_ticket_trace(ticket_id: UUID, user: StaffUser) -> RfpTrace:
    """Which agent did what, and in what order, across Parts 1, 2 and 3."""
    engine = get_engine()
    try:
        if store.get_ticket(engine, ticket_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "RFP ticket not found")
        steps = approval_trace.ticket_trace(engine, ticket_id)
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp trace read") from None
    return RfpTrace(ticket_id=ticket_id, steps=[RfpTraceStep.model_validate(s) for s in steps])
