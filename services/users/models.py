from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, field_validator

from services.core.security import MAX_PASSWORD_BYTES

MIN_PASSWORD_LENGTH = 8

# One definition of what counts as a password, shared by registration, admin
# edits, reset and change. An `Annotated` alias rather than a bare `Field` so
# the same constraint can be reused across models without sharing one instance.
Password = Annotated[str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_BYTES)]


class Role(str, Enum):
    """The only roles the platform accepts. Anything else is rejected with 422."""

    ADMIN = "admin"
    MANAGER = "manager"
    USER = "user"


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserCreate(BaseModel):
    """Registration payload.

    `role` is deliberately absent: self-service registration always produces a
    `user`, so nobody can grant themselves elevated access at signup.
    The profile fields are stored on the linked `Profile`, never on `User`.
    """

    email: EmailStr
    password: Password
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return normalize_email(value)


class UserUpdate(BaseModel):
    """Credential-only updates. `role` and `is_active` are admin-gated in the router."""

    email: EmailStr | None = None
    password: Password | None = None
    role: Role | None = None
    is_active: bool | None = None

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str | None) -> str | None:
        return None if value is None else normalize_email(value)


class UserInDB(BaseModel):
    """The stored record. Never returned over HTTP: it carries the password hash."""

    id: str
    email: EmailStr
    hashed_password: str
    is_active: bool = True
    role: Role = Role.USER
    created_at: datetime


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    is_active: bool
    role: Role
    created_at: datetime
