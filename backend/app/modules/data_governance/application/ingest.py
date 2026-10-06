"""Caso de uso: recebimento e preservação de evidência sem normalização."""

import hashlib
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.modules.data_governance.domain.models import (
    PROGRESSION_METRIC_FIELDS,
    CatalogRecord,
    MediaRights,
    PackageSnapshot,
    Right,
    SourceMetric,
    contains_invalid_postgres_text,
    strict_json_loads,
)
from app.modules.data_governance.ports.repository import IngestionRepository
from app.modules.data_governance.ports.source import LocalCatalogSource, PackageError


class VersionConflict(ValueError):
    pass


class InvalidRecord(ValueError):
    pass


ALLOWED_ATTRIBUTES = {
    "title",
    "platform",
    "region",
    "edition",
    "genre",
    "developer",
    "publisher",
    "year",
    "rating",
    "description",
    "included_items",
    "release_date",
    "release_date_granularity",
}
FORBIDDEN_FIELDS = {"price", "stock", "sku", "preco", "estoque"}


def parse_record(
    record_id: str, raw: str, *, metric_captured_at: datetime | None = None
) -> CatalogRecord:
    try:
        data = strict_json_loads(raw)
        if contains_invalid_postgres_text(data):
            raise ValueError()
        if not isinstance(data, dict) or data.get("id") != record_id:
            raise ValueError()
        attributes = data.get("attributes", {})
        if not isinstance(attributes, dict) or set(attributes) - ALLOWED_ATTRIBUTES:
            raise ValueError()
        if (
            set(data) - {"id", "attributes", "media", "metrics"}
            or set(data) & FORBIDDEN_FIELDS
        ):
            raise ValueError()
        if any(
            (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            )
            if key == "included_items"
            else (not isinstance(value, str) or value not in {"year", "month", "day"})
            if key == "release_date_granularity"
            else not isinstance(value, str)
            for key, value in attributes.items()
        ):
            raise ValueError()
        media_data = data.get("media", [])
        if not isinstance(media_data, list):
            raise ValueError()
        if any(
            not isinstance(item, dict)
            or "path" not in item
            or set(item)
            - {"path", "role", "storage_right", "publication_right", "attribution"}
            for item in media_data
        ):
            raise ValueError()
        media = tuple(
            MediaRights(
                item["path"],
                item.get("role"),
                Right(item.get("storage_right", "unknown")),
                Right(item.get("publication_right", "unknown")),
                item.get("attribution", ""),
            )
            for item in media_data
        )
        if any(
            not isinstance(item.path, str)
            or not item.path
            or not isinstance(item.attribution, str)
            or item.role not in (None, "box_art")
            for item in media
        ):
            raise ValueError()
        if len({item.path for item in media}) != len(media):
            raise ValueError()
        metrics_data = data.get("metrics", {})
        if not isinstance(metrics_data, dict):
            raise ValueError()
        captured_at = metric_captured_at or datetime.now(UTC)
        metrics_list: list[SourceMetric] = []
        for name, value in metrics_data.items():
            if not isinstance(name, str) or name not in PROGRESSION_METRIC_FIELDS:
                raise ValueError()
            if type(value) is not int or value < 0:
                raise ValueError()
            metrics_list.append(
                SourceMetric(
                    name=name,
                    value=value,
                    endpoint="API_GetGameProgression.php",
                    captured_at=captured_at,
                )
            )
        metrics = tuple(sorted(metrics_list, key=lambda metric: metric.name))
        return CatalogRecord(record_id, raw, tuple(sorted(attributes)), media, metrics)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidRecord("invalid_record") from exc


def ingest(
    source: LocalCatalogSource, repository: IngestionRepository, app_version: str
) -> dict[str, object]:
    manifest_identity = getattr(source, "manifest_identity", None)
    if callable(manifest_identity):
        manifest, manifest_hash = manifest_identity()
        with repository.guard(manifest.source, manifest.version):
            existing = repository.existing_run(manifest.source, manifest.version)
            if (
                existing
                and existing["state"] == "completed"
                and existing.get("manifest_hash") == manifest_hash
            ):
                result = repository.summary(UUID(str(existing["id"])))
                assert result is not None
                return result
    snapshot = source.snapshot()
    with repository.guard(snapshot.manifest.source, snapshot.manifest.version):
        return _ingest_snapshot(source, repository, app_version, snapshot)


def _ingest_snapshot(
    source: LocalCatalogSource,
    repository: IngestionRepository,
    app_version: str,
    snapshot: PackageSnapshot,
) -> dict[str, object]:
    manifest = snapshot.manifest
    package_hash = snapshot.package_hash
    existing = repository.existing_run(manifest.source, manifest.version)
    if existing:
        if existing["package_hash"] != package_hash:
            raise VersionConflict("source_version_conflict")
        run_id = UUID(str(existing["id"]))
        if existing["state"] == "completed":
            if existing.get("manifest_hash") is None and snapshot.manifest_hash:
                repository.set_manifest_hash(run_id, snapshot.manifest_hash)
            result = repository.summary(run_id)
            assert result is not None
            return result
    else:
        run_id = uuid5(
            NAMESPACE_URL, f"retrovault:ingest:{manifest.source}:{manifest.version}"
        )
        config_hash = (
            snapshot.config_fingerprint
            or hashlib.sha256(
                f"local-package-v1;max-record-bytes={source.max_record_bytes}".encode()
            ).hexdigest()
        )
        repository.create_run(
            run_id,
            manifest,
            package_hash,
            config_hash,
            app_version,
            snapshot.manifest_hash,
        )
    outcomes = repository.outcomes(run_id)
    preserved = sum(outcome == "preserved" for outcome in outcomes.values())
    rejected = sum(outcome == "rejected" for outcome in outcomes.values())
    for reference, snapshot_record in zip(
        manifest.records, snapshot.records, strict=True
    ):
        if reference.record_id in outcomes:
            continue
        try:
            if snapshot_record.payload is None:
                raise PackageError(snapshot_record.error_code or "record_unavailable")
            payload = snapshot_record.payload
            source_payload = (
                payload
                if snapshot_record.source_payload is None
                else snapshot_record.source_payload
            )
            source_responses = snapshot_record.source_responses
            try:
                raw = payload.decode("utf-8")
            except UnicodeError as exc:
                raise InvalidRecord("invalid_encoding") from exc
            metric_captured_at = next(
                (
                    response.captured_at
                    for response in source_responses
                    if response.endpoint == "API_GetGameProgression.php"
                ),
                manifest.captured_at,
            )
            record = parse_record(
                reference.record_id, raw, metric_captured_at=metric_captured_at
            )
            if record.source_metrics:
                progression_responses = [
                    response
                    for response in source_responses
                    if response.endpoint == "API_GetGameProgression.php"
                ]
                if (
                    manifest.source != "retroachievements"
                    or len(progression_responses) != 1
                ):
                    raise InvalidRecord("metric_provenance_missing")
                try:
                    progression = strict_json_loads(progression_responses[0].payload)
                except (UnicodeError, ValueError, RecursionError) as exc:
                    raise InvalidRecord("metric_provenance_invalid") from exc
                if (
                    not isinstance(progression, dict)
                    or type(progression.get("ID")) is not int
                    or str(progression["ID"]) != reference.record_id
                ):
                    raise InvalidRecord("metric_provenance_invalid")
                source_metrics = {
                    name: progression[name]
                    for name in PROGRESSION_METRIC_FIELDS
                    if name in progression and progression[name] is not None
                }
                if any(
                    type(value) is not int or value < 0
                    for value in source_metrics.values()
                ):
                    raise InvalidRecord("metric_provenance_invalid")
                record_metrics = {
                    metric.name: metric.value for metric in record.source_metrics
                }
                if source_metrics != record_metrics:
                    raise InvalidRecord("metric_provenance_conflict")
            if source_responses:
                payload_digest = hashlib.sha256()
                for response in sorted(
                    source_responses, key=lambda item: item.endpoint
                ):
                    endpoint_bytes = response.endpoint.encode("utf-8")
                    payload_digest.update(len(endpoint_bytes).to_bytes(4, "big"))
                    payload_digest.update(endpoint_bytes)
                    payload_digest.update(len(response.payload).to_bytes(8, "big"))
                    payload_digest.update(response.payload)
                for media_path, media_payload in sorted(snapshot_record.media_bytes):
                    if media_payload is None:
                        continue
                    path_bytes = media_path.encode("utf-8")
                    payload_digest.update(b"media\0")
                    payload_digest.update(len(path_bytes).to_bytes(4, "big"))
                    payload_digest.update(path_bytes)
                    payload_digest.update(len(media_payload).to_bytes(8, "big"))
                    payload_digest.update(media_payload)
                payload_hash = payload_digest.hexdigest()
            else:
                payload_hash = hashlib.sha256(source_payload).hexdigest()
            evidence_id = uuid5(
                NAMESPACE_URL,
                f"retrovault:evidence:{manifest.source}:{reference.record_id}:{payload_hash}",
            )
            repository.preserve(
                run_id,
                evidence_id,
                manifest,
                record,
                payload_hash,
                dict(snapshot_record.media_bytes),
                source_payload=source_payload,
                source_responses=source_responses,
            )
            preserved += 1
        except (PackageError, InvalidRecord):
            repository.fail(run_id, reference.record_id, "record_rejected", uuid4())
            rejected += 1
        except Exception:
            repository.fail(run_id, reference.record_id, "storage_error", uuid4())
            rejected += 1
    repository.finish(run_id, len(manifest.records), preserved, rejected)
    result = repository.summary(run_id)
    assert result is not None
    return result
