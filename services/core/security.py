from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.hash import bcrypt

from services.core.config import get_settings

BCRYPT_ROUNDS = 12
# bcrypt silently ignores anything past 72 bytes, so payloads are capped there.
MAX_PASSWORD_BYTES = 72
# Compared against when no user matches, so login costs the same either way.
DUMMY_HASH = "$2b$12$FkY3JlZ2j8qq561VHZctmuvXqwG8KiobjCTdK.wmAQwzhXRCzZC1e"


def hash_password(plain_password: str) -> str:
    return bcrypt.using(rounds=BCRYPT_ROUNDS).hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def verify_dummy_password(plain_password: str) -> None:
    """Burn one bcrypt verification so a missing account is not faster to probe."""
    verify_password(plain_password, DUMMY_HASH)


def create_access_token(user_id: str, role: str) -> str:
    settings = get_settings()
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Return the token claims, or None when the token is invalid or expired."""
    settings = get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError:
        return None


def access_token_expires_in_seconds() -> int:
    return get_settings().access_token_expire_minutes * 60
