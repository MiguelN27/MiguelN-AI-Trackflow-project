from datetime import datetime
from enum import Enum

from pydantic import BaseModel, EmailStr, Field, field_validator

from services.core.security import MAX_PASSWORD_BYTES

PasswordField = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)


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
    password: str = PasswordField
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
    password: str | None = Field(default=None, min_length=8, max_length=MAX_PASSWORD_BYTES)
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
