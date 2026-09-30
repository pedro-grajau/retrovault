"""Aquisição privada, limitada e determinística pela API Get Game oficial."""

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.modules.data_governance.domain.models import (
    Manifest,
    PackageSnapshot,
    RecordReference,
    SnapshotRecord,
    strict_json_loads,
)
from app.modules.data_governance.ports.source import PackageError

API_URL = "https://retroachievements.org/API/API_GetGame.php"
MEDIA_ORIGINS = {"retroachievements.org", "www.retroachievements.org"}
ALLOWED_CONSOLES = {1: "Mega Drive", 2: "Nintendo 64", 3: "SNES", 12: "PlayStation"}
MAX_MANIFEST_BYTES = 128_000
MAX_API_BYTES = 2_000_000
MAX_MEDIA_BYTES = 5_000_000
MAX_IDS = 1_000
MAX_REDIRECTS = 3
MAX_RETRIES = 2
MAX_RETRY_WAIT_SECONDS = 30.0
_RFC3339_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


class RetroAchievementsSource:
    """Get Game adapter. The credential is only sent as the private ``y`` query."""

    max_record_bytes = MAX_API_BYTES

    def __init__(
        self,
        manifest_path: Path,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not api_key or any(ord(character) < 32 for character in api_key):
            raise ValueError("retroachievements_key_unavailable")
        if timeout <= 0 or timeout > 30:
            raise ValueError("invalid_timeout")
        self.manifest_path = manifest_path
        self.api_key = api_key
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 3.0)),
                follow_redirects=False,
                headers={"User-Agent": "RetroVault-catalog/1.0"},
            )
        return self._client

    def _load_manifest(self) -> tuple[Manifest, bytes]:
        try:
            with self.manifest_path.open("rb") as manifest_file:
                raw = manifest_file.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise PackageError("manifest_too_large")
            value = strict_json_loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "source", "version", "captured_at", "actor", "record_ids"
            }:
                raise ValueError
            source, version, actor = (value[key] for key in ("source", "version", "actor"))
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

    @staticmethod
    def _safe_media_url(path: object) -> str:
        if not isinstance(path, str) or not path.startswith("/Images/"):
            raise PackageError("cover_url_invalid")
        if "\\" in path or "?" in path or "#" in path:
            raise PackageError("cover_url_invalid")
        parts = urlsplit(path)
        if parts.scheme or parts.netloc or any(part in {".", ".."} for part in parts.path.split("/")):
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
            except (TypeError, ValueError, OverflowError):
                return None

    def _request(self, url: str, *, params: dict[str, str] | None = None, max_bytes: int) -> bytes:
        current_url = url
        current_params = params
        deadline = time.monotonic() + self.timeout
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
                        if response.status_code == 429 or 500 <= response.status_code < 600:
                            if attempt >= MAX_RETRIES:
                                raise PackageError("source_rate_limited" if response.status_code == 429 else "source_unavailable")
                            delay = self._retry_delay(response.headers.get("retry-after"))
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
                        return bytes(content)
                except httpx.HTTPError as exc:
                    raise PackageError("source_unavailable") from exc
            else:
                raise PackageError("source_unavailable")
            continue
        raise PackageError("source_redirect_rejected")

    def _game(self, game_id: int) -> tuple[bytes, bytes, bytes | None, str | None]:
        raw = self._request(
            API_URL,
            params={"i": str(game_id), "y": self.api_key},
            max_bytes=MAX_API_BYTES,
        )
        try:
            payload = strict_json_loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise PackageError("source_response_invalid") from exc
        if (
            not isinstance(payload, dict)
            or type(payload.get("ID")) is not int
            or payload["ID"] != game_id
        ):
            raise PackageError("source_response_invalid")
        if self.api_key.encode() in raw:
            raise PackageError("source_response_contains_secret")
        image_url: str | None = None
        image_bytes: bytes | None = None
        if payload.get("ImageBoxArt"):
            try:
                image_url = self._safe_media_url(payload["ImageBoxArt"])
                image_bytes = self._request(image_url, max_bytes=MAX_MEDIA_BYTES)
            except PackageError:
                # A resposta original continua preservada; uma capa indisponível
                # vira uma pendência de quarentena na etapa de processamento.
                image_url = None
                image_bytes = None
        attrs: dict[str, str] = {}
        title = payload.get("Title")
        if isinstance(title, str):
            attrs["title"] = title
        console_id = payload.get("ConsoleID")
        if type(console_id) is int and console_id in ALLOWED_CONSOLES:
            attrs["platform"] = ALLOWED_CONSOLES[console_id]
        for source_key, attribute in (("Publisher", "publisher"), ("Developer", "developer"), ("Genre", "genre")):
            value = payload.get(source_key)
            if isinstance(value, str) and value.strip():
                attrs[attribute] = value
        released = payload.get("Released")
        if isinstance(released, str) and (match := re.match(r"^(\d{4})", released)):
            attrs["year"] = match.group(1)
        mapped = {
            "id": str(game_id),
            "attributes": attrs,
            "media": ([{
                "path": payload["ImageBoxArt"],
                "role": "box_art",
                "storage_right": "confirmed",
                "publication_right": "confirmed",
                "attribution": "RetroAchievements — autorização de portfólio informada em 2026-09-30",
            }] if image_url and image_bytes is not None else []),
        }
        return json.dumps(mapped, ensure_ascii=False, separators=(",", ":")).encode(), raw, image_bytes, str(payload.get("ImageBoxArt")) if image_url else None

    def snapshot(self) -> PackageSnapshot:
        manifest, manifest_bytes = self._load_manifest()
        digest = hashlib.sha256()
        digest.update(len(manifest_bytes).to_bytes(8, "big"))
        digest.update(manifest_bytes)
        records: list[SnapshotRecord] = []
        try:
            for reference in manifest.records:
                game_id = int(reference.record_id)
                try:
                    mapped, source_payload, image_bytes, image_path = self._game(game_id)
                    digest.update(reference.record_id.encode())
                    digest.update(hashlib.sha256(source_payload).digest())
                    if image_bytes is not None:
                        digest.update(hashlib.sha256(image_bytes).digest())
                    media = ((image_path, image_bytes),) if image_path and image_bytes is not None else ()
                    records.append(SnapshotRecord(mapped, media_bytes=media, source_payload=source_payload))
                except PackageError as exc:
                    digest.update(reference.record_id.encode())
                    digest.update(str(exc.args[0]).encode())
                    records.append(SnapshotRecord(None, str(exc.args[0])))
            fingerprint = hashlib.sha256(
                b"retroachievements-get-game-v1;max-api=2000000;max-image=5000000"
            ).hexdigest()
            digest.update(fingerprint.encode())
            return PackageSnapshot(manifest, digest.hexdigest(), tuple(records), fingerprint)
        finally:
            if self._owns_client and self._client is not None:
                self._client.close()
                self._client = None
