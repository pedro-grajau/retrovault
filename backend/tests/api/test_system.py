import json
from uuid import UUID, uuid4

import httpx
import pytest
from starlette.requests import Request

from app.main import app, internal_problem


@pytest.mark.anyio
async def test_version_exposes_configured_version_and_correlation_id() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/system/version")
    assert response.status_code == 200
    assert response.json()["app_version"]
    assert response.json()["correlation_id"] == response.headers["X-Correlation-ID"]


@pytest.mark.anyio
async def test_correlation_id_is_preserved() -> None:
    value = "1f4bfe4d-6a71-4d78-97d2-d4c481cc7bd7"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/system/version", headers={"X-Correlation-ID": value})
    assert response.json()["correlation_id"] == value


@pytest.mark.anyio
async def test_errors_use_problem_contract_and_correlation() -> None:
    value = "2373ba90-431c-4eba-8fd7-49dcaf73f3cc"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/missing", headers={"X-Correlation-ID": value})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "not_found"
    assert response.json()["correlation_id"] == value
    assert response.headers["X-Correlation-ID"] == value


@pytest.mark.anyio
async def test_health_endpoint_is_reachable() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    UUID(response.headers["X-Correlation-ID"])


@pytest.mark.anyio
async def test_unexpected_errors_use_problem_contract() -> None:
    value = uuid4()
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.state.correlation_id = value
    response = await internal_problem(request, RuntimeError("sensitive detail"))
    assert response.status_code == 500
    assert response.media_type == "application/problem+json"
    payload = json.loads(response.body)
    assert payload["code"] == "internal_error"
    assert payload["correlation_id"] == str(value)
    assert "sensitive detail" not in response.body.decode()
