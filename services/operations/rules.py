"""The operational activity rules: what is expected, where, and within which period.

The rules are a versioned file (`config/operational-activity-rules.json`, path
in `OPS_ACTIVITY_RULES_PATH`), validated when the API starts: a rule the monitor
could not honour (an unknown source, warehouse, job, branch or timezone, a
duplicate id) refuses start-up instead of silently watching nothing.

Four sources, one per kind of expected activity (`source` selects the model):
- `warehouse_orders`: a location registers inbound or outbound orders
  (`inventory` tables; the activity names are the BRIEFING's telemetry events);
- `scheduled_job`: a process completes its run (`job_runs`, `reporting.pipeline_runs`);
- `rfp_approval`: a department owner decides on their RFP section (the slot
  opens at `ready_at`);
- `incidents`: a branch triages its open incidents (TinyDB `incidents`).

`operating_hours` restricts the counting to a location's working time, in its own
timezone: two hours without orders means two working hours, never a night.
"""

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from trackflow_shared.incidents import IncidentBranch, IncidentCategory

from data.pipelines.rfp_intake.briefing import DepartmentId
from services.inventory.models import Warehouse
from services.operations.config import get_operations_settings
from services.operations.schemas import Severity

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: Final[tuple[Day, ...]] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A month: a longer expected period is a configuration mistake, not a rule.
MAX_EXPECTED_MINUTES: Final = 31 * 24 * 60


class RulesError(ValueError):
    """The rules file cannot be used. The message names the file and the problem."""


class OperatingHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str
    days: list[Day] = Field(default_factory=lambda: list(DAYS), min_length=1)
    start: time
    end: time

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value

    @model_validator(mode="after")
    def same_day_window(self) -> "OperatingHours":
        if self.start >= self.end:
            raise ValueError("start must be before end (a window within one day)")
        if len(set(self.days)) != len(self.days):
            raise ValueError("days are repeated")
        return self


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    description: str | None = None
    enabled: bool = True
    severity: Severity = "warning"
    expected_within_minutes: int = Field(gt=0, le=MAX_EXPECTED_MINUTES)
    operating_hours: OperatingHours | None = None


class WarehouseOrdersRule(_Rule):
    source: Literal["warehouse_orders"]
    warehouse: Warehouse
    activity: Literal["inbound_order_created", "outbound_order_created"]
    client_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class ScheduledJobRule(_Rule):
    source: Literal["scheduled_job"]
    # `nightly_export` is a `job_runs` job; the weekly report is in `reporting.pipeline_runs`.
    job_name: Literal["nightly_export", "weekly_warehouse_client_performance"]


class RfpApprovalRule(_Rule):
    source: Literal["rfp_approval"]
    departments: list[DepartmentId] | None = None


class IncidentsRule(_Rule):
    source: Literal["incidents"]
    categories: list[IncidentCategory] | None = None
    branches: list[IncidentBranch] | None = None


Rule = Annotated[
    WarehouseOrdersRule | ScheduledJobRule | RfpApprovalRule | IncidentsRule,
    Field(discriminator="source"),
]


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    rules: list[Rule]

    @model_validator(mode="after")
    def unique_ids(self) -> "RuleSet":
        ids = [rule.rule_id for rule in self.rules]
        repeated = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if repeated:
            raise ValueError(f"repeated rule_id: {', '.join(repeated)}")
        return self

    def by_id(self) -> dict[str, Rule]:
        return {rule.rule_id: rule for rule in self.rules}


def activity_of(rule: Rule) -> str:
    """What the rule expects to see, in the alert's `activity` field."""
    if isinstance(rule, WarehouseOrdersRule):
        return rule.activity
    if isinstance(rule, ScheduledJobRule):
        return rule.job_name
    if isinstance(rule, RfpApprovalRule):
        return "rfp_section_approval"
    return "incident_triage"


def load_rules(path: Path) -> RuleSet:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RulesError(f"{path}: cannot be read ({type(error).__name__})") from None
    try:
        return RuleSet.model_validate_json(text)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()[:5]
        )
        raise RulesError(f"{path}: {problems}") from None


@lru_cache(maxsize=1)
def get_rules() -> RuleSet:
    return load_rules(get_operations_settings().activity_rules_path)
