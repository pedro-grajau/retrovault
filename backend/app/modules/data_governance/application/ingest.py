"""Caso de uso: recebimento e preservação de evidência sem normalização."""

import hashlib
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.modules.data_governance.domain.models import (
    CatalogRecord,
    MediaRights,
    PackageSnapshot,
    Right,
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


def parse_record(record_id: str, raw: str) -> CatalogRecord:
    try:
        data = strict_json_loads(raw)
        if not isinstance(data, dict) or data.get("id") != record_id:
            raise ValueError()
        attributes = data.get("attributes", {})
        if not isinstance(attributes, dict) or set(attributes) - ALLOWED_ATTRIBUTES:
            raise ValueError()
        if set(data) - {"id", "attributes", "media"} or set(data) & FORBIDDEN_FIELDS:
            raise ValueError()
        if any(
            (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            )
            if key == "included_items"
            else (
                not isinstance(value, str)
                or value not in {"year", "month", "day"}
            )
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
        return CatalogRecord(record_id, raw, tuple(sorted(attributes)), media)
    except (KeyError, TypeError, ValueError) as exc:
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
        config_hash = snapshot.config_fingerprint or hashlib.sha256(
            f"local-package-v1;max-record-bytes={source.max_record_bytes}".encode()
        ).hexdigest()
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
            try:
                raw = payload.decode("utf-8")
            except UnicodeError as exc:
                raise InvalidRecord("invalid_encoding") from exc
            record = parse_record(reference.record_id, raw)
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
