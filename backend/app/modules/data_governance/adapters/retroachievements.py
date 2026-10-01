"""Aquisição privada, limitada e determinística pela API Get Game oficial."""

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from app.modules.data_governance.domain.models import (
    PROGRESSION_METRIC_FIELDS,
    Manifest,
    PackageSnapshot,
    RecordReference,
    SnapshotRecord,
    SourceResponse,
    strict_json_loads,
)
from app.modules.data_governance.ports.source import PackageError

API_ROOT = "https://retroachievements.org/API/"
API_URL = urljoin(API_ROOT, "API_GetGame.php")
PROGRESSION_URL = urljoin(API_ROOT, "API_GetGameProgression.php")
CONSOLE_IDS_URL = urljoin(API_ROOT, "API_GetConsoleIDs.php")
GAME_LIST_URL = urljoin(API_ROOT, "API_GetGameList.php")
MEDIA_ORIGINS = {"retroachievements.org", "www.retroachievements.org"}
ALLOWED_CONSOLES = {1: "Mega Drive", 2: "Nintendo 64", 3: "SNES", 12: "PlayStation"}
MAX_MANIFEST_BYTES = 128_000
MAX_API_BYTES = 2_000_000
MAX_MEDIA_BYTES = 5_000_000
MAX_IDS = 1_000
MAX_SNAPSHOT_BYTES = 64_000_000
MAX_SNAPSHOT_DURATION_SECONDS = 900.0
MAX_REDIRECTS = 3
MAX_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 30.0
MAX_GAME_LIST_PAGE_BYTES = 2_000_000
MAX_GAME_LIST_BYTES = MAX_SNAPSHOT_BYTES
MAX_GAME_LIST_PAGES = 1_000
MAX_GAME_LIST_RECORDS = 100_000
MAX_GAME_LIST_PAGE_SIZE = 1_000
_RFC3339_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class RetroAchievementsSource:
    """Get Game adapter. The credential is only sent as the private ``y`` query."""

    max_record_bytes = MAX_API_BYTES

    def __init__(
        self,
        manifest_path: Path | None,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        total_timeout: float = MAX_SNAPSHOT_DURATION_SECONDS,
        cache_dir: Path | None = None,
        refresh_cache: bool = False,
        expected_console_id: int | None = None,
    ) -> None:
        if not api_key or any(ord(character) < 32 for character in api_key):
            raise ValueError("retroachievements_key_unavailable")
        if timeout <= 0 or timeout > 30:
            raise ValueError("invalid_timeout")
        if total_timeout <= 0 or total_timeout > MAX_SNAPSHOT_DURATION_SECONDS:
            raise ValueError("invalid_total_timeout")
        self.manifest_path = manifest_path
        self.api_key = api_key
        self.timeout = timeout
        self.total_timeout = total_timeout
        self.cache_dir = cache_dir or (
            manifest_path.parent / ".retroachievements-cache"
            if manifest_path is not None
            else None
        )
        self.refresh_cache = refresh_cache
        self.expected_console_id = expected_console_id
        self._client = client
        self._owns_client = client is None

    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / "responses" / f"{digest}.bin"

    def _invalidate_cache(self, key: str) -> None:
        cache_path = self._cache_path(key)
        if cache_path is not None:
            try:
                cache_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _write_cache(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 3.0)),
                follow_redirects=False,
                headers={"User-Agent": "RetroVault-catalog/1.0"},
            )
        return self._client

    def _load_manifest(self) -> tuple[Manifest, bytes]:
        if self.manifest_path is None:
            raise PackageError("manifest_unavailable")
        try:
            with self.manifest_path.open("rb") as manifest_file:
                raw = manifest_file.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise PackageError("manifest_too_large")
            value = strict_json_loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "source",
                "version",
                "captured_at",
                "actor",
                "record_ids",
            }:
                raise ValueError
            source, version, actor = (
                value[key] for key in ("source", "version", "actor")
            )
            ids = value["record_ids"]
            captured_at = value["captured_at"]
            if (
                source != "retroachievements"
                or not isinstance(version, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", version)
                or actor != "Eduardo"
                or not isinstance(captured_at, str)
                or not _RFC3339_DATETIME.fullmatch(captured_at)
                or not isinstance(ids, list)
                or not ids
                or len(ids) > MAX_IDS
                or any(type(item) is not int or item <= 0 for item in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError
            timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError
            manifest = Manifest(
                source,
                version,
                timestamp.astimezone(UTC),
                actor,
                tuple(RecordReference(str(item), f"{item}.json") for item in ids),
            )
            return manifest, raw
        except PackageError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
            raise PackageError("invalid_manifest") from exc

    def manifest_identity(self) -> tuple[Manifest, str]:
        """Return a local manifest fingerprint without making provider requests."""
        manifest, raw = self._load_manifest()
        return manifest, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _safe_media_url(path: object) -> str:
        if not isinstance(path, str) or not path.startswith("/Images/"):
            raise PackageError("cover_url_invalid")
        if "\\" in path or "?" in path or "#" in path:
            raise PackageError("cover_url_invalid")
        parts = urlsplit(path)
        if (
            parts.scheme
            or parts.netloc
            or any(part in {".", ".."} for part in parts.path.split("/"))
        ):
            raise PackageError("cover_url_invalid")
        return urljoin("https://retroachievements.org", path)

    @staticmethod
    def _retry_delay(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                return max(0.0, (target - datetime.now(target.tzinfo)).total_seconds())
            except TypeError, ValueError, OverflowError:
                return None

    def _request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        max_bytes: int,
        total_deadline: float | None = None,
        cache_key: str | None = None,
    ) -> bytes:
        cache_path = self._cache_path(cache_key) if cache_key else None
        if cache_path is not None and cache_path.is_file() and not self.refresh_cache:
            try:
                with cache_path.open("rb") as cache_file:
                    cached = cache_file.read(max_bytes + 1)
            except OSError as exc:
                raise PackageError("source_cache_unavailable") from exc
            if len(cached) > max_bytes:
                raise PackageError("source_cache_too_large")
            if total_deadline is not None and time.monotonic() >= total_deadline:
                raise PackageError("source_timeout")
            return cached
        current_url = url
        current_params = params
        deadline = time.monotonic() + self.timeout
        if total_deadline is not None:
            deadline = min(deadline, total_deadline)
        for redirect_count in range(MAX_REDIRECTS + 1):
            for attempt in range(MAX_RETRIES + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PackageError("source_timeout")
                try:
                    with self._get_client().stream(
                        "GET",
                        current_url,
                        params=current_params,
                        timeout=remaining,
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or redirect_count >= MAX_REDIRECTS:
                                raise PackageError("source_redirect_rejected")
                            if current_params:
                                # A chave nunca é encaminhada para um redirecionamento.
                                raise PackageError("source_redirect_rejected")
                            destination = urljoin(str(response.url), location)
                            parsed = urlsplit(destination)
                            try:
                                port = parsed.port
                            except ValueError as exc:
                                raise PackageError("source_redirect_rejected") from exc
                            if (
                                parsed.scheme != "https"
                                or parsed.username
                                or parsed.password
                                or parsed.hostname not in MEDIA_ORIGINS
                                or port not in (None, 443)
                            ):
                                raise PackageError("source_redirect_rejected")
                            current_url = destination
                            break
                        if (
                            response.status_code == 429
                            or 500 <= response.status_code < 600
                        ):
                            if attempt >= MAX_RETRIES:
                                raise PackageError(
                                    "source_rate_limited"
                                    if response.status_code == 429
                                    else "source_unavailable"
                                )
                            delay = self._retry_delay(
                                response.headers.get("retry-after")
                            )
                            if delay is None:
                                delay = min(0.5 * (2**attempt), MAX_RETRY_WAIT_SECONDS)
                            if delay > MAX_RETRY_WAIT_SECONDS:
                                raise PackageError("source_rate_limited")
                            if time.monotonic() + delay >= deadline:
                                raise PackageError("source_timeout")
                            if delay > 0:
                                time.sleep(delay)
                            continue
                        if response.status_code != 200:
                            raise PackageError("source_request_rejected")
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() >= deadline:
                                raise PackageError("source_timeout")
                            content.extend(chunk)
                            if len(content) > max_bytes:
                                raise PackageError("source_response_too_large")
                        if time.monotonic() >= deadline:
                            raise PackageError("source_timeout")
                        result = bytes(content)
                        if params and self.api_key.encode() in result:
                            raise PackageError("source_response_contains_secret")
                        if cache_path is not None:
                            try:
                                self._write_cache(cache_path, result)
                            except OSError as exc:
                                raise PackageError("source_cache_unavailable") from exc
                        return result
                except httpx.HTTPError as exc:
                    raise PackageError("source_unavailable") from exc
            else:
                raise PackageError("source_unavailable")
            continue
        raise PackageError("source_redirect_rejected")

    def discover_console_games(
        self, console_id: int = 3, *, page_size: int = 100
    ) -> dict[str, object]:
        """Descobre um sistema ativo e lista jogos com conquistas em páginas limitadas."""
        if (
            type(console_id) is not int
            or console_id != 3
            or console_id not in ALLOWED_CONSOLES
        ):
            raise PackageError("console_not_allowed")
        if type(page_size) is not int or not 1 <= page_size <= MAX_GAME_LIST_PAGE_SIZE:
            raise PackageError("invalid_page_size")
        discovery_deadline = time.monotonic() + self.total_timeout

        systems_bytes = self._request(
            CONSOLE_IDS_URL,
            params={"a": "1", "g": "1", "y": self.api_key},
            max_bytes=MAX_API_BYTES,
            total_deadline=discovery_deadline,
            cache_key="console-ids:a=1:g=1",
        )
        try:
            systems = strict_json_loads(systems_bytes)
        except (UnicodeError, ValueError, RecursionError) as exc:
            self._invalidate_cache("console-ids:a=1:g=1")
            raise PackageError("console_list_invalid") from exc
        if not isinstance(systems, list) or any(
            not isinstance(item, dict) for item in systems
        ):
            self._invalidate_cache("console-ids:a=1:g=1")
            raise PackageError("console_list_invalid")
        matches = [
            item
            for item in systems
            if type(item.get("ID")) is int and item.get("ID") == console_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("Active") is not True
            or matches[0].get("IsGameSystem") is not True
            or not isinstance(matches[0].get("Name"), str)
        ):
            self._invalidate_cache("console-ids:a=1:g=1")
            raise PackageError("console_not_available")

        records: list[dict[str, object]] = []
        seen_ids: set[int] = set()
        records_by_id: dict[int, dict[str, object]] = {}
        retained_bytes = len(systems_bytes)
        page_digests: list[str] = []
        for page_number in range(MAX_GAME_LIST_PAGES + 1):
            if time.monotonic() >= discovery_deadline:
                raise PackageError("catalog_discovery_timeout")
            offset = page_number * page_size
            page_cache_key = (
                f"game-list:console={console_id}:f=1:o={offset}:c={page_size}"
            )
            page_bytes = self._request(
                GAME_LIST_URL,
                params={
                    "i": str(console_id),
                    "f": "1",
                    "o": str(offset),
                    "c": str(page_size),
                    "y": self.api_key,
                },
                max_bytes=MAX_GAME_LIST_PAGE_BYTES,
                total_deadline=discovery_deadline,
                cache_key=page_cache_key,
            )
            retained_bytes += len(page_bytes)
            if retained_bytes > MAX_GAME_LIST_BYTES:
                raise PackageError("game_list_size_limit")
            try:
                page = strict_json_loads(page_bytes)
            except (UnicodeError, ValueError, RecursionError) as exc:
                self._invalidate_cache(page_cache_key)
                raise PackageError("game_list_invalid") from exc
            if (
                not isinstance(page, list)
                or len(page) > page_size
                or any(not isinstance(item, dict) for item in page)
            ):
                self._invalidate_cache(page_cache_key)
                raise PackageError("game_list_invalid")
            if page_number == MAX_GAME_LIST_PAGES:
                if page:
                    raise PackageError("game_list_page_limit")
                break
            page_digests.append(hashlib.sha256(page_bytes).hexdigest())
            new_records = 0
            for item in page:
                game_id = item.get("ID")
                if (
                    type(game_id) is not int
                    or game_id <= 0
                    or type(item.get("ConsoleID")) is not int
                    or item["ConsoleID"] != console_id
                    or not isinstance(item.get("Title"), str)
                    or not item["Title"].strip()
                    or type(item.get("NumAchievements")) is not int
                    or item["NumAchievements"] <= 0
                ):
                    self._invalidate_cache(page_cache_key)
                    raise PackageError("game_list_invalid")
                if game_id in seen_ids:
                    previous = records_by_id[game_id]
                    if previous != item:
                        self._invalidate_cache(page_cache_key)
                        raise PackageError("game_list_duplicate_conflict")
                    continue
                seen_ids.add(game_id)
                records_by_id[game_id] = item
                records.append(item)
                new_records += 1
            if len(records) > MAX_GAME_LIST_RECORDS:
                raise PackageError("game_list_record_limit")
            if page and not new_records:
                self._invalidate_cache(page_cache_key)
                raise PackageError("game_list_repeated_page")
            if len(page) < page_size:
                break
        if time.monotonic() >= discovery_deadline:
            raise PackageError("catalog_discovery_timeout")

        digest = hashlib.sha256()
        digest.update(systems_bytes)
        for page_hash in page_digests:
            digest.update(page_hash.encode("ascii"))
        catalog_hash = digest.hexdigest()
        version = f"console-{console_id}-{catalog_hash[:32]}"
        now = datetime.now(UTC)
        if self.refresh_cache:
            version = f"{version}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", version):
            raise PackageError("catalog_version_invalid")

        if self.cache_dir is not None:
            snapshot_path = self.cache_dir / "catalogs" / version / "catalog.json"
            if snapshot_path.is_file() and not self.refresh_cache:
                try:
                    with snapshot_path.open("rb") as catalog_file:
                        cached_bytes = catalog_file.read(MAX_GAME_LIST_BYTES + 1)
                    if len(cached_bytes) > MAX_GAME_LIST_BYTES:
                        raise ValueError("catalog_cache_too_large")
                    cached_catalog = strict_json_loads(cached_bytes)
                except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                    raise PackageError("catalog_cache_invalid") from exc
                if (
                    not isinstance(cached_catalog, dict)
                    or cached_catalog.get("source") != "retroachievements"
                    or cached_catalog.get("version") != version
                    or cached_catalog.get("console_id") != console_id
                    or cached_catalog.get("catalog_hash") != catalog_hash
                    or cached_catalog.get("games") != records
                ):
                    raise PackageError("catalog_cache_conflict")
                captured_at = cached_catalog.get("captured_at")
                if not isinstance(captured_at, str) or not _RFC3339_DATETIME.fullmatch(
                    captured_at
                ):
                    raise PackageError("catalog_cache_invalid")
                try:
                    cached_timestamp = datetime.fromisoformat(
                        captured_at.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise PackageError("catalog_cache_invalid") from exc
                if (
                    cached_timestamp.tzinfo is None
                    or cached_timestamp.utcoffset() is None
                ):
                    raise PackageError("catalog_cache_invalid")
            else:
                captured_at = now.isoformat()
                catalog_value = {
                    "source": "retroachievements",
                    "version": version,
                    "console_id": console_id,
                    "console_name": matches[0]["Name"],
                    "filter": {"f": 1},
                    "page_size": page_size,
                    "page_count": len(page_digests),
                    "catalog_hash": catalog_hash,
                    "captured_at": captured_at,
                    "games": records,
                }
                try:
                    self._write_cache(
                        snapshot_path,
                        json.dumps(
                            catalog_value,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8"),
                    )
                except OSError as exc:
                    raise PackageError("catalog_cache_unavailable") from exc
        else:
            captured_at = now.isoformat()
        return {
            "source": "retroachievements",
            "version": version,
            "console_id": console_id,
            "console_name": matches[0]["Name"],
            "page_size": page_size,
            "page_count": len(page_digests),
            "catalog_hash": catalog_hash,
            "captured_at": captured_at,
            "games": records,
        }

    def create_batch_manifests(
        self, catalog: dict[str, object], *, batch_size: int = MAX_IDS
    ) -> list[Path]:
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_IDS:
            raise PackageError("invalid_batch_size")
        if self.cache_dir is None:
            raise PackageError("source_cache_unavailable")
        version = catalog.get("version")
        captured_at = catalog.get("captured_at")
        games = catalog.get("games")
        if (
            not isinstance(version, str)
            or not isinstance(captured_at, str)
            or not isinstance(games, list)
        ):
            raise PackageError("catalog_snapshot_invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", version):
            raise PackageError("catalog_version_invalid")
        if not _RFC3339_DATETIME.fullmatch(captured_at):
            raise PackageError("catalog_snapshot_invalid")
        try:
            captured_timestamp = datetime.fromisoformat(
                captured_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PackageError("catalog_snapshot_invalid") from exc
        if captured_timestamp.tzinfo is None or captured_timestamp.utcoffset() is None:
            raise PackageError("catalog_snapshot_invalid")
        manifest_dir = (
            self.cache_dir / "manifests" / version / f"batch-size-{batch_size:04d}"
        )
        paths: list[Path] = []
        for batch_index, start in enumerate(range(0, len(games), batch_size), start=1):
            batch = games[start : start + batch_size]
            record_ids = [item["ID"] for item in batch]
            batch_version = f"{version}-n{batch_size}-b{batch_index:04d}"
            manifest_path = manifest_dir / f"batch-{batch_index:04d}.json"
            manifest_value = {
                "source": "retroachievements",
                "version": batch_version,
                "captured_at": captured_at,
                "actor": "Eduardo",
                "record_ids": record_ids,
            }
            content = json.dumps(
                manifest_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if manifest_path.is_file() and not self.refresh_cache:
                try:
                    if manifest_path.read_bytes() != content:
                        raise PackageError("manifest_cache_conflict")
                except OSError as exc:
                    raise PackageError("source_cache_unavailable") from exc
            else:
                try:
                    self._write_cache(manifest_path, content)
                except OSError as exc:
                    raise PackageError("source_cache_unavailable") from exc
            paths.append(manifest_path)
        return paths

    def _game(
        self, game_id: int, total_deadline: float, source_version: str
    ) -> tuple[bytes, bytes, bytes | None, str | None, tuple[SourceResponse, ...]]:
        raw = self._request(
            API_URL,
            params={"i": str(game_id), "y": self.api_key},
            max_bytes=MAX_API_BYTES,
            total_deadline=total_deadline,
            cache_key=f"{source_version}:game:{game_id}",
        )
        game_captured_at = datetime.now(UTC)
        try:
            payload = strict_json_loads(raw)
        except (UnicodeError, ValueError, RecursionError) as exc:
            self._invalidate_cache(f"{source_version}:game:{game_id}")
            raise PackageError("source_response_invalid") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("ID")) is not int
            or payload["ID"] != game_id
            or (
                self.expected_console_id is not None
                and (
                    type(payload.get("ConsoleID")) is not int
                    or payload.get("ConsoleID") != self.expected_console_id
                )
            )
        ):
            self._invalidate_cache(f"{source_version}:game:{game_id}")
            raise PackageError("source_response_invalid")
        if self.api_key.encode() in raw:
            self._invalidate_cache(f"{source_version}:game:{game_id}")
            raise PackageError("source_response_contains_secret")
        image_url: str | None = None
        image_bytes: bytes | None = None
        if payload.get("ImageBoxArt"):
            try:
                image_url = self._safe_media_url(payload["ImageBoxArt"])
                cover_cache_key = f"{source_version}:cover:{image_url}"
                image_bytes = self._request(
                    image_url,
                    max_bytes=MAX_MEDIA_BYTES,
                    total_deadline=total_deadline,
                    cache_key=cover_cache_key,
                )
            except PackageError:
                if image_url:
                    self._invalidate_cache(f"{source_version}:cover:{image_url}")
                # A resposta original continua preservada; uma capa indisponível
                # vira uma pendência de quarentena na etapa de processamento.
                image_url = None
                image_bytes = None
        progression_raw = self._request(
            PROGRESSION_URL,
            params={"i": str(game_id), "y": self.api_key},
            max_bytes=MAX_API_BYTES,
            total_deadline=total_deadline,
            cache_key=f"{source_version}:progression:{game_id}",
        )
        progression_captured_at = datetime.now(UTC)
        if self.api_key.encode() in progression_raw:
            self._invalidate_cache(f"{source_version}:progression:{game_id}")
            raise PackageError("source_response_contains_secret")
        try:
            progression = strict_json_loads(progression_raw)
        except (UnicodeError, ValueError, RecursionError) as exc:
            self._invalidate_cache(f"{source_version}:progression:{game_id}")
            raise PackageError("progression_response_invalid") from exc
        if (
            not isinstance(progression, dict)
            or type(progression.get("ID")) is not int
            or progression["ID"] != game_id
        ):
            self._invalidate_cache(f"{source_version}:progression:{game_id}")
            raise PackageError("progression_response_invalid")
        metrics: dict[str, int] = {}
        for metric_name in sorted(PROGRESSION_METRIC_FIELDS):
            if metric_name not in progression or progression[metric_name] is None:
                continue
            metric_value = progression[metric_name]
            if type(metric_value) is not int or metric_value < 0:
                self._invalidate_cache(f"{source_version}:progression:{game_id}")
                raise PackageError("progression_metric_invalid")
            metrics[metric_name] = metric_value
        attrs: dict[str, str] = {}
        title = payload.get("Title")
        if isinstance(title, str):
            attrs["title"] = title
        console_id = payload.get("ConsoleID")
        if type(console_id) is int and console_id in ALLOWED_CONSOLES:
            attrs["platform"] = ALLOWED_CONSOLES[console_id]
        for source_key, attribute in (
            ("Publisher", "publisher"),
            ("Developer", "developer"),
            ("Genre", "genre"),
        ):
            value = payload.get(source_key)
            if isinstance(value, str) and value.strip():
                attrs[attribute] = value
        released = payload.get("Released")
        if isinstance(released, str) and released:
            attrs["release_date"] = released
            granularity = payload.get("ReleasedAtGranularity")
            if isinstance(granularity, str) and granularity in {"year", "month", "day"}:
                attrs["release_date_granularity"] = granularity
            if match := re.match(r"^(\d{4})", released):
                attrs["year"] = match.group(1)
        mapped = {
            "id": str(game_id),
            "attributes": attrs,
            "metrics": metrics,
            "media": (
                [
                    {
                        "path": payload["ImageBoxArt"],
                        "role": "box_art",
                        "storage_right": "confirmed",
                        "publication_right": "unknown",
                        "attribution": "RetroAchievements",
                    }
                ]
                if image_url and image_bytes is not None
                else []
            ),
        }
        try:
            mapped_bytes = json.dumps(
                mapped, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        except UnicodeError as exc:
            raise PackageError("source_response_invalid") from exc
        return (
            mapped_bytes,
            raw,
            image_bytes,
            str(payload.get("ImageBoxArt")) if image_url else None,
            (
                SourceResponse("API_GetGame.php", raw, game_captured_at),
                SourceResponse(
                    "API_GetGameProgression.php",
                    progression_raw,
                    progression_captured_at,
                ),
            ),
        )

    def snapshot(self) -> PackageSnapshot:
        total_deadline = time.monotonic() + self.total_timeout
        manifest, manifest_bytes = self._load_manifest()
        digest = hashlib.sha256()
        digest.update(len(manifest_bytes).to_bytes(8, "big"))
        digest.update(manifest_bytes)
        records: list[SnapshotRecord] = []
        retained_bytes = len(manifest_bytes)
        try:
            for reference in manifest.records:
                game_id = int(reference.record_id)
                if time.monotonic() >= total_deadline:
                    code = "snapshot_deadline_exceeded"
                    digest.update(reference.record_id.encode())
                    digest.update(code.encode())
                    records.append(SnapshotRecord(None, code))
                    continue
                try:
                    (
                        mapped,
                        source_payload,
                        image_bytes,
                        image_path,
                        source_responses,
                    ) = self._game(game_id, total_deadline, manifest.version)
                    media = (
                        ((image_path, image_bytes),)
                        if image_path and image_bytes is not None
                        else ()
                    )
                    response_size = sum(
                        len(response.payload) for response in source_responses
                    )
                    record_size = (
                        len(mapped)
                        + len(source_payload)
                        + response_size
                        + sum(
                            len(content) for _, content in media if content is not None
                        )
                    )
                    if retained_bytes + record_size > MAX_SNAPSHOT_BYTES:
                        code = "snapshot_size_limit"
                        digest.update(reference.record_id.encode())
                        digest.update(code.encode())
                        records.append(SnapshotRecord(None, code))
                        continue
                    retained_bytes += record_size
                    digest.update(reference.record_id.encode())
                    digest.update(hashlib.sha256(source_payload).digest())
                    for response in source_responses:
                        digest.update(response.endpoint.encode("utf-8"))
                        digest.update(hashlib.sha256(response.payload).digest())
                    if image_bytes is not None:
                        digest.update(hashlib.sha256(image_bytes).digest())
                    records.append(
                        SnapshotRecord(
                            mapped,
                            media_bytes=media,
                            source_payload=source_payload,
                            source_responses=source_responses,
                        )
                    )
                except PackageError as exc:
                    digest.update(reference.record_id.encode())
                    digest.update(str(exc.args[0]).encode())
                    records.append(SnapshotRecord(None, str(exc.args[0])))
            fingerprint = hashlib.sha256(
                b"retroachievements-get-game-progression-v2;max-api=2000000;max-image=5000000;"
                b"max-snapshot=64000000;total-timeout=900"
            ).hexdigest()
            digest.update(fingerprint.encode())
            return PackageSnapshot(
                manifest,
                digest.hexdigest(),
                tuple(records),
                fingerprint,
                hashlib.sha256(manifest_bytes).hexdigest(),
            )
        finally:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None
