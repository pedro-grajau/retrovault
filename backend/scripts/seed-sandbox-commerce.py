"""Seed arbitrary Commerce facts for published games in the local Sandbox."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, text

from app.platform.config.settings import settings


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
            ON CONFLICT (id) DO UPDATE
            SET offer_id = EXCLUDED.offer_id,
                state = EXCLUDED.state,
                condition_summary = EXCLUDED.condition_summary,
                defects = EXCLUDED.defects,
                included_items = EXCLUDED.included_items
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
        existing_offers = list(connection.execute(text("""
            SELECT id, game_id, mode, demo_rank
            FROM commerce.offers
            ORDER BY game_id, mode
        """)).mappings())
        for offer in existing_offers:
            _seed_units(
                connection,
                game_id=offer["game_id"],
                offer_id=offer["id"],
                mode=offer["mode"],
                rank=offer["demo_rank"],
                now=now,
            )

        games = list(connection.execute(text("""
            SELECT id FROM catalog.published_games
            WHERE active
              AND NOT EXISTS (
                  SELECT 1 FROM commerce.offers o WHERE o.game_id = published_games.id
              )
            ORDER BY id
            LIMIT 80
        """)).scalars())
        existing_modes = {
            (row["game_id"], row["mode"])
            for row in connection.execute(text("SELECT game_id, mode FROM commerce.offers")).mappings()
        }
        ranks_by_game = {
            row["game_id"]: row["demo_rank"]
            for row in connection.execute(text("""
                SELECT game_id, min(demo_rank) AS demo_rank
                FROM commerce.offers GROUP BY game_id
            """)).mappings()
        }
        next_rank = connection.execute(text("""
            SELECT COALESCE(max(demo_rank), 0) + 1 FROM commerce.offers
        """)).scalar_one()
        for game_id in games:
            rank = ranks_by_game.get(game_id)
            if rank is None:
                rank = next_rank
                ranks_by_game[game_id] = rank
                next_rank += 1
            modes = ("purchase",) if rank % 4 == 0 else ("purchase", "rental")
            for mode in modes:
                if (game_id, mode) in existing_modes:
                    continue
                offer_id = uuid5(
                    NAMESPACE_URL, f"retrovault:sandbox:offer:{game_id}:{mode}"
                )
                sku_code = (
                    f"RV-{game_id.hex.upper()}-{mode[0].upper()}"
                )
                connection.execute(text("""
                    INSERT INTO commerce.offers
                        (id, game_id, mode, price_minor, currency, condition_summary,
                         demo_rank, sandbox, sku_code, created_at)
                    VALUES (:id, :game_id, :mode, :price_minor, 'BRL',
                            'Consulte as condições de cada unidade demonstrativa.',
                            :rank, true, :sku_code, :created_at)
                    ON CONFLICT (game_id, mode) DO UPDATE
                    SET sku_code = EXCLUDED.sku_code
                """), {
                    "id": offer_id,
                    "game_id": game_id,
                    "mode": mode,
                    "price_minor": 4990 if mode == "purchase" else 990,
                    "rank": rank,
                    "sku_code": sku_code,
                    "created_at": now,
                })
                _seed_units(
                    connection,
                    game_id=game_id,
                    offer_id=offer_id,
                    mode=mode,
                    rank=rank,
                    now=now,
                )
                existing_modes.add((game_id, mode))
    engine.dispose()


if __name__ == "__main__":
    main()
