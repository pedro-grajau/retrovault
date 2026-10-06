from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.modules.catalog.ports.repository import CatalogReadUnavailable
from app.modules.concierge.api import router as concierge_router
from app.modules.concierge.application.context_reference import (
    ContextReferenceService,
    ContextReferencesUnavailable,
    InvalidContextReference,
)


class Catalog:
    def __init__(self, game_id, *, unavailable=False):
        self.game_id = game_id
        self.unavailable = unavailable

    def get_game(self, game_id):
        if self.unavailable:
            raise CatalogReadUnavailable
        return object() if game_id == self.game_id else None


def test_context_reference_lifecycle_and_expiration() -> None:
    game_id = uuid4()
    service = ContextReferenceService(Catalog(game_id), "test-secret", 300)

    token, issued = service.create_token(game_id, now=1000)

    assert issued.expires_at.timestamp() == 1300
    assert service.validate(token, now=1299) == issued
    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.validate(token, now=1300)


def test_context_reference_rejects_tampering_and_unpublished_games() -> None:
    game_id = uuid4()
    service = ContextReferenceService(Catalog(game_id), "test-secret", 300)
    token, _ = service.create_token(game_id, now=1000)
    unsigned, signature = token.rsplit(".", 1)
    changed_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = f"{unsigned}.{changed_signature}"

    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.validate(tampered, now=1001)
    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.create_token(uuid4(), now=1000)


def test_context_reference_reports_catalog_and_signing_unavailability() -> None:
    game_id = uuid4()
    with pytest.raises(ContextReferencesUnavailable, match="catalog_unavailable"):
        ContextReferenceService(Catalog(game_id, unavailable=True), "secret").create_token(
            game_id, now=1000
        )
    with pytest.raises(ContextReferencesUnavailable, match="context_reference_unavailable"):
        ContextReferenceService(Catalog(game_id), "").create_token(game_id, now=1000)


@pytest.mark.anyio
async def test_global_concierge_works_without_secret_but_contextual_call_is_unavailable(
    monkeypatch,
) -> None:
    game_id = uuid4()
    monkeypatch.setattr(concierge_router, "_context_references", None)
    monkeypatch.setattr(concierge_router, "_whatsapp_number", "")
    concierge_router.configure_services(
        Catalog(game_id),
        secret="",
        whatsapp_number="5500000000000",
        ttl_seconds=300,
    )
    app = FastAPI()
    app.include_router(concierge_router.router)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        global_response = await client.post(
            "/api/v1/concierge/context-references", json={}
        )
        contextual_response = await client.post(
            "/api/v1/concierge/context-references", json={"game_id": str(game_id)}
        )

    assert global_response.status_code == 201
    assert global_response.json()["reference"] is None
    assert contextual_response.status_code == 503
    assert contextual_response.json()["detail"] == "concierge_unavailable"
