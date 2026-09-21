from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from services.auth.dependencies import CurrentUser
from services.auth.models import AuthenticatedUserResponse, LoginRequest, TokenResponse
from services.core.security import (
    access_token_expires_in_seconds,
    create_access_token,
    verify_dummy_password,
    verify_password,
)
from services.profiles import service as profiles_service
from services.users import service as users_service
from services.users.models import UserInDB

router = APIRouter(prefix="/auth", tags=["auth"])


def _authenticate(email: str, password: str) -> UserInDB:
    user = users_service.get_user_by_email(email)
    if user is None:
        # Same cost as a real check, so a wrong email is not distinguishable.
        verify_dummy_password(password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account",
        )

    return user


def _issue_token(user: UserInDB) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user_id=user.id, role=user.role.value),
        expires_in=access_token_expires_in_seconds(),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest) -> TokenResponse:
    """Validate email and password, and return a signed JWT access token."""
    return _issue_token(_authenticate(payload.email, payload.password))


@router.post("/token", response_model=TokenResponse)
def login_for_access_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> TokenResponse:
    """OAuth2 password-flow variant of `/auth/login`.

    Same credentials, form-encoded, with the email in the `username` field.
    This is the endpoint the Authorize button in `/docs` posts to.
    """
    return _issue_token(_authenticate(form_data.username, form_data.password))


@router.get("/me", response_model=AuthenticatedUserResponse)
def read_current_user(current_user: CurrentUser) -> AuthenticatedUserResponse:
    """Return the authenticated user's credentials plus the linked profile."""
    return AuthenticatedUserResponse(
        id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        is_active=current_user.is_active,
        profile=profiles_service.get_profile_by_user(current_user.id),
    )
