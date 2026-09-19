from datetime import datetime, timezone

from tinydb import Query

from services.suppliers.db import get_db_path, get_table
from services.suppliers.models import SupplierCreate, SupplierInDB
from services.suppliers.seed_data import SUPPLIERS_SEED


def main() -> None:
    table = get_table()
    supplier = Query()
    inserted = 0
    skipped = 0

    for raw in SUPPLIERS_SEED:
        payload = SupplierCreate(**raw)
        if table.contains(supplier.name == payload.name):
            skipped += 1
            continue
        record = SupplierInDB(
            **payload.model_dump(),
            updated_at=datetime.now(timezone.utc),
        )
        table.insert(record.model_dump(mode="json"))
        inserted += 1

    print(f"Database: {get_db_path()}")
    print(f"Inserted {inserted} suppliers ({skipped} skipped, already present).")
    print(f"Total suppliers in directory: {len(table)}")


if __name__ == "__main__":
    main()
