"""Fixtures for the operational monitor tests: one SQLite database with every table it reads.

The inventory tables, `job_runs`, the RFP tables, the live outbox and the alerts
table, in one file, as the API sees them in one PostgreSQL database. Incidents
are TinyDB rows in the per-test `DB_PATH` (the `db_path` fixture).
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from data.notifications import outbox
from data.pipelines.rfp_intake import store as rfp_store
from data.pipelines.rfp_intake.briefing import DEPARTMENT_BY_ID
from data.pipelines.rfp_intake.models import RfpDepartmentSection, RfpMetadata, RfpTicket
from services.incidents import db as incidents_db
from services.inventory.models import InboundOrder, OutboundOrder, Product, Warehouse
from services.job_runner import models as job_models
from services.operations import models as ops_models
from services.operations.rules import RuleSet
from tests.pipelines.rfp_intake.support import new_ticket

UTC = timezone.utc


def ops_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(
        engine,
        tables=[
            SQLModel.metadata.tables[str(model.__tablename__)]
            for model in (Product, InboundOrder, OutboundOrder)
        ],
    )
    job_models.metadata.create_all(engine)
    rfp_store.initialize_schema(engine)  # the RFP tables and the live outbox
    ops_models.initialize_schema(engine)
    return engine


def rules(*rules: dict[str, Any]) -> RuleSet:
    return RuleSet.model_validate({"version": 1, "rules": list(rules)})


def at(text: str) -> datetime:
    """An aware UTC instant from "2026-11-27 10:00"."""
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# --- Inventory ---------------------------------------------------------------------------


def product(engine: Engine, warehouse: str = "Zaragoza", client_id: str | None = None) -> UUID:
    with Session(engine) as session:
        item = Product(
            name="Laptop",
            sku=f"SKU-{uuid4().hex[:8]}",
            warehouse=Warehouse(warehouse),
            client_id=client_id,
        )
        session.add(item)
        session.commit()
        return item.id


def order(engine: Engine, product_id: UUID, created_at: datetime, *, outbound: bool = True) -> None:
    model = OutboundOrder if outbound else InboundOrder
    with Session(engine) as session:
        session.add(
            model(product_id=product_id, quantity=1, created_at=created_at, user_uuid=uuid4())
        )
        session.commit()


# --- Scheduled jobs ------------------------------------------------------------------------


def job_run(engine: Engine, status: str, at_time: datetime, name: str = "nightly_export") -> None:
    finished = at_time if status in {"completed", "failed"} else None
    with engine.begin() as connection:
        connection.execute(
            insert(job_models.job_runs).values(
                id=uuid4(),
                job_name=name,
                target_date=date(2026, 11, 1) + timedelta(days=at_time.toordinal() % 300),
                status=status,
                started_at=at_time - timedelta(minutes=5),
                finished_at=finished,
                error_message="OperationalError" if status == "failed" else None,
                created_at=at_time - timedelta(minutes=5),
            )
        )


# --- RFP approvals ---------------------------------------------------------------------------


def approval_slot(
    engine: Engine,
    directory: Path,
    ready_at: datetime,
    department: str = "warehouse",
    client_name: str | None = "ModaViva",
) -> tuple[UUID, UUID]:
    """A ticket waiting for its owners, with one pending section; (ticket_id, section_id)."""
    ticket_id = new_ticket(engine, directory)
    with Session(engine) as session:
        session.execute(
            update(RfpTicket)
            .where(RfpTicket.id == ticket_id)  # type: ignore[arg-type]
            .values(status="waiting_for_approval")
        )
        ticket = session.get(RfpTicket, ticket_id)
        assert ticket is not None
        if client_name is not None:
            session.add(
                RfpMetadata(rfp_id=ticket.rfp_id, ticket_id=ticket_id, client_name=client_name)
            )
        section = RfpDepartmentSection(
            ticket_id=ticket_id,
            department_id=department,
            owner=DEPARTMENT_BY_ID[department].owner,
            approval_status="pending",
            ready_at=ready_at,
        )
        session.add(section)
        session.commit()
        return ticket_id, section.id


def decide(engine: Engine, section_id: UUID, status: str = "approved") -> None:
    with Session(engine) as session:
        session.execute(
            update(RfpDepartmentSection)
            .where(RfpDepartmentSection.id == section_id)  # type: ignore[arg-type]
            .values(approval_status=status)
        )
        session.commit()


def reopen(engine: Engine, section_id: UUID, ready_at: datetime) -> None:
    """A revision round: the section is pending again, its clock reset."""
    with Session(engine) as session:
        session.execute(
            update(RfpDepartmentSection)
            .where(RfpDepartmentSection.id == section_id)  # type: ignore[arg-type]
            .values(approval_status="pending", ready_at=ready_at)
        )
        session.commit()


# --- Incidents (TinyDB) ------------------------------------------------------------------------


def incident(
    created_at: datetime,
    branch: str = "zaragoza_warehouse",
    category: str = "lost_parcel",
    status: str = "open",
    reference: str | None = None,
) -> str:
    incident_id = str(uuid4())
    incidents_db.get_table().insert(
        {
            "id": incident_id,
            "title": "Parcel missing",
            "description": "Lost between hub and van",
            "category": category,
            "origin": "branch",
            "branch": branch,
            "status": status,
            "created_at": created_at.isoformat(),
            "updated_at": created_at.isoformat(),
            "source_incident_id": reference,
        }
    )
    return incident_id


def pick_up(incident_id: str) -> None:
    from tinydb import Query

    incidents_db.get_table().update({"status": "in_progress"}, Query().id == incident_id)


def announcements(engine: Engine) -> list[dict[str, Any]]:
    return outbox.notifications_after(engine, 0, limit=1000)
