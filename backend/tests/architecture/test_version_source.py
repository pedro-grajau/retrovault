from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_compose_propagates_one_app_version_to_api_and_frontend() -> None:
    compose = (ROOT / "compose.yml").read_text()
    assert "APP_VERSION: ${APP_VERSION:-dev}" in compose
    assert "VITE_APP_VERSION: ${APP_VERSION:-dev}" in compose
