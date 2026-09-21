from pydantic import BaseModel, Field


class ProfileBase(BaseModel):
    """Display name and contact data. These fields live here, never on `User`."""

    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    address: str | None = Field(default=None, max_length=255)


class ProfileUpdate(ProfileBase):
    """Omitted fields are left untouched; send an explicit null to clear one."""


class ProfileInDB(ProfileBase):
    id: str
    user_id: str


# A profile carries no secrets, so the stored shape is also the wire shape.
# `User` needs a separate response model because it holds the password hash.
ProfileResponse = ProfileInDB
