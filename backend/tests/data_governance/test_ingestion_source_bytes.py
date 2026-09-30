from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.application.ingest import ingest
from app.modules.data_governance.domain.models import (
    CatalogRecord,
    Manifest,
    PackageSnapshot,
    RecordReference,
    SnapshotRecord,
)


class SourceWithSeparatePayloads:
    max_record_bytes = 10_000

    def __init__(self, snapshot: PackageSnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> PackageSnapshot:
        return self._snapshot


class RecordingRepository:
    def __init__(self) -> None:
        self.run_id = None
        self.preserved = None

    def guard(self, source, version):
        return nullcontext()

    def existing_run(self, source, version):
        return None

    def set_manifest_hash(self, run_id, manifest_hash):
        self.manifest_hash = manifest_hash

    def outcomes(self, run_id):
        return {}

    def create_run(
        self, run_id, manifest, package_hash, config_hash, app_version,
        manifest_hash=None,
    ):
        self.run_id = run_id

    def preserve(
        self,
        run_id,
        evidence_id,
        manifest,
        record,
        payload_hash,
        media_bytes,
        *,
        source_payload=None,
    ):
        self.preserved = {
            "run_id": run_id,
            "raw_payload": record.raw_payload.encode(),
            "source_payload": source_payload,
            "payload_hash": payload_hash,
        }

    def fail(self, run_id, record_id, code, correlation_id):
        raise AssertionError(f"fixture should not fail: {code}")

    def finish(self, run_id, received, preserved, rejected):
        self.finished = (received, preserved, rejected)

    def summary(self, run_id):
        return {"id": run_id, "state": "completed"}


def test_ingest_hashes_and_persists_the_original_source_bytes() -> None:
    mapped_payload = (
        '{"id":"42","attributes":{"title":"Título mapeado",'
        '"platform":"SNES"},"media":[]}'
    ).encode()
    source_payload = b'{"ID":42,"Title":"Original RA","ConsoleID":3}'
    assert mapped_payload != source_payload
    manifest = Manifest(
        "retroachievements",
        "fixture-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    snapshot = PackageSnapshot(
        manifest,
        "p" * 64,
        (SnapshotRecord(payload=mapped_payload, source_payload=source_payload),),
        "c" * 64,
    )
    repository = RecordingRepository()

    ingest(SourceWithSeparatePayloads(snapshot), repository, "test-version")

    assert repository.preserved is not None
    assert repository.preserved["raw_payload"] == mapped_payload
    assert repository.preserved["source_payload"] == source_payload
    assert repository.preserved["payload_hash"] == sha256(source_payload).hexdigest()
    assert repository.preserved["run_id"] == uuid5(
        NAMESPACE_URL, "retrovault:ingest:retroachievements:fixture-v1"
    )
    assert repository.finished == (1, 1, 0)


def test_ingest_preserves_an_explicitly_empty_source_payload() -> None:
    mapped_payload = b'{"id":"42","attributes":{"title":"Jogo","platform":"SNES"},"media":[]}'
    manifest = Manifest(
        "retroachievements",
        "empty-source-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    snapshot = PackageSnapshot(
        manifest,
        "p" * 64,
        (SnapshotRecord(payload=mapped_payload, source_payload=b""),),
        "c" * 64,
    )
    repository = RecordingRepository()

    ingest(SourceWithSeparatePayloads(snapshot), repository, "test-version")

    assert repository.preserved is not None
    assert repository.preserved["source_payload"] == b""
    assert repository.preserved["payload_hash"] == sha256(b"").hexdigest()


class CachedSource:
    max_record_bytes = 10_000

    def __init__(self, manifest: Manifest, manifest_hash: str) -> None:
        self.manifest = manifest
        self.manifest_hash = manifest_hash
        self.snapshot_calls = 0

    def manifest_identity(self):
        return self.manifest, self.manifest_hash

    def snapshot(self):
        self.snapshot_calls += 1
        raise AssertionError("completed manifest must be returned from cache")


class CachedRepository(RecordingRepository):
    def __init__(self, run_id, manifest_hash):
        super().__init__()
        self.run_id = run_id
        self.manifest_hash = manifest_hash

    def existing_run(self, source, version):
        return {
            "id": self.run_id,
            "state": "completed",
            "package_hash": "stored-package-hash",
            "manifest_hash": self.manifest_hash,
        }

    def summary(self, run_id):
        return {"id": run_id, "state": "completed"}


def test_completed_retroachievements_manifest_skips_provider_snapshot() -> None:
    manifest = Manifest(
        "retroachievements",
        "cached-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    manifest_hash = "m" * 64
    source = CachedSource(manifest, manifest_hash)
    run_id = uuid4()
    repository = CachedRepository(run_id, manifest_hash)

    result = ingest(source, repository, "test-version")

    assert result == {"id": run_id, "state": "completed"}
    assert source.snapshot_calls == 0


class PreserveResult:
    def __init__(self, evidence_id=None) -> None:
        self.evidence_id = evidence_id

    def scalar_one_or_none(self):
        return self.evidence_id


class PreserveConnection:
    def __init__(self, evidence_id) -> None:
        self.evidence_id = evidence_id
        self.raw_evidence = None

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split()).lower()
        if "insert into data_governance.raw_evidence" in normalized:
            self.raw_evidence = (normalized, dict(parameters or {}))
            return PreserveResult(self.evidence_id)
        return PreserveResult()


class PreserveEngine:
    def __init__(self, evidence_id) -> None:
        self.connection = PreserveConnection(evidence_id)

    @contextmanager
    def begin(self):
        yield self.connection


def test_postgres_repository_binds_original_source_bytes_for_raw_evidence() -> None:
    mapped_payload = '{"id":"42","attributes":{"title":"Título mapeado"},"media":[]}'
    source_payload = b'{"ID":42,"Title":"Original RA","ConsoleID":3}'
    evidence_id, run_id = uuid4(), uuid4()
    manifest = Manifest(
        "retroachievements",
        "fixture-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    engine = PreserveEngine(evidence_id)
    repository = PostgresRepository(engine)

    repository.preserve(
        run_id,
        evidence_id,
        manifest,
        CatalogRecord("42", mapped_payload, (), ()),
        sha256(source_payload).hexdigest(),
        source_payload=source_payload,
    )

    assert engine.connection.raw_evidence is not None
    query, parameters = engine.connection.raw_evidence
    assert "raw_payload_bytes" in query
    assert parameters["payload"] == mapped_payload
    assert parameters["payload_bytes"] == source_payload
    assert parameters["hash"] == sha256(source_payload).hexdigest()


def test_postgres_repository_does_not_replace_empty_original_bytes() -> None:
    mapped_payload = '{"id":"42","attributes":{"title":"Título mapeado"},"media":[]}'
    evidence_id, run_id = uuid4(), uuid4()
    manifest = Manifest(
        "retroachievements",
        "empty-source-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    engine = PreserveEngine(evidence_id)
    repository = PostgresRepository(engine)

    repository.preserve(
        run_id,
        evidence_id,
        manifest,
        CatalogRecord("42", mapped_payload, (), ()),
        sha256(b"").hexdigest(),
        source_payload=b"",
    )

    assert engine.connection.raw_evidence is not None
    _, parameters = engine.connection.raw_evidence
    assert parameters["payload_bytes"] == b""
