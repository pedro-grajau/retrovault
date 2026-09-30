"""Leitura confinada a arquivos locais; não copia nem busca mídia."""

import hashlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from app.modules.data_governance.domain.models import (
    Manifest,
    PackageSnapshot,
    RecordReference,
    SnapshotRecord,
    strict_json_loads,
)
from app.modules.data_governance.ports.source import PackageError


class LocalPackage:
    def __init__(self, root: Path, max_record_bytes: int = 262_144) -> None:
        self.root = root.resolve(strict=True)
        self.max_record_bytes = max_record_bytes

    def _read(self, filename: str) -> bytes:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            raise PackageError("invalid_file_name")
        try:
            descriptor = os.open(self.root / filename, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as file:
                payload = file.read(self.max_record_bytes + 1)
        except OSError as exc:
            raise PackageError("record_unavailable") from exc
        if len(payload) > self.max_record_bytes:
            raise PackageError("record_too_large")
        return payload

    def _manifest(self, raw: bytes) -> Manifest:
        try:
            data = strict_json_loads(raw)
            if not isinstance(data, dict) or set(data) != {"source", "version", "captured_at", "actor", "records"}:
                raise ValueError()
            source, version, actor = (
                data[key] for key in ("source", "version", "actor")
            )
            if not all(
                isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
                for value in (source, version, actor)
            ):
                raise ValueError()
            captured_at = datetime.fromisoformat(
                data["captured_at"].replace("Z", "+00:00")
            )
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValueError()
            references = tuple(
                RecordReference(item["id"], item["file"]) for item in data["records"]
            )
            if any(not isinstance(item, dict) or set(item) != {"id", "file"} for item in data["records"]):
                raise ValueError()
            if (
                not references
                or len(references) > 1000
                or len({item.record_id for item in references}) != len(references)
            ):
                raise ValueError()
            if any(
                not isinstance(item.record_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", item.record_id)
                for item in references
            ):
                raise ValueError()
            return Manifest(
                source, version, captured_at.astimezone(UTC), actor, references
            )
        except (
            OSError,
            UnicodeError,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise PackageError("invalid_manifest") from exc

    def snapshot(self) -> PackageSnapshot:
        manifest_bytes = self._read("manifest.json")
        manifest = self._manifest(manifest_bytes)
        digest = hashlib.sha256()

        def add(part: bytes) -> None:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)

        add(manifest_bytes)
        records: list[SnapshotRecord] = []
        for ref in manifest.records:
            add(ref.record_id.encode())
            add(ref.file.encode())
            try:
                payload = self._read(ref.file)
                add(payload)
                records.append(SnapshotRecord(payload))
            except PackageError as exc:
                # A referência ausente também participa da identidade do pacote.
                code = str(exc.args[0])
                add(code.encode())
                records.append(SnapshotRecord(None, code))
        return PackageSnapshot(manifest, digest.hexdigest(), tuple(records))
