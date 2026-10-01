import json
import time
from io import BytesIO
from pathlib import Path

import httpx
import pytest
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
    responses = {
        key: json.dumps(value, separators=(",", ":")).encode()
        for key, value in original.items()
    }
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path.endswith("API_GetGame.php"):
            game_id = int(request.url.params["i"])
            assert request.url.params["y"] == "fixture-secret"
            return httpx.Response(
                200,
                content=responses[game_id],
                headers={"content-type": "application/json"},
            )
        if request.url.path.endswith("API_GetGameProgression.php"):
            game_id = int(request.url.params["i"])
            metrics = {"NumDistinctPlayers": 10}
            if game_id == 10:
                metrics["TimesUsedInCompletionMedian"] = 3
            return httpx.Response(200, json={"ID": game_id, **metrics})
        if request.url.path == "/Images/001.png":
            return httpx.Response(
                200, content=_png(), headers={"content-type": "image/png"}
            )
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
    assert mapped["metrics"] == {
        "NumDistinctPlayers": 10,
        "TimesUsedInCompletionMedian": 3,
    }
    assert mapped["media"][0]["role"] == "box_art"
    assert mapped["media"][0]["publication_right"] == "unknown"
    assert mapped["media"][0]["attribution"] == "RetroAchievements"
    assert dict(first.media_bytes)["/Images/001.png"] == _png()
    assert [response.endpoint for response in first.source_responses] == [
        "API_GetGame.php",
        "API_GetGameProgression.php",
    ]
    assert second.source_payload == responses[11]
    assert "platform" not in json.loads(second.payload or b"{}")["attributes"]
    assert json.loads(second.payload or b"{}")["media"] == []
    assert third.source_payload == responses[12]
    assert json.loads(third.payload or b"{}")["media"] == []
    assert all(url.host == "retroachievements.org" for url in seen)
    assert "fixture-secret" not in snapshot.package_hash


def test_expected_console_guard_rejects_game_from_another_console(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "expected-console-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [55],
            }
        )
    )
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={"ID": 55, "Title": "Jogo de outro console", "ConsoleID": 2},
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        expected_console_id=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert snapshot.records[0].payload is None
    assert snapshot.records[0].error_code == "source_response_invalid"
    assert requests == ["/API/API_GetGame.php"]


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
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, json={"ID": 20})
        return httpx.Response(
            302, headers={"location": "https://evil.example/cover.png"}
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert len(seen) == 3
    assert seen[0].url.params["y"] == "fixture-secret"
    assert seen[1].url.host == "retroachievements.org"
    assert seen[2].url.path.endswith("API_GetGameProgression.php")
    assert all(request.url.host != "evil.example" for request in seen)
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
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, json={"ID": int(request.url.params["i"])})
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
    assert (
        json.loads(snapshot.records[1].payload or b"{}")["attributes"]["title"]
        == "Sucesso independente"
    )


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
                return httpx.Response(
                    200, json={"ID": True, "Title": "Bool", "ConsoleID": 3}
                )
            if game_id == 2:
                return httpx.Response(200, json={"Title": "Sem ID", "ConsoleID": 3})
            if game_id == 4:
                return httpx.Response(
                    200,
                    json={
                        "ID": 4,
                        "Title": "Redirect inválido",
                        "ConsoleID": 3,
                        "ImageBoxArt": "/Images/redirect.png",
                    },
                )
            return httpx.Response(
                200,
                json={"ID": game_id, "Title": f"Jogo {game_id}", "ConsoleID": 3},
            )
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, json={"ID": int(request.url.params["i"])})
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
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, json={"ID": int(request.url.params["i"])})
        return httpx.Response(200, stream=SlowStream())

    started = time.monotonic()
    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        timeout=0.075,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()
    elapsed = time.monotonic() - started

    assert elapsed < 0.4
    assert chunks_seen < 8
    assert snapshot.records[0].source_payload is not None
    assert json.loads(snapshot.records[0].payload or b"{}")["media"] == []
    assert snapshot.records[1].source_payload is not None


def test_snapshot_limit_counts_original_endpoint_response_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [80],
            }
        )
    )
    game_payload = b'{"ID":80,"Title":"Jogo 80","ConsoleID":3}'
    progression_payload = b'{"ID":80,"NumDistinctPlayers":5}'
    mapped_payload = (
        b'{"id":"80","attributes":{"title":"Jogo 80","platform":"SNES"},'
        b'"metrics":{"NumDistinctPlayers":5},"media":[]}'
    )
    required_bytes = (
        len(manifest_path.read_bytes())
        + len(mapped_payload)
        + 2 * len(game_payload)
        + len(progression_payload)
    )
    monkeypatch.setattr(adapter, "MAX_SNAPSHOT_BYTES", required_bytes - 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, content=progression_payload)
        return httpx.Response(200, content=game_payload)

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()

    assert snapshot.records[0].error_code == "snapshot_size_limit"
    assert snapshot.records[0].payload is None
    assert len(mapped_payload) + len(game_payload) < adapter.MAX_SNAPSHOT_BYTES


def test_discovery_rejects_non_integer_console_id_without_requests(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="console_not_allowed"):
        source.discover_console_games(3.0)  # type: ignore[arg-type]
    assert requests == []


def test_response_cache_is_read_with_a_size_bound(tmp_path: Path) -> None:
    source = RetroAchievementsSource(
        None, "fixture-secret", cache_dir=tmp_path / "cache"
    )
    cache_path = source._cache_path("large-cached-response")
    assert cache_path is not None
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"x" * 1_000_000)

    with pytest.raises(ValueError, match="source_cache_too_large"):
        source._request(
            "https://retroachievements.org/API/example",
            max_bytes=4,
            cache_key="large-cached-response",
        )


def test_discovery_accepts_empty_terminal_page_after_page_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    monkeypatch.setattr(adapter, "MAX_GAME_LIST_PAGES", 1)
    page_offsets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        page_offsets.append(request.url.params["o"])
        if request.url.params["o"] == "0":
            return httpx.Response(
                200,
                json=[{"ID": 1, "Title": "A", "ConsoleID": 3, "NumAchievements": 1}],
            )
        return httpx.Response(200, json=[])

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    catalog = source.discover_console_games(3, page_size=1)

    assert [item["ID"] for item in catalog["games"]] == [1]
    assert catalog["page_count"] == 1
    assert page_offsets == ["0", "1"]


def test_discovery_rejects_data_beyond_page_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    monkeypatch.setattr(adapter, "MAX_GAME_LIST_PAGES", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        game_id = int(request.url.params["o"]) + 1
        return httpx.Response(
            200,
            json=[
                {
                    "ID": game_id,
                    "Title": f"Jogo {game_id}",
                    "ConsoleID": 3,
                    "NumAchievements": 1,
                }
            ],
        )

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="game_list_page_limit"):
        source.discover_console_games(3, page_size=1)


def test_discovery_uses_one_deadline_for_console_and_all_pages(
    tmp_path: Path,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        time.sleep(0.04)
        return httpx.Response(
            200,
            json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
        )

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        total_timeout=0.02,
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="source_timeout"):
        source.discover_console_games(3)

    assert requested_paths == ["/API/API_GetConsoleIDs.php"]


def test_invalid_success_response_is_evicted_from_cache_for_retry(
    tmp_path: Path,
) -> None:
    page_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_attempts
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        page_attempts += 1
        if page_attempts == 1:
            return httpx.Response(200, content=b"not json")
        return httpx.Response(200, json=[])

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ValueError, match="game_list_invalid"):
        source.discover_console_games(3, page_size=10)

    catalog = source.discover_console_games(3, page_size=10)

    assert catalog["games"] == []
    assert page_attempts == 2


def test_refresh_cache_fetches_changed_source_responses(tmp_path: Path) -> None:
    title = "Primeiro"
    list_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_requests
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        list_requests += 1
        return httpx.Response(
            200,
            json=[{"ID": 1, "Title": title, "ConsoleID": 3, "NumAchievements": 1}],
        )

    cache_dir = tmp_path / "cache"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    initial = RetroAchievementsSource(
        None, "fixture-secret", cache_dir=cache_dir, client=client
    ).discover_console_games(3, page_size=10)
    title = "Atualizado"
    refreshed = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=cache_dir,
        refresh_cache=True,
        client=client,
    ).discover_console_games(3, page_size=10)

    assert initial["games"][0]["Title"] == "Primeiro"
    assert refreshed["games"][0]["Title"] == "Atualizado"
    assert initial["version"] != refreshed["version"]
    assert list_requests == 2


def test_cached_catalog_timestamp_is_validated(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        return httpx.Response(200, json=[])

    cache_dir = tmp_path / "cache"
    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=cache_dir,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    catalog = source.discover_console_games(3)
    cache_path = cache_dir / "catalogs" / str(catalog["version"]) / "catalog.json"
    cached = json.loads(cache_path.read_text())
    cached["captured_at"] = "not-a-timestamp"
    cache_path.write_text(json.dumps(cached))

    with pytest.raises(ValueError, match="catalog_cache_invalid"):
        source.discover_console_games(3)


def test_snapshot_deadline_covers_the_entire_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [90, 91, 92],
            }
        )
    )
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetGameProgression.php"):
            return httpx.Response(200, json={"ID": int(request.url.params["i"])})
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
        record.error_code == "snapshot_deadline_exceeded" for record in snapshot.records
    )


def test_malformed_json_and_surrogate_only_fail_their_records(
    tmp_path: Path, monkeypatch
) -> None:
    import app.modules.data_governance.adapters.retroachievements as adapter

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [100, 101, 102],
            }
        )
    )
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
        return httpx.Response(200, json={"ID": 102, "Title": "Válido", "ConsoleID": 3})

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
    assert (
        json.loads(snapshot.records[2].payload or b"{}")["attributes"]["title"]
        == "Válido"
    )


def test_release_date_preserves_api_granularity(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source": "retroachievements",
                "version": "portfolio-v1",
                "captured_at": "2026-09-30T12:00:00Z",
                "actor": "Eduardo",
                "record_ids": [110],
            }
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ID": 110,
                "Title": "Jogo",
                "ConsoleID": 3,
                "Released": "1992-06-02 00:00:00",
                "ReleasedAtGranularity": "day",
            },
        )

    snapshot = RetroAchievementsSource(
        manifest_path,
        "fixture-secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).snapshot()
    attributes = json.loads(snapshot.records[0].payload or b"{}")["attributes"]

    assert attributes["year"] == "1992"
    assert attributes["release_date"] == "1992-06-02 00:00:00"
    assert attributes["release_date_granularity"] == "day"


def test_snes_catalog_discovery_pages_caches_and_writes_resumable_manifests(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    calls: list[httpx.URL] = []
    pages = {
        0: [
            {"ID": 1, "Title": "A", "ConsoleID": 3, "NumAchievements": 5},
            {"ID": 2, "Title": "B", "ConsoleID": 3, "NumAchievements": 8},
        ],
        2: [{"ID": 3, "Title": "C", "ConsoleID": 3, "NumAchievements": 2}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        assert request.url.params["y"] == "fixture-secret"
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            assert request.url.params["a"] == "1"
            assert request.url.params["g"] == "1"
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        assert request.url.path.endswith("API_GetGameList.php")
        assert request.url.params["i"] == "3"
        assert request.url.params["f"] == "1"
        offset = int(request.url.params["o"])
        assert request.url.params["c"] == "2"
        return httpx.Response(200, json=pages.get(offset, []))

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=cache_dir,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    catalog = source.discover_console_games(3, page_size=2)

    assert catalog["console_name"] == "SNES"
    assert [row["ID"] for row in catalog["games"]] == [1, 2, 3]
    assert catalog["page_count"] == 2
    assert len(calls) == 3
    manifests = source.create_batch_manifests(catalog, batch_size=2)
    assert len(manifests) == 2
    batches = [json.loads(path.read_text()) for path in manifests]
    assert batches[0]["record_ids"] == [1, 2]
    assert batches[1]["record_ids"] == [3]
    smaller_batches = source.create_batch_manifests(catalog, batch_size=1)
    assert len(smaller_batches) == 3
    assert smaller_batches != manifests

    resumed = source.discover_console_games(3, page_size=2)
    assert resumed["version"] == catalog["version"]
    assert resumed["captured_at"] == catalog["captured_at"]
    assert len(calls) == 3
    assert source.create_batch_manifests(resumed, batch_size=2) == manifests


def test_console_discovery_fails_before_listing_if_snes_is_not_active(
    tmp_path: Path,
) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(
            200,
            json=[{"ID": 3, "Name": "SNES", "Active": False, "IsGameSystem": True}],
        )

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    from app.modules.data_governance.ports.source import PackageError

    try:
        source.discover_console_games()
    except PackageError as error:
        assert str(error) == "console_not_available"
    else:
        raise AssertionError("SNES inativo deve interromper antes da listagem")

    assert requested_paths == ["/API/API_GetConsoleIDs.php"]


def test_game_list_repeated_page_is_rejected_and_never_persisted_partially(
    tmp_path: Path,
) -> None:
    repeated = [{"ID": 1, "Title": "A", "ConsoleID": 3, "NumAchievements": 5}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("API_GetConsoleIDs.php"):
            return httpx.Response(
                200,
                json=[{"ID": 3, "Name": "SNES", "Active": True, "IsGameSystem": True}],
            )
        return httpx.Response(200, json=repeated)

    source = RetroAchievementsSource(
        None,
        "fixture-secret",
        cache_dir=tmp_path / "cache",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    from app.modules.data_governance.ports.source import PackageError

    try:
        source.discover_console_games(3, page_size=1)
    except PackageError as error:
        assert str(error) == "game_list_repeated_page"
    else:
        raise AssertionError("página repetida deve ser rejeitada")

    assert not (tmp_path / "cache" / "catalogs").exists()
