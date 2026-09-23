"""TrackFlow API composition root.

Mounts every domain router behind one FastAPI app. Authentication is stateless
JWT only: no sessions, no auth cookies.
"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from services.auth.router import router as auth_router
from services.core.errors import IncidentNotFound, ValidationFailed
from services.core.http_errors import (
    incident_not_found_handler,
    make_validation_error_handler,
    unhandled_exception_handler,
    validation_failed_handler,
)
from services.incidents.router import router as incidents_router
from services.profiles.router import router as profiles_router
from services.suppliers.router import router as suppliers_router
from services.users.router import router as users_router

app = FastAPI(
    title="TrackFlow API",
    description=(
        "Supplier directory, identity and incident services for TrackFlow "
        "Los Angeles and Zaragoza operations. Protected routes expect an "
        "`Authorization: Bearer <token>` header; get a token from "
        "`POST /auth/login`."
    ),
    version="0.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(users_router)
app.include_router(profiles_router)
app.include_router(suppliers_router)
app.include_router(incidents_router)

# Error translation.
#
# The `RequestValidationError` remapping - FastAPI's 422-and-a-pydantic-array
# into a 400 naming one field in plain language - applies only under the
# incident router's own prefix, which is read off the router so the two cannot
# drift. Every other domain keeps the 422 its clients were built against; see
# `make_validation_error_handler`.
#
# The domain-error handlers are scoped by their exception type, and the
# `Exception` handler is the app-wide backstop that keeps a traceback from ever
# reaching a client.
app.add_exception_handler(
    RequestValidationError,
    make_validation_error_handler((incidents_router.prefix,)),
)
app.add_exception_handler(ValidationFailed, validation_failed_handler)
app.add_exception_handler(IncidentNotFound, incident_not_found_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
