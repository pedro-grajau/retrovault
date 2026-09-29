from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app.platform.config.settings import settings


class VersionResponse(BaseModel):
    app_version: str
    correlation_id: UUID


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    code: str
    correlation_id: UUID


app = FastAPI(title="RetroVault API", version="1.0.0", openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["GET"], allow_headers=["X-Correlation-ID"], expose_headers=["X-Correlation-ID"])


def correlation_id(request: Request) -> UUID:
    raw_value = request.headers.get("X-Correlation-ID")
    if raw_value:
        try:
            return UUID(raw_value)
        except ValueError:
            pass
    return uuid4()


@app.middleware("http")
async def add_correlation_header(request: Request, call_next):  # type: ignore[no-untyped-def]
    request.state.correlation_id = correlation_id(request)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = str(request.state.correlation_id)
    return response


@app.exception_handler(RequestValidationError)
async def validation_problem(request: Request, _: RequestValidationError) -> JSONResponse:
    problem = Problem(title="Request validation failed", status=422, code="request_validation_failed", correlation_id=request.state.correlation_id)
    return JSONResponse(problem.model_dump(mode="json"), status_code=422, media_type="application/problem+json")


@app.exception_handler(StarletteHTTPException)
async def http_problem(request: Request, error: StarletteHTTPException) -> JSONResponse:
    problem = Problem(
        title="Resource not found" if error.status_code == 404 else "Request failed",
        status=error.status_code,
        code="not_found" if error.status_code == 404 else "http_error",
        correlation_id=request.state.correlation_id,
    )
    return JSONResponse(problem.model_dump(mode="json"), status_code=error.status_code, media_type="application/problem+json")


@app.exception_handler(Exception)
async def internal_problem(request: Request, _: Exception) -> JSONResponse:
    problem = Problem(
        title="Internal server error",
        status=500,
        code="internal_error",
        correlation_id=request.state.correlation_id,
    )
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=500,
        media_type="application/problem+json",
    )


@app.get("/api/v1/system/version", response_model=VersionResponse, tags=["system"])
async def version(request: Request) -> VersionResponse:
    return VersionResponse(app_version=settings.app_version, correlation_id=request.state.correlation_id)


@app.get("/api/v1/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
