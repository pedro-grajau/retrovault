"""Tipos puros da evidência recebida pelo Data Governance."""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


def strict_json_loads(raw: str | bytes) -> Any:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique_object)


PROGRESSION_METRIC_FIELDS = frozenset(
    {
        "NumDistinctPlayers",
        "TimesUsedInBeatMedian",
        "TimesUsedInHardcoreBeatMedian",
        "MedianTimeToBeat",
        "MedianTimeToBeatHardcore",
        "TimesUsedInCompletionMedian",
        "TimesUsedInMasteryMedian",
        "MedianTimeToComplete",
        "MedianTimeToMaster",
        "NumAchievements",
    }
)


class Right(StrEnum):
    CONFIRMED = "confirmed"
    DENIED = "denied"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecordReference:
    record_id: str
    file: str


@dataclass(frozen=True)
class Manifest:
    source: str
    version: str
    captured_at: datetime
    actor: str
    records: tuple[RecordReference, ...]


@dataclass(frozen=True)
class MediaRights:
    path: str
    role: str | None
    storage: Right
    publication: Right
    attribution: str

    @property
    def eligible(self) -> bool:
        return self.storage is Right.CONFIRMED and self.publication is Right.CONFIRMED


@dataclass(frozen=True)
class CatalogRecord:
    record_id: str
    raw_payload: str
    attributes: tuple[str, ...]
    media: tuple[MediaRights, ...]
    source_metrics: tuple[SourceMetric, ...] = ()


@dataclass(frozen=True)
class SourceMetric:
    name: str
    value: int
    endpoint: str
    captured_at: datetime


@dataclass(frozen=True)
class SourceResponse:
    endpoint: str
    payload: bytes
    captured_at: datetime


@dataclass(frozen=True)
class SnapshotRecord:
    payload: bytes | None
    error_code: str | None = None
    error_fingerprint: str | None = None
    media_bytes: tuple[tuple[str, bytes | None], ...] = ()
    source_payload: bytes | None = None
    source_responses: tuple[SourceResponse, ...] = ()


@dataclass(frozen=True)
class PackageSnapshot:
    manifest: Manifest
    package_hash: str
    records: tuple[SnapshotRecord, ...]
    config_fingerprint: str = ""
    manifest_hash: str | None = None
