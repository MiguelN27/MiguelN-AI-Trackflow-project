from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, SQLModel, create_engine, text
from tinydb import TinyDB

from services.core.config import ConfigurationError, get_settings
from services.core.db import get_db as get_tinydb
from services.inventory import models


def get_identity_db() -> TinyDB:
    return get_tinydb()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = make_url(get_settings().database_url.get_secret_value()).set(
        drivername="postgresql+psycopg2"
    )
    return create_engine(
        url,
        echo=False,
        hide_parameters=True,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        isolation_level="READ COMMITTED",
        connect_args={"sslmode": url.query.get("sslmode", "require"), "connect_timeout": 10},
    )


def get_db() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def check_connection() -> None:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise ConfigurationError(
            "PostgreSQL connection failed. Check DATABASE_URL and Supabase availability."
        ) from None


def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


def initialize_schema() -> None:
    try:
        tables = [
            SQLModel.metadata.tables[models.Product.__tablename__],
            SQLModel.metadata.tables[models.InboundOrder.__tablename__],
            SQLModel.metadata.tables[models.OutboundOrder.__tablename__],
        ]
        with get_engine().begin() as connection:
            SQLModel.metadata.create_all(connection, tables=tables)
            if connection.dialect.name == "postgresql":
                quote = connection.dialect.identifier_preparer.quote
                for table in tables:
                    connection.execute(
                        text(f"ALTER TABLE {quote(table.name)} ENABLE ROW LEVEL SECURITY")
                    )
    except SQLAlchemyError:
        raise ConfigurationError(
            "Inventory schema initialization failed. Check PostgreSQL permissions and availability."
        ) from None
