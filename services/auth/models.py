from pydantic import BaseModel, EmailStr, Field, field_validator

from services.profiles.models import ProfileResponse
from services.users.models import Role, normalize_email


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def lowercase_email(cls, value: str) -> str:
        return normalize_email(value)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthenticatedUserResponse(BaseModel):
    """`GET /auth/me`: credentials from `User`, contact data from `Profile`."""

    id: str
    email: EmailStr
    role: Role
    is_active: bool
    profile: ProfileResponse | None = None
