"""Contrato da persistência transacional da ingestão."""

from contextlib import AbstractContextManager
from typing import Protocol
from uuid import UUID

from app.modules.data_governance.domain.models import CatalogRecord, Manifest


class IngestionRepository(Protocol):
    def guard(self, source: str, version: str) -> AbstractContextManager[None]: ...

    def existing_run(self, source: str, version: str) -> dict[str, object] | None: ...

    def outcomes(self, run_id: UUID) -> dict[str, str]: ...

    def create_run(
        self,
        run_id: UUID,
        manifest: Manifest,
        package_hash: str,
        config_hash: str,
        app_version: str,
    ) -> None: ...

    def preserve(
        self,
        run_id: UUID,
        evidence_id: UUID,
        manifest: Manifest,
        record: CatalogRecord,
        payload_hash: str,
    ) -> None: ...

    def fail(
        self, run_id: UUID, record_id: str, code: str, correlation_id: UUID
    ) -> None: ...

    def finish(
        self, run_id: UUID, received: int, preserved: int, rejected: int
    ) -> None: ...

    def summary(self, run_id: UUID) -> dict[str, object] | None: ...
