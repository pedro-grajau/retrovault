"""PostgreSQL adapter for reading offers and available physical units."""

from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.modules.commerce.domain.offers import Offer
from app.modules.commerce.ports.offers import CommerceReadUnavailable


class PostgresOfferReader:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def list_offers(self, game_ids: list[UUID]) -> dict[UUID, list[Offer]]:
        if not game_ids:
            return {}
        query = text("""
            SELECT o.id, o.game_id, o.mode, o.price_minor, o.currency,
                   o.condition_summary, o.demo_rank, o.sandbox,
                   count(u.id) FILTER (WHERE u.state = 'available') AS available_units
            FROM commerce.offers o
            LEFT JOIN commerce.physical_units u ON u.offer_id = o.id
            WHERE o.game_id = ANY(CAST(:game_ids AS uuid[]))
            GROUP BY o.id, o.game_id, o.mode, o.price_minor, o.currency,
                     o.condition_summary, o.demo_rank, o.sandbox
            ORDER BY o.demo_rank, o.game_id, o.mode
        """)
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query, {"game_ids": game_ids}).mappings()
                result: dict[UUID, list[Offer]] = {}
                for row in rows:
                    offer = Offer(
                        id=row["id"],
                        game_id=row["game_id"],
                        mode=row["mode"],
                        price_minor=row["price_minor"],
                        currency=row["currency"],
                        condition_summary=row["condition_summary"],
                        available_units=row["available_units"],
                        demo_rank=row["demo_rank"],
                        sandbox=row["sandbox"],
                    )
                    result.setdefault(offer.game_id, []).append(offer)
        except SQLAlchemyError as exc:
            raise CommerceReadUnavailable from exc
        return result
