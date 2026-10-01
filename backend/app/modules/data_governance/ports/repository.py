"""Contrato da persistência transacional da ingestão."""

from contextlib import AbstractContextManager
from typing import Any, Protocol
from uuid import UUID

from app.modules.data_governance.domain.models import (
    CatalogRecord,
    Manifest,
    SourceResponse,
)


class IngestionRepository(Protocol):
    def guard(self, source: str, version: str) -> AbstractContextManager[None]: ...

    def existing_run(self, source: str, version: str) -> dict[str, object] | None: ...

    def set_manifest_hash(self, run_id: UUID, manifest_hash: str) -> None: ...

    def outcomes(self, run_id: UUID) -> dict[str, str]: ...

    def create_run(
        self,
        run_id: UUID,
        manifest: Manifest,
        package_hash: str,
        config_hash: str,
        app_version: str,
        manifest_hash: str | None = None,
    ) -> None: ...

    def preserve(
        self,
        run_id: UUID,
        evidence_id: UUID,
        manifest: Manifest,
        record: CatalogRecord,
        payload_hash: str,
        media_bytes: dict[str, bytes | None],
        *,
        source_payload: bytes | None = None,
        source_responses: tuple[SourceResponse, ...] = (),
    ) -> None: ...

    def fail(
        self, run_id: UUID, record_id: str, code: str, correlation_id: UUID
    ) -> None: ...

    def finish(
        self, run_id: UUID, received: int, preserved: int, rejected: int
    ) -> None: ...

    def summary(self, run_id: UUID) -> dict[str, object] | None: ...


class ProcessingRepository(Protocol):
    def processing_guard(
        self, run_id: UUID, rule_version: str
    ) -> AbstractContextManager[None]: ...
    def processing_inputs(self, run_id: UUID) -> list[dict[str, Any]] | None: ...

    def processing_peers(self, run_id: UUID, rule_version: str) -> list[Any]: ...
    def processing_summary(
        self, run_id: UUID, rule_version: str
    ) -> dict[str, object] | None: ...
    def save_processing(
        self, run_id: UUID, rule_version: str, fingerprint: str, candidates: list[Any]
    ) -> None: ...
