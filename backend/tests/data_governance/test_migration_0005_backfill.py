import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.platform.config.settings import settings


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_0005_backfills_existing_raw_evidence_bytes(monkeypatch) -> None:
    backend_root = Path(__file__).parents[2]
    admin_url = make_url(settings.database_url)
    test_database = f"migration_{uuid4().hex}"
    maintenance_database = admin_url.set(database="postgres")
    admin_engine = create_engine(
        maintenance_database, isolation_level="AUTOCOMMIT"
    )
    created = False
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{test_database}"'))
        created = True
    except Exception as exc:
        pytest.skip(f"A role PostgreSQL não pode criar banco isolado: {type(exc).__name__}")
    finally:
        admin_engine.dispose()

    database_url = admin_url.set(database=test_database)
    test_engine = create_engine(database_url)
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "app/alembic"))
    monkeypatch.setattr(settings, "database_url", database_url.render_as_string(hide_password=False))
    payload = '{"título":"Jogo migrado"}'
    evidence_id = uuid4()
    try:
        command.upgrade(config, "0004_merge_catalog_heads")
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO data_governance.raw_evidence
                    (id, source, source_record_id, payload_hash, raw_payload,
                     captured_at, preserved_at)
                    VALUES (:id, 'migration-test', 'existing-game', :hash, :payload,
                            now(), now())
                """),
                {
                    "id": evidence_id,
                    "hash": "a" * 64,
                    "payload": payload,
                },
            )

        command.upgrade(config, "head")
        with test_engine.connect() as connection:
            row = connection.execute(
                text("""
                    SELECT raw_payload, raw_payload_bytes
                    FROM data_governance.raw_evidence WHERE id=:id
                """),
                {"id": evidence_id},
            ).one()
        assert row.raw_payload == payload
        assert bytes(row.raw_payload_bytes) == payload.encode("utf-8")
    finally:
        test_engine.dispose()
        if created:
            admin_engine = create_engine(
                maintenance_database, isolation_level="AUTOCOMMIT"
            )
            try:
                with admin_engine.connect() as connection:
                    connection.execute(text(
                        f'REVOKE CONNECT ON DATABASE "{test_database}" FROM public'
                    ))
                    connection.execute(text(f'DROP DATABASE "{test_database}"'))
            finally:
                admin_engine.dispose()
