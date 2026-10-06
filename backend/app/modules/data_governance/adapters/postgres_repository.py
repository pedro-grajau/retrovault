"""Persistência privada com transação independente por registro."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from PIL import Image
from sqlalchemy import Connection, Engine, text

from app.modules.data_governance.domain.models import (
    CatalogRecord,
    Manifest,
    Right,
    SourceResponse,
    contains_invalid_postgres_text,
)
from app.modules.data_governance.domain.normalization import (
    Candidate,
    candidate_from_evidence,
    valid_image,
)


class ReviewConflict(ValueError):
    pass


class ReviewNotFound(ValueError):
    pass


class InvalidCorrection(ValueError):
    pass


EDITABLE_FIELDS = {
    "title", "platform", "region", "edition", "genre", "developer",
    "publisher", "year", "release_date", "release_date_granularity",
    "rating", "description", "included_items",
}
MAX_INCLUDED_ITEMS = 100
MAX_CORRECTION_TEXT = 5000
PRIVATE_USE_TERMS_REFERENCE = "https://retroachievements.org/terms"
PRIVATE_USE_BASIS = (
    "O responsável pelo projeto declarou que este é um portfólio particular e "
    "invocou a exceção de reproduções individuais para uso privado indicada na "
    "seção Copyrights dos termos da RetroAchievements. A publicação fica restrita "
    "à instância local."
)
PRIVATE_USE_SCOPE = "loopback_only"
PRIVATE_USE_ACTOR = "Eduardo"


class PostgresRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @contextmanager
    def guard(self, source: str, version: str) -> Iterator[None]:
        # Uma transação mantém o lock até o fim da execução sem bloquear os commits
        # independentes de cada registro em outras conexões.
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"retrovault:ingest:{source}:{version}"},
            )
            yield

    def existing_run(self, source: str, version: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT id, package_hash, state, manifest_hash FROM data_governance.ingest_runs WHERE source=:source AND source_version=:version"
                    ),
                    {"source": source, "version": version},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def set_manifest_hash(self, run_id: UUID, manifest_hash: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE data_governance.ingest_runs
                    SET manifest_hash=:manifest_hash
                    WHERE id=:id AND manifest_hash IS NULL
                """),
                {"id": run_id, "manifest_hash": manifest_hash},
            )

    def outcomes(self, run_id: UUID) -> dict[str, str]:
        with self.engine.connect() as connection:
            preserved = connection.execute(
                text("""
                    SELECT e.source_record_id FROM data_governance.run_evidence re
                    JOIN data_governance.raw_evidence e ON e.id = re.evidence_id
                    WHERE re.run_id = :id
                """),
                {"id": run_id},
            ).scalars()
            failures = connection.execute(
                text(
                    "SELECT source_record_id FROM data_governance.ingest_failures WHERE run_id = :id"
                ),
                {"id": run_id},
            ).scalars()
            result = dict.fromkeys(preserved, "preserved")
            result.update(dict.fromkeys(failures, "rejected"))
            return result

    def catalog_run_ids(
        self,
        catalog_version: str,
        *,
        expected_game_count: int,
        expected_batch_count: int,
        batch_size: int | None = None,
    ) -> list[UUID]:
        """Return the completed batch runs belonging to one SNES catalog version."""
        if (
            type(expected_game_count) is not int
            or expected_game_count <= 0
            or type(expected_batch_count) is not int
            or expected_batch_count <= 0
            or (
                batch_size is not None
                and (type(batch_size) is not int or batch_size <= 0)
            )
        ):
            raise ValueError("catalog_batch_expectations_invalid")
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT id, state, source_version, received, preserved, rejected
                    FROM data_governance.ingest_runs
                    WHERE source='retroachievements'
                      AND left(source_version, length(:prefix))=:prefix
                      AND substring(source_version from length(:prefix) + 1)
                          ~ '^-n[1-9][0-9]*-b[0-9]+$'
                    ORDER BY source_version
                """),
                {"prefix": catalog_version},
            ).mappings().all()
        if not rows:
            raise ValueError("catalog_run_not_found")

        groups: dict[int, list[tuple[int, dict[str, object]]]] = {}
        suffix_pattern = re.compile(r"-n([1-9][0-9]*)-b([0-9]+)")
        for row in rows:
            suffix = str(row["source_version"])[len(catalog_version) :]
            match = suffix_pattern.fullmatch(suffix)
            if match is None:
                raise ValueError("catalog_run_invalid")
            size = int(match.group(1))
            if batch_size is None or size == batch_size:
                groups.setdefault(size, []).append((int(match.group(2)), dict(row)))

        complete: list[list[tuple[int, dict[str, object]]]] = []
        for group in groups.values():
            indexes = sorted(index for index, _ in group)
            if (
                len(group) == expected_batch_count
                and indexes == list(range(1, expected_batch_count + 1))
                and all(row["state"] == "completed" for _, row in group)
                and sum(cast(int, row["received"]) for _, row in group) == expected_game_count
                and sum(cast(int, row["preserved"]) for _, row in group) == expected_game_count
                and sum(cast(int, row["rejected"]) for _, row in group) == 0
            ):
                complete.append(group)
        if len(complete) > 1:
            raise ValueError("catalog_batch_size_ambiguous")
        if not complete:
            if batch_size is not None and batch_size not in groups:
                raise ValueError("catalog_run_not_found")
            raise ValueError("catalog_run_incomplete")
        selected = complete[0]
        return [UUID(str(row["id"])) for _, row in sorted(selected)]

    def record_private_use_for_catalog(
        self,
        catalog_version: str,
        *,
        expected_game_count: int,
        expected_batch_count: int,
        batch_size: int | None = None,
    ) -> dict[str, int]:
        """Append a user-declared, loopback-only use decision for valid SNES covers."""
        run_ids = self.catalog_run_ids(
            catalog_version,
            expected_game_count=expected_game_count,
            expected_batch_count=expected_batch_count,
            batch_size=batch_size,
        )
        with self.engine.begin() as connection:
            created = 0
            existing = 0
            eligible = 0
            seen: set[tuple[UUID, str]] = set()
            for run_id in run_ids:
                rows = connection.execute(
                    text("""
                        SELECT e.id AS evidence_id, e.raw_payload,
                               mr.media_path, mr.role, mr.storage_right,
                               mr.publication_right,
                               mr.attribution, pm.content
                        FROM data_governance.run_evidence re
                        JOIN data_governance.raw_evidence e ON e.id=re.evidence_id
                        JOIN data_governance.media_rights mr ON mr.evidence_id=e.id
                        LEFT JOIN data_governance.private_media pm
                          ON pm.run_id=re.run_id AND pm.evidence_id=e.id
                         AND pm.media_path=mr.media_path
                        WHERE re.run_id=:run_id
                          AND e.source='retroachievements'
                          AND mr.role='box_art'
                        ORDER BY e.id, mr.media_path, (pm.content IS NOT NULL) DESC
                    """),
                    {"run_id": run_id},
                ).mappings().all()
                for row in rows:
                    evidence_key = (UUID(str(row["evidence_id"])), str(row["media_path"]))
                    if evidence_key in seen:
                        continue
                    seen.add(evidence_key)
                    try:
                        attributes = json.loads(row["raw_payload"]).get("attributes", {})
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ValueError("catalog_evidence_invalid") from exc
                    if not isinstance(attributes, dict) or attributes.get("platform") != "SNES":
                        raise ValueError("catalog_not_snes")
                    content = bytes(row["content"]) if row["content"] is not None else None
                    if (
                        row["storage_right"] != "confirmed"
                        or row["publication_right"] == "denied"
                        or not isinstance(row["attribution"], str)
                        or not row["attribution"].strip()
                        or not valid_image(content)
                    ):
                        continue
                    eligible += 1
                    inserted = connection.execute(
                        text("""
                            INSERT INTO data_governance.private_use_cover_decisions
                            (id, evidence_id, media_path, scope, basis, terms_reference,
                             actor, decided_at)
                            VALUES (:id, :evidence_id, :media_path, :scope, :basis,
                                    :terms_reference, :actor, :decided_at)
                            ON CONFLICT DO NOTHING
                            RETURNING id
                        """),
                        {
                            "id": uuid4(),
                            "evidence_id": row["evidence_id"],
                            "media_path": row["media_path"],
                            "scope": PRIVATE_USE_SCOPE,
                            "basis": PRIVATE_USE_BASIS,
                            "terms_reference": PRIVATE_USE_TERMS_REFERENCE,
                            "actor": PRIVATE_USE_ACTOR,
                            "decided_at": datetime.now(UTC),
                        },
                    ).scalar_one_or_none()
                    if inserted is None:
                        existing += 1
                    else:
                        created += 1
        if eligible == 0:
            raise ValueError("catalog_has_no_valid_covers")
        return {
            "eligible_covers": eligible,
            "decisions_created": created,
            "decisions_existing": existing,
        }

    def create_run(
        self,
        run_id: UUID,
        manifest: Manifest,
        package_hash: str,
        config_hash: str,
        app_version: str,
        manifest_hash: str | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_governance.ingest_runs
                (id, source, source_version, captured_at, started_at, state, package_hash, config_fingerprint, app_version, received, manifest_hash)
                VALUES (:id, :source, :version, :captured_at, :started_at, 'running', :package_hash, :config_hash, :app_version, :received, :manifest_hash)
            """),
                {
                    "id": run_id,
                    "source": manifest.source,
                    "version": manifest.version,
                    "captured_at": manifest.captured_at,
                    "started_at": datetime.now(UTC),
                    "package_hash": package_hash,
                    "config_hash": config_hash,
                    "app_version": app_version,
                    "received": len(manifest.records),
                    "manifest_hash": manifest_hash,
                },
            )

    def preserve(
        self,
        run_id: UUID,
        evidence_id: UUID,
        manifest: Manifest,
        record: CatalogRecord,
        payload_hash: str,
        media_bytes: dict[str, bytes | None] | None = None,
        *,
        source_payload: bytes | None = None,
        source_responses: tuple[SourceResponse, ...] = (),
    ) -> None:
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text("""
                INSERT INTO data_governance.raw_evidence
                (id, source, source_record_id, payload_hash, raw_payload, raw_payload_bytes, captured_at, preserved_at)
                VALUES (:id, :source, :record_id, :hash, :payload, :payload_bytes, :captured_at, :preserved_at)
                ON CONFLICT (source, source_record_id, payload_hash) DO NOTHING
                RETURNING id
            """),
                {
                    "id": evidence_id,
                    "source": manifest.source,
                    "record_id": record.record_id,
                    "hash": payload_hash,
                    "payload": record.raw_payload,
                    "payload_bytes": (
                        record.raw_payload.encode("utf-8")
                        if source_payload is None
                        else source_payload
                    ),
                    "captured_at": manifest.captured_at,
                    "preserved_at": datetime.now(UTC),
                },
            ).scalar_one_or_none()
            if inserted is None:
                evidence_id = connection.execute(
                    text("""
                    SELECT id FROM data_governance.raw_evidence
                    WHERE source=:source AND source_record_id=:record_id AND payload_hash=:hash
                """),
                    {
                        "source": manifest.source,
                        "record_id": record.record_id,
                        "hash": payload_hash,
                    },
                ).scalar_one()
            connection.execute(
                text("""
                INSERT INTO data_governance.run_evidence (run_id, evidence_id) VALUES (:run_id, :evidence_id)
                ON CONFLICT DO NOTHING
            """),
                {"run_id": run_id, "evidence_id": evidence_id},
            )
            if inserted is not None:
                for path in record.attributes:
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.attribute_origins
                        (evidence_id, attribute_path, source, source_version, source_record_id, actor, captured_at)
                        VALUES (:evidence_id, :path, :source, :version, :record_id, :actor, :captured_at)
                    """),
                        {
                            "evidence_id": evidence_id,
                            "path": path,
                            "source": manifest.source,
                            "version": manifest.version,
                            "record_id": record.record_id,
                            "actor": manifest.actor,
                            "captured_at": manifest.captured_at,
                        },
                    )
                for media in record.media:
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.media_rights
                        (evidence_id, media_path, role, storage_right, publication_right, attribution, eligible)
                        VALUES (:evidence_id, :path, :role, :storage, :publication, :attribution, :eligible)
                    """),
                        {
                            "evidence_id": evidence_id,
                            "path": media.path,
                            "role": media.role,
                            "storage": media.storage.value,
                            "publication": media.publication.value,
                            "attribution": media.attribution,
                            "eligible": media.eligible,
                        },
                    )
            for response in source_responses:
                response_hash = sha256(response.payload).hexdigest()
                stored_hash = connection.execute(
                    text("""
                        INSERT INTO data_governance.source_endpoint_responses
                        (evidence_id, endpoint, payload_hash, payload_bytes, captured_at)
                        VALUES (:evidence_id, :endpoint, :payload_hash, :payload_bytes, :captured_at)
                        ON CONFLICT (evidence_id, endpoint) DO NOTHING
                        RETURNING payload_hash
                    """),
                    {
                        "evidence_id": evidence_id,
                        "endpoint": response.endpoint,
                        "payload_hash": response_hash,
                        "payload_bytes": response.payload,
                        "captured_at": response.captured_at,
                    },
                ).scalar_one_or_none()
                if stored_hash is None:
                    stored_hash = connection.execute(
                        text("""
                            SELECT payload_hash
                            FROM data_governance.source_endpoint_responses
                            WHERE evidence_id=:evidence_id AND endpoint=:endpoint
                        """),
                        {"evidence_id": evidence_id, "endpoint": response.endpoint},
                    ).scalar_one()
                if stored_hash.strip() != response_hash:
                    raise ValueError("source_response_conflict")
            for metric in record.source_metrics:
                connection.execute(
                    text("""
                        INSERT INTO data_governance.source_metrics
                        (evidence_id, metric_name, metric_value, endpoint, source,
                         source_version, source_record_id, actor, captured_at)
                        VALUES (:evidence_id, :metric_name, CAST(:metric_value AS jsonb),
                                :endpoint, :source, :source_version, :source_record_id,
                                :actor, :captured_at)
                        ON CONFLICT (evidence_id, metric_name) DO NOTHING
                    """),
                    {
                        "evidence_id": evidence_id,
                        "metric_name": metric.name,
                        "metric_value": json.dumps(metric.value),
                        "endpoint": metric.endpoint,
                        "source": manifest.source,
                        "source_version": manifest.version,
                        "source_record_id": record.record_id,
                        "actor": manifest.actor,
                        "captured_at": metric.captured_at,
                    },
                )
            for media in record.media:
                payload = (media_bytes or {}).get(media.path)
                if payload is not None and media.storage is Right.CONFIRMED:
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.private_media
                        (run_id, evidence_id, media_path, content_hash, content)
                        VALUES (:run_id, :evidence_id, :path, :hash, :content)
                        ON CONFLICT DO NOTHING
                    """),
                        {
                            "run_id": run_id,
                            "evidence_id": evidence_id,
                            "path": media.path,
                            "hash": sha256(payload).hexdigest(),
                            "content": payload,
                        },
                    )

    def fail(
        self, run_id: UUID, record_id: str, code: str, correlation_id: UUID
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_governance.ingest_failures
                (id, run_id, source_record_id, code, correlation_id, cause)
                VALUES (:id, :run_id, :record_id, :code, :correlation_id, :cause)
                ON CONFLICT (run_id, source_record_id) DO NOTHING
            """),
                {
                    "id": uuid5(run_id, record_id),
                    "run_id": run_id,
                    "record_id": record_id,
                    "code": code,
                    "correlation_id": correlation_id,
                    "cause": (
                        "Falha de persistência do registro."
                        if code == "storage_error"
                        else "Registro não preservado; verificar formato local."
                    ),
                },
            )

    def finish(
        self, run_id: UUID, received: int, preserved: int, rejected: int
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                UPDATE data_governance.ingest_runs
                SET state='completed', finished_at=:finished_at, received=:received,
                    preserved=:preserved, rejected=:rejected, pending=:preserved
                WHERE id=:id
            """),
                {
                    "id": run_id,
                    "finished_at": datetime.now(UTC),
                    "received": received,
                    "preserved": preserved,
                    "rejected": rejected,
                },
            )

    def summary(self, run_id: UUID) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text("""
                SELECT id, source, source_version AS version, captured_at, state,
                    config_fingerprint, app_version, received, preserved, rejected, pending
                FROM data_governance.ingest_runs WHERE id=:id
            """),
                    {"id": run_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            result = dict(row)
            if result["state"] == "running":
                outcomes = self.outcomes(run_id)
                result["preserved"] = sum(
                    value == "preserved" for value in outcomes.values()
                )
                result["rejected"] = sum(
                    value == "rejected" for value in outcomes.values()
                )
                result["pending"] = result["preserved"]
            result["failures"] = [
                dict(failure)
                for failure in connection.execute(
                    text("""
                SELECT source_record_id AS record_id, code, correlation_id, cause
                FROM data_governance.ingest_failures WHERE run_id=:id ORDER BY source_record_id
            """),
                    {"id": run_id},
                ).mappings()
            ]
            return result

    @contextmanager
    def processing_guard(self, run_id: UUID, rule_version: str) -> Iterator[None]:
        with self.engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"retrovault:process:{rule_version}"},
            )
            yield

    def processing_inputs(self, run_id: UUID) -> list[dict[str, object]] | None:
        with self.engine.connect() as connection:
            state = connection.execute(
                text("SELECT state FROM data_governance.ingest_runs WHERE id=:id"),
                {"id": run_id},
            ).scalar_one_or_none()
            if state != "completed":
                return None
            result = []
            evidence = connection.execute(
                text("""
                SELECT e.id, e.source_record_id, e.raw_payload
                FROM data_governance.run_evidence re
                JOIN data_governance.raw_evidence e ON e.id=re.evidence_id
                WHERE re.run_id=:id ORDER BY e.source_record_id
            """),
                {"id": run_id},
            ).mappings()
            for row in evidence:
                media = [
                    dict(item)
                    for item in connection.execute(
                        text("""
                    SELECT mr.media_path, mr.role, mr.storage_right,
                       CASE WHEN pud.scope='loopback_only'
                                  AND mr.publication_right <> 'denied' THEN 'confirmed'
                            ELSE mr.publication_right END AS publication_right,
                           mr.publication_right AS source_publication_right,
                           mr.attribution, pm.content IS NOT NULL AS content_available,
                           pm.content, pud.id AS rights_decision_id,
                           pud.scope AS rights_scope, pud.basis AS rights_basis,
                           pud.terms_reference, pud.actor AS rights_actor,
                           pud.decided_at AS rights_decided_at
                    FROM data_governance.media_rights mr
                    LEFT JOIN data_governance.private_media pm
                      ON pm.run_id=:run_id AND pm.evidence_id=mr.evidence_id AND pm.media_path=mr.media_path
                    LEFT JOIN LATERAL (
                      SELECT id, scope, basis, terms_reference, actor, decided_at
                      FROM data_governance.private_use_cover_decisions
                      WHERE evidence_id=mr.evidence_id AND media_path=mr.media_path
                      ORDER BY decided_at DESC, id DESC LIMIT 1
                    ) pud ON true
                    WHERE mr.evidence_id=:evidence_id
                """),
                        {"run_id": run_id, "evidence_id": row["id"]},
                    ).mappings()
                ]
                for item in media:
                    if item["content"] is not None:
                        item["content"] = bytes(item["content"])
                result.append(
                    {
                        "evidence_id": row["id"],
                        "record_id": row["source_record_id"],
                        "attributes": json.loads(row["raw_payload"]).get(
                            "attributes", {}
                        ),
                        "media": media,
                    }
                )
            return result

    def save_processing(
        self,
        run_id: UUID,
        rule_version: str,
        fingerprint: str,
        candidates: list[Candidate],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_governance.processing_runs
                (run_id, rule_version, rule_fingerprint, processed_at)
                VALUES (:run_id, :rule, :fingerprint, :now)
            """),
                {
                    "run_id": run_id,
                    "rule": rule_version,
                    "fingerprint": fingerprint,
                    "now": datetime.now(UTC),
                },
            )
            for candidate in candidates:
                params = {
                    "run_id": run_id,
                    "rule": rule_version,
                    "evidence_id": candidate.evidence_id,
                }
                connection.execute(
                    text("""
                    INSERT INTO data_governance.staging_records
                    (run_id, rule_version, evidence_id, source_record_id, identity_key,
                     state, optional_missing, ambiguous_identity)
                    VALUES (:run_id, :rule, :evidence_id, :record_id, :identity_key,
                            :state, :missing, :ambiguous)
                """),
                    {
                        **params,
                        "record_id": candidate.record_id,
                        "identity_key": json.dumps(
                            candidate.identity, ensure_ascii=False
                        ),
                        "state": candidate.state,
                        "missing": list(candidate.missing),
                        "ambiguous": candidate.ambiguous,
                    },
                )
                for path, value in candidate.values.items():
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.staging_values
                        (run_id, rule_version, evidence_id, attribute_path, normalized_value, raw_attribute_path)
                        VALUES (:run_id, :rule, :evidence_id, :path, CAST(:value AS jsonb), :path)
                    """),
                        {
                            **params,
                            "path": path,
                            "value": json.dumps(value, ensure_ascii=False),
                        },
                    )

                for other_id, kind, field_name in candidate.matches:
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.staging_matches
                        (run_id, rule_version, evidence_id, other_evidence_id, kind, field)
                        VALUES (:run_id, :rule, :evidence_id, :other, :kind, :field)
                    """),
                        {
                            **params,
                            "other": other_id,
                            "kind": kind,
                            "field": field_name,
                        },
                    )
                for issue in candidate.issues:
                    connection.execute(
                        text("""
                        INSERT INTO data_governance.quarantine_issues
                        (run_id, rule_version, evidence_id, field, code, cause)
                        VALUES (:run_id, :rule, :evidence_id, :field, :code, :cause)
                        ON CONFLICT DO NOTHING
                    """),
                        {
                            **params,
                            "field": issue.field,
                            "code": issue.code,
                            "cause": issue.cause,
                        },
                    )

    def processing_summary(
        self, run_id: UUID, rule_version: str
    ) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            run = (
                connection.execute(
                    text("""
                SELECT run_id, rule_version, rule_fingerprint, processed_at
                FROM data_governance.processing_runs
                WHERE run_id=:id AND rule_version=:rule
            """),
                    {"id": run_id, "rule": rule_version},
                )
                .mappings()
                .first()
            )
            if run is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    text("""
                SELECT source_record_id AS record_id, state, optional_missing,
                       ambiguous_identity
                FROM data_governance.staging_records
                WHERE run_id=:id AND rule_version=:rule ORDER BY source_record_id
            """),
                    {"id": run_id, "rule": rule_version},
                ).mappings()
            ]
            match_rows = connection.execute(
                text("""
                SELECT sr.source_record_id AS record_id, other.source_record_id AS other_record_id,
                       sm.kind, sm.field,
                       own_value.normalized_value AS own_value,
                       other_value.normalized_value AS other_value,
                       own_origin.source AS own_source,
                       own_origin.source_version AS own_version,
                       own_origin.source_record_id AS own_source_record_id,
                       own_origin.actor AS own_actor,
                       own_origin.captured_at AS own_captured_at,
                       other_origin.source AS other_source,
                       other_origin.source_version AS other_version,
                       other_origin.source_record_id AS other_source_record_id,
                       other_origin.actor AS other_actor,
                       other_origin.captured_at AS other_captured_at
                FROM data_governance.staging_matches sm
                JOIN data_governance.staging_records sr ON sr.run_id=sm.run_id AND sr.rule_version=sm.rule_version AND sr.evidence_id=sm.evidence_id
                JOIN data_governance.raw_evidence other ON other.id=sm.other_evidence_id
                LEFT JOIN data_governance.staging_values own_value
                  ON own_value.run_id=sm.run_id AND own_value.rule_version=sm.rule_version
                 AND own_value.evidence_id=sm.evidence_id AND own_value.attribute_path=sm.field
                LEFT JOIN data_governance.attribute_origins own_origin
                  ON own_origin.evidence_id=sm.evidence_id AND own_origin.attribute_path=sm.field
                LEFT JOIN LATERAL (
                    SELECT normalized_value FROM data_governance.staging_values
                    WHERE evidence_id=sm.other_evidence_id AND rule_version=sm.rule_version
                      AND attribute_path=sm.field
                    ORDER BY run_id LIMIT 1
                ) other_value ON true
                LEFT JOIN data_governance.attribute_origins other_origin
                  ON other_origin.evidence_id=sm.other_evidence_id AND other_origin.attribute_path=sm.field
                WHERE sm.run_id=:id AND sm.rule_version=:rule ORDER BY record_id, other_record_id, kind, field
            """),
                {"id": run_id, "rule": rule_version},
            ).mappings()
            matches = []
            for row in match_rows:
                match = {
                    "record_id": row["record_id"],
                    "other_record_id": row["other_record_id"],
                    "kind": row["kind"],
                    "field": row["field"],
                    "alternatives": [],
                }
                if row["kind"] == "conflict":
                    for side, record_id in (
                        ("own", row["record_id"]),
                        ("other", row["other_record_id"]),
                    ):
                        match["alternatives"].append(
                            {
                                "record_id": record_id,
                                "value": row[f"{side}_value"],
                                "origin": {
                                    "source": row[f"{side}_source"],
                                    "source_version": row[f"{side}_version"],
                                    "source_record_id": row[f"{side}_source_record_id"],
                                    "actor": row[f"{side}_actor"],
                                    "captured_at": row[f"{side}_captured_at"],
                                },
                            }
                        )
                matches.append(match)
            issues = [
                dict(row)
                for row in connection.execute(
                    text("""
                SELECT sr.source_record_id AS record_id, qi.field, qi.code, qi.cause
                FROM data_governance.quarantine_issues qi
                JOIN data_governance.staging_records sr ON sr.run_id=qi.run_id AND sr.rule_version=qi.rule_version AND sr.evidence_id=qi.evidence_id
                WHERE qi.run_id=:id AND qi.rule_version=:rule ORDER BY record_id, field, code
            """),
                    {"id": run_id, "rule": rule_version},
                ).mappings()
            ]
            missing: dict[str, int] = {}
            for record in rows:
                for field_name in record["optional_missing"]:
                    missing[field_name] = missing.get(field_name, 0) + 1
            return {
                **dict(run),
                "total": len(rows),
                "review": sum(row["state"] == "review" for row in rows),
                "quarantine": sum(row["state"] == "quarantine" for row in rows),
                "optional_missing": missing,
                "records": rows,
                "matches": matches,
                "issues": issues,
            }

    def processing_peers(self, run_id: UUID, rule_version: str) -> list[Candidate]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text("""
                    SELECT sr.evidence_id, sr.source_record_id, sr.identity_key,
                           sr.optional_missing, sr.ambiguous_identity,
                           sv.attribute_path, sv.normalized_value
                    FROM data_governance.staging_records sr
                    LEFT JOIN data_governance.staging_values sv
                      ON sv.run_id=sr.run_id AND sv.rule_version=sr.rule_version
                     AND sv.evidence_id=sr.evidence_id
                    WHERE sr.run_id<>:run_id AND sr.rule_version=:rule
                    ORDER BY sr.evidence_id
                """),
                {"run_id": run_id, "rule": rule_version},
            ).mappings()
            peers: dict[UUID, Candidate] = {}
            for row in rows:
                evidence_id = row["evidence_id"]
                if evidence_id not in peers:
                    peers[evidence_id] = Candidate(
                        evidence_id,
                        row["source_record_id"],
                        {},
                        tuple(json.loads(row["identity_key"])),
                        tuple(row["optional_missing"]),
                        row["ambiguous_identity"],
                    )
                if row["attribute_path"] is not None:
                    peers[evidence_id].values[row["attribute_path"]] = row[
                        "normalized_value"
                    ]
            return list(peers.values())

    @staticmethod
    def _review_etag(
        run_id: UUID,
        rule_version: str,
        evidence_id: UUID,
        values: dict[str, Any],
        correction_ids: list[str],
        rights_decision_ids: list[str] | None = None,
    ) -> str:
        body = json.dumps(
            {
                "run_id": str(run_id),
                "rule_version": rule_version,
                "evidence_id": str(evidence_id),
                "values": values,
                "corrections": correction_ids,
                "rights_decisions": sorted(rights_decision_ids or []),
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return f'"{sha256(body).hexdigest()}"'

    def _review_candidate(
        self,
        connection: Connection,
        run_id: UUID,
        record_id: str,
        rule_version: str,
        include_private_media: bool = False,
    ) -> dict[str, object]:
        row = connection.execute(
            text("""
                SELECT sr.evidence_id, sr.state, sr.ambiguous_identity,
                       e.source, e.source_record_id, e.payload_hash, e.captured_at,
                       ir.source_version, ir.app_version
                FROM data_governance.staging_records sr
                JOIN data_governance.raw_evidence e ON e.id=sr.evidence_id
                JOIN data_governance.run_evidence re
                  ON re.evidence_id=e.id AND re.run_id=sr.run_id
                JOIN data_governance.ingest_runs ir ON ir.id=sr.run_id
                WHERE sr.run_id=:run_id AND sr.rule_version=:rule
                  AND sr.source_record_id=:record_id
            """),
            {"run_id": run_id, "rule": rule_version, "record_id": record_id},
        ).mappings().first()
        if row is None:
            raise ReviewNotFound("review_not_found")
        evidence_id = row["evidence_id"]
        values = {
            item["attribute_path"]: item["normalized_value"]
            for item in connection.execute(
                text("""
                    SELECT attribute_path, normalized_value
                    FROM data_governance.staging_values
                    WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence_id
                """),
                {"run_id": run_id, "rule": rule_version, "evidence_id": evidence_id},
            ).mappings()
        }
        corrections = list(connection.execute(
            text("""
                SELECT id, field, value, previous_value, revision, actor, reason, previous_etag,
                       resulting_etag, corrected_at
                FROM data_governance.review_corrections
                WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence_id
                ORDER BY revision
            """),
            {"run_id": run_id, "rule": rule_version, "evidence_id": evidence_id},
        ).mappings())
        for correction in corrections:
            values[correction["field"]] = correction["value"]
        correction_ids = [str(item["id"]) for item in corrections]
        media_rows = list(connection.execute(
            text("""
                SELECT mr.media_path, mr.role, mr.storage_right,
                       CASE WHEN pud.scope='loopback_only'
                                  AND mr.publication_right <> 'denied' THEN 'confirmed'
                            ELSE mr.publication_right END AS publication_right,
                       mr.publication_right AS source_publication_right,
                       mr.attribution,
                       CASE WHEN pud.scope='loopback_only'
                                  AND mr.publication_right <> 'denied' THEN true
                            ELSE mr.eligible END AS eligible,
                       pm.content_hash, pm.content,
                       pud.id AS rights_decision_id, pud.scope AS rights_scope,
                       pud.basis AS rights_basis, pud.terms_reference,
                       pud.actor AS rights_actor, pud.decided_at AS rights_decided_at
                FROM data_governance.media_rights mr
                LEFT JOIN data_governance.private_media pm
                  ON pm.run_id=:run_id AND pm.evidence_id=mr.evidence_id
                 AND pm.media_path=mr.media_path
                LEFT JOIN LATERAL (
                  SELECT id, scope, basis, terms_reference, actor, decided_at
                  FROM data_governance.private_use_cover_decisions
                  WHERE evidence_id=mr.evidence_id AND media_path=mr.media_path
                  ORDER BY decided_at DESC, id DESC LIMIT 1
                ) pud ON true
                WHERE mr.evidence_id=:evidence_id
                ORDER BY mr.media_path
            """),
            {"run_id": run_id, "evidence_id": evidence_id},
        ).mappings())
        rights_decision_ids = sorted(
            str(item["rights_decision_id"])
            for item in media_rows
            if item["rights_decision_id"] is not None
        )
        etag = self._review_etag(
            run_id, rule_version, evidence_id, values, correction_ids,
            rights_decision_ids,
        )
        media = []
        validation_media = []
        for item in media_rows:
            content = bytes(item["content"]) if item["content"] is not None else None
            current = {
                "path": item["media_path"],
                "role": item["role"],
                "storage_right": item["storage_right"],
                "publication_right": item["publication_right"],
                "source_publication_right": item["source_publication_right"],
                "attribution": item["attribution"],
                "eligible": item["eligible"],
                "content_hash": item["content_hash"],
                "content_available": item["content"] is not None,
                "rights_decision_id": (
                    str(item["rights_decision_id"])
                    if item["rights_decision_id"] is not None else None
                ),
                "rights_scope": item["rights_scope"],
            }
            if item["rights_decision_id"] is not None:
                current["rights_decision"] = {
                    "id": str(item["rights_decision_id"]),
                    "scope": item["rights_scope"],
                    "basis": item["rights_basis"],
                    "terms_reference": item["terms_reference"],
                    "actor": item["rights_actor"],
                    "decided_at": item["rights_decided_at"],
                }
            if include_private_media and content is not None:
                current["content"] = content
            media.append(current)
            validation_media.append({
                "role": item["role"],
                "storage_right": item["storage_right"],
                "publication_right": item["publication_right"],
                "source_publication_right": item["source_publication_right"],
                "attribution": item["attribution"],
                "content_available": content is not None,
                "content": content,
            })
        origins = {
            item["attribute_path"]: {
                "source": item["source"],
                "source_version": item["source_version"],
                "source_record_id": item["source_record_id"],
                "actor": item["actor"],
                "captured_at": item["captured_at"],
            }
            for item in connection.execute(
                text("""
                    SELECT ao.attribute_path, ao.source, ao.source_version,
                           ao.source_record_id, ao.actor, ao.captured_at
                    FROM data_governance.attribute_origins ao
                    WHERE ao.evidence_id=:evidence_id ORDER BY ao.attribute_path
                """),
                {"evidence_id": evidence_id},
            ).mappings()
        }
        for correction in corrections:
            origins[correction["field"]] = {
                "source": "human-review",
                "source_version": str(correction["revision"]),
                "source_record_id": record_id,
                "previous_value": correction["previous_value"],
                "actor": correction["actor"],
                "reason": correction["reason"],
                "revision": correction["revision"],
                "corrected_at": correction["corrected_at"],
                "previous_etag": correction["previous_etag"],
                "resulting_etag": correction["resulting_etag"],
            }
        candidate = candidate_from_evidence(
            {
                "evidence_id": evidence_id,
                "record_id": record_id,
                "attributes": values,
                "media": validation_media,
            },
            rule_version,
        )
        existing_issues = [dict(item) for item in connection.execute(
            text("""
                SELECT field, code, cause FROM data_governance.quarantine_issues
                WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence_id
                ORDER BY field, code
            """),
            {"run_id": run_id, "rule": rule_version, "evidence_id": evidence_id},
        ).mappings()]
        corrected_fields = {item["field"] for item in corrections}
        unresolved = [
            issue for issue in existing_issues
            if issue["field"] not in corrected_fields
        ]
        issues = [
            {"field": item.field, "code": item.code, "cause": item.cause}
            for item in candidate.issues
        ]
        known = {(item["field"], item["code"]) for item in issues}
        issues.extend(
            item for item in unresolved if (item["field"], item["code"]) not in known
        )
        stable_id = uuid5(NAMESPACE_URL, f"retrovault:catalog:{row['source']}:{record_id}")
        cover = None
        for metadata, validation in zip(media, validation_media, strict=True):
            attribution = metadata["attribution"]
            content = validation["content"]
            if (
                metadata["role"] == "box_art"
                and metadata["storage_right"] == "confirmed"
                and metadata["publication_right"] == "confirmed"
                and isinstance(attribution, str)
                and bool(attribution.strip())
                and isinstance(content, bytes)
            ):
                try:
                    with Image.open(BytesIO(content)) as image:
                        image_format = image.format
                except (Image.DecompressionBombError, OSError, ValueError):
                    issues.append({
                        "field": "box_art",
                        "code": "cover_inaccessible",
                        "cause": "Capa privada inacessível ou inválida.",
                    })
                    continue
                mime_types = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
                if image_format in mime_types:
                    rights_reference = None
                    if metadata.get("rights_scope") == PRIVATE_USE_SCOPE:
                        rights_reference = (
                            "Uso privado declarado pelo responsável do projeto para "
                            "portfólio particular; fundamento invocado: RetroAchievements "
                            "Terms, Copyrights, "
                            f"{metadata.get('terms_reference')}; escopo: instância local "
                            "servida somente em 127.0.0.1. Fonte da capa: "
                            f"{attribution}."
                        )
                    cover = {
                        "path": metadata["path"],
                        "content": content if include_private_media else None,
                        "content_type": mime_types[image_format],
                        "content_hash": metadata["content_hash"],
                        "storage_right": metadata["storage_right"],
                        "publication_right": metadata["publication_right"],
                        "source_publication_right": metadata["source_publication_right"],
                        "rights_scope": metadata.get("rights_scope"),
                        "attribution": metadata["attribution"],
                        "rights_reference": rights_reference,
                        "rights_decision_id": (
                            str(metadata["rights_decision_id"])
                            if metadata["rights_decision_id"] is not None else None
                        ),
                    }
                    break
        return {
            "run_id": run_id,
            "rule_version": rule_version,
            "evidence_id": evidence_id,
            "record_id": record_id,
            "source": row["source"],
            "source_version": row["source_version"],
            "payload_hash": row["payload_hash"],
            "captured_at": row["captured_at"],
            "app_version": row["app_version"],
            "public_id": stable_id,
            "values": values,
            "media": media,
            "cover": cover,
            "rights_decision_ids": rights_decision_ids,
            "lineage": origins,
            "corrections": [dict(item) for item in corrections],
            "etag": etag,
            "state": "quarantine" if issues else "review",
            "issues": issues,
            "optional_missing": tuple(
                field for field in (
                    "publisher", "developer", "genre", "description", "year",
                    "rating", "included_items",
                )
                if not values.get(field)
            ),
            "ambiguous_identity": candidate.ambiguous,
        }

    def review_candidate(
        self,
        run_id: UUID,
        record_id: str,
        rule_version: str = "editorial-v1",
        *,
        include_private_media: bool = False,
    ) -> dict[str, object]:
        with self.engine.connect() as connection:
            return self._review_candidate(
                connection, run_id, record_id, rule_version, include_private_media
            )

    def lock_review_candidate(
        self,
        connection: Connection,
        run_id: UUID,
        record_id: str,
        rule_version: str = "editorial-v1",
        *,
        include_private_media: bool = False,
    ) -> dict[str, object]:
        """Lock and reload a candidate inside a caller-owned publication transaction."""
        evidence_id = connection.execute(
            text("""
                SELECT evidence_id FROM data_governance.staging_records
                WHERE run_id=:run_id AND rule_version=:rule AND source_record_id=:record_id
            """),
            {"run_id": run_id, "rule": rule_version, "record_id": record_id},
        ).scalar_one_or_none()
        if evidence_id is None:
            raise ReviewNotFound("review_not_found")
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"retrovault:review:{run_id}:{rule_version}:{evidence_id}"},
        )
        connection.execute(
            text("""
                SELECT 1 FROM data_governance.staging_records
                WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence_id
                FOR UPDATE
            """),
            {"run_id": run_id, "rule": rule_version, "evidence_id": evidence_id},
        ).one()
        return self._review_candidate(
            connection, run_id, record_id, rule_version, include_private_media
        )

    def correct_review(
        self,
        run_id: UUID,
        record_id: str,
        field: str,
        value: object,
        reason: str,
        expected_etag: str,
        rule_version: str = "editorial-v1",
        actor: str = "Eduardo",
    ) -> dict[str, object]:
        if (
            field not in EDITABLE_FIELDS
            or actor != "Eduardo"
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1000
            or contains_invalid_postgres_text(reason)
            or (
                field == "included_items"
                and (
                    not isinstance(value, list)
                    or len(value) > MAX_INCLUDED_ITEMS
                    or not all(isinstance(item, str) for item in value)
                    or sum(len(item) for item in value if isinstance(item, str))
                    > MAX_CORRECTION_TEXT
                    or contains_invalid_postgres_text(value)
                )
            )
            or (
                field != "included_items"
                and (
                    not isinstance(value, str)
                    or contains_invalid_postgres_text(value)
                )
            )
            or (
                field == "release_date_granularity"
                and value not in {"year", "month", "day"}
            )
            or (isinstance(value, str) and len(value) > MAX_CORRECTION_TEXT)
        ):
            raise InvalidCorrection("invalid_correction")
        with self.engine.begin() as connection:
            evidence_id = connection.execute(
                text("""
                    SELECT evidence_id FROM data_governance.staging_records
                    WHERE run_id=:run_id AND rule_version=:rule AND source_record_id=:record_id
                """),
                {"run_id": run_id, "rule": rule_version, "record_id": record_id},
            ).scalar_one_or_none()
            if evidence_id is None:
                raise ReviewNotFound("review_not_found")
            current = self.lock_review_candidate(
                connection, run_id, record_id, rule_version
            )
            if current["etag"] != expected_etag:
                raise ReviewConflict("etag_conflict")
            revision = connection.execute(
                text("""
                    SELECT COALESCE(max(revision), 0) + 1
                    FROM data_governance.review_corrections
                    WHERE run_id=:run_id AND rule_version=:rule AND evidence_id=:evidence_id
                """),
                {"run_id": run_id, "rule": rule_version, "evidence_id": evidence_id},
            ).scalar_one()
            correction_id = uuid4()
            current_values = current["values"]
            current_corrections = current["corrections"]
            assert isinstance(current_values, dict)
            assert isinstance(current_corrections, list)
            updated_values = dict(current_values)
            updated_values[field] = value
            corrections = cast(list[dict[str, Any]], current_corrections)
            correction_ids = [str(item["id"]) for item in corrections] + [str(correction_id)]
            resulting_etag = self._review_etag(
                run_id, rule_version, evidence_id, updated_values, correction_ids,
                cast(list[str], current["rights_decision_ids"]),
            )
            connection.execute(
                text("""
                    INSERT INTO data_governance.review_corrections
                    (id, run_id, rule_version, evidence_id, revision, field, value, actor,
                     previous_value, reason, previous_etag, resulting_etag, corrected_at)
                    VALUES (:id, :run_id, :rule, :evidence_id, :revision, :field,
                            CAST(:value AS jsonb), :actor, CAST(:previous_value AS jsonb), :reason, :previous_etag,
                            :resulting_etag, :corrected_at)
                """),
                {
                    "id": correction_id,
                    "run_id": run_id,
                    "rule": rule_version,
                    "evidence_id": evidence_id,
                    "revision": revision,
                    "field": field,
                    "value": json.dumps(value, ensure_ascii=False),
                    "previous_value": json.dumps(current_values.get(field), ensure_ascii=False),
                    "actor": actor,
                    "reason": reason.strip(),
                    "previous_etag": expected_etag,
                    "resulting_etag": resulting_etag,
                    "corrected_at": datetime.now(UTC),
                },
            )
            return self._review_candidate(connection, run_id, record_id, rule_version)
