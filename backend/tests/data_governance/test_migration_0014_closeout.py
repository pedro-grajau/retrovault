import os
import runpy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.platform.config.settings import settings


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL isolado do compose não está ativo",
)
def test_closeout_migrations_preserve_commerce_and_guard_staging_truncate(
    monkeypatch,
) -> None:
    backend_root = Path(__file__).parents[2]
    admin_url = make_url(settings.database_url)
    test_database = f"closeout_{uuid4().hex}"
    maintenance_database = admin_url.set(database="postgres")
    admin_engine = create_engine(maintenance_database, isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{test_database}"'))
        created = True
    except DBAPIError as exc:
        sqlstate = getattr(exc.orig, "sqlstate", None) or getattr(
            exc.orig, "pgcode", None
        )
        if sqlstate != "42501":
            raise
        pytest.skip(f"A role PostgreSQL não pode criar banco isolado: {type(exc).__name__}")
    finally:
        admin_engine.dispose()

    database_url = admin_url.set(database=test_database)
    test_engine = create_engine(database_url)
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "app/alembic"))
    monkeypatch.setattr(
        settings,
        "database_url",
        database_url.render_as_string(hide_password=False),
    )
    game_id, offer_id = uuid4(), uuid4()
    unit_id = uuid5(
        NAMESPACE_URL,
        f"retrovault:sandbox:unit:{game_id}:purchase:0",
    )
    try:
        command.upgrade(config, "0012_catalog_title_search")
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO commerce.offers
                    (id, game_id, mode, price_minor, currency, condition_summary,
                     demo_rank, sandbox, created_at)
                    VALUES (:id, :game, 'purchase', 4990, 'BRL', 'Condição existente',
                            1, true, now())
                """),
                {"id": offer_id, "game": game_id},
            )
            connection.execute(
                text("""
                    INSERT INTO commerce.physical_units (id, offer_id, state, created_at)
                    VALUES (:id, :offer, 'available', now())
                """),
                {"id": unit_id, "offer": offer_id},
            )

        command.upgrade(config, "head")
        with test_engine.begin() as connection:
            migrated = connection.execute(
                text("""
                    SELECT o.sku_code, o.mode, o.price_minor, o.currency,
                           o.demo_rank, o.sandbox, u.condition_summary,
                           u.defects, u.included_items, u.state
                    FROM commerce.offers o
                    JOIN commerce.physical_units u ON u.offer_id=o.id
                    WHERE o.id=:offer AND u.id=:unit
                """),
                {"offer": offer_id, "unit": unit_id},
            ).one()
            connection.execute(
                text("""
                    UPDATE commerce.physical_units
                    SET state='unavailable', condition_summary='Fato editado',
                        defects='["Defeito editado"]'::jsonb,
                        included_items='["Item editado"]'::jsonb
                    WHERE id=:id
                """),
                {"id": unit_id},
            )

        assert migrated.sku_code == f"RV-{game_id.hex.upper()}-P"
        assert migrated.mode == "purchase"
        assert migrated.price_minor == 4990
        assert migrated.currency == "BRL"
        assert migrated.demo_rank == 1
        assert migrated.sandbox is True
        assert migrated.condition_summary == "Condição existente"
        assert migrated.defects is None and migrated.included_items is None

        seed_main = runpy.run_path(str(backend_root / "scripts/seed-sandbox-commerce.py"))["main"]
        seed_main()
        with test_engine.connect() as connection:
            facts = connection.execute(
                text("""
                    SELECT o.mode, o.price_minor, o.currency, o.demo_rank, o.sandbox,
                           u.state, u.condition_summary, u.defects, u.included_items
                    FROM commerce.physical_units u
                    JOIN commerce.offers o ON o.id=u.offer_id WHERE u.id=:id
                """),
                {"id": unit_id},
            ).one()
        assert facts.state == "unavailable"
        assert facts.condition_summary == "Fato editado"
        assert facts.defects == ["Defeito editado"]
        assert facts.included_items == ["Item editado"]
        assert facts.mode == "purchase"
        assert facts.price_minor == 4990
        assert facts.currency == "BRL"
        assert facts.demo_rank == 1
        assert facts.sandbox is True

        for table in (
            "private_media",
            "processing_runs",
            "staging_records",
            "staging_values",
            "staging_matches",
            "quarantine_issues",
        ):
            with pytest.raises(DBAPIError):
                with test_engine.begin() as connection:
                    connection.execute(text(f"TRUNCATE data_governance.{table}"))
    finally:
        test_engine.dispose()
        if created:
            admin_engine = create_engine(
                maintenance_database, isolation_level="AUTOCOMMIT"
            )
            try:
                with admin_engine.connect() as connection:
                    connection.execute(
                        text(f'REVOKE CONNECT ON DATABASE "{test_database}" FROM public')
                    )
                    connection.execute(text(f'DROP DATABASE "{test_database}"'))
            finally:
                admin_engine.dispose()
