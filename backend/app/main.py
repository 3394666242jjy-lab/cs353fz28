from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.routes import router
from app.config import Settings, get_settings
from app.schemas.common import Envelope, ErrorBody, ErrorDetail, ErrorEnvelope
from app.schemas.system import Capabilities, Liveness, Readiness
from app.services.readiness import check_dependencies


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="CareerPilot API",
        version="0.1.0",
        description="Development scaffold. Business endpoints return 501 until implemented.",
        responses={
            404: {"model": ErrorEnvelope, "description": "Resource not found"},
            422: {"model": ErrorEnvelope, "description": "Invalid request parameters"},
            500: {"model": ErrorEnvelope, "description": "Internal service error"},
        },
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request: Request, status: int, body: ErrorBody, headers=None):
        result = ErrorEnvelope(error=body, request_id=request.state.request_id)
        return JSONResponse(status_code=status, content=result.model_dump(), headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = detail.get("code", "RESOURCE_NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR")
        message = detail.get(
            "message", "资源不存在。" if exc.status_code == 404 else "请求无法完成。"
        )
        return error_response(
            request, exc.status_code, ErrorBody(code=code, message=message), exc.headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo invalid inputs (e.g. passwords) into the response.
        details = [
            ErrorDetail(field=".".join(map(str, item["loc"])), message=item["msg"])
            for item in exc.errors()
        ]
        return error_response(
            request,
            422,
            ErrorBody(code="INVALID_INPUT", message="请检查请求参数。", details=details),
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        response = error_response(
            request,
            500,
            ErrorBody(code="INTERNAL_ERROR", message="服务内部错误，请通过请求 ID 排查。"),
        )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.get("/health/live", response_model=Envelope[Liveness], tags=["system"])
    async def live(request: Request):
        return Envelope(data=Liveness(), request_id=request.state.request_id)

    @app.get(
        "/health/ready",
        response_model=Envelope[Readiness],
        responses={503: {"model": Envelope[Readiness]}},
        tags=["system"],
    )
    async def ready(request: Request):
        checks = await check_dependencies(settings)
        ok = all(value == "ok" for value in checks.values())
        payload = Envelope(
            data=Readiness(status="ready" if ok else "not_ready", dependencies=checks),
            request_id=request.state.request_id,
        )
        return JSONResponse(status_code=200 if ok else 503, content=payload.model_dump())

    @app.get("/api/v1/meta", response_model=Envelope[Capabilities], tags=["system"])
    async def meta(request: Request):
        return Envelope(data=Capabilities(), request_id=request.state.request_id)

    app.include_router(router)
    return app


app = create_app()
