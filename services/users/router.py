from fastapi import APIRouter, Depends, HTTPException, Response, status

from services.auth.dependencies import CurrentUser, get_current_user, is_admin
from services.core.errors import EmailAlreadyRegistered
from services.users import service as users_service
from services.users.models import UserCreate, UserInDB, UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: UserInDB) -> UserResponse:
    return UserResponse(**user.model_dump(exclude={"hashed_password"}))


def _get_user_or_404(user_id: str) -> UserInDB:
    user = users_service.get_user(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )
    return user


def _require_self_or_admin(current_user: UserInDB, user_id: str) -> None:
    if current_user.id != user_id and not is_admin(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only act on your own account",
        )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(payload: UserCreate) -> UserResponse:
    """Public registration. Hashes the password and creates the linked profile.

    The only route in this module that does not require a token.
    """
    try:
        user = users_service.create_user(payload)
    except EmailAlreadyRegistered as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return _to_response(user)


@router.get(
    "",
    response_model=list[UserResponse],
    dependencies=[Depends(get_current_user)],
)
def list_users() -> list[UserResponse]:
    return [_to_response(user) for user in users_service.list_users()]


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    dependencies=[Depends(get_current_user)],
)
def get_user(user_id: str) -> UserResponse:
    return _to_response(_get_user_or_404(user_id))


@router.put("/{user_id}", response_model=UserResponse)
def update_user(user_id: str, payload: UserUpdate, current_user: CurrentUser) -> UserResponse:
    """Update credential fields. Only the user themselves or an admin may call it.

    `role` and `is_active` are admin-only, so a user cannot promote or
    reactivate themselves by editing their own record.
    """
    _require_self_or_admin(current_user, user_id)
    _get_user_or_404(user_id)

    if not is_admin(current_user):
        restricted = [
            field for field in ("role", "is_active") if field in payload.model_fields_set
        ]
        if restricted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Only an admin can change: {', '.join(restricted)}",
            )

    try:
        updated = users_service.update_user(user_id, payload)
    except EmailAlreadyRegistered as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User {user_id} not found",
        )
    return _to_response(updated)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, current_user: CurrentUser) -> Response:
    """Delete a user and its linked profile. Self or admin only."""
    _require_self_or_admin(current_user, user_id)
    _get_user_or_404(user_id)
    users_service.delete_user(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
