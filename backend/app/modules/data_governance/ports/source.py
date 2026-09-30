"""Porta para um pacote local versionado."""

from typing import Protocol

from app.modules.data_governance.domain.models import PackageSnapshot


class PackageError(ValueError):
    def __init__(self, code: str, fingerprint: str | None = None) -> None:
        super().__init__(code)
        self.fingerprint = fingerprint


class LocalCatalogSource(Protocol):
    max_record_bytes: int

    def snapshot(self) -> PackageSnapshot: ...
