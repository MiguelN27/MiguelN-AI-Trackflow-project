"""RFP intake routes: upload a PDF, list tickets, read one ticket.

HTTP only (briefing §2.4: "Routers import and trigger; they do not own agent
logic"). The upload validates and stores the PDF under `data/raw/rfp/`,
creates the ticket (`analyzing`) and answers 202 at once. Two background
tasks then run in this same API process, in order:
- the `rfp_intake` graph (`runner.run_intake`);
- with `RFP_AUTO_DRAFT` on, the proposal drafting queue
  (`proposal.runner.run_pending`), which picks up the handoff the intake has
  just queued.

Neither ever raises. The UI polls the ticket.

Staff only (admin or manager): RFPs carry client budgets and volumes, and a
self-registered account is a customer. Database failures are a 503 that names
nothing; the log keeps the error class only.
"""

import hashlib
import logging
import os
from pathlib import Path
from typing import Annotated, Final
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.config import ROOT_DIR, get_rfp_settings
from data.pipelines.rfp_intake.proposal import runner as proposal_runner
from services.auth.dependencies import StaffUser
from services.inventory.database import get_engine
from services.rfp.models import (
    RfpTicketAccepted,
    RfpTicketDetail,
    RfpTicketList,
    RfpTicketSummary,
)

logger = logging.getLogger("api.rfp")

router = APIRouter(prefix="/rfp", tags=["rfp"])

PDF_SIGNATURE: Final = b"%PDF-"
# The PDF header may follow a few bytes of junk; readers look in the first 1 KiB.
SIGNATURE_WINDOW: Final = 1024
CHUNK_BYTES: Final = 1024 * 1024
PDF_CONTENT_TYPES: Final = frozenset({"application/pdf", "application/x-pdf"})


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
    background.add_task(runner.run_intake, ticket_id, engine)
    if settings.auto_draft:
        # Runs after the intake: drafting starts from the handoff it queues.
        background.add_task(proposal_runner.run_pending, engine)
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


@router.get("/tickets/{ticket_id}", response_model=RfpTicketDetail)
def get_rfp_ticket(ticket_id: UUID, user: StaffUser) -> RfpTicketDetail:
    """Everything Sales needs from one ticket. 404 for an unknown id."""
    try:
        detail = store.get_ticket_detail(get_engine(), ticket_id)
    except SQLAlchemyError as error:
        raise _unavailable(error, "rfp ticket read") from None
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "RFP ticket not found")
    return RfpTicketDetail.model_validate(detail)
