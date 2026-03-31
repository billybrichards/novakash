"""
JWT Token Utilities

Creates and verifies access tokens (15 min) and refresh tokens (7 days).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from pydantic import BaseModel

from db.database import get_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 15
REFRESH_TOKEN_EXPIRE_DAYS = 7


class TokenData(BaseModel):
    user_id: int
    username: str
    token_type: str  # "access" | "refresh"


def _get_secret() -> str:
    return get_settings().SECRET_KEY


def create_access_token(user_id: int, username: str) -> str:
    """
    Create a short-lived JWT access token (15 minutes).

    Args:
        user_id:  Database user ID.
        username: Display name / login handle.

    Returns:
        Signed JWT string.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def create_refresh_token(user_id: int, username: str) -> str:
    """
    Create a long-lived JWT refresh token (7 days).

    Args:
        user_id:  Database user ID.
        username: Display name / login handle.

    Returns:
        Signed JWT string.
    """
    expire = datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "username": username,
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, _get_secret(), algorithm=ALGORITHM)


def verify_token(token: str, expected_type: str = "access") -> Optional[TokenData]:
    """
    Decode and validate a JWT token.

    Args:
        token:         The raw JWT string.
        expected_type: "access" or "refresh".

    Returns:
        TokenData if valid, None if expired or tampered.
    """
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
        username: str = payload["username"]
        token_type: str = payload.get("type", "access")

        if token_type != expected_type:
            return None

        return TokenData(user_id=user_id, username=username, token_type=token_type)
    except (JWTError, KeyError, ValueError):
        return None
