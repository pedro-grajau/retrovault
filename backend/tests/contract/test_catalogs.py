import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from app.main import app
from app.modules.data_governance.application.ingest import parse_record
from app.modules.data_governance.domain.models import strict_json_loads

ROOT = Path(__file__).parents[3]
REQUIRED = {"api", "commands", "states", "events", "adapters", "handoff", "ai_ledger", "jobs_outbox", "evaluation"}
CATALOG_FILENAMES = {
    "api": "endpoint-matrix.v1.json",
    "commands": "catalog.v1.json",
    "states": "catalog.v1.json",
    "events": "catalog.v1.json",
    "adapters": "catalog.v1.json",
    "handoff": "catalog.v1.json",
    "ai_ledger": "catalog.v1.json",
    "jobs_outbox": "catalog.v1.json",
    "evaluation": "catalog.v1.json",
}


def test_all_v1_contract_catalogs_are_owned_and_explicitly_versioned() -> None:
    found: set[str] = set()
    for directory in REQUIRED:
        catalog = ROOT / "contracts" / directory / CATALOG_FILENAMES[directory]
        assert catalog.is_file(), directory
        for contract in (ROOT / "contracts" / directory).glob("*.v1.json"):
            payload = json.loads(contract.read_text())
            assert payload["owner"]
            assert payload["version"] == 1
            assert payload["status"] in {"active", "reserved"}
        found.add(directory)
    assert found == REQUIRED


def test_endpoint_matrix_matches_the_foundation_and_published_catalog_api() -> None:
    payload = json.loads(
        (ROOT / "contracts" / "api" / "endpoint-matrix.v1.json").read_text()
    )
    endpoints = {(entry["method"], entry["path"]) for entry in payload["endpoints"]}
    assert endpoints == {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/system/version"),
        ("GET", "/api/v1/catalog/facets"),
        ("GET", "/api/v1/catalog/games"),
        ("GET", "/api/v1/catalog/games/{game_id}"),
        ("GET", "/api/v1/catalog/games/{game_id}/box-art"),
        ("POST", "/api/v1/concierge/context-references"),
        ("POST", "/api/v1/concierge/context-references/validate"),
    }
    openapi_endpoints = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert openapi_endpoints == endpoints


def test_publication_commands_and_state_contract_are_active() -> None:
    commands = json.loads((ROOT / "contracts/commands/catalog.v1.json").read_text())
    command_names = {item["name"] for item in commands["commands"]}
    assert {"catalog-approve", "catalog-withdraw"} <= command_names
    publication = json.loads(
        (ROOT / "contracts/states/publication.v1.json").read_text()
    )
    assert publication["owner"] == "catalog"
    assert publication["status"] == "active"
    state_machine = publication["state_machines"][0]
    assert state_machine["states"] == ["published", "retired"]
    assert state_machine["stable_public_id"] is True
    assert state_machine["audit_and_outbox_atomic"] is True


def test_catalog_manifest_and_record_schemas_cover_parser_fixtures() -> None:
    manifest_path = ROOT / "fixtures/catalog/manifest.schema.json"
    record_path = ROOT / "fixtures/catalog/record.schema.json"
    manifest_schema = json.loads(manifest_path.read_text())
    record_schema = json.loads(record_path.read_text())
    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator.check_schema(record_schema)

    format_checker = FormatChecker()
    assert "date-time" in format_checker.checkers
    manifest_validator = Draft202012Validator(
        manifest_schema, format_checker=format_checker
    )
    record_validator = Draft202012Validator(record_schema)
    contract = json.loads((ROOT / "contracts/adapters/catalog.v1.json").read_text())
    adapter = next(item for item in contract["adapters"] if item["name"] == "local-package")
    assert adapter["schema"] == "fixtures/catalog/manifest.schema.json"
    assert adapter["record_schema"] == "fixtures/catalog/record.schema.json"

    manifest = strict_json_loads((ROOT / "fixtures/catalog/manifest.json").read_bytes())
    manifest_validator.validate(manifest)
    for reference in manifest["records"]:
        record = strict_json_loads(
            (ROOT / "fixtures/catalog" / reference["file"]).read_bytes()
        )
        record_validator.validate(record)
        parse_record(reference["id"], json.dumps(record, ensure_ascii=False))

    # Formas mínimas aceitas pelo parser também pertencem ao contrato público.
    for record in (
        {"id": "id-only"},
        {"id": "empty-attributes", "attributes": {}},
        {"id": "empty-media", "media": []},
        {
            "id": "full-record",
            "attributes": {
                "title": "Título",
                "platform": "SNES",
                "included_items": ["Manual", "Cartucho"],
            },
            "media": [{"path": "cover", "storage_right": "confirmed"}],
        },
    ):
        parse_record(record["id"], json.dumps(record, ensure_ascii=False))
        record_validator.validate(record)

    lowercase_timestamp_manifest = json.loads(json.dumps(manifest))
    lowercase_timestamp_manifest["captured_at"] = "2026-09-29t12:00:00z"
    manifest_validator.validate(lowercase_timestamp_manifest)

    for field, value in (
        ("actor", "Outra pessoa"),
        ("captured_at", "2026-02-30T12:00:00Z"),
        ("captured_at", "2026-09-29 12:00:00Z"),
        ("file", 7),
        ("file", "../outside.json"),
    ):
        invalid_manifest = json.loads(json.dumps(manifest))
        if field == "file":
            invalid_manifest["records"][0]["file"] = value
        else:
            invalid_manifest[field] = value
        assert not manifest_validator.is_valid(invalid_manifest)


@pytest.mark.parametrize(
    "record",
    [
        {"id": "x", "extra": True},
        {"id": "x", "attributes": {"unknown": "value"}},
        {"id": "x", "attributes": {"included_items": [1]}},
        {"id": "x", "media": [{"path": "cover", "storage_right": "maybe"}]},
    ],
)
def test_catalog_record_schema_rejects_unknown_and_invalid_values(
    record: dict[str, object],
) -> None:
    schema = json.loads((ROOT / "fixtures/catalog/record.schema.json").read_text())
    with pytest.raises(ValidationError):
        Draft202012Validator(schema).validate(record)
