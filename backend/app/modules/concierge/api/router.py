from __future__ import annotations

from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.modules.catalog.ports.repository import PublishedCatalog
from app.modules.concierge.application.context_reference import (
    ContextReferenceService,
    ContextReferencesUnavailable,
    InvalidContextReference,
)

router = APIRouter(prefix="/api/v1/concierge", tags=["concierge"])
_context_references: ContextReferenceService | None = None
_whatsapp_number = ""


def configure_services(
    catalog: PublishedCatalog,
    *,
    secret: str,
    whatsapp_number: str,
    ttl_seconds: int,
) -> None:
    global _context_references, _whatsapp_number
    _context_references = ContextReferenceService(catalog, secret, ttl_seconds)
    _whatsapp_number = whatsapp_number


def _context_reference_service() -> ContextReferenceService:
    if _context_references is None:
        raise RuntimeError("concierge_services_not_configured")
    return _context_references


class ContextReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: UUID | None = None


class ContextReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str | None
    expires_at: str | None
    whatsapp_url: str
    web_whatsapp_url: str


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
        503: {"description": "WhatsApp, assinatura ou Catálogo indisponível."},
    },
)
async def create_context_reference(
    request: ContextReferenceRequest,
) -> ContextReferenceResponse:
    if not _whatsapp_number:
        raise HTTPException(status_code=503, detail="whatsapp_unavailable")

    reference: str | None = None
    expires_at: str | None = None
    message = "Olá! Quero conversar com o Pixel."
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
        message += f" Referência de contexto: {reference}"

    query = urlencode({"text": message})
    return ContextReferenceResponse(
        reference=reference,
        expires_at=expires_at,
        whatsapp_url=f"https://wa.me/{_whatsapp_number}?{query}",
        web_whatsapp_url=(
            f"https://web.whatsapp.com/send?phone={_whatsapp_number}&{query}"
        ),
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
