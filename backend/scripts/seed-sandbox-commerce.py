"""Seed arbitrary Commerce facts for published games in the local Sandbox."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, text

from app.platform.config.settings import settings

MIN_GAME_OFFERS = 60
MAX_GAME_OFFERS = 100


def _unit_facts(unit_number: int) -> tuple[str, list[str], list[str]]:
    conditions = ("Muito bom", "Bom", "Aceitável")
    defects = (
        [],
        ["Marcas superficiais demonstrativas no estojo."],
        [],
        ["Desgaste demonstrativo na etiqueta do cartucho."],
    )[unit_number % 4]
    included_items = (
        ["Cartucho", "Manual demonstrativo"],
        ["Cartucho"],
        ["Cartucho", "Estojo"],
    )[unit_number % 3]
    return conditions[unit_number % len(conditions)], defects, included_items


def _seed_units(
    connection,
    *,
    game_id: UUID,
    offer_id: UUID,
    mode: str,
    rank: int,
    now: datetime,
) -> None:
    state = "unavailable" if rank % 5 == 0 else "available"
    count = 1 + rank % 3
    for unit_number in range(count):
        unit_id: UUID = uuid5(
            NAMESPACE_URL,
            f"retrovault:sandbox:unit:{game_id}:{mode}:{unit_number}",
        )
        condition, defects, included_items = _unit_facts(unit_number)
        connection.execute(text("""
            INSERT INTO commerce.physical_units
                (id, offer_id, state, condition_summary, defects,
                 included_items, created_at)
            VALUES (:unit_id, :offer_id, :state, :condition_summary,
                    CAST(:defects AS jsonb), CAST(:included_items AS jsonb),
                    :created_at)
            ON CONFLICT (id) DO NOTHING
        """), {
            "unit_id": unit_id,
            "offer_id": offer_id,
            "state": state,
            "condition_summary": condition,
            "defects": json.dumps(defects),
            "included_items": json.dumps(included_items),
            "created_at": now,
        })


def main() -> None:
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "retrovault:seed-sandbox-commerce:game-offers-v2"},
        )
        existing_offers = list(connection.execute(text("""
            SELECT id, game_id, mode, demo_rank, sandbox
            FROM commerce.offers
            WHERE game_id IS NOT NULL
            ORDER BY game_id, mode
        """)).mappings())
        for offer in existing_offers:
            if offer["sandbox"]:
                _seed_units(
                    connection,
                    game_id=offer["game_id"],
                    offer_id=offer["id"],
                    mode=offer["mode"],
                    rank=offer["demo_rank"],
                    now=now,
                )
        game_offer_count_before = len(existing_offers)
        game_offer_count = game_offer_count_before
        games = list(
            connection.execute(
                text("""
                    SELECT id FROM catalog.published_games
                    WHERE active
                    ORDER BY id
                """)
            ).scalars()
        )
        existing_modes = {
            (row["game_id"], row["mode"])
            for row in connection.execute(text("""
                SELECT game_id, mode FROM commerce.offers WHERE game_id IS NOT NULL
            """)).mappings()
        }
        ranks_by_game = {
            row["game_id"]: row["demo_rank"]
            for row in connection.execute(text("""
                SELECT game_id, min(demo_rank) AS demo_rank
                FROM commerce.offers
                WHERE game_id IS NOT NULL
                GROUP BY game_id
            """)).mappings()
        }
        next_rank = connection.execute(text("""
            SELECT COALESCE(max(demo_rank), 0) + 1
            FROM commerce.offers WHERE game_id IS NOT NULL
        """)).scalar_one()
        new_game_offers = 0
        if game_offer_count < MIN_GAME_OFFERS:
            offered_game_ids = {game_id for game_id, _ in existing_modes}
            game_order = [
                *[game_id for game_id in games if game_id not in offered_game_ids],
                *[game_id for game_id in games if game_id in offered_game_ids],
            ]
            for game_id in game_order:
                if game_offer_count >= MIN_GAME_OFFERS:
                    break
                rank = ranks_by_game.get(game_id)
                if rank is None:
                    rank = next_rank
                    ranks_by_game[game_id] = rank
                    next_rank += 1
                modes = ("purchase",) if rank % 4 == 0 else ("purchase", "rental")
                for mode in modes:
                    if (game_id, mode) in existing_modes:
                        continue
                    if game_offer_count >= MIN_GAME_OFFERS:
                        break
                    if game_offer_count >= MAX_GAME_OFFERS:
                        break
                    offer_id = uuid5(
                        NAMESPACE_URL, f"retrovault:sandbox:offer:{game_id}:{mode}"
                    )
                    sku_code = f"RV-{game_id.hex.upper()}-{mode[0].upper()}"
                    inserted_offer = connection.execute(text("""
                        INSERT INTO commerce.offers
                            (id, game_id, mode, price_minor, currency, condition_summary,
                             demo_rank, sandbox, sku_code, created_at)
                        VALUES (:id, :game_id, :mode, :price_minor, 'BRL',
                                'Consulte as condições de cada unidade demonstrativa.',
                                :rank, true, :sku_code, :created_at)
                        ON CONFLICT (game_id, mode) DO NOTHING
                        RETURNING id
                    """), {
                        "id": offer_id,
                        "game_id": game_id,
                        "mode": mode,
                        "price_minor": 4990 if mode == "purchase" else 990,
                        "rank": rank,
                        "sku_code": sku_code,
                        "created_at": now,
                    }).scalar_one_or_none()
                    if inserted_offer is None:
                        existing_modes.add((game_id, mode))
                        continue
                    _seed_units(
                        connection,
                        game_id=game_id,
                        offer_id=offer_id,
                        mode=mode,
                        rank=rank,
                        now=now,
                    )
                    existing_modes.add((game_id, mode))
                    game_offer_count += 1
                    new_game_offers += 1
        summary = {
            "game_offer_count_before": game_offer_count_before,
            "new_game_offers": new_game_offers,
            "game_offer_count": game_offer_count,
            "target_minimum": MIN_GAME_OFFERS,
            "maximum": MAX_GAME_OFFERS,
            "within_target_range": MIN_GAME_OFFERS <= game_offer_count <= MAX_GAME_OFFERS,
        }
    engine.dispose()
    sys.stdout.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
