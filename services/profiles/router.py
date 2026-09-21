from fastapi import APIRouter, HTTPException, status

from services.auth.dependencies import CurrentUser
from services.profiles import service as profiles_service
from services.profiles.models import ProfileResponse, ProfileUpdate

router = APIRouter(prefix="/profiles", tags=["profiles"])


def _get_own_profile_or_404(user_id: str) -> ProfileResponse:
    profile = profiles_service.get_profile_by_user(user_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No profile is linked to this account",
        )
    return profile


@router.get("/me", response_model=ProfileResponse)
def read_my_profile(current_user: CurrentUser) -> ProfileResponse:
    """Return the authenticated user's profile."""
    return _get_own_profile_or_404(current_user.id)


@router.put("/me", response_model=ProfileResponse)
def update_my_profile(payload: ProfileUpdate, current_user: CurrentUser) -> ProfileResponse:
    """Update name, phone and address.

    Scoped to `/me`, so a caller can only ever reach their own profile.
    """
    _get_own_profile_or_404(current_user.id)
    updated = profiles_service.update_profile_by_user(current_user.id, payload)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No profile is linked to this account",
        )
    return updated
