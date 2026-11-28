"""Who is asking: TrackFlow staff or a customer, and what each may reach.

The briefing (SEC-114) makes the agent customer-facing - B2B brands and B2C
recipients - while TrackFlow's own CX staff keep using it. Sessions are scoped
by the account's role, decided with the developer:

- `admin` / `manager` accounts are **staff**: every tool (tickets, stock, the
  knowledge base), any order in TrackFlow's records, and team memory - they
  may be offered a memory proposal and approve it.
- `user` accounts are **customers**: their own orders, the knowledge base and
  incident procedures. No ticket or stock lookup (those hold other customers'
  incidents and other clients' stock), never a memory proposal (a customer
  must not be able to plant a "fact" every other user would then be told),
  and only the team memories about carriers and recurring incidents, never
  another client's report preferences.

The CLIs run as staff (a local operator), with `--as-customer EMAIL` to act as
a customer. The restriction is enforced in code, after the model has chosen:
the prompts only make it less likely that the model asks for what it cannot get.
"""

from dataclasses import dataclass
from typing import Any, Final, Literal

Audience = Literal["staff", "customer"]

STAFF: Final = "staff"
CUSTOMER: Final = "customer"

# Account roles (services.users.models.Role values) that are TrackFlow staff.
STAFF_ROLES: Final = frozenset({"admin", "manager"})

# The planner's sources a customer session may use.
CUSTOMER_SOURCES: Final = frozenset({"knowledge_base"})
# The team-memory categories a customer session may be told.
CUSTOMER_MEMORY_CATEGORIES: Final = ("carrier_rule", "recurring_incident")
# At most this many of a customer's own orders are attached to one message.
MAX_ATTACHED_ORDERS: Final = 5


def audience_for_role(role: Any) -> Audience:
    """`staff` for admin and manager accounts, `customer` for every other role."""
    value = getattr(role, "value", role)
    return "staff" if str(value) in STAFF_ROLES else "customer"


@dataclass(frozen=True)
class Session:
    """The authenticated session a message comes from.

    `email` is how a customer's orders are found (the order records carry the
    customer's email). It never enters the graph state, the checkpoints or
    the trace: the runner resolves it into the tracking numbers the customer
    owns before the run starts.
    """

    audience: Audience = "staff"
    email: str | None = None

    @property
    def is_customer(self) -> bool:
        return self.audience == CUSTOMER


def session_state(audience: Audience, owned_orders: list[str]) -> dict[str, Any]:
    """The `session` state field: who is asking, and which orders are theirs."""
    return {"audience": audience, "owned_orders": sorted(set(owned_orders))}


def is_customer(state_session: dict[str, Any] | None) -> bool:
    """True when a run's `session` field is a customer's; a run without one is staff."""
    return (state_session or {}).get("audience") == CUSTOMER
