"""Exportar somente métricas agregadas do catálogo publicado local."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from app.platform.config.settings import settings

OPTIONAL_FIELDS = (
    "publisher",
    "developer",
    "genre",
    "description",
    "year",
    "rating",
    "included_items",
)


def _is_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_is_present(item) for item in value)
    return value is not None


def _identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Mn"
    )
    return " ".join(re.findall(r"\w+", without_marks))


def collect_evidence(engine) -> dict[str, Any]:
    with engine.connect() as connection:
        games = list(
            connection.execute(
                text("""
                    SELECT g.id::text AS id, g.source, g.source_record_id, g.version, g.title,
                           g.platform, g.editorial, g.cover_hash,
                           m.attribution AS cover_attribution,
                           m.rights_reference AS cover_rights_reference
                    FROM catalog.published_games g
                    LEFT JOIN catalog.published_media m ON m.content_hash=g.cover_hash
                    WHERE g.active
                    ORDER BY g.id
                """)
            ).mappings()
        )
        offers = list(
            connection.execute(
                text("""
                    SELECT mode, count(*) AS offer_count,
                           count(DISTINCT game_id) AS game_count
                    FROM commerce.offers
                    WHERE game_id IS NOT NULL
                    GROUP BY mode
                    ORDER BY mode
                """)
            ).mappings()
        )
        staging_matches = list(
            connection.execute(
                text("""
                    WITH latest_rule AS (
                        SELECT DISTINCT ON (run_id) run_id, rule_version
                        FROM data_governance.processing_runs
                        ORDER BY run_id, processed_at DESC, rule_version DESC
                    )
                    SELECT m.kind, count(*) AS pair_count,
                           count(DISTINCT m.evidence_id) AS record_count
                    FROM data_governance.staging_matches m
                    JOIN latest_rule lr
                      ON lr.run_id=m.run_id
                     AND lr.rule_version=m.rule_version
                    JOIN data_governance.raw_evidence e ON e.id=m.evidence_id
                    JOIN catalog.published_games g
                      ON g.source=e.source
                     AND g.source_record_id=e.source_record_id
                     AND g.active
                    GROUP BY m.kind
                    ORDER BY m.kind
                """)
            ).mappings()
        )
        source_versions = list(
            connection.execute(
                text("""
                    SELECT DISTINCT ao.source_version
                    FROM catalog.published_games g
                    CROSS JOIN LATERAL jsonb_each(
                        coalesce(g.editorial->'lineage', '{}'::jsonb)
                    ) AS lineage(attribute, origin)
                    JOIN data_governance.attribute_origins ao
                      ON ao.source=g.source
                     AND ao.source_record_id=g.source_record_id
                     AND ao.attribute_path=lineage.attribute
                    WHERE g.active
                    ORDER BY ao.source_version
                """)
            ).scalars()
        )

    platform_counts = Counter(str(game["platform"]) for game in games)
    optional_present = Counter()
    optional_total = len(games) * len(OPTIONAL_FIELDS)
    attribute_count = 0
    lineage_count = 0
    critical_complete_count = 0
    identity_groups: Counter[tuple[str, str, str, str]] = Counter()
    dataset_rows: list[tuple[str, str, int]] = []
    for game in games:
        editorial = game["editorial"] if isinstance(game["editorial"], dict) else {}
        attributes = editorial.get("attributes", {})
        lineage = editorial.get("lineage", {})
        if not isinstance(attributes, dict):
            attributes = {}
        if not isinstance(lineage, dict):
            lineage = {}
        for field in OPTIONAL_FIELDS:
            if _is_present(attributes.get(field)):
                optional_present[field] += 1
        for attribute in attributes:
            attribute_count += 1
            origin = lineage.get(attribute)
            if (
                isinstance(origin, dict)
                and isinstance(origin.get("source"), str)
                and bool(origin["source"].strip())
                and isinstance(origin.get("source_version"), str)
                and bool(origin["source_version"].strip())
                and isinstance(origin.get("source_record_id"), str)
                and bool(origin["source_record_id"].strip())
            ):
                lineage_count += 1
        if (
            _is_present(game["id"])
            and _is_present(game["source"])
            and _is_present(game["source_record_id"])
            and _is_present(game["title"])
            and _is_present(game["platform"])
            and _is_present(game["cover_attribution"])
            and _is_present(game["cover_rights_reference"])
        ):
            critical_complete_count += 1
        region = attributes.get("region")
        edition = attributes.get("edition")
        identity_groups[
            (
                _identity(str(game["title"])),
                _identity(str(game["platform"])),
                _identity(region) if isinstance(region, str) else "",
                _identity(edition) if isinstance(edition, str) else "",
            )
        ] += 1
        dataset_rows.append(
            (str(game["id"]), str(game["source"]), int(game.get("version", 1)))
        )

    game_offer_count = sum(int(row["offer_count"]) for row in offers)
    exact_duplicate_groups = sum(count > 1 for count in identity_groups.values())
    exact_duplicate_excess = sum(max(0, count - 1) for count in identity_groups.values())
    manifest = json.dumps(dataset_rows, separators=(",", ":"), ensure_ascii=True)
    offer_counts = {
        str(row["mode"]): {
            "offers": int(row["offer_count"]),
            "distinct_games": int(row["game_count"]),
        }
        for row in offers
    }
    lineage_percent = round(100 * lineage_count / attribute_count, 2) if attribute_count else None
    optional_percent = (
        round(100 * sum(optional_present.values()) / optional_total, 2)
        if optional_total
        else None
    )

    return {
        "report_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "complete" if games else "inconclusive_no_published_games",
        "scope": "Agregados do catálogo ativo e ofertas de jogos no PostgreSQL local.",
        "dataset": {
            "fingerprint_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "active_published_games": len(games),
            "source_manifest_versions": source_versions,
            "games_by_platform": dict(sorted(platform_counts.items())),
        },
        "game_offers": {
            "count": game_offer_count,
            "by_mode": offer_counts,
            "target_minimum": 60,
            "target_maximum": 100,
            "within_target_range": 60 <= game_offer_count <= 100,
        },
        "quality": {
            "critical_fields_complete": critical_complete_count,
            "critical_fields_total": len(games),
            "critical_completeness_percent": (
                round(100 * critical_complete_count / len(games), 2) if games else None
            ),
            "optional_field_completeness_percent": optional_percent,
            "optional_fields": {
                field: {
                    "present": optional_present[field],
                    "total": len(games),
                    "completeness_percent": (
                        round(100 * optional_present[field] / len(games), 2)
                        if games
                        else None
                    ),
                }
                for field in OPTIONAL_FIELDS
            },
            "published_attributes_with_identified_lineage": lineage_count,
            "published_attribute_count": attribute_count,
            "lineage_completeness_percent": lineage_percent,
            "staging_match_pairs_by_kind": {
                str(row["kind"]): {
                    "pairs": int(row["pair_count"]),
                    "records": int(row["record_count"]),
                }
                for row in staging_matches
            },
            "published_exact_identity_duplicate_groups": exact_duplicate_groups,
            "published_exact_identity_duplicate_excess": exact_duplicate_excess,
        },
        "limitations": [
            "O fingerprint identifica o conjunto publicado ativo, não prova que a carga está completa em relação à API de origem.",
            "Duplicatas publicadas são uma verificação exata de título, plataforma, região e edição normalizados; o sinal de staging é reportado separadamente.",
            "Ofertas contabilizadas são linhas Commerce ligadas a jogos; nenhuma oferta de console é contada.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Exportar métricas agregadas do catálogo local")
    parser.add_argument("--output", type=Path, help="Arquivo JSON local para o relatório")
    args = parser.parse_args()
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        report = collect_evidence(engine)
    finally:
        engine.dispose()
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        args.output.chmod(0o600)
        sys.stdout.write(
            json.dumps({"report": str(args.output), "status": report["status"]})
            + "\n"
        )
    else:
        sys.stdout.write(payload)
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
