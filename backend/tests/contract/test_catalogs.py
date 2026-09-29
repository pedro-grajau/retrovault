import json
from pathlib import Path

from app.main import app

ROOT = Path(__file__).parents[3]
REQUIRED = {"api", "commands", "states", "events", "adapters", "handoff", "ai_ledger", "jobs_outbox", "evaluation"}


def test_all_v1_contract_catalogs_are_owned_and_explicitly_versioned() -> None:
    found: set[str] = set()
    for directory in REQUIRED:
        files = list((ROOT / "contracts" / directory).glob("*.v1.json"))
        assert len(files) == 1, directory
        payload = json.loads(files[0].read_text())
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
