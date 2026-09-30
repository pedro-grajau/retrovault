"""PostgreSQL persistence for the published Catalog projection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import Connection, Engine, text

from app.modules.catalog.domain.publication import (
    GameNotFound,
    IdempotencyConflict,
    PublicationConflict,
    PublishedGame,
)

_EDITORIAL_FIELDS = {
    "title", "platform", "region", "edition", "genre", "developer",
    "publisher", "year", "rating", "description", "included_items",
}
_FORBIDDEN_FIELDS = {"price", "stock", "sku", "preco", "estoque", "condition", "availability"}


class PostgresCatalogRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _etag(value: str) -> str:
        return value.strip().strip('"')

    @contextmanager
    def _transaction(
        self, transaction_connection: Connection | None
    ) -> Generator[Connection]:
        if transaction_connection is not None:
            yield transaction_connection
        else:
            with self.engine.begin() as connection:
                yield connection

    @staticmethod
    def _cursor_encode(game_id: UUID) -> str:
        return base64.urlsafe_b64encode(str(game_id).encode()).decode().rstrip("=")

    @staticmethod
    def _cursor_decode(value: str | None) -> UUID | None:
        if value is None:
            return None
        if not value or len(value) > 128:
            raise ValueError("invalid_cursor")
        try:
            raw = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
            decoded = raw.decode("ascii")
            game_id = UUID(decoded)
            if str(game_id) != decoded:
                raise ValueError
            return game_id
        except (ValueError, TypeError, UnicodeError, binascii.Error) as exc:
            raise ValueError("invalid_cursor") from exc

    @staticmethod
    def _json_default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    @staticmethod
    def _row_game(row: Any) -> PublishedGame:
        return PublishedGame(
            id=row["id"],
            title=row["title"],
            platform=row["platform"],
            editorial=dict(row["editorial"]),
            source=row["source"],
            source_record_id=row["source_record_id"],
            version=row["version"],
            etag=row["etag"],
            verified_at=row["verified_at"],
            cover_hash=row["cover_hash"].strip(),
            cover_content_type=row["content_type"],
            cover_attribution=row["attribution"],
        )

    def list_games(
        self, *, limit: int, cursor: str | None = None, platform: str | None = None
    ) -> tuple[list[PublishedGame], str | None]:
        decoded = self._cursor_decode(cursor)
        params: dict[str, Any] = {"limit": limit + 1}
        clauses = ["g.active"]
        if decoded is not None:
            clauses.append("g.id > :last_id")
            params["last_id"] = decoded
        if platform:
            clauses.append("lower(g.platform) = lower(:platform)")
            params["platform"] = platform
        query = text(f"""
            SELECT g.id, g.title, g.platform, g.editorial, g.source,
                   g.source_record_id, g.version, g.etag, g.updated_at AS verified_at,
                   g.cover_hash, m.content_type, m.attribution
            FROM catalog.published_games g
            JOIN catalog.published_media m ON m.content_hash = g.cover_hash
            WHERE {' AND '.join(clauses)}
            ORDER BY g.id
            LIMIT :limit
        """)
        with self.engine.connect() as connection:
            rows = list(connection.execute(query, params).mappings())
        has_more = len(rows) > limit
        games = [self._row_game(row) for row in rows[:limit]]
        next_cursor = None
        if has_more and games:
            last = games[-1]
            next_cursor = self._cursor_encode(last.id)
        return games, next_cursor

    def get_game(self, game_id: UUID) -> PublishedGame | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("""
                    SELECT g.id, g.title, g.platform, g.editorial, g.source,
                           g.source_record_id, g.version, g.etag, g.updated_at AS verified_at,
                           g.cover_hash, m.content_type, m.attribution
                    FROM catalog.published_games g
                    JOIN catalog.published_media m ON m.content_hash = g.cover_hash
                    WHERE g.id=:id AND g.active
                """),
                {"id": game_id},
            ).mappings().first()
        return self._row_game(row) if row else None

    def get_cover(self, game_id: UUID) -> tuple[bytes, str, str] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("""
                    SELECT m.content, m.content_type, m.content_hash
                    FROM catalog.published_games g
                    JOIN catalog.published_media m ON m.content_hash=g.cover_hash
                    WHERE g.id=:id AND g.active
                """),
                {"id": game_id},
            ).first()
        if row is None:
            return None
        return bytes(row[0]), row[1], row[2].strip()

    def current_etag(
        self, source: str, source_record_id: str,
        connection: Connection | None = None,
    ) -> str | None:
        if connection is not None:
            return self._current_etag(connection, source, source_record_id)
        with self.engine.connect() as read_connection:
            return self._current_etag(read_connection, source, source_record_id)

    @staticmethod
    def _current_etag(
        connection: Connection, source: str, source_record_id: str
    ) -> str | None:
        result = connection.execute(
            text("SELECT etag FROM catalog.published_games WHERE source=:source AND source_record_id=:record_id"),
            {"source": source, "record_id": source_record_id},
        ).scalar_one_or_none()
        return result

    @staticmethod
    def _request_hash(command: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"command": command, **payload},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()

    def _idempotent_result(
        self,
        idempotency_key: str,
        request_hash: str,
        connection: Connection | None,
    ) -> dict[str, Any] | None:
        with self._transaction(connection) as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"retrovault:catalog:idempotency:{idempotency_key}"},
            )
            previous = connection.execute(
                text("SELECT request_hash, response FROM catalog.command_idempotency WHERE idempotency_key=:key"),
                {"key": idempotency_key},
            ).mappings().first()
            if previous is None:
                return None
            if previous["request_hash"] != request_hash:
                raise IdempotencyConflict("idempotency_key_reused")
            return dict(previous["response"])

    @staticmethod
    def _rights_reference(candidate: dict[str, Any], cover: dict[str, Any]) -> str:
        reference = candidate.get("rights_reference") or cover.get("rights_reference")
        if not isinstance(reference, str) or not reference.strip():
            # This records the user-reported portfolio authorization, not a general license.
            reference = "Autorização RetroAchievements para portfólio informada por Pedro-Lucas em 2026-09-30"
        return reference[:500]

    def publish(
        self,
        candidate: dict[str, Any],
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        published_etag: str | None = None,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        if actor != "Eduardo" or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid_publication_decision")
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("invalid_idempotency_key")
        source = candidate.get("source")
        source_record_id = str(candidate.get("record_id", ""))
        request_hash = self._request_hash(
            "publish",
            {
                "candidate_etag": expected_etag,
                "source": source,
                "source_record_id": source_record_id,
                "reason": reason,
                "actor": actor,
            },
        )
        cached = self._idempotent_result(
            idempotency_key, request_hash, connection
        )
        if cached is not None:
            return cached

        if candidate.get("state") != "review" or candidate.get("issues"):
            raise PublicationConflict("candidate_not_eligible")
        run_id = candidate.get("run_id")
        rule_version = candidate.get("rule_version")
        evidence_id = candidate.get("evidence_id")
        values = candidate.get("values")
        cover = candidate.get("cover")
        review_etag = str(candidate.get("etag", ""))
        if not expected_etag or self._etag(expected_etag) != self._etag(review_etag):
            raise PublicationConflict("etag_conflict")
        if (
            not isinstance(source, str)
            or source != "retroachievements"
            or not source_record_id
            or not isinstance(values, dict)
            or not isinstance(cover, dict)
            or not isinstance(cover.get("content"), bytes)
            or cover.get("storage_right") != "confirmed"
            or cover.get("publication_right") != "confirmed"
            or not str(cover.get("attribution", "")).strip()
        ):
            raise PublicationConflict("candidate_etag_or_cover_invalid")
        if set(values) - _EDITORIAL_FIELDS or set(values) & _FORBIDDEN_FIELDS:
            raise ValueError("invalid_editorial_fields")
        title, platform = values.get("title"), values.get("platform")
        if not isinstance(title, str) or not title.strip() or not isinstance(platform, str) or not platform.strip():
            raise PublicationConflict("required_fields_missing")

        from io import BytesIO

        from PIL import Image, UnidentifiedImageError

        image_content = cover["content"]
        try:
            with Image.open(BytesIO(image_content)) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > 20_000_000:
                    raise PublicationConflict("cover_invalid")
                image.load()
                content_type = Image.MIME.get(image.format or "")
                if image.format not in {"PNG", "JPEG", "WEBP"} or content_type not in {
                    "image/png", "image/jpeg", "image/webp"
                }:
                    raise PublicationConflict("cover_invalid")
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            raise PublicationConflict("cover_invalid") from exc

        cover_hash = hashlib.sha256(image_content).hexdigest()
        rights_reference = self._rights_reference(candidate, cover)
        now = datetime.now(UTC)
        try:
            verified_at = candidate.get("captured_at")
            if isinstance(verified_at, str):
                verified_at = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
            if not isinstance(verified_at, datetime) or verified_at.tzinfo is None:
                verified_at = now
        except ValueError:
            verified_at = now
        lineage = candidate.get("lineage")
        if not isinstance(lineage, dict):
            lineage = {}
        editorial = {
            "attributes": values,
            "lineage": lineage,
            "verified_at": verified_at.isoformat(),
        }
        with self._transaction(connection) as connection:
            lock_key = f"retrovault:review:{run_id}:{rule_version}:{evidence_id}"
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"retrovault:catalog:idempotency:{idempotency_key}"},
            )
            previous = connection.execute(
                text("SELECT request_hash, response FROM catalog.command_idempotency WHERE idempotency_key=:key"),
                {"key": idempotency_key},
            ).mappings().first()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency_key_reused")
                return dict(previous["response"])

            if run_id and rule_version and evidence_id:
                locked_stage = connection.execute(
                    text("""
                        SELECT 1 FROM data_governance.staging_records
                        WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence
                        FOR UPDATE
                    """),
                    {"run_id": run_id, "rule": rule_version, "evidence": evidence_id},
                ).first()
                if locked_stage is None:
                    raise PublicationConflict("candidate_not_found")
                latest_review_etag = connection.execute(
                    text("""
                        SELECT resulting_etag FROM data_governance.review_corrections
                        WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence
                        ORDER BY revision DESC LIMIT 1
                    """),
                    {"run_id": run_id, "rule": rule_version, "evidence": evidence_id},
                ).scalar_one_or_none()
                if latest_review_etag and self._etag(latest_review_etag) != self._etag(review_etag):
                    raise PublicationConflict("review_etag_stale")

            existing = connection.execute(
                text("""
                    SELECT id, version, etag, active FROM catalog.published_games
                    WHERE source=:source AND source_record_id=:record_id FOR UPDATE
                """),
                {"source": source, "record_id": source_record_id},
            ).mappings().first()
            if existing:
                if not existing["active"]:
                    raise PublicationConflict("retired_game_cannot_be_republished")
                elif published_etag is None or self._etag(published_etag) != self._etag(existing["etag"]):
                    raise PublicationConflict("published_etag_stale")
                game_id = existing["id"]
                version = existing["version"] + 1
                action = "update"
            else:
                game_id = UUID(str(candidate.get("public_id"))) if candidate.get("public_id") else uuid5(
                    NAMESPACE_URL, f"retrovault:catalog:game:{source}:{source_record_id}"
                )
                version = 1
                action = "publish"

            snapshot = {
                "id": str(game_id),
                "source": source,
                "source_record_id": source_record_id,
                "version": version,
                "editorial": editorial,
                "cover_hash": cover_hash,
                "cover_content_type": content_type,
                "cover_attribution": cover["attribution"],
                "rights_reference": rights_reference,
                "active": True,
            }
            etag = hashlib.sha256(
                json.dumps(
                    snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                    default=self._json_default,
                ).encode()
            ).hexdigest()
            snapshot["etag"] = etag
            connection.execute(
                text("""
                    INSERT INTO catalog.published_media
                    (content_hash, content, content_type, attribution, rights_reference, created_at)
                    VALUES (:hash, :content, :content_type, :attribution, :rights_reference, :now)
                    ON CONFLICT (content_hash) DO NOTHING
                """),
                {"hash": cover_hash, "content": image_content, "content_type": content_type,
                 "attribution": cover["attribution"], "rights_reference": rights_reference, "now": now},
            )
            if existing:
                connection.execute(
                    text("""
                        UPDATE catalog.published_games SET title=:title, platform=:platform,
                            editorial=CAST(:editorial AS jsonb), version=:version, etag=:etag,
                            active=true, cover_hash=:cover_hash, updated_at=:now
                        WHERE id=:id
                    """),
                    {"id": game_id, "title": title.strip(), "platform": platform.strip(),
                     "editorial": json.dumps(editorial, ensure_ascii=False, default=self._json_default), "version": version,
                     "etag": etag, "cover_hash": cover_hash, "now": verified_at},
                )
            else:
                connection.execute(
                    text("""
                        INSERT INTO catalog.published_games
                        (id, source, source_record_id, title, platform, editorial, version,
                         etag, active, cover_hash, updated_at)
                        VALUES (:id, :source, :record_id, :title, :platform, CAST(:editorial AS jsonb),
                                :version, :etag, true, :cover_hash, :now)
                    """),
                    {"id": game_id, "source": source, "record_id": source_record_id,
                     "title": title.strip(), "platform": platform.strip(),
                     "editorial": json.dumps(editorial, ensure_ascii=False, default=self._json_default), "version": version,
                     "etag": etag, "cover_hash": cover_hash, "now": verified_at},
                )
            connection.execute(
                text("""
                    INSERT INTO catalog.published_game_versions
                    (game_id, version, action, snapshot, etag, actor, reason, source,
                     source_record_id, created_at)
                    VALUES (:id, :version, :action, CAST(:snapshot AS jsonb), :etag, :actor,
                            :reason, :source, :record_id, :now)
                """),
                {"id": game_id, "version": version, "action": action,
                 "snapshot": json.dumps(snapshot, ensure_ascii=False, default=self._json_default), "etag": etag,
                 "actor": actor, "reason": reason.strip(), "source": source,
                 "record_id": source_record_id, "now": now},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.publication_audit
                    (id, game_id, version, action, actor, reason, source, source_record_id,
                     correlation_id, occurred_at)
                    VALUES (:id, :game, :version, :action, :actor, :reason, :source,
                            :record_id, :correlation, :now)
                """),
                {"id": uuid4(), "game": game_id, "version": version, "action": action,
                 "actor": actor, "reason": reason.strip(), "source": source,
                 "record_id": source_record_id, "correlation": uuid4(), "now": now},
            )
            response = {"game_id": str(game_id), "version": version, "etag": etag, "state": "published"}
            event_id = uuid4()
            connection.execute(
                text("""
                    INSERT INTO platform.outbox_events
                    (id, topic, aggregate_type, aggregate_id, payload, created_at, available_at)
                    VALUES (:id, 'catalog.game.published.v1', 'published_game', :game,
                            CAST(:payload AS jsonb), :now, :now)
                """),
                {"id": event_id, "game": game_id,
                 "payload": json.dumps({"event_id": str(event_id), **response, "action": action},
                                       ensure_ascii=False), "now": now},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.command_idempotency
                    (id, command, idempotency_key, request_hash, response, created_at)
                    VALUES (:id, 'publish', :key, :request_hash, CAST(:response AS jsonb), :now)
                """),
                {"id": uuid4(), "key": idempotency_key, "request_hash": request_hash,
                 "response": json.dumps(response), "now": now},
            )
            return response

    def retire(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        if actor != "Eduardo" or not reason.strip() or len(reason) > 1000:
            raise ValueError("invalid_publication_decision")
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("invalid_idempotency_key")
        request_data = {"game_id": str(game_id), "expected_etag": expected_etag,
                        "reason": reason, "actor": actor}
        request_hash = self._request_hash("retire", request_data)
        with self._transaction(connection) as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"retrovault:catalog:idempotency:{idempotency_key}"},
            )
            previous = connection.execute(
                text("SELECT request_hash, response FROM catalog.command_idempotency WHERE idempotency_key=:key"),
                {"key": idempotency_key},
            ).mappings().first()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise IdempotencyConflict("idempotency_key_reused")
                return dict(previous["response"])
            game = connection.execute(
                text("SELECT * FROM catalog.published_games WHERE id=:id FOR UPDATE"),
                {"id": game_id},
            ).mappings().first()
            if game is None or not game["active"]:
                raise GameNotFound("game_not_found")
            if self._etag(expected_etag) != self._etag(game["etag"]):
                raise PublicationConflict("published_etag_stale")
            version = game["version"] + 1
            now = datetime.now(UTC)
            snapshot = {
                "id": str(game_id), "source": game["source"],
                "source_record_id": game["source_record_id"], "version": version,
                "editorial": dict(game["editorial"]), "cover_hash": game["cover_hash"].strip(),
                "active": False,
            }
            etag = hashlib.sha256(
                json.dumps(
                    snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                    default=self._json_default,
                ).encode()
            ).hexdigest()
            snapshot["etag"] = etag
            connection.execute(
                text("UPDATE catalog.published_games SET active=false, version=:version, etag=:etag, updated_at=:now WHERE id=:id"),
                {"id": game_id, "version": version, "etag": etag, "now": now},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.published_game_versions
                    (game_id, version, action, snapshot, etag, actor, reason, source,
                     source_record_id, created_at)
                    VALUES (:id, :version, 'retire', CAST(:snapshot AS jsonb), :etag, :actor,
                            :reason, :source, :record_id, :now)
                """),
                {"id": game_id, "version": version, "snapshot": json.dumps(snapshot, default=self._json_default),
                 "etag": etag, "actor": actor, "reason": reason.strip(),
                 "source": game["source"], "record_id": game["source_record_id"], "now": now},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.publication_audit
                    (id, game_id, version, action, actor, reason, source, source_record_id,
                     correlation_id, occurred_at)
                    VALUES (:id, :game, :version, 'retire', :actor, :reason, :source,
                            :record_id, :correlation, :now)
                """),
                {"id": uuid4(), "game": game_id, "version": version, "actor": actor,
                 "reason": reason.strip(), "source": game["source"],
                 "record_id": game["source_record_id"], "correlation": uuid4(), "now": now},
            )
            response = {"game_id": str(game_id), "version": version, "etag": etag, "state": "retired"}
            event_id = uuid4()
            connection.execute(
                text("""
                    INSERT INTO platform.outbox_events
                    (id, topic, aggregate_type, aggregate_id, payload, created_at, available_at)
                    VALUES (:id, 'catalog.game.retired.v1', 'published_game', :game,
                            CAST(:payload AS jsonb), :now, :now)
                """),
                {"id": event_id, "game": game_id,
                 "payload": json.dumps({"event_id": str(event_id), **response}, ensure_ascii=False),
                 "now": now},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.command_idempotency
                    (id, command, idempotency_key, request_hash, response, created_at)
                    VALUES (:id, 'retire', :key, :request_hash, CAST(:response AS jsonb), :now)
                """),
                {"id": uuid4(), "key": idempotency_key, "request_hash": request_hash,
                 "response": json.dumps(response), "now": now},
            )
            return response

    def withdraw(
        self,
        game_id: UUID,
        *,
        actor: str,
        reason: str,
        idempotency_key: str,
        expected_etag: str,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        return self.retire(
            game_id,
            actor=actor,
            reason=reason,
            idempotency_key=idempotency_key,
            expected_etag=expected_etag,
            connection=connection,
        )
