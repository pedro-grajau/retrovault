from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.application.ingest import ingest
from app.modules.data_governance.domain.models import (
    CatalogRecord,
    Manifest,
    PackageSnapshot,
    RecordReference,
    SnapshotRecord,
    SourceMetric,
    SourceResponse,
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
        self,
        run_id,
        manifest,
        package_hash,
        config_hash,
        app_version,
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
        source_responses=(),
    ):
        self.preserved = {
            "run_id": run_id,
            "raw_payload": record.raw_payload.encode(),
            "source_payload": source_payload,
            "payload_hash": payload_hash,
            "source_responses": source_responses,
            "source_metrics": record.source_metrics,
        }

    def fail(self, run_id, record_id, code, correlation_id, *, cause=None):
        raise AssertionError(f"fixture should not fail: {code}")

    def finish(self, run_id, received, preserved, rejected):
        self.finished = (received, preserved, rejected)

    def summary(self, run_id):
        return {"id": run_id, "state": "completed"}


class RejectionRecordingRepository(RecordingRepository):
    def fail(self, run_id, record_id, code, correlation_id, *, cause=None):
        self.failure = (run_id, record_id, code, correlation_id, cause)


def test_deeply_nested_local_record_is_rejected_without_aborting_other_records(
    tmp_path,
) -> None:
    package = tmp_path / "deep-package"
    package.mkdir()
    (package / "manifest.json").write_text(
        '{"source":"local-test","version":"deep-v1",'
        '"captured_at":"2026-09-30T12:00:00Z","actor":"Eduardo",'
        '"records":[{"id":"deep","file":"deep.json"},'
        '{"id":"valid","file":"valid.json"}]}'
    )
    (package / "deep.json").write_text(
        '{"id":"deep","attributes":{"nested":' + "[" * 1200 + "0" + "]" * 1200 + "}}"
    )
    valid_payload = b'{"id":"valid","attributes":{"title":"Jogo","platform":"SNES"}}'
    (package / "valid.json").write_bytes(valid_payload)
    repository = RejectionRecordingRepository()

    ingest(LocalPackage(package), repository, "test-version")

    assert repository.failure[1:3] == ("deep", "record_rejected")
    assert repository.failure[4] == "invalid_record"
    assert repository.preserved["raw_payload"] == valid_payload
    assert repository.finished == (2, 1, 1)


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
    mapped_payload = (
        b'{"id":"42","attributes":{"title":"Jogo","platform":"SNES"},"media":[]}'
    )
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


def test_ingest_uses_progression_capture_time_and_hashes_media_bytes() -> None:
    captured_at = datetime(2026, 9, 30, 13, 14, tzinfo=UTC)
    game_payload = b'{"ID":42,"Title":"Jogo","ConsoleID":3}'
    progression_payload = b'{"ID":42,"NumDistinctPlayers":5}'
    mapped_payload = (
        b'{"id":"42","attributes":{"title":"Jogo","platform":"SNES"},'
        b'"metrics":{"NumDistinctPlayers":5},"media":[]}'
    )
    manifest = Manifest(
        "retroachievements",
        "metrics-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    snapshot = PackageSnapshot(
        manifest,
        "p" * 64,
        (
            SnapshotRecord(
                payload=mapped_payload,
                source_payload=game_payload,
                media_bytes=(("/Images/cover.png", b"image-bytes"),),
                source_responses=(
                    SourceResponse("API_GetGame.php", game_payload, captured_at),
                    SourceResponse(
                        "API_GetGameProgression.php",
                        progression_payload,
                        captured_at,
                    ),
                ),
            ),
        ),
        "c" * 64,
    )
    repository = RecordingRepository()

    ingest(SourceWithSeparatePayloads(snapshot), repository, "test-version")

    assert repository.preserved is not None
    assert repository.preserved["payload_hash"] != sha256(game_payload).hexdigest()
    assert repository.preserved["source_metrics"][0].captured_at == captured_at


def test_local_package_cannot_inject_retroachievements_metrics() -> None:
    mapped_payload = (
        b'{"id":"42","attributes":{"title":"Jogo"},'
        b'"metrics":{"NumDistinctPlayers":5},"media":[]}'
    )
    manifest = Manifest(
        "local-test",
        "injected-metric-v1",
        datetime(2026, 9, 30, tzinfo=UTC),
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    snapshot = PackageSnapshot(
        manifest,
        "p" * 64,
        (SnapshotRecord(payload=mapped_payload),),
        "c" * 64,
    )
    repository = RejectionRecordingRepository()

    ingest(SourceWithSeparatePayloads(snapshot), repository, "test-version")

    assert repository.preserved is None
    assert repository.finished == (1, 0, 1)
    assert repository.failure[1:3] == ("42", "record_rejected")


def test_retroachievements_metrics_must_match_progression_response() -> None:
    captured_at = datetime(2026, 9, 30, 13, 14, tzinfo=UTC)
    game_payload = b'{"ID":42,"Title":"Jogo","ConsoleID":3}'
    progression_payload = b'{"ID":42,"NumDistinctPlayers":7}'
    mapped_payload = (
        b'{"id":"42","attributes":{"title":"Jogo","platform":"SNES"},'
        b'"metrics":{"NumDistinctPlayers":5},"media":[]}'
    )
    manifest = Manifest(
        "retroachievements",
        "mismatched-metric-v1",
        captured_at,
        "Eduardo",
        (RecordReference("42", "42.json"),),
    )
    snapshot = PackageSnapshot(
        manifest,
        "p" * 64,
        (
            SnapshotRecord(
                payload=mapped_payload,
                source_payload=game_payload,
                source_responses=(
                    SourceResponse("API_GetGame.php", game_payload, captured_at),
                    SourceResponse(
                        "API_GetGameProgression.php", progression_payload, captured_at
                    ),
                ),
            ),
        ),
        "c" * 64,
    )
    repository = RejectionRecordingRepository()

    ingest(SourceWithSeparatePayloads(snapshot), repository, "test-version")

    assert repository.preserved is None
    assert repository.finished == (1, 0, 1)
    assert repository.failure[1:3] == ("42", "record_rejected")


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
        self.endpoint_response = None
        self.metric = None

    def execute(self, statement, parameters=None):
        normalized = " ".join(str(statement).split()).lower()
        if "insert into data_governance.raw_evidence" in normalized:
            self.raw_evidence = (normalized, dict(parameters or {}))
            return PreserveResult(self.evidence_id)
        if "insert into data_governance.source_endpoint_responses" in normalized:
            self.endpoint_response = (normalized, dict(parameters or {}))
            return PreserveResult((parameters or {})["payload_hash"])
        if "insert into data_governance.source_metrics" in normalized:
            self.metric = (normalized, dict(parameters or {}))
            return PreserveResult()
        return PreserveResult()


class PreserveEngine:
    def __init__(self, evidence_id) -> None:
        self.connection = PreserveConnection(evidence_id)

    @contextmanager
    def begin(self):
        yield self.connection


def test_postgres_repository_binds_raw_source_responses_and_metrics() -> None:
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
    source_response = SourceResponse(
        "API_GetGameProgression.php",
        b'{"ID":42,"NumDistinctPlayers":5}',
        datetime(2026, 9, 30, tzinfo=UTC),
    )
    metric = SourceMetric(
        "NumDistinctPlayers",
        5,
        "API_GetGameProgression.php",
        datetime(2026, 9, 30, tzinfo=UTC),
    )

    repository.preserve(
        run_id,
        evidence_id,
        manifest,
        CatalogRecord("42", mapped_payload, (), (), (metric,)),
        sha256(source_payload).hexdigest(),
        source_payload=source_payload,
        source_responses=(source_response,),
    )

    assert engine.connection.raw_evidence is not None
    assert engine.connection.endpoint_response is not None
    assert (
        engine.connection.endpoint_response[1]["endpoint"]
        == "API_GetGameProgression.php"
    )
    assert (
        engine.connection.endpoint_response[1]["payload_bytes"]
        == source_response.payload
    )
    assert engine.connection.metric is not None
    assert engine.connection.metric[1]["metric_name"] == "NumDistinctPlayers"
    assert engine.connection.metric[1]["metric_value"] == "5"

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
