"""
Auth Middleware — FastAPI dependency for JWT-protected routes.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth.jwt import TokenData, verify_token

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> TokenData:
    """
    FastAPI dependency: extract and verify the Bearer JWT from the request.

    Raises:
        HTTPException 401 if the token is missing, expired, or invalid.

    Returns:
        TokenData with user_id and username.

    Usage:
        @router.get("/protected")
        async def protected_route(user: TokenData = Depends(get_current_user)):
            ...
    """
    token_data = verify_token(credentials.credentials, expected_type="access")

    if token_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token_data
