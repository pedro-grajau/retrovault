"""Seed arbitrary Commerce facts for published games in the local Sandbox."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import create_engine, text

from app.platform.config.settings import settings


def main() -> None:
    now = datetime.now(UTC)
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    with engine.begin() as connection:
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
                state = "unavailable" if rank % 5 == 0 else "available"
                count = 1 + rank % 3
                connection.execute(text("""
                    INSERT INTO commerce.offers
                        (id, game_id, mode, price_minor, currency, condition_summary,
                         demo_rank, sandbox, created_at)
                    VALUES (:id, :game_id, :mode, :price_minor, 'BRL',
                            'Condição demonstrativa Sandbox; não representa uma unidade real.',
                            :rank, true, :created_at)
                    ON CONFLICT (game_id, mode) DO NOTHING
                """), {
                    "id": offer_id,
                    "game_id": game_id,
                    "mode": mode,
                    "price_minor": 4990 if mode == "purchase" else 990,
                    "rank": rank,
                    "created_at": now,
                })
                for unit_number in range(count):
                    unit_id: UUID = uuid5(
                        NAMESPACE_URL,
                        f"retrovault:sandbox:unit:{game_id}:{mode}:{unit_number}",
                    )
                    connection.execute(text("""
                        INSERT INTO commerce.physical_units (id, offer_id, state, created_at)
                        SELECT :unit_id, id, :state, :created_at
                        FROM commerce.offers WHERE game_id = :game_id AND mode = :mode
                        ON CONFLICT (id) DO NOTHING
                    """), {
                        "unit_id": unit_id,
                        "game_id": game_id,
                        "mode": mode,
                        "state": state,
                        "created_at": now,
                    })
                existing_modes.add((game_id, mode))
    engine.dispose()


if __name__ == "__main__":
    main()
