"""Incident analysis upload and export endpoints."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from flask import Blueprint, Flask, current_app, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from scripts.analyze_incidents import (
    VALID_CATEGORIES,
    VALID_STATUSES,
    AnalysisResult,
    analyze_incidents,
    metric_rows,
)


incidents_blueprint = Blueprint("incidents", __name__, url_prefix="/api/incidents")


def serialize_result(result: AnalysisResult) -> dict[str, Any]:
    """Convert an analysis result into the API response representation."""
    return {
        "records_processed": result.total_records,
        "records_valid": result.valid_records,
        "records_invalid": len(result.invalid_records),
        "records_by_category": {
            category: result.categories[category] for category in sorted(VALID_CATEGORIES)
        },
        "records_by_status": {
            status: result.statuses[status] for status in ("OPEN", "CLOSED", "DISCARDED")
        },
        "closed_satisfaction_average": (
            None
            if result.closed_satisfaction_average is None
            else round(result.closed_satisfaction_average, 2)
        ),
        "closed_satisfaction_count": result.closed_satisfaction_count,
        "invalid_records": [
            {
                "row_number": invalid.row_number,
                "incident_id": invalid.incident_id,
                "reasons": list(invalid.reasons),
            }
            for invalid in result.invalid_records
        ],
    }


def export_csv(result: AnalysisResult) -> bytes:
    """Build the existing metrics export format in memory."""
    destination = io.StringIO(newline="")
    writer = csv.DictWriter(destination, fieldnames=("metric", "dimension", "value"))
    writer.writeheader()
    writer.writerows(metric_rows(result))
    return destination.getvalue().encode("utf-8")


def analyze_upload(contents: bytes) -> AnalysisResult:
    """Run the command-line analyzer against uploaded CSV content."""
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as temporary_file:
            temporary_file.write(contents)
            temporary_path = Path(temporary_file.name)
        return analyze_incidents(temporary_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@incidents_blueprint.post("/analyze")
def analyze_incident_file():
    """Validate an uploaded incident CSV and return its metrics as JSON."""
    if not request.mimetype or request.mimetype != "multipart/form-data":
        return jsonify({"error": "Request must use multipart/form-data."}), 400

    uploaded_file = request.files.get("file")
    if uploaded_file is None:
        return jsonify({"error": "A CSV file is required in the 'file' field."}), 400
    if not uploaded_file.filename or not uploaded_file.filename.strip():
        return jsonify({"error": "Uploaded file must have a filename."}), 400
    if not uploaded_file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Uploaded file must use the .csv extension."}), 400

    contents = uploaded_file.read()
    if not contents.strip():
        return jsonify({"error": "Uploaded CSV file is empty."}), 400

    try:
        result = analyze_upload(contents)
    except AttributeError:
        return jsonify({"error": "Invalid CSV file: a row is missing one or more fields."}), 400
    except (csv.Error, UnicodeDecodeError, ValueError) as error:
        return jsonify({"error": f"Invalid CSV file: {error}"}), 400

    serialized_result = serialize_result(result)
    current_app.extensions["incident_analysis"] = {
        "analysis": serialized_result,
        "csv": export_csv(result),
    }
    return jsonify(
        {
            "analysis": serialized_result,
            "export_url": "/api/incidents/results/exports",
        }
    )


@incidents_blueprint.get("/results/exports")
def download_export():
    """Download the metrics CSV from the most recent successful analysis."""
    analysis_state = current_app.extensions["incident_analysis"]
    csv_contents = analysis_state.get("csv")
    if csv_contents is None:
        return jsonify({"error": "No incident analysis is available for export."}), 404

    return send_file(
        io.BytesIO(csv_contents),
        mimetype="text/csv; charset=utf-8",
        as_attachment=True,
        download_name="incident-analysis-results.csv",
    )


def register_incident_routes(app: Flask) -> None:
    """Register incident routes and consistent upload-limit errors."""
    app.register_blueprint(incidents_blueprint)

    @app.errorhandler(RequestEntityTooLarge)
    def handle_file_too_large(error: RequestEntityTooLarge):
        return jsonify({"error": "File is too large. Maximum upload size is 10 MB."}), 413