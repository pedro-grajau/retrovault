import json
import os
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.application.ingest import (
    InvalidRecord,
    VersionConflict,
    ingest,
    parse_record,
)
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


def test_record_size_limit_is_applied_to_bytes_read(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    (package / "game.json").write_bytes(b"x" * 513)
    snapshot = LocalPackage(package, max_record_bytes=512).snapshot()
    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "record_too_large"


def test_manifest_rejects_extra_and_duplicate_keys(tmp_path: Path) -> None:
    package = tmp_path / "package"
    _package(package, "local-test", "1")
    manifest_path = package / "manifest.json"
    original = manifest_path.read_text()
    manifest_path.write_text(original.replace('"actor": "Eduardo"', '"actor": "Eduardo", "extra": 1'))
    from app.modules.data_governance.ports.source import PackageError

    with pytest.raises(PackageError):
        LocalPackage(package).snapshot()
    manifest_path.write_text(original.replace('"actor": "Eduardo"', '"actor": "Eduardo", "actor": "outro"'))
    with pytest.raises(PackageError):
        LocalPackage(package).snapshot()


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
