"""Transactional unit events, public inbox retries and migration with existing rows."""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from app.modules.commerce.adapters.postgres_availability import (
    PostgresAvailabilityWriter,
)
from app.modules.commerce.domain.availability import UnitAvailabilityChange
from app.platform.config.settings import settings
from app.platform.outbox.postgres_events import PostgresEventReader

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires PostgreSQL integration service"
)


def test_unit_transaction_emits_available_unavailable_and_explicit_sold_with_readable_inbox():
    engine = create_engine(settings.database_url)
    game, offer, unit = uuid4(), uuid4(), uuid4()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO commerce.offers(id,game_id,mode,price_minor,currency,condition_summary,demo_rank,sandbox,created_at,sku_code) VALUES(:offer,:game,'purchase',4990,'BRL','Boa',1,true,now(),:sku)"
                ),
                {"offer": offer, "game": game, "sku": str(offer)},
            )
            conn.execute(
                text(
                    "INSERT INTO commerce.physical_units(id,offer_id,state,created_at,condition_summary,defects,included_items) VALUES(:unit,:offer,'available',now(),'Boa','[]','[]')"
                ),
                {"unit": unit, "offer": offer},
            )
        writer = PostgresAvailabilityWriter(engine)
        assert writer.change(UnitAvailabilityChange(unit, "unavailable", "available"))
        assert writer.change(UnitAvailabilityChange(unit, "sold", "unavailable"))
        assert not writer.change(
            UnitAvailabilityChange(unit, "available", "unavailable")
        )
        with engine.connect() as conn:
            events = (
                conn.execute(
                    text(
                        "SELECT payload FROM platform.outbox_events WHERE aggregate_id=:game ORDER BY created_at"
                    ),
                    {"game": game},
                )
                .scalars()
                .all()
            )
            assert [e["state"] for e in events] == ["available", "unavailable", "sold"]
            assert all(e["version"] == "availability.v1" for e in events)
        consumer = f"test-{uuid4()}"
        reader = PostgresEventReader(engine)
        owned = []
        for _ in range(30):
            claimed = reader.claim(consumer=consumer, limit=100)
            if not claimed:
                break
            for e in claimed:
                if e.aggregate_id == game:
                    owned.append(e)
                reader.finish(e, consumer=consumer, incompatible=e.aggregate_id == game)
        assert len(owned) == 3
        assert not reader.claim(consumer=consumer)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM platform.event_consumptions WHERE consumer=:consumer AND status='dead_letter'"
                    ),
                    {"consumer": consumer},
                ).scalar_one()
                == 3
            )
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM platform.event_consumptions WHERE consumer=:consumer OR event_id IN (SELECT id FROM platform.outbox_events WHERE aggregate_id=:game)"
                ),
                {"consumer": locals().get("consumer", ""), "game": game},
            )
            conn.execute(
                text("DELETE FROM platform.outbox_events WHERE aggregate_id=:game"),
                {"game": game},
            )
            conn.execute(
                text("DELETE FROM commerce.physical_units WHERE offer_id=:offer"),
                {"offer": offer},
            )
            conn.execute(
                text("DELETE FROM commerce.offers WHERE id=:offer"), {"offer": offer}
            )
        engine.dispose()


def test_migration_upgrade_preserves_existing_units_and_downgrade_rolls_back():
    engine = create_engine(settings.database_url)
    module_path = (
        Path(__file__).parents[2] / "app/alembic/versions/0022_unmet_demand.py"
    )
    spec = importlib.util.spec_from_file_location("demand_migration", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            game, offer, unit = uuid4(), uuid4(), uuid4()
            conn.execute(
                text("INSERT INTO commerce.offers(id,game_id,mode,price_minor,currency,condition_summary,demo_rank,sandbox,created_at,sku_code) VALUES(:offer,:game,'purchase',4990,'BRL','Boa',1,true,now(),:sku)"),
                {"offer": offer, "game": game, "sku": str(offer)},
            )
            conn.execute(
                text("INSERT INTO commerce.physical_units(id,offer_id,state,created_at,condition_summary,defects,included_items) VALUES(:unit,:offer,'available',now(),'Boa','[]','[]')"),
                {"unit": unit, "offer": offer},
            )
            before = conn.execute(
                text("SELECT id,offer_id,state,condition_summary,defects,included_items FROM commerce.physical_units ORDER BY id")
            ).all()
            assert any(row.id == unit for row in before)
            with Operations.context(MigrationContext.configure(conn)):
                module.downgrade()
                module.upgrade()
            assert (
                conn.execute(
                    text("SELECT id,offer_id,state,condition_summary,defects,included_items FROM commerce.physical_units ORDER BY id")
                ).all()
                == before
            )
            assert (
                conn.execute(
                    text("SELECT to_regclass('concierge.demands')")
                ).scalar_one()
                == "concierge.demands"
            )
        finally:
            transaction.rollback()
    engine.dispose()

def test_event_crash_reclaims_dead_letter_after_five_attempts():
    import json

    engine = create_engine(settings.database_url)
    event_id, game_id = uuid4(), uuid4()
    consumer = f"crash-regression-{uuid4()}"
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """INSERT INTO platform.outbox_events(id,topic,aggregate_type,aggregate_id,payload,created_at,available_at) VALUES(:id,'commerce.availability.v1','game',:game,CAST(:payload AS jsonb),now(),now())"""
                ),
                {
                    "id": event_id,
                    "game": game_id,
                    "payload": json.dumps({"version": "availability.v1"}),
                },
            )
        reader = PostgresEventReader(engine)
        for attempt in range(1, 6):
            batch = reader.claim(consumer=consumer, limit=1000)
            current = next(e for e in batch if e.id == event_id)
            assert current.attempts == attempt
            for other in batch:
                if other.id != event_id:
                    reader.finish(other, consumer=consumer)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE platform.event_consumptions SET lease_until=now()-interval '1 second' WHERE consumer=:consumer AND event_id=:id"
                    ),
                    {"consumer": consumer, "id": event_id},
                )
        assert not [
            e for e in reader.claim(consumer=consumer, limit=1000) if e.id == event_id
        ]
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT status,attempts FROM platform.event_consumptions WHERE consumer=:consumer AND event_id=:id"
                ),
                {"consumer": consumer, "id": event_id},
            ).one() == ("dead_letter", 5)
    finally:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM platform.event_consumptions WHERE consumer=:consumer OR event_id=:id"
                ),
                {"consumer": consumer, "id": event_id},
            )
            conn.execute(
                text("DELETE FROM platform.outbox_events WHERE id=:id"),
                {"id": event_id},
            )
        engine.dispose()
