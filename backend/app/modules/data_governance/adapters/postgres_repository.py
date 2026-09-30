"""Persistência privada com transação independente por registro."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import Engine, text

from app.modules.data_governance.domain.models import CatalogRecord, Manifest


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
                text("SELECT source_record_id FROM data_governance.ingest_failures WHERE run_id = :id"),
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
                        (evidence_id, media_path, storage_right, publication_right, attribution, eligible)
                        VALUES (:evidence_id, :path, :storage, :publication, :attribution, :eligible)
                    """),
                        {
                            "evidence_id": evidence_id,
                            "path": media.path,
                            "storage": media.storage.value,
                            "publication": media.publication.value,
                            "attribution": media.attribution,
                            "eligible": media.eligible,
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
                result["preserved"] = sum(value == "preserved" for value in outcomes.values())
                result["rejected"] = sum(value == "rejected" for value in outcomes.values())
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
