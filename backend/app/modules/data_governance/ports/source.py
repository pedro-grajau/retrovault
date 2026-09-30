"""Porta para um pacote local versionado."""

from typing import Protocol

from app.modules.data_governance.domain.models import PackageSnapshot


class PackageError(ValueError):
    pass


class LocalCatalogSource(Protocol):
    max_record_bytes: int

    def snapshot(self) -> PackageSnapshot: ...
