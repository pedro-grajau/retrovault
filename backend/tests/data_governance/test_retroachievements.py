import json
import time
from io import BytesIO
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from PIL import Image

from app.modules.data_governance.adapters.retroachievements import (
    RetroAchievementsSource,
)


def test_ra_manifest_fixture_matches_versioned_schema() -> None:
    root = Path(__file__).parents[3]
    schema = json.loads(
        (root / "fixtures/retroachievements/manifest.schema.json").read_text()
    )
    fixture = json.loads(
        (root / "fixtures/retroachievements/manifest.json").read_text()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(fixture)


def _png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 3), "red").save(output, format="PNG")
    return output.getvalue()


def test_get_game_adapter_preserves_source_bytes_and_isolates_bad_media(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    fixture = Path(__file__).parents[3] / "fixtures/retroachievements/manifest.json"
    manifest_path.write_bytes(fixture.read_bytes())
    original = {
        10: {
            "ID": 10,
            "Title": "Jogo RA",
            "ConsoleID": 3,
            "ConsoleName": "SNES",
            "Publisher": "Editora",
            "ImageBoxArt": "/Images/001.png",
        },
        11: {
            "ID": 11,
            "Title": "Plataforma não mapeada",
            "ConsoleID": 999,
            "ConsoleName": "Console futuro",
            "ImageBoxArt": "https://evil.example/cover.png",
        },
        12: {"ID": 12, "Title": "Sem capa", "ConsoleID": 2},
    }
    responses = {key: json.dumps(value, separators=(",", ":")).encode() for key, value in original.items()}
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path.endswith("API_GetGame.php"):
            game_id = int(request.url.params["i"])
            assert request.url.params["y"] == "fixture-secret"
            return httpx.Response(200, content=responses[game_id], headers={"content-type": "application/json"})
        if request.url.path == "/Images/001.png":
            return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
        raise AssertionError("o adapter tentou buscar URL de mídia não autorizada")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = RetroAchievementsSource(
        manifest_path, "fixture-secret", client=client
    ).snapshot()

    assert snapshot.manifest.source == "retroachievements"
    assert [item.record_id for item in snapshot.manifest.records] == ["10", "11", "12"]
    first, second, third = snapshot.records
    assert first.source_payload == responses[10]
    mapped = json.loads(first.payload or b"{}")
    assert mapped["attributes"]["platform"] == "SNES"
    assert mapped["attributes"]["title"] == "Jogo RA"
    assert mapped["media"][0]["role"] == "box_art"
    assert dict(first.media_bytes)["/Images/001.png"] == _png()
    assert second.source_payload == responses[11]
    assert "platform" not in json.loads(second.payload or b"{}")["attributes"]
    assert json.loads(second.payload or b"{}")["media"] == []
    assert third.source_payload == responses[12]
    assert json.loads(third.payload or b"{}")["media"] == []
    assert all(url.host == "retroachievements.org" for url in seen)
    assert "fixture-secret" not in snapshot.package_hash


def test_get_game_adapter_rejects_untrusted_redirect_without_forwarding_key(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [20],
            }
        )
    )
    api_body = json.dumps(
        {"ID": 20, "Title": "Jogo", "ConsoleID": 3, "ImageBoxArt": "/Images/a.png"}
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("API_GetGame.php"):
            return httpx.Response(200, text=api_body)
        return httpx.Response(302, headers={"location": "https://evil.example/cover.png"})

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert len(seen) == 2
    assert seen[0].url.params["y"] == "fixture-secret"
    assert seen[1].url.host == "retroachievements.org"
    assert snapshot.records[0].source_payload == api_body.encode()
    assert json.loads(snapshot.records[0].payload or b"{}")["media"] == []


def test_get_game_failures_are_isolated_and_retries_are_bounded(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [30, 31],
            }
        )
    )
    attempts: dict[int, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        game_id = int(request.url.params["i"])
        attempts[game_id] = attempts.get(game_id, 0) + 1
        if game_id == 30:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            json={"ID": 31, "Title": "Sucesso independente", "ConsoleID": 2},
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert attempts == {30: 3, 31: 1}
    assert snapshot.records[0].payload is None
    assert snapshot.records[1].source_payload is not None
    assert json.loads(snapshot.records[1].payload or b"{}") ["attributes"]["title"] == "Sucesso independente"


def test_bad_ids_and_malformed_redirect_ports_are_isolated(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [1, 2, 3, 4, 5],
            }
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetGame.php"):
            game_id = int(request.url.params["i"])
            if game_id == 1:
                return httpx.Response(200, json={"ID": True, "Title": "Bool", "ConsoleID": 3})
            if game_id == 2:
                return httpx.Response(200, json={"Title": "Sem ID", "ConsoleID": 3})
            if game_id == 4:
                return httpx.Response(
                    200,
                    json={"ID": 4, "Title": "Redirect inválido", "ConsoleID": 3, "ImageBoxArt": "/Images/redirect.png"},
                )
            return httpx.Response(
                200,
                json={"ID": game_id, "Title": f"Jogo {game_id}", "ConsoleID": 3},
            )
        return httpx.Response(
            302,
            headers={"location": "https://retroachievements.org:bad/Images/cover.png"},
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert snapshot.records[0].payload is None
    assert snapshot.records[1].payload is None
    assert snapshot.records[2].source_payload is not None
    assert snapshot.records[3].source_payload is not None
    assert json.loads(snapshot.records[3].payload or b"{}")["media"] == []
    assert snapshot.records[4].source_payload is not None


def test_request_enforces_overall_deadline_for_trickling_body(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [70, 71],
            }
        )
    )
    chunks_seen = 0

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal chunks_seen
            for _ in range(8):
                time.sleep(0.03)
                chunks_seen += 1
                yield b"x"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetGame.php"):
            game_id = int(request.url.params["i"])
            payload = {"ID": game_id, "Title": "Jogo", "ConsoleID": 3}
            if game_id == 70:
                payload["ImageBoxArt"] = "/Images/slow.png"
            return httpx.Response(200, json=payload)
        return httpx.Response(200, stream=SlowStream())

    started = time.monotonic()
    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        timeout=0.075,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert chunks_seen < 8
    assert snapshot.records[0].source_payload is not None
    assert json.loads(snapshot.records[0].payload or b"{}")["media"] == []
    assert snapshot.records[1].source_payload is not None


def test_snapshot_limits_total_retained_payload_and_media(tmp_path: Path, monkeypatch) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "source": "retroachievements",
        "version": "portfolio-v1",
        "captured_at": "2026-09-30T12:00:00Z",
        "actor": "Eduardo",
        "record_ids": [80, 81, 82],
    }))
    monkeypatch.setattr(adapter, "MAX_SNAPSHOT_BYTES", 150)

    def handler(request: httpx.Request) -> httpx.Response:
        game_id = int(request.url.params["i"])
        return httpx.Response(
            200,
            json={"ID": game_id, "Title": f"Jogo {game_id}", "ConsoleID": 3},
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    retained = sum(
        len(record.payload or b"")
        + len(record.source_payload or b"")
        + sum(len(content or b"") for _, content in record.media_bytes)
        for record in snapshot.records
    )
    assert retained <= adapter.MAX_SNAPSHOT_BYTES
    assert any(record.error_code == "snapshot_size_limit" for record in snapshot.records)


def test_snapshot_deadline_covers_the_entire_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "source": "retroachievements",
        "version": "portfolio-v1",
        "captured_at": "2026-09-30T12:00:00Z",
        "actor": "Eduardo",
        "record_ids": [90, 91, 92],
    }))
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        game_id = int(request.url.params["i"])
        requested.append(game_id)
        time.sleep(0.04)
        return httpx.Response(
            200,
            json={"ID": game_id, "Title": f"Jogo {game_id}", "ConsoleID": 3},
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        total_timeout=0.06,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert len(requested) < 3
    assert any(
        record.error_code == "snapshot_deadline_exceeded"
        for record in snapshot.records
    )


def test_malformed_json_and_surrogate_only_fail_their_records(
    tmp_path: Path, monkeypatch
) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "source": "retroachievements",
        "version": "portfolio-v1",
        "captured_at": "2026-09-30T12:00:00Z",
        "actor": "Eduardo",
        "record_ids": [100, 101, 102],
    }))
    parse_json = adapter.strict_json_loads

    def raise_recursion_for_first_record(raw):
        if b'"ID":100' in raw:
            raise RecursionError("deeply nested response")
        return parse_json(raw)

    monkeypatch.setattr(adapter, "strict_json_loads", raise_recursion_for_first_record)

    def handler(request: httpx.Request) -> httpx.Response:
        game_id = int(request.url.params["i"])
        if game_id == 100:
            return httpx.Response(200, json={"ID": 100, "Title": "Malformado"})
        if game_id == 101:
            return httpx.Response(
                200, content=b'{"ID":101,"Title":"\\ud800","ConsoleID":3}'
            )
        return httpx.Response(
            200, json={"ID": 102, "Title": "Válido", "ConsoleID": 3}
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert [record.error_code for record in snapshot.records[:2]] == [
        "source_response_invalid",
        "source_response_invalid",
    ]
    assert snapshot.records[2].source_payload is not None
    assert json.loads(snapshot.records[2].payload or b"{}")["attributes"]["title"] == "Válido"


def test_release_date_preserves_api_granularity(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "source": "retroachievements",
        "version": "portfolio-v1",
        "captured_at": "2026-09-30T12:00:00Z",
        "actor": "Eduardo",
        "record_ids": [110],
    }))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ID": 110,
            "Title": "Jogo",
            "ConsoleID": 3,
            "Released": "1992-06-02 00:00:00",
            "ReleasedAtGranularity": "day",
        })

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()
    attributes = json.loads(snapshot.records[0].payload or b"{}")["attributes"]

    assert attributes["year"] == "1992"
    assert attributes["release_date"] == "1992-06-02 00:00:00"
    assert attributes["release_date_granularity"] == "day"
