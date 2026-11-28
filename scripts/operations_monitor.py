"""The operational inactivity monitor from the command line (`npm run ops -- ...`).

- `rules`: validate the rules file and list what is watched;
- `check [--dry-run]`: evaluate the rules once. Without `--dry-run` it opens and
  resolves alerts and writes their live events, exactly as the API's monitor
  does (a running API pushes them within its poll interval). With `--dry-run`
  it only reports what would change and writes nothing, not even the tables;
- `alerts [--all]`: the open alerts (and, with `--all`, those resolved this week).

Exit codes: 0 done; 2 the rules file is invalid; 3 the database is unavailable.
"""

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from data.notifications import outbox
from services.inventory.database import get_engine
from services.operations import models, service
from services.operations.monitor import OpenAlert, evaluate, preview
from services.operations.rules import RulesError, WarehouseOrdersRule, get_rules

EXIT_CONFIG = 2
EXIT_DATABASE = 3


def _duration(minutes: int) -> str:
    """As the backoffice says it: "45 min", "2 h 14 min", "3 d 4 h"."""
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def _hours(rule) -> str:
    hours = rule.operating_hours
    if hours is None:
        return "always"
    return f"{','.join(hours.days)} {hours.start:%H:%M}-{hours.end:%H:%M} {hours.timezone}"


def cmd_rules(_: argparse.Namespace) -> int:
    rules = get_rules()
    for rule in rules.rules:
        where = rule.warehouse.value if isinstance(rule, WarehouseOrdersRule) else rule.source
        state = "on " if rule.enabled else "off"
        print(
            f"{state} {rule.rule_id:<24} {where:<16} within {rule.expected_within_minutes:>5} min"
            f"  ({_hours(rule)})  {rule.severity}"
        )
    print(f"{len(rules.rules)} rules, valid.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    engine, rules = get_engine(), get_rules()
    now = datetime.now(timezone.utc)
    if args.dry_run:
        intents = preview(engine, rules, now=now)
        for intent in intents:
            if isinstance(intent, OpenAlert):
                overdue = intent.assessment.overdue_minutes
                late = "never happened" if overdue is None else f"overdue by {_duration(overdue)}"
                print(f"would OPEN    {intent.rule.rule_id:<24} {intent.watch.label} ({late})")
            else:
                print(f"would RESOLVE {intent.alert_id} ({intent.reason})")
        print(f"{len(intents)} change(s) would be made; nothing was written.")
        return 0
    outbox.initialize_schema(engine)
    models.initialize_schema(engine)
    changes = evaluate(engine, rules, now=now)
    for change in changes:
        print(f"{change.event:<34} {change.alert.rule_id:<24} {change.alert.label}")
    print(f"{len(changes)} change(s) announced.")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    alerts = service.list_alerts(
        get_engine(), status="all" if args.all else "open", since_hours=24 * 7
    )
    for alert in alerts:
        ack = f"  acknowledged by {alert.acknowledged_by}" if alert.acknowledged_by else ""
        print(f"{alert.status:<9} {alert.detected_at:%Y-%m-%d %H:%M} {alert.label}{ack}")
    print(f"{len(alerts)} alert(s).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="npm run ops --", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("rules", help="validate the rules file and list the rules")
    check = commands.add_parser("check", help="evaluate the rules once")
    check.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    alerts = commands.add_parser("alerts", help="list open alerts")
    alerts.add_argument("--all", action="store_true", help="include those resolved this week")
    args = parser.parse_args(argv)
    handlers = {"rules": cmd_rules, "check": cmd_check, "alerts": cmd_alerts}
    try:
        return handlers[args.command](args)
    except RulesError as error:
        print(f"Invalid rules: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except SQLAlchemyError as error:
        print(f"Database unavailable: {type(error).__name__}", file=sys.stderr)
        return EXIT_DATABASE


if __name__ == "__main__":
    sys.exit(main())
