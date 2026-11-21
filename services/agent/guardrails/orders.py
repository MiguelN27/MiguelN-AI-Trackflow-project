"""TrackFlow's customer orders, as the order-authorization guard sees them (read-only).

The briefing (SEC-114) describes each order by a tracking number, the country it
ships in (United States or Spain), the customer type (B2B or B2C) and the
customer it belongs to. TrackFlow's helpdesk export,
`data/raw/incidents-trackflow.csv`, is the record that carries exactly that:
one row per tracking number with its country, customer type, carrier, the
customer's email and the incident reported on it. The directory reads it once
and answers two questions:

- `owned_by(email)`: the orders of the signed-in customer (the account email is
  the order's `customer_email`), so any other tracking number is refused for
  lack of authorization - never for lack of data, and never revealing whether
  it exists;
- `get(identifier)`: one order, for staff sessions and for the evidence the
  agent answers from (its country decides which country's terms apply).

Rows are validated with the helpdesk export's own rules
(`trackflow_shared.incidents_csv.validate_csv_row`); a row whose tracking
number, country, carrier or email is invalid is not an order and is skipped.
The email never leaves this module: `CustomerOrder.to_state()` and the evidence
passages omit it.
"""

import csv
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from trackflow_shared.incidents_csv import validate_csv_row

from services.agent.config import get_agent_settings
from services.agent.guardrails.scope import COUNTRY_NAMES

logger = logging.getLogger("api.agent.orders")

ORDERS_DOCUMENT: Final = "customer-orders"

# Validation rules that make a row unusable as an order. The others (an unknown
# incident category, a closed incident without a score) describe the incident,
# not the order, which stays.
_ORDER_RULES: Final = frozenset(
    {"invalid_tracking_number", "invalid_country", "invalid_carrier", "invalid_email"}
)

CARRIER_LABELS: Final[dict[str, str]] = {
    "UPS": "UPS",
    "FEDEX": "FedEx",
    "DHL_US": "DHL",
    "DHL_ES": "DHL",
    "MRW": "MRW",
    "SEUR": "SEUR",
    "LOCAL_ES": "a local last-mile carrier (Zaragoza)",
}


def canonical_identifier(identifier: str) -> str:
    """Tracking numbers compare case-, space- and '#'-insensitively."""
    return "".join(identifier.split()).lstrip("#").upper()


def _label(value: str) -> str:
    return value.replace("_", " ").strip().lower()


@dataclass(frozen=True)
class OrderIncident:
    reference: str
    category: str
    status: str
    reported_on: str
    description: str


@dataclass(frozen=True)
class CustomerOrder:
    tracking_number: str
    country: str
    customer_type: str
    carrier: str
    owner_email: str
    incidents: tuple[OrderIncident, ...] = ()

    def to_state(self) -> dict[str, Any]:
        """The order as the graph state holds it: without the customer's email."""
        return {
            "tracking_number": self.tracking_number,
            "country": self.country,
            "customer_type": self.customer_type,
            "carrier": self.carrier,
            "incidents": [
                {
                    "reference": incident.reference,
                    "category": incident.category,
                    "status": incident.status,
                    "reported_on": incident.reported_on,
                    "description": incident.description,
                }
                for incident in self.incidents
            ],
        }


class OrderDirectory:
    """The orders, by tracking number and by owner."""

    def __init__(self, orders: Iterable[CustomerOrder]) -> None:
        self._by_number: dict[str, CustomerOrder] = {}
        for order in orders:
            key = canonical_identifier(order.tracking_number)
            known = self._by_number.get(key)
            if known is not None:  # several incidents on one order: one order
                order = CustomerOrder(
                    known.tracking_number,
                    known.country,
                    known.customer_type,
                    known.carrier,
                    known.owner_email,
                    known.incidents + order.incidents,
                )
            self._by_number[key] = order

    def __len__(self) -> int:
        return len(self._by_number)

    def get(self, identifier: str) -> CustomerOrder | None:
        return self._by_number.get(canonical_identifier(identifier))

    def owned_by(self, email: str | None) -> list[CustomerOrder]:
        """The orders whose customer is `email` (case-insensitive); none for no email."""
        wanted = (email or "").strip().lower()
        if not wanted:
            return []
        return [order for order in self._by_number.values() if order.owner_email == wanted]

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, str | None]]) -> "OrderDirectory":
        orders: list[CustomerOrder] = []
        skipped = 0
        for row in rows:
            rules = {issue.rule for issue in validate_csv_row(row)}
            if rules & _ORDER_RULES:
                skipped += 1
                continue
            text = {key: (value or "").strip() for key, value in row.items() if key}
            orders.append(
                CustomerOrder(
                    tracking_number=canonical_identifier(text["tracking_number"]),
                    country=text["country"],
                    customer_type=text.get("customer_type") or "unknown",
                    carrier=CARRIER_LABELS.get(text["carrier"], text["carrier"]),
                    owner_email=text["customer_email"].lower(),
                    incidents=(
                        OrderIncident(
                            reference=text.get("incident_id", ""),
                            category=_label(text.get("category") or "unclassified"),
                            status=_label(text.get("status") or "unknown"),
                            reported_on=text.get("date", ""),
                            description=text.get("description", ""),
                        ),
                    ),
                )
            )
        if skipped:
            logger.info("order directory: %d invalid row(s) skipped", skipped)
        return cls(orders)

    @classmethod
    def from_csv(cls, path: Path) -> "OrderDirectory":
        with path.open(newline="", encoding="utf-8") as handle:
            return cls.from_rows(csv.DictReader(handle))


@lru_cache(maxsize=1)
def get_order_directory() -> OrderDirectory:
    """The directory from `AGENT_ORDERS_PATH`. Missing, no order belongs to anyone."""
    path = get_agent_settings().agent_orders_path
    if not path.is_file():
        logger.warning("order directory: %s not found; no customer owns an order", path)
        return OrderDirectory(())
    return OrderDirectory.from_csv(path)


def order_passages(orders: list[dict[str, Any]], *, owned: bool) -> list[dict[str, Any]]:
    """Evidence passages for generation: one per order, never with an email."""
    passages: list[dict[str, Any]] = []
    for order in orders:
        country = COUNTRY_NAMES.get(order["country"], order["country"])
        lines = [
            f"Order {order['tracking_number']}"
            + (" (an order of the signed-in customer)" if owned else ""),
            f"Ships in: {country}",
            f"Customer type: {order['customer_type']}",
            f"Carrier: {order['carrier']}",
        ]
        for incident in order.get("incidents") or []:
            lines.append(
                f"Incident {incident['reference']} reported on {incident['reported_on']}: "
                f"{incident['category']}, {incident['status']}. "
                f"Description: {incident['description']}"
            )
        if not order.get("incidents"):
            lines.append("No incident is recorded on this order.")
        passages.append(
            {
                "source_document": ORDERS_DOCUMENT,
                "section": f"order {order['tracking_number']}",
                "text": "\n".join(lines),
            }
        )
    return passages
