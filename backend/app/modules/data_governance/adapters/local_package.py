"""Leitura confinada a arquivos locais; não copia nem busca mídia."""

import hashlib
import os
import re
import stat
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

_RFC3339_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})"
)


class LocalPackage:
    def __init__(self, root: Path, max_record_bytes: int = 262_144) -> None:
        if max_record_bytes < 0:
            raise ValueError("max_record_bytes must be non-negative")
        self.root = root.resolve(strict=True)
        self.max_record_bytes = max_record_bytes

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory_fd = os.open(self.root.anchor, flags)
        try:
            for component in self.root.parts[1:]:
                next_fd = os.open(component, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            return directory_fd
        except OSError:
            os.close(directory_fd)
            raise

    def _read(self, directory_fd: int, filename: str) -> bytes:
        if not isinstance(filename, str):
            raise PackageError("invalid_file_name")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename):
            raise PackageError("invalid_file_name")
        try:
            descriptor = os.open(
                filename,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_NONBLOCK
                | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise PackageError("record_unavailable")
                payload = bytearray()
                content_hash = hashlib.sha256()
                total_bytes = 0
                too_large = False
                while chunk := file.read(64 * 1024):
                    content_hash.update(chunk)
                    total_bytes += len(chunk)
                    if not too_large and total_bytes <= self.max_record_bytes:
                        payload.extend(chunk)
                    else:
                        too_large = True
                        payload.clear()
        except OSError as exc:
            raise PackageError("record_unavailable") from exc
        if too_large:
            raise PackageError("record_too_large", content_hash.hexdigest())
        return bytes(payload)

    def _manifest(self, raw: bytes) -> Manifest:
        try:
            data = strict_json_loads(raw)
            if not isinstance(data, dict) or set(data) != {"source", "version", "captured_at", "actor", "records"}:
                raise ValueError()
            source, version, actor = (data[key] for key in ("source", "version", "actor"))
            if not all(
                isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value)
                for value in (source, version, actor)
            ):
                raise ValueError()
            if actor != "Eduardo":
                raise ValueError()
            captured_at_value = data["captured_at"]
            if not isinstance(captured_at_value, str) or not _RFC3339_DATETIME.fullmatch(
                captured_at_value
            ):
                raise ValueError()
            normalized_timestamp = captured_at_value.replace("t", "T", 1)
            if normalized_timestamp[-1] in "Zz":
                normalized_timestamp = normalized_timestamp[:-1] + "+00:00"
            captured_at = datetime.fromisoformat(normalized_timestamp)
            if captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValueError()
            raw_references = data["records"]
            if not isinstance(raw_references, list):
                raise ValueError()
            references_list: list[RecordReference] = []
            for item in raw_references:
                if not isinstance(item, dict) or set(item) != {"id", "file"}:
                    raise ValueError()
                record_id = item["id"]
                filename = item["file"]
                if (
                    not isinstance(record_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", record_id)
                    or not isinstance(filename, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]+\.json", filename)
                ):
                    raise ValueError()
                references_list.append(RecordReference(record_id, filename))
            references = tuple(references_list)
            if (
                not references
                or len(references) > 1000
                or len({item.record_id for item in references}) != len(references)
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
        try:
            directory_fd = self._open_root()
        except OSError as exc:
            raise PackageError("package_unavailable") from exc
        try:
            manifest_bytes = self._read(directory_fd, "manifest.json")
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
                    payload = self._read(directory_fd, ref.file)
                    add(payload)
                    records.append(SnapshotRecord(payload))
                except PackageError as exc:
                    # Falhas e o hash completo de registros grandes integram a identidade.
                    code = str(exc.args[0])
                    add(code.encode())
                    if exc.fingerprint is not None:
                        add(exc.fingerprint.encode())
                    records.append(
                        SnapshotRecord(None, code, exc.fingerprint)
                    )
            return PackageSnapshot(manifest, digest.hexdigest(), tuple(records))
        finally:
            os.close(directory_fd)
