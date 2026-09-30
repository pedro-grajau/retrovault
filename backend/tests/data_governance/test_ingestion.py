import json
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from PIL import Image
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import (
    InvalidCorrection,
    PostgresRepository,
    ReviewConflict,
)
from app.modules.data_governance.application.ingest import (
    InvalidRecord,
    VersionConflict,
    ingest,
    parse_record,
)
from app.modules.data_governance.application.process import process_run
from app.modules.data_governance.domain.models import PackageSnapshot
from app.platform.config.settings import settings


def _package(
    root: Path,
    source: str,
    version: str,
    *,
    bad: bool = False,
    title: str = "Jogo sintético",
) -> None:
    root.mkdir(exist_ok=True)
    manifest = {
        "source": source,
        "version": version,
        "captured_at": "2026-09-29T12:00:00Z",
        "actor": "Eduardo",
        "records": [
            {"id": "game", "file": "game.json"},
            {"id": "bad", "file": "bad.json"},
        ]
        if bad
        else [{"id": "game", "file": "game.json"}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "game.json").write_text(
        json.dumps(
            {
                "id": "game",
                "attributes": {"title": title, "platform": "SNES"},
                "media": [
                    {
                        "path": "cover",
                        "storage_right": "unknown",
                        "publication_right": "unknown",
                        "attribution": "Eduardo",
                    }
                ],
            }
        )
    )
    if bad:
        (root / "bad.json").write_text('{"id":"bad","attributes":{"price":99}}')


def test_record_validation_rejects_commerce_and_invalid_rights() -> None:
    with pytest.raises(InvalidRecord):
        parse_record("x", '{"id":"x","attributes":{"title":"X","sku":"Y"}}')
    with pytest.raises(InvalidRecord):
        parse_record(
            "x",
            '{"id":"x","attributes":{"title":"X"},"media":[{"path":"cover","storage_right":"maybe","publication_right":"unknown","attribution":""}]}',
        )
    with pytest.raises(InvalidRecord):
        parse_record("x", '{"id":"x","attributes":{"title":"ok","title":"outro"}}')


def test_incomplete_editorial_record_remains_raw_evidence() -> None:
    record = parse_record("x", '{"id":"x","attributes":{"platform":"SNES"}}')
    assert record.attributes == ("platform",)
    assert parse_record("x", '{"id":"x","attributes":{}}').attributes == ()
    assert parse_record("x", '{"id":"x"}').attributes == ()
    media_without_rights = parse_record("x", '{"id":"x","media":[{"path":"cover"}]}')
    assert not media_without_rights.media[0].eligible
    assert media_without_rights.media[0].storage.value == "unknown"
    assert media_without_rights.media[0].publication.value == "unknown"


def test_versioned_fixture_is_local_and_deterministic() -> None:
    root = Path(__file__).parents[3] / "fixtures" / "catalog"
    package = LocalPackage(root)
    snapshot = package.snapshot()
    assert snapshot.manifest.actor == "Eduardo"
    assert snapshot.package_hash == package.snapshot().package_hash
    assert len(snapshot.records) == 2


def test_manifest_accepts_lowercase_rfc3339_separators_and_normalizes_utc(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["captured_at"] = "2026-09-29t12:00:00z"
    manifest_path.write_text(json.dumps(manifest))

    snapshot = LocalPackage(package).snapshot()

    assert snapshot.manifest.captured_at.isoformat() == "2026-09-29T12:00:00+00:00"


def test_snapshot_stays_on_open_directory_when_root_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1", title="Conteúdo original")
    outside = tmp_path / "outside"
    _package(outside, "local-test", "1", title="Conteúdo externo")
    moved_package = tmp_path / "package-original"
    source = LocalPackage(package)
    parse_manifest = source._manifest

    def replace_root_after_manifest(raw: bytes):
        manifest = parse_manifest(raw)
        package.rename(moved_package)
        package.symlink_to(outside, target_is_directory=True)
        return manifest

    monkeypatch.setattr(source, "_manifest", replace_root_after_manifest)
    snapshot = source.snapshot()

    assert snapshot.records[0].payload is not None
    title = json.loads(snapshot.records[0].payload)["attributes"]["title"]
    assert title == "Conteúdo original"


def test_snapshot_rejects_parent_replaced_with_symlink_before_open(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    package = parent / "package"
    _package(package, "local-test", "1")
    outside = tmp_path / "outside"
    outside.mkdir()
    _package(outside / "package", "local-test", "1", title="Conteúdo externo")
    source = LocalPackage(package)

    parent.rename(tmp_path / "original-parent")
    parent.symlink_to(outside, target_is_directory=True)

    from app.modules.data_governance.ports.source import PackageError

    with pytest.raises(PackageError, match="package_unavailable"):
        source.snapshot()


def test_package_rejects_escaping_symlink_and_isolates_unreadable_record(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    outside = tmp_path / "outside.json"
    outside.write_text('{"id":"game","attributes":{"title":"fora"}}')
    (package / "game.json").unlink()
    (package / "game.json").symlink_to(outside)
    snapshot = LocalPackage(package).snapshot()
    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "record_unavailable"
    (package / "game.json").unlink()
    (package / "game.json").mkdir()
    snapshot = LocalPackage(package).snapshot()
    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "record_unavailable"


def test_fifo_reference_is_rejected_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    (package / "game.json").unlink()
    os.mkfifo(package / "game.json")
    real_open = os.open

    def assert_nonblocking_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "game.json":
            assert flags & os.O_NONBLOCK
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", assert_nonblocking_open)

    snapshot = LocalPackage(package).snapshot()

    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "record_unavailable"


def test_record_size_limit_is_applied_to_bytes_read(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    (package / "game.json").write_bytes(b"x" * 513)
    snapshot = LocalPackage(package, max_record_bytes=512).snapshot()
    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "record_too_large"
    assert snapshot.records[0].error_fingerprint == sha256(b"x" * 513).hexdigest()


def test_negative_record_size_limit_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_record_bytes"):
        LocalPackage(tmp_path, max_record_bytes=-1)


def test_oversized_contents_change_package_fingerprint(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "same-source", "same-version")
    source = LocalPackage(package, max_record_bytes=512)
    common_prefix = b"x" * 513
    first_content = common_prefix + b"a"
    second_content = common_prefix + b"b"

    (package / "game.json").write_bytes(first_content)
    first = source.snapshot()
    (package / "game.json").write_bytes(second_content)
    second = source.snapshot()

    assert first.records[0].error_fingerprint == sha256(first_content).hexdigest()
    assert second.records[0].error_fingerprint == sha256(second_content).hexdigest()
    assert first_content[:513] == second_content[:513]
    assert first_content[513:] != second_content[513:]
    assert first.package_hash != second.package_hash


def test_manifest_rejects_extra_and_duplicate_keys(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    manifest_path = package / "manifest.json"
    original = manifest_path.read_text()
    manifest_path.write_text(original.replace('"actor": "Eduardo"', '"actor": "Eduardo", "extra": 1'))
    from app.modules.data_governance.ports.source import PackageError

    with pytest.raises(PackageError):
        LocalPackage(package).snapshot()
    manifest_path.write_text(
        original.replace('"actor": "Eduardo"', '"actor": "Eduardo", "actor": "outro"')
    )
    with pytest.raises(PackageError):
        LocalPackage(package).snapshot()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "Outra pessoa"),
        ("captured_at", "2026-09-29 12:00:00Z"),
        ("captured_at", "2026-02-30T12:00:00Z"),
        ("file", 7),
        ("file", "../outside.json"),
    ],
)
def test_manifest_rejects_invalid_actor_and_file_before_opening_references(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    manifest_path = package / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if field == "actor":
        manifest["actor"] = value
    elif field == "captured_at":
        manifest["captured_at"] = value
    else:
        manifest["records"][0]["file"] = value
    manifest_path.write_text(json.dumps(manifest))
    source = LocalPackage(package)
    opened: list[str] = []
    read_file = source._read

    def record_open(directory_fd: int, filename: str) -> bytes:
        opened.append(filename)
        return read_file(directory_fd, filename)

    monkeypatch.setattr(source, "_read", record_open)
    from app.modules.data_governance.ports.source import PackageError

    with pytest.raises(PackageError):
        source.snapshot()
    assert opened == ["manifest.json"]

    from app.modules.data_governance.api.cli import main

    assert main(["ingest", str(package)]) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "invalid_package"}


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_persistence_idempotency_lineage_rights_conflict_and_partial_failure(
    tmp_path: Path,
) -> None:
    source_name = f"synthetic-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1", bad=True)
    engine = create_engine(settings.database_url)
    first = ingest(LocalPackage(package), PostgresRepository(engine), "test-version")
    assert (
        first["received"],
        first["preserved"],
        first["rejected"],
        first["pending"],
    ) == (2, 1, 1, 1)
    assert first["state"] == "completed"
    assert first["app_version"] == "test-version"
    assert len(first["config_fingerprint"]) == 64
    assert first["failures"][0]["code"] == "record_rejected"
    assert first["failures"][0]["correlation_id"]
    assert "price" not in first["failures"][0]["cause"]
    assert "price" not in json.dumps(first, default=str)
    engine.dispose()

    # Nova conexão: evidência e seus vínculos sobrevivem ao reinício.
    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    again = ingest(LocalPackage(package), repository, "different-version")
    assert again == first
    with engine.connect() as connection:
        evidence = (
            connection.execute(
                text("""
            SELECT e.id, e.raw_payload, e.payload_hash FROM data_governance.raw_evidence e
            JOIN data_governance.run_evidence re ON re.evidence_id=e.id WHERE re.run_id=:id
        """),
                {"id": first["id"]},
            )
            .mappings()
            .one()
        )
        assert evidence["payload_hash"] and '"title"' in evidence["raw_payload"]
        origins = connection.execute(
            text(
                "SELECT attribute_path, actor FROM data_governance.attribute_origins WHERE evidence_id=:id"
            ),
            {"id": evidence["id"]},
        ).all()
        assert set(origins) == {("title", "Eduardo"), ("platform", "Eduardo")}
        rights = connection.execute(
            text(
                "SELECT storage_right, publication_right, attribution, eligible FROM data_governance.media_rights WHERE evidence_id=:id"
            ),
            {"id": evidence["id"]},
        ).one()
        assert rights == ("unknown", "unknown", "Eduardo", False)
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM data_governance.raw_evidence WHERE source=:source"
                ),
                {"source": source_name},
            ).scalar_one()
            == 1
        )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE data_governance.raw_evidence SET raw_payload='altered' WHERE id=:id"),
                {"id": evidence["id"]},
            )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM data_governance.attribute_origins WHERE evidence_id=:id"),
                {"id": evidence["id"]},
            )
    for mutation in (
        "UPDATE data_governance.run_evidence SET evidence_id=evidence_id WHERE run_id=:id",
        "DELETE FROM data_governance.run_evidence WHERE run_id=:id",
        "UPDATE data_governance.ingest_failures SET code='changed' WHERE run_id=:id",
        "DELETE FROM data_governance.ingest_failures WHERE run_id=:id",
        "UPDATE data_governance.media_rights SET attribution='changed' WHERE evidence_id=:id",
        "DELETE FROM data_governance.media_rights WHERE evidence_id=:id",
    ):
        target_id = evidence["id"] if "media_rights" in mutation else first["id"]
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text(mutation), {"id": target_id})
    for table in (
        "data_governance.raw_evidence",
        "data_governance.attribute_origins",
        "data_governance.media_rights",
        "data_governance.run_evidence",
        "data_governance.ingest_failures",
    ):
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    changed = json.loads((package / "game.json").read_text())
    changed["attributes"]["title"] = "Jogo novo"
    (package / "game.json").write_text(json.dumps(changed))
    with pytest.raises(VersionConflict):
        ingest(LocalPackage(package), repository, "test-version")
    _package(package, source_name, "2", title="Jogo novo")
    confirmed = json.loads((package / "game.json").read_text())
    confirmed["media"][0]["storage_right"] = "confirmed"
    confirmed["media"][0]["publication_right"] = "confirmed"
    (package / "game.json").write_text(json.dumps(confirmed))
    second = ingest(LocalPackage(package), repository, "test-version")
    assert second["id"] != first["id"]
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM data_governance.raw_evidence WHERE source=:source"
                ),
                {"source": source_name},
            ).scalar_one()
            == 2
        )
        old = connection.execute(
            text("SELECT raw_payload FROM data_governance.raw_evidence WHERE id=:id"),
            {"id": evidence["id"]},
        ).scalar_one()
        assert json.loads(old)["attributes"]["title"] == "Jogo sintético"
        assert connection.execute(
            text("""
                SELECT mr.eligible FROM data_governance.media_rights mr
                JOIN data_governance.run_evidence re ON re.evidence_id = mr.evidence_id
                WHERE re.run_id = :id
            """),
            {"id": second["id"]},
        ).scalar_one() is True
    engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_running_execution_resumes_without_duplicate_evidence_or_failures(
    tmp_path: Path,
) -> None:
    source_name = f"resume-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1", bad=True)
    snapshot = LocalPackage(package).snapshot()
    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    run_id = uuid5(NAMESPACE_URL, f"retrovault:ingest:{source_name}:1")
    repository.create_run(run_id, snapshot.manifest, snapshot.package_hash, "0" * 64, "test-version")
    payload = snapshot.records[0].payload
    assert payload is not None
    payload_hash = sha256(payload).hexdigest()
    evidence_id = uuid5(NAMESPACE_URL, f"retrovault:evidence:{source_name}:game:{payload_hash}")
    repository.preserve(run_id, evidence_id, snapshot.manifest, parse_record("game", payload.decode()), payload_hash)
    repository.fail(run_id, "bad", "record_rejected", uuid4())
    running = repository.summary(run_id)
    assert running is not None
    assert (running["received"], running["preserved"], running["rejected"], running["pending"]) == (2, 1, 1, 1)

    resumed = ingest(LocalPackage(package), repository, "changed-version")
    assert resumed["id"] == run_id
    assert resumed["app_version"] == "test-version"
    assert (resumed["received"], resumed["preserved"], resumed["rejected"], resumed["pending"]) == (2, 1, 1, 1)
    assert resumed["state"] == "completed"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM data_governance.run_evidence WHERE run_id=:id"), {"id": run_id}).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM data_governance.ingest_failures WHERE run_id=:id"), {"id": run_id}).scalar_one() == 1
    engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_evidence_uses_the_exact_bytes_in_package_fingerprint(tmp_path: Path) -> None:
    source_name = f"snapshot-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1")

    class MutatingSource:
        max_record_bytes = 262_144

        def snapshot(self) -> PackageSnapshot:
            result = LocalPackage(package).snapshot()
            changed = json.loads((package / "game.json").read_text())
            changed["attributes"]["title"] = "Alterado após snapshot"
            (package / "game.json").write_text(json.dumps(changed))
            return result

    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    result = ingest(MutatingSource(), repository, "test-version")
    with engine.connect() as connection:
        row = connection.execute(text("""
            SELECT r.package_hash, e.raw_payload, e.payload_hash
            FROM data_governance.ingest_runs r
            JOIN data_governance.run_evidence re ON re.run_id=r.id
            JOIN data_governance.raw_evidence e ON e.id=re.evidence_id
            WHERE r.id=:id
        """), {"id": result["id"]}).one()
    assert json.loads(row.raw_payload)["attributes"]["title"] == "Jogo sintético"
    assert row.payload_hash == sha256(row.raw_payload.encode()).hexdigest()
    assert row.package_hash != LocalPackage(package).snapshot().package_hash
    engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_changed_oversized_record_conflicts_for_same_source_version(
    tmp_path: Path,
) -> None:
    source_name = f"large-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1")
    common_prefix = b"x" * 513
    (package / "game.json").write_bytes(common_prefix + b"a")
    source = LocalPackage(package, max_record_bytes=512)
    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)

    first = ingest(source, repository, "test-version")
    assert (first["received"], first["preserved"], first["rejected"]) == (1, 0, 1)
    assert first["failures"][0]["code"] == "record_rejected"

    (package / "game.json").write_bytes(common_prefix + b"b")
    with pytest.raises(VersionConflict):
        ingest(source, repository, "test-version")
    engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_simultaneous_replay_uses_one_run_and_one_evidence(tmp_path: Path) -> None:
    source_name = f"concurrent-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1")
    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: ingest(LocalPackage(package), repository, "test-version"),
                range(2),
            )
        )
    assert results[0] == results[1]
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM data_governance.ingest_runs WHERE source=:source"),
            {"source": source_name},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM data_governance.raw_evidence WHERE source=:source"),
            {"source": source_name},
        ).scalar_one() == 1
    engine.dispose()


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_cli_reports_declared_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from app.modules.data_governance.api.cli import main

    assert main(["summary", str(uuid4())]) == 1
    assert json.loads(capsys.readouterr().out) == {"code": "run_not_found"}
    assert main(["ingest", str(tmp_path / "missing")]) == 2
    assert json.loads(capsys.readouterr().out) == {"code": "invalid_package"}

    package = tmp_path / "valid-package"
    _package(package, f"cli-{uuid4().hex}", "1")
    assert main(["ingest", str(package)]) == 0
    output = capsys.readouterr().out
    summary = json.loads(output)
    assert summary["state"] == "completed"
    assert (summary["received"], summary["preserved"], summary["rejected"]) == (
        1,
        1,
        0,
    )
    assert "Jogo sintético" not in output
    assert "raw_payload" not in summary


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_review_corrections_are_versioned_and_etag_guarded(tmp_path: Path) -> None:
    source_name = f"review-{uuid4().hex}"
    package = tmp_path / "package"
    _package(package, source_name, "1", title="Título original")
    cover = BytesIO()
    Image.new("RGB", (4, 4), "blue").save(cover, format="PNG")
    (package / "cover.png").write_bytes(cover.getvalue())
    record_path = package / "game.json"
    record = json.loads(record_path.read_text())
    record["media"] = [
        {
            "path": "cover.png",
            "role": "box_art",
            "storage_right": "confirmed",
            "publication_right": "confirmed",
            "attribution": "RetroAchievements",
        }
    ]
    record_path.write_text(json.dumps(record))

    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    ingested = ingest(LocalPackage(package), repository, "review-test")
    processed = process_run(repository, UUID(str(ingested["id"])))
    assert processed["review"] == 1

    candidate = repository.review_candidate(UUID(str(ingested["id"])), "game")
    assert candidate["state"] == "review"
    assert candidate["cover"]["content"] is None  # Bytes require explicit private access.
    etag = str(candidate["etag"])
    corrected = repository.correct_review(
        UUID(str(ingested["id"])),
        "game",
        "title",
        "Título corrigido",
        "Corrigir erro editorial",
        etag,
    )
    assert corrected["values"]["title"] == "Título corrigido"
    assert corrected["etag"] != etag
    assert corrected["state"] == "review"
    assert corrected["lineage"]["title"]["source"] == "human-review"
    assert corrected["lineage"]["title"]["reason"] == "Corrigir erro editorial"
    assert corrected["lineage"]["title"]["previous_etag"] == etag
    assert corrected["lineage"]["title"]["resulting_etag"] == corrected["etag"]
    assert len(corrected["corrections"]) == 1
    assert corrected["corrections"][0]["previous_value"] == "Título original"
    cover_candidate = repository.review_candidate(
        UUID(str(ingested["id"])), "game", include_private_media=True
    )
    assert cover_candidate["cover"]["content"] == cover.getvalue()

    with pytest.raises(ReviewConflict):
        repository.correct_review(
            UUID(str(ingested["id"])),
            "game",
            "title",
            "Outro título",
            "Tentativa obsoleta",
            etag,
        )
    with pytest.raises(InvalidCorrection):
        repository.correct_review(
            UUID(str(ingested["id"])),
            "game",
            "price",
            "99.00",
            "Campo comercial proibido",
            str(corrected["etag"]),
        )

    with engine.connect() as connection:
        correction_count = connection.execute(
            text("SELECT count(*) FROM data_governance.review_corrections WHERE run_id=:id"),
            {"id": ingested["id"]},
        ).scalar_one()
        original = connection.execute(
            text("""
                SELECT sv.normalized_value
                FROM data_governance.staging_values sv
                JOIN data_governance.staging_records sr
                  ON sr.run_id=sv.run_id AND sr.rule_version=sv.rule_version AND sr.evidence_id=sv.evidence_id
                WHERE sr.run_id=:id AND sr.source_record_id='game' AND sv.attribute_path='title'
            """),
            {"id": ingested["id"]},
        ).scalar_one()
    assert correction_count == 1
    assert original == "Título original"
    engine.dispose()
