import base64
import hashlib
import hmac
import json
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

    assert len(token) == 56
    assert service.create_token(game_id, now=1000)[0] != token
    assert token.isascii() and all(
        character.isalnum() or character in "_-" for character in token
    )
    assert issued.expires_at.timestamp() == 1300
    assert service.validate(token, now=1299) == issued
    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.validate(token, now=1300)


def test_context_reference_keeps_pre_nonce_compact_tokens_valid() -> None:
    game_id = uuid4()
    secret = "test-secret"
    service = ContextReferenceService(Catalog(game_id), secret, 300)
    payload = b"\x02" + game_id.bytes + (1300).to_bytes(4, "big")
    signature = hmac.new(
        secret.encode(), b"telegram-start-v2:" + payload, hashlib.sha256
    ).digest()[:12]
    token = "2" + base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    assert len(token) == 45
    assert service.validate(token, now=1299).game_id == game_id


def test_context_reference_rejects_noncanonical_compact_base64url_alias() -> None:
    game_id = uuid4()
    service = ContextReferenceService(Catalog(game_id), "test-secret", 300)
    token, _ = service.create_token(game_id, now=1000)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    encoded = token[1:]
    canonical_value = alphabet.index(encoded[-1])
    alias = "2" + encoded[:-1] + alphabet[canonical_value | 1]

    assert base64.urlsafe_b64decode(alias[1:] + "=") == base64.urlsafe_b64decode(
        encoded + "="
    )
    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.validate(alias, now=1001)


def test_context_reference_rejects_tampering_and_unpublished_games() -> None:
    game_id = uuid4()
    service = ContextReferenceService(Catalog(game_id), "test-secret", 300)
    token, _ = service.create_token(game_id, now=1000)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.validate(tampered, now=1001)
    with pytest.raises(InvalidContextReference, match="invalid_context_reference"):
        service.create_token(uuid4(), now=1000)


def test_context_reference_keeps_legacy_v1_tokens_valid_until_expiry() -> None:
    game_id = uuid4()
    secret = "test-secret"
    service = ContextReferenceService(Catalog(game_id), secret, 300)
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"v": 1, "game_id": str(game_id), "exp": 1300},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).decode().rstrip("=")
    unsigned = f"v1.{payload}"
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), unsigned.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

    assert service.validate(f"{unsigned}.{signature}", now=1299).game_id == game_id
    with pytest.raises(InvalidContextReference):
        service.validate(f"{unsigned}.{signature}", now=1300)


def test_context_reference_reports_catalog_and_signing_unavailability() -> None:
    game_id = uuid4()
    with pytest.raises(ContextReferencesUnavailable, match="catalog_unavailable"):
        ContextReferenceService(Catalog(game_id, unavailable=True), "secret").create_token(
            game_id, now=1000
        )
    with pytest.raises(ContextReferencesUnavailable, match="context_reference_unavailable"):
        ContextReferenceService(Catalog(game_id), "").create_token(game_id, now=1000)


@pytest.mark.anyio
async def test_context_reference_requires_a_fully_configured_telegram_channel(
    monkeypatch,
) -> None:
    game_id = uuid4()
    monkeypatch.setattr(concierge_router, "_context_references", None)
    concierge_router.configure_services(
        Catalog(game_id),
        secret="context-secret",
        telegram_bot_username="PixelTestBot",
        ttl_seconds=300,
        webhook_secret="",
        allowed_user_ids=frozenset({12345}),
        telegram_bot_token_configured=True,
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

    assert global_response.status_code == 503
    assert global_response.json()["detail"] == "telegram_unavailable"
    assert contextual_response.status_code == 503


@pytest.mark.anyio
async def test_global_entry_can_be_created_without_context_signing_secret(monkeypatch) -> None:
    game_id = uuid4()
    concierge_router.configure_services(
        Catalog(game_id),
        secret="",
        telegram_bot_username="PixelTestBot",
        ttl_seconds=300,
        webhook_secret="test-webhook-secret",
        allowed_user_ids=frozenset({12345}),
        telegram_bot_token_configured=True,
    )
    app = FastAPI()
    app.include_router(concierge_router.router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        global_response = await client.post(
            "/api/v1/concierge/context-references", json={}
        )
        contextual_response = await client.post(
            "/api/v1/concierge/context-references", json={"game_id": str(game_id)}
        )

    assert global_response.status_code == 201
    assert global_response.json()["reference"] is None
    assert global_response.json()["telegram_url"] == "https://t.me/PixelTestBot"
    assert contextual_response.status_code == 503
    assert contextual_response.json()["detail"] == "concierge_unavailable"
