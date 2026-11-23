"""RFP intake start-up, called from the `services/main.py` lifespan.

Creates the five `rfp_*` tables when missing (initialization, not migration),
then closes tickets whose run died with a previous API process.
"""

import logging

from sqlalchemy.exc import SQLAlchemyError

from data.pipelines.rfp_intake import runner, store
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
        runner.sweep(engine)
    except SQLAlchemyError as error:
        # Not worth refusing to start over: the list route sweeps again.
        logger.error("rfp stale-ticket sweep failed at startup: %s", type(error).__name__)
