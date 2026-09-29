import json
from pathlib import Path

from app.main import app

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


def test_endpoint_matrix_contains_only_the_foundation_api() -> None:
    payload = json.loads(
        (ROOT / "contracts" / "api" / "endpoint-matrix.v1.json").read_text()
    )
    endpoints = {(entry["method"], entry["path"]) for entry in payload["endpoints"]}
    assert endpoints == {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/system/version"),
    }
    openapi_endpoints = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert openapi_endpoints == endpoints
