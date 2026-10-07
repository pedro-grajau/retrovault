from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from app.modules.catalog.adapters.postgres_repository import PostgresCatalogRepository
from app.modules.catalog.api.router import (
    configure_services,
)
from app.modules.catalog.api.router import (
    router as catalog_router,
)
from app.modules.commerce.adapters.postgres_offers import PostgresOfferReader
from app.modules.concierge.adapters.postgres_sessions import (
    PostgresCheckpointFactory,
    PostgresSessionRepository,
    SessionsUnavailable,
)
from app.modules.concierge.adapters.telegram_bot import (
    TelegramBotClient,
    TelegramUnavailable,
)
from app.modules.concierge.api.router import (
    configure_services as configure_concierge_services,
)
from app.modules.concierge.api.router import (
    router as concierge_router,
)
from app.modules.concierge.domain.session import OutboxReply
from app.platform.config.settings import settings

logger = logging.getLogger(__name__)

_database_engine = create_engine(settings.database_url, pool_pre_ping=True)
_catalog_repository = PostgresCatalogRepository(_database_engine)
_session_repository = PostgresSessionRepository(_database_engine)
_checkpoint_factory = PostgresCheckpointFactory(settings.database_url)
_telegram_messenger = TelegramBotClient(
    settings.pixel_telegram_bot_token.get_secret_value()
)
configure_services(
    _catalog_repository,
    PostgresOfferReader(_database_engine),
)
configure_concierge_services(
    _catalog_repository,
    secret=settings.pixel_context_reference_secret.get_secret_value(),
    telegram_bot_username=settings.pixel_telegram_bot_username,
    ttl_seconds=settings.pixel_context_reference_ttl_seconds,
    session_store=_session_repository,
    checkpointer_factory=_checkpoint_factory,
    telegram_messenger=_telegram_messenger,
    telegram_bot_token_configured=bool(
        settings.pixel_telegram_bot_token.get_secret_value()
    ),
    webhook_secret=settings.pixel_telegram_webhook_secret.get_secret_value(),
    allowed_user_ids=settings.telegram_user_allowlist,
    webhook_max_body_bytes=settings.pixel_telegram_webhook_max_body_bytes,
    message_max_age_seconds=settings.pixel_telegram_message_max_age_seconds,
    retention_days=settings.pixel_telegram_retention_days,
    typing_threshold_seconds=settings.pixel_telegram_typing_threshold_seconds,
)


class VersionResponse(BaseModel):
    app_version: str
    correlation_id: UUID


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    code: str
    correlation_id: UUID


async def _session_retention_loop() -> None:
    while True:
        await asyncio.sleep(86400)
        try:
            await asyncio.to_thread(
                _session_repository.purge_expired_sessions,
                settings.pixel_telegram_retention_days,
            )
        except SessionsUnavailable:
            logger.error("Concierge retention job failed")


async def _deliver_outbox_reply(reply: OutboxReply) -> None:
    try:
        await _telegram_messenger.send_message(reply.chat_id, reply.text)
    except TelegramUnavailable:
        logger.warning("Concierge outbox delivery failed for update %s", reply.update_id)
        await asyncio.to_thread(
            _session_repository.release_reply,
            reply.update_id,
            channel=reply.channel,
            lease_token=reply.lease_token,
        )
        return
    await asyncio.to_thread(
        _session_repository.mark_reply_delivered,
        reply.update_id,
        channel=reply.channel,
        lease_token=reply.lease_token,
    )


async def _outbox_delivery_loop() -> None:
    while True:
        try:
            replies = await asyncio.to_thread(
                _session_repository.claim_pending_replies,
                now=datetime.now(UTC),
                limit=20,
                lease_seconds=30,
            )
            if replies:
                await asyncio.gather(*(_deliver_outbox_reply(reply) for reply in replies))
        except SessionsUnavailable:
            logger.error("Concierge outbox delivery worker failed")
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(_checkpoint_factory.setup)
    await asyncio.to_thread(
        _session_repository.purge_expired_sessions,
        settings.pixel_telegram_retention_days,
    )
    retention_task = asyncio.create_task(_session_retention_loop())
    outbox_task = (
        asyncio.create_task(_outbox_delivery_loop())
        if settings.pixel_telegram_bot_token.get_secret_value()
        else None
    )
    try:
        yield
    finally:
        retention_task.cancel()
        with suppress(asyncio.CancelledError):
            await retention_task
        if outbox_task is not None:
            outbox_task.cancel()
            with suppress(asyncio.CancelledError):
                await outbox_task


app = FastAPI(title="RetroVault API", version="1.0.0", openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:4173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Correlation-ID", "If-None-Match"],
    expose_headers=["X-Correlation-ID", "ETag"],
)
app.include_router(catalog_router)
app.include_router(concierge_router)


CORRELATION_ID_PARAMETER = {
    "name": "X-Correlation-ID",
    "in": "header",
    "required": False,
    "description": "UUID de correlação opcional. Um UUID é gerado quando ele não é informado ou é inválido.",
    "schema": {"type": "string", "format": "uuid"},
}
CORRELATION_ID_RESPONSE_HEADER = {
    "X-Correlation-ID": {
        "description": "UUID de correlação da resposta.",
        "schema": {"type": "string", "format": "uuid"},
    }
}


def problem_responses(*status_codes: int) -> dict[int, dict[str, object]]:
    schema = Problem.model_json_schema()
    return {
        status_code: {
            "description": "Resposta de problema padronizada.",
            "content": {"application/problem+json": {"schema": schema}},
        }
        for status_code in status_codes
    }


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
    try:
        response = await call_next(request)
    except Exception as error:
        logger.exception("Unhandled request error")
        response = await internal_problem(request, error)
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
    return JSONResponse(
        problem.model_dump(mode="json"),
        status_code=error.status_code,
        headers=error.headers,
        media_type="application/problem+json",
    )


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


@app.get(
    "/api/v1/system/version",
    response_model=VersionResponse,
    tags=["system"],
    responses={500: problem_responses(500)[500], 200: {"headers": CORRELATION_ID_RESPONSE_HEADER}},
    openapi_extra={"parameters": [CORRELATION_ID_PARAMETER]},
)
async def version(request: Request) -> VersionResponse:
    return VersionResponse(app_version=settings.app_version, correlation_id=request.state.correlation_id)


@app.get(
    "/api/v1/health",
    tags=["system"],
    responses={500: problem_responses(500)[500], 200: {"headers": CORRELATION_ID_RESPONSE_HEADER}},
    openapi_extra={"parameters": [CORRELATION_ID_PARAMETER]},
)
async def health() -> dict[str, str]:
    return {"status": "ok"}
