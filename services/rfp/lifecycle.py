"""RFP workflow start-up and shutdown, called from the `services/main.py` lifespan.

Start-up:
- creates the `rfp_*` tables when missing (initialization, not migration);
- prepares the approval graph's checkpointer (PostgreSQL: its own schema and
  LangGraph's checkpoint tables, with RLS on);
- closes tickets whose Part 1 or Part 2 run died with a previous API process;
- in a background thread, resumes Part 3 runs a previous process left behind
  and applies decisions it recorded but never applied. Recovery can redraft a
  section, so it never delays start-up.

Shutdown closes the checkpointer's connections.
"""

import logging
import threading

from sqlalchemy.exc import SQLAlchemyError

from data.pipelines.rfp_intake import runner, store
from data.pipelines.rfp_intake.approval import checkpoint
from data.pipelines.rfp_intake.approval import runner as approval_runner
from data.pipelines.rfp_intake.config import get_rfp_settings
from services.core.config import ConfigurationError
from services.inventory.database import get_engine

logger = logging.getLogger("api.rfp")


def initialize() -> None:
    engine = get_engine()
    try:
        store.initialize_schema(engine)
    except SQLAlchemyError:
        raise ConfigurationError(
            "RFP intake schema initialization failed. Check PostgreSQL permissions and "
            "availability."
        ) from None
    try:
        checkpoint.get_checkpointer(engine, get_rfp_settings().checkpoint_schema)
    except Exception as error:  # noqa: BLE001 - psycopg, pool and driver errors alike
        raise ConfigurationError(
            "RFP approval checkpointer initialization failed "
            f"({type(error).__name__}). Check PostgreSQL permissions to create the "
            "RFP_CHECKPOINT_SCHEMA schema."
        ) from None
    try:
        runner.sweep(engine)
    except SQLAlchemyError as error:
        # Not worth refusing to start over: the list route sweeps again.
        logger.error("rfp stale-ticket sweep failed at startup: %s", type(error).__name__)
    threading.Thread(
        target=approval_runner.recover, args=(engine,), name="rfp-approval-recover", daemon=True
    ).start()


def shutdown() -> None:
    checkpoint.close_checkpointers()
