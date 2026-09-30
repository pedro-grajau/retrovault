"""Matriz de normalização, reconciliação e quarentena."""

import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.modules.data_governance.adapters.local_package import LocalPackage
from app.modules.data_governance.adapters.postgres_repository import PostgresRepository
from app.modules.data_governance.application.ingest import ingest
from app.modules.data_governance.application.process import process_run
from app.modules.data_governance.domain.normalization import (
    candidate_from_evidence,
    reconcile,
)
from app.platform.config.settings import settings

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
)


def _row(
    record_id: str,
    attributes: dict,
    *,
    content: bytes | None = PNG,
    rights: str = "confirmed",
) -> dict:
    return {
        "evidence_id": uuid4(),
        "record_id": record_id,
        "attributes": attributes,
        "media": [
            {
                "role": "box_art",
                "content_available": content is not None,
                "content": content,
                "storage_right": rights,
                "publication_right": rights,
                "attribution": "Eduardo",
            }
        ],
    }


def test_complete_record_enters_review_and_optional_gaps_are_measured() -> None:
    candidate = candidate_from_evidence(
        _row("a", {"title": "  Sonic  2 ", "platform": "Mega Drive"})
    )
    assert candidate.state == "review"
    assert candidate.values["title"] == "Sonic 2"
    assert set(candidate.missing) == {
        "publisher",
        "developer",
        "genre",
        "description",
        "year",
        "rating",
        "included_items",
    }
    assert candidate.ambiguous


def test_rule_v2_removes_invisible_format_characters_without_changing_v1() -> None:
    raw = _row("a", {"title": "Jo\u200bgo", "platform": "SNES"})
    first = candidate_from_evidence(raw, "editorial-v1")
    second = candidate_from_evidence(raw, "editorial-v2")
    assert first.values["title"] == "Jo\u200bgo"
    assert second.values["title"] == "Jogo"
    assert first.identity[0] == "jo go"
    assert second.identity[0] == "jogo"


def test_versioned_fixture_snapshots_private_cover_bytes() -> None:
    fixture = Path(__file__).parents[3] / "fixtures" / "catalog" / "processing"
    snapshot = LocalPackage(fixture).snapshot()
    assert len(snapshot.records) == 2
    assert dict(snapshot.records[0].media_bytes)["cover.png"] == PNG
    assert snapshot.package_hash == LocalPackage(fixture).snapshot().package_hash


def test_snapshot_does_not_read_cover_without_storage_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    _package(package, "rights-test")
    for record_name in ("first", "second"):
        record_path = package / f"{record_name}.json"
        record = json.loads(record_path.read_text())
        record["media"][0]["storage_right"] = "denied"
        record_path.write_text(json.dumps(record))

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("mídia sem direito de armazenamento foi lida")

    monkeypatch.setattr(LocalPackage, "_read_media", forbidden_read)
    snapshot = LocalPackage(package).snapshot()
    assert dict(snapshot.records[0].media_bytes)["cover.png"] is None


def test_snapshot_caps_total_media_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.data_governance.adapters import local_package

    package = tmp_path / "package"
    _package(package, "media-budget")
    monkeypatch.setattr(local_package, "MAX_PACKAGE_MEDIA_BYTES", 4)
    snapshot = LocalPackage(package).snapshot()
    assert dict(snapshot.records[0].media_bytes)["cover.png"] is None
    assert dict(snapshot.records[1].media_bytes)["cover.png"] is None


@pytest.mark.parametrize(
    "media,code", [(None, "cover_inaccessible"), (b"not image", "cover_inaccessible")]
)
def test_inaccessible_or_invalid_cover_is_quarantined(
    media: bytes | None, code: str
) -> None:
    candidate = candidate_from_evidence(
        _row("a", {"title": "A", "platform": "SNES"}, content=media)
    )
    assert candidate.state == "quarantine"
    assert any(
        issue.field == "box_art" and issue.code == code for issue in candidate.issues
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xd8\xff\xe0\x00\x04JFIF\x00\x00\xff\xda\x00\x02arbitrary-scan\xff\xd9",
        b"RIFF\x14\x00\x00\x00WEBPVP8 \x01\x00\x00\x00",
    ],
)
def test_plausible_but_undecodable_image_headers_are_rejected(payload: bytes) -> None:
    candidate = candidate_from_evidence(
        _row("a", {"title": "A", "platform": "SNES"}, content=payload)
    )
    assert candidate.state == "quarantine"
    assert any(issue.code == "cover_inaccessible" for issue in candidate.issues)


def test_missing_cover_rights_and_required_fields_are_quarantined() -> None:
    candidate = candidate_from_evidence(
        _row("a", {"title": " ", "platform": "SNES"}, rights="unknown")
    )
    assert {issue.code for issue in candidate.issues} == {
        "required_invalid",
        "cover_rights_unconfirmed",
    }
    assert candidate.state == "quarantine"
    absent = candidate_from_evidence(
        {**_row("b", {"title": "B", "platform": "SNES"}), "media": []}
    )
    assert any(issue.code == "cover_missing" for issue in absent.issues)


def test_duplicates_conflicts_and_identity_dimensions() -> None:
    base = {
        "title": "Crôno Trigger",
        "platform": "SNES",
        "region": "BR",
        "edition": "Padrão",
        "genre": "RPG",
        "developer": "Equipe A",
    }
    first = candidate_from_evidence(_row("a", base))
    same = candidate_from_evidence(_row("b", {**base, "title": "Crono Trigger"}))
    divergent = candidate_from_evidence(
        _row("c", {**base, "genre": "Aventura", "developer": "Equipe B"})
    )
    other_region = candidate_from_evidence(_row("d", {**base, "region": "JP"}))
    missing_edition = candidate_from_evidence(_row("e", {**base, "edition": ""}))
    partial_conflict = candidate_from_evidence(
        _row("f", {"title": "Crono Trigger", "platform": "SNES", "genre": "Ação"})
    )
    candidates = [first, same, divergent, other_region, missing_edition, partial_conflict]
    reconcile(candidates)
    assert any(kind == "duplicate" for _, kind, _ in first.matches)
    assert {field for _, kind, field in first.matches if kind == "conflict"} == {
        "genre",
        "developer",
    }
    assert {
        issue.field for issue in first.issues if issue.code == "unresolved_conflict"
    } == {
        "genre",
        "developer",
    }
    assert first.state == divergent.state == "quarantine"
    assert not any(other == other_region.evidence_id for other, _, _ in first.matches)
    assert any(kind == "ambiguous" for _, kind, _ in missing_edition.matches)
    assert any(kind == "ambiguous" for _, kind, _ in partial_conflict.matches)
    assert any(kind == "conflict" and field == "genre" for _, kind, field in partial_conflict.matches)
    assert partial_conflict.state == "quarantine"


def _package(root: Path, source: str) -> None:
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "source": source,
                "version": "1",
                "captured_at": "2026-09-29T12:00:00Z",
                "actor": "Eduardo",
                "records": [
                    {"id": key, "file": f"{key}.json"}
                    for key in ("first", "second", "invalid")
                ],
            }
        )
    )
    (root / "cover.png").write_bytes(PNG)
    for key, genre in (("first", "RPG"), ("second", "Aventura"), ("invalid", "RPG")):
        (root / f"{key}.json").write_text(
            json.dumps(
                {
                    "id": key,
                    "attributes": {
                        "title": "Jo\u200bgo",
                        "platform": "SNES",
                        "publisher": "Outra editora" if key == "second" else "Editora",
                        "genre": genre,
                    },
                    "media": (
                        [
                            {
                                "path": "cover.png",
                                "role": "box_art",
                                "storage_right": "confirmed",
                                "publication_right": "confirmed",
                                "attribution": "Eduardo",
                            }
                        ]
                        if key != "invalid"
                        else []
                    ),
                }
            )
        )


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_processing_persists_lineage_conflicts_idempotently_without_publication(
    tmp_path: Path,
) -> None:
    package = tmp_path / "package"
    source = f"processing-{uuid4().hex}"
    _package(package, source)
    engine = create_engine(settings.database_url)
    repository = PostgresRepository(engine)
    run = ingest(LocalPackage(package), repository, "test")
    result = process_run(repository, run["id"])
    assert result["total"] == 3
    assert result["quarantine"] == 3
    assert {issue["code"] for issue in result["issues"]} == {
        "cover_missing",
        "unresolved_conflict",
    }
    pair = [
        match
        for match in result["matches"]
        if match["record_id"] == "first"
        and match["other_record_id"] == "second"
        and match["kind"] == "conflict"
        and all(item["origin"]["source"] == source for item in match["alternatives"])
    ]
    assert {match["field"] for match in pair} == {"genre", "publisher"}
    assert {
        issue["field"]
        for issue in result["issues"]
        if issue["record_id"] == "first" and issue["code"] == "unresolved_conflict"
    } == {"genre", "publisher"}
    alternatives = {
        match["field"]: {item["record_id"]: item for item in match["alternatives"]}
        for match in pair
    }
    assert alternatives["genre"]["first"]["value"] == "RPG"
    assert alternatives["genre"]["second"]["value"] == "Aventura"
    assert alternatives["publisher"]["first"]["value"] == "Editora"
    assert alternatives["publisher"]["second"]["value"] == "Outra editora"
    for field_alternatives in alternatives.values():
        for item in field_alternatives.values():
            assert item["origin"]["source"] == source
            assert item["origin"]["source_version"] == "1"
            assert item["origin"]["source_record_id"] == item["record_id"]
            assert item["origin"]["actor"] == "Eduardo"
    assert "content" not in json.dumps(result, default=str)
    assert "raw_payload" not in json.dumps(result, default=str)
    assert process_run(repository, run["id"]) == result
    revised = process_run(repository, run["id"], "editorial-v2")
    assert revised["rule_fingerprint"] != result["rule_fingerprint"]
    assert revised["quarantine"] == result["quarantine"]
    assert process_run(repository, run["id"]) == result
    with engine.connect() as connection:
        versions = connection.execute(
            text("""
            SELECT sr.rule_version, sr.identity_key, sv.normalized_value
            FROM data_governance.staging_records sr
            JOIN data_governance.staging_values sv
              ON sv.run_id=sr.run_id AND sv.rule_version=sr.rule_version
             AND sv.evidence_id=sr.evidence_id AND sv.attribute_path='title'
            WHERE sr.run_id=:id AND sr.source_record_id='first'
        """),
            {"id": run["id"]},
        ).mappings()
        by_rule = {row["rule_version"]: row for row in versions}
        assert by_rule["editorial-v1"]["normalized_value"] == "Jo\u200bgo"
        assert by_rule["editorial-v2"]["normalized_value"] == "Jogo"
        assert json.loads(by_rule["editorial-v1"]["identity_key"])[0] == "jo go"
        assert json.loads(by_rule["editorial-v2"]["identity_key"])[0] == "jogo"
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM data_governance.processing_runs WHERE run_id=:id"
                ),
                {"id": run["id"]},
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM data_governance.staging_values WHERE run_id=:id AND rule_version='editorial-v1'"
                ),
                {"id": run["id"]},
            ).scalar_one()
            == 12
        )
        assert (
            connection.execute(
                text("""
            SELECT count(*) FROM data_governance.staging_values sv
            JOIN data_governance.attribute_origins ao
              ON ao.evidence_id=sv.evidence_id AND ao.attribute_path=sv.raw_attribute_path
            WHERE sv.run_id=:id AND sv.rule_version='editorial-v1'
        """),
                {"id": run["id"]},
            ).scalar_one()
            == 12
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM data_governance.private_media WHERE run_id=:id"
                ),
                {"id": run["id"]},
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_tables WHERE schemaname IN ('catalog', 'commerce')"
                )
            ).scalar_one()
            == 0
        )
    subsequent = tmp_path / "subsequent"
    _package(subsequent, source)
    manifest = json.loads((subsequent / "manifest.json").read_text())
    manifest["version"] = "2"
    manifest["records"] = [{"id": "first", "file": "first.json"}]
    (subsequent / "manifest.json").write_text(json.dumps(manifest))
    changed = json.loads((subsequent / "first.json").read_text())
    changed["attributes"]["genre"] = "Estratégia"
    (subsequent / "first.json").write_text(json.dumps(changed))
    next_run = ingest(LocalPackage(subsequent), repository, "test")
    next_result = process_run(repository, next_run["id"])
    assert next_result["quarantine"] == 1
    assert any(match["kind"] == "conflict" for match in next_result["matches"])
    engine.dispose()
