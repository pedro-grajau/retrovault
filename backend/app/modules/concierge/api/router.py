from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.modules.catalog.ports.repository import PublishedCatalog
from app.modules.concierge.adapters.telegram_bot import (
    InvalidTelegramUpdate,
    TelegramUnavailable,
    TelegramUpdateAdapter,
)
from app.modules.concierge.application.context_reference import (
    ContextReferenceService,
    ContextReferencesUnavailable,
    InvalidContextReference,
)
from app.modules.concierge.application.handoff import HandoffService
from app.modules.concierge.application.intent_extraction import IntentExtractionService
from app.modules.concierge.application.session_workflow import (
    CheckpointerFactory,
    SessionWorkflow,
)
from app.modules.concierge.ports.handoffs import HandoffStore
from app.modules.concierge.ports.sessions import SessionStore, TelegramMessenger

logger = logging.getLogger(__name__)
_SESSION_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="concierge-session"
)

router = APIRouter(prefix="/api/v1/concierge", tags=["concierge"])
_context_references: ContextReferenceService | None = None
_telegram_bot_username = ""
_session_store: SessionStore | None = None
_session_workflow: SessionWorkflow | None = None
_handoff_service: HandoffService | None = None
_telegram_messenger: TelegramMessenger | None = None
_telegram_updates: TelegramUpdateAdapter | None = None
_webhook_secret = ""
_telegram_bot_token_configured = False
_allowed_user_ids: frozenset[int] = frozenset()
_webhook_max_body_bytes = 65536
_retention_days = 30
_typing_threshold_seconds = 3.0
_retention_checked_at = 0.0
_retention_lock = threading.Lock()


def configure_services(
    catalog: PublishedCatalog,
    *,
    secret: str,
    telegram_bot_username: str,
    ttl_seconds: int,
    session_store: SessionStore | None = None,
    checkpointer_factory: CheckpointerFactory | None = None,
    intent_extraction_service: IntentExtractionService | None = None,
    handoff_store: HandoffStore | None = None,
    telegram_messenger: TelegramMessenger | None = None,
    webhook_secret: str = "",
    allowed_user_ids: frozenset[int] = frozenset(),
    telegram_bot_token_configured: bool = False,
    webhook_max_body_bytes: int = 65536,
    message_max_age_seconds: int = 900,
    retention_days: int = 30,
    typing_threshold_seconds: float = 3.0,
) -> None:
    global _context_references, _telegram_bot_username, _session_store
    global _session_workflow, _handoff_service, _telegram_messenger, _telegram_updates, _webhook_secret
    global _telegram_bot_token_configured, _allowed_user_ids
    global _webhook_max_body_bytes, _retention_days
    global _typing_threshold_seconds, _retention_checked_at
    _context_references = ContextReferenceService(catalog, secret, ttl_seconds)
    _telegram_bot_username = telegram_bot_username.removeprefix("@")
    _session_store = session_store
    _handoff_service = HandoffService(
        handoff_store,
        allowed_user_ids,
        telegram_token_configured=telegram_bot_token_configured,
    )
    _session_workflow = (
        SessionWorkflow(
            session_store,
            _context_references,
            checkpointer_factory,
            intent_extraction=intent_extraction_service,
            handoff_service=_handoff_service,
            max_message_age_seconds=message_max_age_seconds,
        )
        if session_store is not None and checkpointer_factory is not None
        else None
    )
    _telegram_messenger = telegram_messenger
    _telegram_updates = TelegramUpdateAdapter(allowed_user_ids)
    _webhook_secret = webhook_secret
    _telegram_bot_token_configured = telegram_bot_token_configured
    _allowed_user_ids = allowed_user_ids
    _webhook_max_body_bytes = webhook_max_body_bytes
    _retention_days = retention_days
    _typing_threshold_seconds = typing_threshold_seconds
    _retention_checked_at = 0.0


def _context_reference_service() -> ContextReferenceService:
    if _context_references is None:
        raise RuntimeError("concierge_services_not_configured")
    return _context_references


def _telegram_channel_ready() -> bool:
    return bool(
        _telegram_bot_username
        and _telegram_bot_token_configured
        and _webhook_secret
        and _allowed_user_ids
    )


async def _await_thread_result(future: Future[Any]) -> Any:
    while not future.done():
        await asyncio.sleep(0.01)
    return future.result()


class ContextReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: UUID | None = None


class ContextReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str | None
    expires_at: str | None
    telegram_url: str


class ContextReferenceValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str = Field(min_length=1, max_length=2048)


class ContextReferenceValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: Literal[True]
    game_id: UUID
    expires_at: str


@router.post(
    "/context-references",
    response_model=ContextReferenceResponse,
    status_code=201,
    responses={
        404: {"description": "Jogo publicado não encontrado."},
        503: {"description": "Telegram, assinatura ou Catálogo indisponível."},
    },
)
async def create_context_reference(
    request: ContextReferenceRequest,
) -> ContextReferenceResponse:
    if not _telegram_channel_ready():
        raise HTTPException(status_code=503, detail="telegram_unavailable")

    reference: str | None = None
    expires_at: str | None = None
    if request.game_id is not None:
        try:
            reference, verified = _context_reference_service().create_token(
                request.game_id
            )
        except InvalidContextReference as exc:
            raise HTTPException(status_code=404, detail="not_found") from exc
        except ContextReferencesUnavailable as exc:
            raise HTTPException(status_code=503, detail="concierge_unavailable") from exc
        expires_at = verified.expires_at.isoformat().replace("+00:00", "Z")

    username = quote(_telegram_bot_username, safe="_")
    deep_link = f"https://t.me/{username}"
    if reference:
        deep_link += f"?start={reference}"
    return ContextReferenceResponse(
        reference=reference,
        expires_at=expires_at,
        telegram_url=deep_link,
    )


@router.post(
    "/context-references/validate",
    response_model=ContextReferenceValidationResponse,
    responses={
        400: {"description": "Referência inválida, expirada ou sem jogo publicado."},
        503: {"description": "Validação ou Catálogo indisponível."},
    },
)
async def validate_context_reference(
    request: ContextReferenceValidationRequest,
) -> ContextReferenceValidationResponse:
    try:
        verified = _context_reference_service().validate(request.reference)
    except InvalidContextReference as exc:
        raise HTTPException(status_code=400, detail="invalid_context_reference") from exc
    except ContextReferencesUnavailable as exc:
        raise HTTPException(status_code=503, detail="concierge_unavailable") from exc
    return ContextReferenceValidationResponse(
        valid=True,
        game_id=verified.game_id,
        expires_at=verified.expires_at.isoformat().replace("+00:00", "Z"),
    )


async def _read_bounded_body(request: Request) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            content_length = int(declared_length)
            if content_length < 0:
                raise ValueError
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
        if content_length > _webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="telegram_update_too_large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _webhook_max_body_bytes:
            raise HTTPException(status_code=413, detail="telegram_update_too_large")
        body.extend(chunk)
    return bytes(body)


async def _maybe_purge_expired_sessions() -> None:
    global _retention_checked_at
    if _session_store is None:
        return
    with _retention_lock:
        current = time.monotonic()
        if current - _retention_checked_at < 86400:
            return
        _retention_checked_at = current
    try:
        await _await_thread_result(
            _SESSION_EXECUTOR.submit(
                partial(_session_store.purge_expired_sessions, _retention_days)
            )
        )
    except Exception:
        with _retention_lock:
            _retention_checked_at = 0.0
        raise


async def _handle_and_deliver(message: Any) -> str:
    await _maybe_purge_expired_sessions()
    if _session_workflow is None or _telegram_messenger is None:
        raise RuntimeError("telegram_webhook_unavailable")
    result = await _await_thread_result(
        _SESSION_EXECUTOR.submit(partial(_session_workflow.handle, message))
    )
    if result.status == "in_progress":
        raise HTTPException(status_code=503, detail="telegram_update_in_progress")
    delivery = await _await_thread_result(
        _SESSION_EXECUTOR.submit(
            partial(
                _session_workflow.claim_reply,
                message.update_id,
                channel=message.channel,
            )
        )
    )
    if delivery is not None:
        try:
            await _telegram_messenger.send_message(delivery.chat_id, delivery.text)
        except TelegramUnavailable:
            await _await_thread_result(
                _SESSION_EXECUTOR.submit(
                    partial(
                        _session_workflow.release_reply,
                        message.update_id,
                        channel=message.channel,
                        lease_token=delivery.lease_token,
                    )
                )
            )
            raise
        await _await_thread_result(
            _SESSION_EXECUTOR.submit(
                partial(
                    _session_workflow.mark_reply_delivered,
                    message.update_id,
                    channel=message.channel,
                    lease_token=delivery.lease_token,
                )
            )
        )
    return result.status


@router.post(
    "/telegram/webhook",
    status_code=200,
    responses={
        400: {"description": "Content-Length inválido."},
        401: {"description": "Segredo de webhook inválido."},
        413: {"description": "Atualização excede o tamanho permitido."},
        503: {"description": "Telegram ou persistência indisponível."},
    },
)
async def telegram_webhook(request: Request) -> dict[str, str | bool]:
    if (
        not _telegram_channel_ready()
        or _session_workflow is None
        or _telegram_messenger is None
        or _telegram_updates is None
    ):
        raise HTTPException(status_code=503, detail="telegram_webhook_unavailable")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not supplied_secret.isascii() or not hmac.compare_digest(
        _webhook_secret, supplied_secret
    ):
        raise HTTPException(status_code=401, detail="invalid_webhook_secret")
    raw_body = await _read_bounded_body(request)
    try:
        payload = json.loads(raw_body)
    except (ValueError, RecursionError):
        return {"ok": True, "status": "ignored"}
    try:
        message = _telegram_updates.normalize(
            payload,
            correlation_id=getattr(request.state, "correlation_id", uuid4()),
        )
    except InvalidTelegramUpdate:
        return {"ok": True, "status": "ignored"}
    if message is None:
        return {"ok": True, "status": "ignored"}

    try:
        processing_task = asyncio.create_task(_handle_and_deliver(message))
        try:
            status = await asyncio.wait_for(
                asyncio.shield(processing_task), timeout=_typing_threshold_seconds
            )
        except TimeoutError:
            try:
                await _telegram_messenger.send_chat_action(message.external_chat_id)
            except TelegramUnavailable:
                logger.warning(
                    "Telegram typing action failed for correlation %s",
                    message.correlation_id,
                )
            status = await processing_task
        return {"ok": True, "status": status}
    except HTTPException:
        raise
    except TelegramUnavailable as exc:
        raise HTTPException(status_code=503, detail="telegram_delivery_unavailable") from exc
    except ContextReferencesUnavailable as exc:
        raise HTTPException(status_code=503, detail="concierge_unavailable") from exc
    except Exception as exc:
        logger.error(
            "Telegram session processing failed for correlation %s (%s)",
            message.correlation_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="concierge_unavailable") from exc
