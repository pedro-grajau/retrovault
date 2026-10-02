"""Regras puras e versionadas para propostas editoriais privadas."""

import re
import unicodedata
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

RULE_VERSION = "editorial-v1"
_RULE_DESCRIPTIONS = {
    "editorial-v1": "unicode-nfkc:casefold:identity-title-platform-region-edition:box-art",
    "editorial-v2": "unicode-nfkc:strip-unicode-format-cf:casefold:identity-title-platform-region-edition:box-art",
    "editorial-v3": "unicode-nfkc:strip-unicode-format-cf:casefold:identity-title-platform-region-edition:box-art:append-only-loopback-private-use-decisions",
}
RULE_FINGERPRINTS = {
    version: sha256(f"{version}:{description}".encode()).hexdigest()
    for version, description in _RULE_DESCRIPTIONS.items()
}
OPTIONAL_FIELDS = (
    "publisher",
    "developer",
    "genre",
    "description",
    "year",
    "rating",
    "included_items",
)
REQUIRED_FIELDS = ("title", "platform")


def normalize_text(value: str, rule_version: str = RULE_VERSION) -> str:
    value = unicodedata.normalize("NFKC", value)
    if rule_version in {"editorial-v2", "editorial-v3"}:
        value = "".join(char for char in value if unicodedata.category(char) != "Cf")
    return " ".join(value.split())


def identity_text(value: str, rule_version: str = RULE_VERSION) -> str:
    value = unicodedata.normalize(
        "NFKD", normalize_text(value, rule_version)
    ).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", value))


def valid_image(payload: bytes | None) -> bool:
    if not payload:
        return False
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                return False
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > 20_000_000:
                return False
            image.load()
        return True
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return False


@dataclass
class Issue:
    field: str
    code: str
    cause: str


@dataclass
class Candidate:
    evidence_id: Any
    record_id: str
    values: dict[str, Any]
    identity: tuple[str, str, str, str]
    missing: tuple[str, ...]
    ambiguous: bool
    issues: list[Issue] = field(default_factory=list)
    matches: list[tuple[Any, str, str]] = field(default_factory=list)

    @property
    def state(self) -> str:
        return "quarantine" if self.issues else "review"


def candidate_from_evidence(
    row: dict[str, Any], rule_version: str = RULE_VERSION
) -> Candidate:
    attributes = row["attributes"]
    values: dict[str, Any] = {}
    issues: list[Issue] = []
    for key, raw in attributes.items():
        if isinstance(raw, str):
            values[key] = normalize_text(raw, rule_version)
        elif key == "included_items" and isinstance(raw, list):
            values[key] = [normalize_text(item, rule_version) for item in raw]
    for field_name in REQUIRED_FIELDS:
        if not values.get(field_name) or not identity_text(
            values[field_name], rule_version
        ):
            issues.append(
                Issue(
                    field_name,
                    "required_invalid",
                    "Campo obrigatório ausente ou vazio.",
                )
            )
    box_art = [m for m in row["media"] if m["role"] == "box_art"]
    if not box_art:
        issues.append(
            Issue("box_art", "cover_missing", "Capa privada não identificada.")
        )
    else:
        valid = False
        for media in box_art:
            if (
                media["storage_right"] != "confirmed"
                or media["publication_right"] != "confirmed"
                or not media["attribution"].strip()
            ):
                issues.append(
                    Issue(
                        "box_art",
                        "cover_rights_unconfirmed",
                        "Direitos ou atribuição da capa não confirmados.",
                    )
                )
            elif not media["content_available"] or not valid_image(media["content"]):
                issues.append(
                    Issue(
                        "box_art",
                        "cover_inaccessible",
                        "Capa privada inacessível ou inválida.",
                    )
                )
            else:
                valid = True
        if valid:
            issues = [issue for issue in issues if issue.field != "box_art"]
    identity: tuple[str, str, str, str] = (
        identity_text(str(values.get("title", "")), rule_version),
        identity_text(str(values.get("platform", "")), rule_version),
        identity_text(str(values.get("region", "")), rule_version),
        identity_text(str(values.get("edition", "")), rule_version),
    )
    return Candidate(
        row["evidence_id"],
        row["record_id"],
        values,
        identity,
        tuple(key for key in OPTIONAL_FIELDS if not values.get(key)),
        not identity[2] or not identity[3],
        issues,
    )


def reconcile(candidates: list[Candidate]) -> None:
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if left.evidence_id == right.evidence_id:
                continue
            if (
                not left.identity[0]
                or not left.identity[1]
                or left.identity[:2] != right.identity[:2]
            ):
                continue
            if any(
                a and b and a != b
                for a, b in zip(left.identity[2:], right.identity[2:], strict=True)
            ):
                continue
            differences = sorted(
                key
                for key in left.values.keys() | right.values.keys()
                if key not in ("title", "platform", "region", "edition")
                and key in left.values
                and key in right.values
                and left.values[key] != right.values[key]
            )
            identity_ambiguous = (
                left.ambiguous or right.ambiguous or left.identity != right.identity
            )
            if identity_ambiguous:
                for candidate, other_id in ((left, right.evidence_id), (right, left.evidence_id)):
                    candidate.matches.append((other_id, "ambiguous", "identity"))
            if differences:
                fields = differences
                kind = "conflict"
            elif not identity_ambiguous:
                fields = ["identity"]
                kind = "duplicate"
            else:
                fields = []
                kind = "ambiguous"
            for field_name in fields:
                left.matches.append((right.evidence_id, kind, field_name))
                right.matches.append((left.evidence_id, kind, field_name))
                if kind == "conflict":
                    for candidate in (left, right):
                        candidate.issues.append(
                            Issue(
                                field_name,
                                "unresolved_conflict",
                                "Valores editoriais divergentes sem precedência aprovada.",
                            )
                        )
