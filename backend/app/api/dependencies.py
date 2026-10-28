from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pymongo.errors import PyMongoError

from app.config import Settings
from app.schemas.resources import UserResult
from app.services.auth import resolve_user

bearer = HTTPBearer(auto_error=False)


def request_settings(request: Request) -> Settings:
    return request.app.state.settings


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "UNAUTHORIZED", "message": "邮箱或密码错误，或登录令牌无效、已过期。"},
        headers={"WWW-Authenticate": "Bearer"},
    )


def database_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "DATABASE_UNAVAILABLE", "message": "数据库暂时不可用，请稍后重试。"},
    )


def current_user(
    settings: Annotated[Settings, Depends(request_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> UserResult:
    if credentials is None:
        raise unauthorized()
    try:
        user = resolve_user(settings, credentials.credentials)
    except PyMongoError as exc:
        raise database_unavailable() from exc
    if user is None:
        raise unauthorized()
    return user
