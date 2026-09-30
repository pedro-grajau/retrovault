"""Persistência privada com transação independente por registro."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from app.modules.data_governance.domain.models import CatalogRecord, Manifest, Right
from app.modules.data_governance.domain.normalization import Candidate


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
                        "SELECT id, package_hash, state FROM data_governance.ingest_runs WHERE source=:source AND source_version=:version"
                    ),
                    {"source": source, "version": version},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

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

    def create_run(
        self,
        run_id: UUID,
        manifest: Manifest,
        package_hash: str,
        config_hash: str,
        app_version: str,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO data_governance.ingest_runs
                (id, source, source_version, captured_at, started_at, state, package_hash, config_fingerprint, app_version, received)
                VALUES (:id, :source, :version, :captured_at, :started_at, 'running', :package_hash, :config_hash, :app_version, :received)
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
    ) -> None:
        with self.engine.begin() as connection:
            inserted = connection.execute(
                text("""
                INSERT INTO data_governance.raw_evidence
                (id, source, source_record_id, payload_hash, raw_payload, captured_at, preserved_at)
                VALUES (:id, :source, :record_id, :hash, :payload, :captured_at, :preserved_at)
                ON CONFLICT (source, source_record_id, payload_hash) DO NOTHING
                RETURNING id
            """),
                {
                    "id": evidence_id,
                    "source": manifest.source,
                    "record_id": record.record_id,
                    "hash": payload_hash,
                    "payload": record.raw_payload,
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
                    SELECT mr.role, mr.storage_right, mr.publication_right,
                           mr.attribution, pm.content IS NOT NULL AS content_available,
                           pm.content
                    FROM data_governance.media_rights mr
                    LEFT JOIN data_governance.private_media pm
                      ON pm.run_id=:run_id AND pm.evidence_id=mr.evidence_id AND pm.media_path=mr.media_path
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
            for row in rows:
                for field_name in row["optional_missing"]:
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
