"""FastAPI dependencies shared by business routes."""

import asyncio
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.security import decode_access_token
from app.services.users import find_user_by_id

bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=401,
    detail={"code": "UNAUTHENTICATED", "message": "未登录或令牌已失效。"},
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _UNAUTHENTICATED
    claims = decode_access_token(get_settings(), credentials.credentials)
    if claims is None:
        raise _UNAUTHENTICATED
    user = await asyncio.to_thread(find_user_by_id, claims.sub)
    if user is None:
        raise _UNAUTHENTICATED
    return user
