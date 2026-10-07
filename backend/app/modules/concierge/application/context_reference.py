"""Short-lived signed references for contextual Pixel handoffs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.modules.catalog.ports.repository import (
    CatalogReadUnavailable,
    PublishedCatalog,
)


class InvalidContextReference(ValueError):
    """The reference is malformed, expired, or no longer points to a game."""


class ContextReferencesUnavailable(RuntimeError):
    """Signing is not configured for this environment."""


@dataclass(frozen=True)
class VerifiedContextReference:
    game_id: UUID
    expires_at: datetime


class ContextReferenceService:
    def __init__(
        self,
        catalog: PublishedCatalog,
        secret: str,
        ttl_seconds: int = 1800,
    ) -> None:
        self.catalog = catalog
        self._secret = secret.encode("utf-8")
        self.ttl_seconds = ttl_seconds

    def issue(self, game_id: UUID, *, now: int | None = None) -> VerifiedContextReference:
        try:
            published_game = self.catalog.get_game(game_id)
        except CatalogReadUnavailable as exc:
            raise ContextReferencesUnavailable("catalog_unavailable") from exc
        if published_game is None:
            raise InvalidContextReference("invalid_context_reference")
        if not self._secret:
            raise ContextReferencesUnavailable("context_reference_unavailable")
        expires_at_epoch = (int(time.time()) if now is None else now) + self.ttl_seconds
        return VerifiedContextReference(
            game_id=game_id,
            expires_at=datetime.fromtimestamp(expires_at_epoch, UTC),
        )

    def create_token(self, game_id: UUID, *, now: int | None = None) -> tuple[str, VerifiedContextReference]:
        reference = self.issue(game_id, now=now)
        expires_at_epoch = int(reference.expires_at.timestamp())
        if not 0 <= expires_at_epoch <= 0xFFFFFFFF:
            raise ContextReferencesUnavailable("context_reference_expiration_unsupported")
        payload = (
            b"\x02"
            + game_id.bytes
            + expires_at_epoch.to_bytes(4, "big")
            + secrets.token_bytes(8)
        )
        signature = hmac.new(
            self._secret, b"telegram-start-v2:" + payload, hashlib.sha256
        ).digest()[:12]
        token = "2" + _base64url(payload + signature)
        if len(token) > 64:
            raise ContextReferencesUnavailable("context_reference_too_long")
        return token, reference

    def validate(self, token: str, *, now: int | None = None) -> VerifiedContextReference:
        if not self._secret:
            raise ContextReferencesUnavailable("context_reference_unavailable")
        if token.startswith("2"):
            return self._validate_compact_token(token, now=now)
        try:
            version, encoded_payload, encoded_signature = token.split(".")
            if version != "v1" or len(token) > 2048:
                raise ValueError
            payload_bytes = _base64url_decode(encoded_payload)
            signature_bytes = _base64url_decode(encoded_signature)
            if (
                _base64url(payload_bytes) != encoded_payload
                or _base64url(signature_bytes) != encoded_signature
            ):
                raise ValueError
            unsigned = f"{version}.{encoded_payload}"
            expected_signature = hmac.new(
                self._secret, unsigned.encode(), hashlib.sha256
            ).digest()
            supplied_signature = signature_bytes
            if not hmac.compare_digest(expected_signature, supplied_signature):
                raise ValueError
            payload = json.loads(payload_bytes)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"v", "game_id", "exp"}
                or payload["v"] != 1
                or not isinstance(payload["game_id"], str)
                or type(payload["exp"]) is not int
            ):
                raise ValueError
            game_id = UUID(payload["game_id"])
            if str(game_id) != payload["game_id"]:
                raise ValueError
            expires_at_epoch = payload["exp"]
            current_time = int(time.time()) if now is None else now
            if expires_at_epoch <= current_time:
                raise ValueError
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as exc:
            raise InvalidContextReference("invalid_context_reference") from exc

        try:
            published_game = self.catalog.get_game(game_id)
        except CatalogReadUnavailable as exc:
            raise ContextReferencesUnavailable("catalog_unavailable") from exc
        if published_game is None:
            raise InvalidContextReference("invalid_context_reference")
        return VerifiedContextReference(
            game_id=game_id,
            expires_at=datetime.fromtimestamp(expires_at_epoch, UTC),
        )

    def _validate_compact_token(
        self, token: str, *, now: int | None = None
    ) -> VerifiedContextReference:
        try:
            if len(token) not in {45, 56} or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in token
            ):
                raise ValueError
            raw = _base64url_decode(token[1:])
            if _base64url(raw) != token[1:]:
                raise ValueError
            if len(raw) == 33:
                payload_length = 21
            elif len(raw) == 41:
                payload_length = 29
            else:
                raise ValueError
            payload, supplied_signature = raw[:payload_length], raw[payload_length:]
            if payload[0] != 2:
                raise ValueError
            expected_signature = hmac.new(
                self._secret, b"telegram-start-v2:" + payload, hashlib.sha256
            ).digest()[:12]
            if not hmac.compare_digest(expected_signature, supplied_signature):
                raise ValueError
            game_id = UUID(bytes=payload[1:17])
            expires_at_epoch = int.from_bytes(payload[17:21], "big")
            current_time = int(time.time()) if now is None else now
            if expires_at_epoch <= current_time:
                raise ValueError
        except (ValueError, TypeError, binascii.Error) as exc:
            raise InvalidContextReference("invalid_context_reference") from exc

        try:
            published_game = self.catalog.get_game(game_id)
        except CatalogReadUnavailable as exc:
            raise ContextReferencesUnavailable("catalog_unavailable") from exc
        if published_game is None:
            raise InvalidContextReference("invalid_context_reference")
        return VerifiedContextReference(
            game_id=game_id,
            expires_at=datetime.fromtimestamp(expires_at_epoch, UTC),
        )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _base64url_decode(value: str) -> bytes:
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("invalid_context_reference")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
