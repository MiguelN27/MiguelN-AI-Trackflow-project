"""HTTP API services for TrackFlow."""

from flask import Flask

from .incidents import register_incident_routes


def register_api(app: Flask) -> None:
    """Configure TrackFlow API routes on a Flask application."""
    if app.config.get("MAX_CONTENT_LENGTH") is None:
        app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.extensions["incident_analysis"] = {}
    register_incident_routes(app)