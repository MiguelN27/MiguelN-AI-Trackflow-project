#!/usr/bin/env python3
"""Validate and summarize TrackFlow incident CSV files."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import fmean
from typing import Iterable


REQUIRED_COLUMNS = (
    "incident_id",
    "date",
    "country",
    "customer_type",
    "tracking_number",
    "carrier",
    "category",
    "description",
    "status",
    "customer_email",
    "satisfaction_score",
)
VALID_CATEGORIES = {
    "DAMAGE",
    "DELAYED_DELIVERY",
    "LOST_PARCEL",
    "RETURN_REQUEST",
    "WRONG_ADDRESS",
}
VALID_STATUSES = {"OPEN", "CLOSED", "DISCARDED"}
VALID_CUSTOMER_TYPES = {"B2B", "B2C"}
INCIDENT_ID_PATTERN = re.compile(r"TRF-\d{6}\Z")
TRACKING_NUMBER_PATTERN = re.compile(r"[A-Z0-9]{10,20}\Z")
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")


@dataclass(frozen=True)
class InvalidRecord:
    row_number: int
    incident_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AnalysisResult:
    total_records: int
    valid_records: int
    invalid_records: tuple[InvalidRecord, ...]
    categories: Counter[str]
    statuses: Counter[str]
    closed_satisfaction_average: float | None
    closed_satisfaction_count: int


def validation_reasons(record: dict[str, str]) -> list[str]:
    """Return all rule violations for one parsed CSV record."""
    reasons: list[str] = []
    incident_id = record["incident_id"].strip()
    if not INCIDENT_ID_PATTERN.fullmatch(incident_id):
        reasons.append("incident_id must match TRF-000000")

    try:
        date.fromisoformat(record["date"].strip())
    except ValueError:
        reasons.append("date must use YYYY-MM-DD")

    country = record["country"].strip()
    if not re.fullmatch(r"[A-Z]{2}", country):
        reasons.append("country must be a two-letter uppercase code")

    if record["customer_type"].strip() not in VALID_CUSTOMER_TYPES:
        reasons.append("customer_type must be B2B or B2C")
    if not TRACKING_NUMBER_PATTERN.fullmatch(record["tracking_number"].strip()):
        reasons.append("tracking_number must be 10-20 uppercase letters or digits")
    if not record["carrier"].strip():
        reasons.append("carrier is required")
    if record["category"].strip() not in VALID_CATEGORIES:
        reasons.append("category is missing or unsupported")
    if not record["description"].strip():
        reasons.append("description is required")

    status = record["status"].strip()
    if status not in VALID_STATUSES:
        reasons.append("status must be OPEN, CLOSED, or DISCARDED")
    if not EMAIL_PATTERN.fullmatch(record["customer_email"].strip()):
        reasons.append("customer_email is invalid")

    score = record["satisfaction_score"].strip()
    if score:
        try:
            if not 1 <= int(score) <= 5:
                raise ValueError
        except ValueError:
            reasons.append("satisfaction_score must be an integer from 1 to 5")
    return reasons


def analyze_incidents(csv_path: Path) -> AnalysisResult:
    """Load, validate, and calculate valid-record incident metrics."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            raise ValueError("CSV file is missing a header row")
        missing_columns = set(REQUIRED_COLUMNS) - set(reader.fieldnames)
        if missing_columns:
            raise ValueError(f"CSV file is missing required columns: {', '.join(sorted(missing_columns))}")

        valid_records: list[dict[str, str]] = []
        invalid_records: list[InvalidRecord] = []
        total_records = 0
        for row_number, record in enumerate(reader, start=2):
            total_records += 1
            reasons = validation_reasons(record)
            if reasons:
                invalid_records.append(
                    InvalidRecord(row_number, record["incident_id"].strip(), tuple(reasons))
                )
            else:
                valid_records.append(record)

    scores = [
        int(record["satisfaction_score"])
        for record in valid_records
        if record["status"].strip() == "CLOSED" and record["satisfaction_score"].strip()
    ]
    return AnalysisResult(
        total_records=total_records,
        valid_records=len(valid_records),
        invalid_records=tuple(invalid_records),
        categories=Counter(record["category"].strip() for record in valid_records),
        statuses=Counter(record["status"].strip() for record in valid_records),
        closed_satisfaction_average=fmean(scores) if scores else None,
        closed_satisfaction_count=len(scores),
    )


def print_summary(result: AnalysisResult) -> None:
    """Print a readable report for a completed analysis."""
    width = 54
    print("=" * width)
    print("TRACKFLOW INCIDENT FILE ANALYSIS".center(width))
    print("=" * width)
    print(f"{'Records processed:':<36}{result.total_records:>18}")
    print(f"{'Valid records:':<36}{result.valid_records:>18}")
    print(f"{'Invalid records:':<36}{len(result.invalid_records):>18}")
    print("-" * width)
    print("Valid records by category")
    for category in sorted(VALID_CATEGORIES):
        print(f"  {category:<34}{result.categories[category]:>18}")
    print("Valid records by status")
    for status in ("OPEN", "CLOSED", "DISCARDED"):
        print(f"  {status:<34}{result.statuses[status]:>18}")
    average = "N/A" if result.closed_satisfaction_average is None else f"{result.closed_satisfaction_average:.2f}"
    print(f"{'Closed-case satisfaction average:':<36}{average:>18}")
    print(f"{'Closed cases with a score:':<36}{result.closed_satisfaction_count:>18}")

    if result.invalid_records:
        print("-" * width)
        print("Invalid records")
        for invalid in result.invalid_records:
            identifier = invalid.incident_id or "<missing incident_id>"
            print(f"  Row {invalid.row_number} ({identifier}): {'; '.join(invalid.reasons)}")
    print("=" * width)


def metric_rows(result: AnalysisResult) -> Iterable[dict[str, str | int | float]]:
    """Produce one exportable row per metric."""
    yield {"metric": "records_processed", "dimension": "all", "value": result.total_records}
    yield {"metric": "records_valid", "dimension": "all", "value": result.valid_records}
    yield {"metric": "records_invalid", "dimension": "all", "value": len(result.invalid_records)}
    for category in sorted(VALID_CATEGORIES):
        yield {"metric": "records_by_category", "dimension": category, "value": result.categories[category]}
    for status in ("OPEN", "CLOSED", "DISCARDED"):
        yield {"metric": "records_by_status", "dimension": status, "value": result.statuses[status]}
    yield {
        "metric": "closed_satisfaction_average",
        "dimension": "scored_closed_cases",
        "value": "" if result.closed_satisfaction_average is None else f"{result.closed_satisfaction_average:.2f}",
    }
    for invalid in result.invalid_records:
        yield {
            "metric": "invalid_record",
            "dimension": f"row_{invalid.row_number}",
            "value": "; ".join(invalid.reasons),
        }


def export_results(result: AnalysisResult, output_path: Path) -> None:
    with output_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=("metric", "dimension", "value"))
        writer.writeheader()
        writer.writerows(metric_rows(result))


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Validate and analyze TrackFlow incident CSV data.")
    parser.add_argument("csv_path", nargs="?", type=Path, default=project_root / "incidents-trackflow.csv")
    parser.add_argument("--output", type=Path, default=project_root / "results.csv")
    args = parser.parse_args()

    try:
        result = analyze_incidents(args.csv_path)
    except (OSError, ValueError, csv.Error) as error:
        parser.error(str(error))
    print_summary(result)
    if input("Export results to CSV? [y / n] ").strip().lower() == "y":
        export_results(result, args.output)
        print(f"Results exported to {args.output}")


if __name__ == "__main__":
    main()