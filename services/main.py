"""TrackFlow API composition root.

Mounts every domain router behind one FastAPI app. Authentication is stateless
JWT only: no sessions, no auth cookies.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.auth.router import router as auth_router
from services.profiles.router import router as profiles_router
from services.suppliers.router import router as suppliers_router
from services.users.router import router as users_router

app = FastAPI(
    title="TrackFlow API",
    description=(
        "Supplier directory and identity services for TrackFlow USA and Spain "
        "operations. Protected routes expect an `Authorization: Bearer <token>` "
        "header; get a token from `POST /auth/login`."
    ),
    version="0.2.0",
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
