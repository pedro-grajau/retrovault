import os
import runpy
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app.modules.concierge.adapters.postgres_sessions import (
    PostgresSessionRepository,
    SessionLeaseLost,
)
from app.modules.concierge.domain.session import IncomingMessage
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
    legacy_session_id = uuid4()
    legacy_processing_message_id = uuid4()
    legacy_update_id = 7001
    legacy_user_id = f"migration-user-{uuid4().hex}"
    legacy_correlation_id = uuid4()
    media_hash = "a" * 64
    published_game_ids = [
        uuid5(NAMESPACE_URL, f"retrovault:closeout:published-game:{index}")
        for index in range(70)
    ]
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

        command.upgrade(config, "0015_concierge_sessions")
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO concierge.sessions (
                        id, channel, external_user_id, external_chat_id,
                        context_game_id, status, workflow_version, correlation_id,
                        started_at, updated_at, last_message_at, last_message_id
                    ) VALUES (
                        :id, 'telegram', :user_id, :chat_id, NULL, 'active',
                        '2.1.v1', :correlation_id, now(), now(), now(), 7001
                    )
                """),
                {
                    "id": legacy_session_id,
                    "user_id": legacy_user_id,
                    "chat_id": legacy_user_id,
                    "correlation_id": legacy_correlation_id,
                },
            )
            connection.execute(
                text("""
                    INSERT INTO concierge.messages (
                        id, session_id, channel, external_update_id,
                        external_message_id, external_user_id, external_chat_id,
                        text, sent_at, received_at, status, lease_until,
                        correlation_id
                    ) VALUES (
                        :id, :session_id, 'telegram', :update_id, 7001,
                        :user_id, :chat_id, 'Mensagem em processamento',
                        now(), now(), 'processing', NULL, :correlation_id
                    )
                """),
                {
                    "id": legacy_processing_message_id,
                    "session_id": legacy_session_id,
                    "update_id": legacy_update_id,
                    "user_id": legacy_user_id,
                    "chat_id": legacy_user_id,
                    "correlation_id": legacy_correlation_id,
                },
            )

        command.upgrade(config, "head")
        with test_engine.connect() as connection:
            backfilled_lease = connection.execute(
                text("""
                    SELECT lease_token, lease_until
                    FROM concierge.messages WHERE id=:id
                """),
                {"id": legacy_processing_message_id},
            ).one()
        assert backfilled_lease.lease_token is not None
        assert backfilled_lease.lease_until is not None

        # Exercise actual advisory-lock exclusion on separate PostgreSQL
        # connections, matching workers running in different app processes.
        repository = PostgresSessionRepository(test_engine)
        lock_session_id = uuid4()
        attempting, acquired = Event(), Event()

        def acquire_session_lock() -> None:
            attempting.set()
            with repository.session_processing_lock(lock_session_id):
                acquired.set()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with repository.session_processing_lock(lock_session_id):
                lock_future = pool.submit(acquire_session_lock)
                assert attempting.wait(timeout=2)
                assert not acquired.wait(timeout=0.1)
            lock_future.result(timeout=5)
        assert acquired.is_set()

        # A reclaimed message lease fences the old worker; the current worker
        # can complete and its Telegram outbox entry is claimable from PostgreSQL.
        now = datetime.now(UTC)
        telegram_message = IncomingMessage(
            channel="telegram",
            external_user_id=f"repo-user-{uuid4().hex}",
            external_chat_id=f"repo-chat-{uuid4().hex}",
            update_id=7101,
            message_id=7101,
            text="Mensagem de integração",
            sent_at=now,
            received_at=now,
            correlation_id=uuid4(),
        )
        first_claim = repository.claim_message(
            telegram_message,
            safe_text=telegram_message.text,
            context_game_id=None,
            now=now,
            max_age_seconds=900,
        )
        assert first_claim.processing_lease_token is not None
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE concierge.messages SET lease_until=:expired_at
                    WHERE channel=:channel AND external_update_id=:update_id
                """),
                {
                    "expired_at": now - timedelta(seconds=1),
                    "channel": telegram_message.channel,
                    "update_id": telegram_message.update_id,
                },
            )
        reclaimed_claim = repository.claim_message(
            telegram_message,
            safe_text=telegram_message.text,
            context_game_id=None,
            now=now + timedelta(minutes=3),
            max_age_seconds=900,
        )
        assert reclaimed_claim.processing_lease_token is not None
        assert reclaimed_claim.processing_lease_token != first_claim.processing_lease_token
        assert reclaimed_claim.session_id == first_claim.session_id
        assert first_claim.session_id is not None
        assert not repository.is_processing_lease_current(
            telegram_message.update_id,
            channel=telegram_message.channel,
            session_id=first_claim.session_id,
            processing_lease_token=first_claim.processing_lease_token,
            now=now + timedelta(minutes=3),
        )
        assert repository.is_processing_lease_current(
            telegram_message.update_id,
            channel=telegram_message.channel,
            session_id=reclaimed_claim.session_id,
            processing_lease_token=reclaimed_claim.processing_lease_token,
            now=now + timedelta(minutes=3),
        )
        with pytest.raises(SessionLeaseLost):
            repository.complete_message(
                telegram_message.update_id,
                channel=telegram_message.channel,
                session_id=first_claim.session_id,
                processing_lease_token=first_claim.processing_lease_token,
                reply_text="Resposta antiga",
                workflow_version="2.1.v1",
            )
        repository.complete_message(
            telegram_message.update_id,
            channel=telegram_message.channel,
            session_id=reclaimed_claim.session_id,
            processing_lease_token=reclaimed_claim.processing_lease_token,
            reply_text="Resposta atual",
            workflow_version="2.1.v1",
        )

        simulator_message = IncomingMessage(
            channel="simulator",
            external_user_id=f"sim-user-{uuid4().hex}",
            external_chat_id=f"sim-chat-{uuid4().hex}",
            update_id=7201,
            message_id=7201,
            text="Simulação sem Telegram",
            sent_at=now,
            received_at=now,
            correlation_id=uuid4(),
        )
        simulator_claim = repository.claim_message(
            simulator_message,
            safe_text=simulator_message.text,
            context_game_id=None,
            now=now,
            max_age_seconds=900,
        )
        assert simulator_claim.session_id is not None
        assert simulator_claim.processing_lease_token is not None
        repository.complete_message(
            simulator_message.update_id,
            channel=simulator_message.channel,
            session_id=simulator_claim.session_id,
            processing_lease_token=simulator_claim.processing_lease_token,
            reply_text="Resposta simulada",
            workflow_version="2.1.v1",
        )
        pending = repository.claim_pending_replies(
            now=datetime.now(UTC) + timedelta(seconds=1), limit=20
        )
        assert [reply.update_id for reply in pending] == [
            telegram_message.update_id
        ]
        assert pending[0].channel == "telegram"
        assert repository.release_reply(
            telegram_message.update_id,
            channel="telegram",
            lease_token=pending[0].lease_token,
        )
        assert repository.claim_pending_replies(
            now=datetime.now(UTC) + timedelta(seconds=1), limit=20
        ) == []
        retried = repository.claim_pending_replies(
            now=datetime.now(UTC) + timedelta(seconds=31), limit=20
        )
        assert [reply.update_id for reply in retried] == [
            telegram_message.update_id
        ]
        assert retried[0].lease_token != pending[0].lease_token
        assert not repository.mark_reply_delivered(
            telegram_message.update_id,
            channel="telegram",
            lease_token=pending[0].lease_token,
        )
        assert repository.mark_reply_delivered(
            telegram_message.update_id,
            channel="telegram",
            lease_token=retried[0].lease_token,
        )

        # A row lock lets a refresh that began first win; the purge's delete
        # rechecks updated_at and must leave that refreshed session in place.
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    UPDATE concierge.sessions
                    SET updated_at=now() - INTERVAL '31 days'
                    WHERE id=:session_id
                """),
                {"session_id": legacy_session_id},
            )
        refresh_connection = test_engine.connect()
        refresh_transaction = refresh_connection.begin()
        try:
            refresh_connection.execute(
                text("SELECT id FROM concierge.sessions WHERE id=:id FOR UPDATE"),
                {"id": legacy_session_id},
            )
            refresh_connection.execute(
                text("UPDATE concierge.sessions SET updated_at=now() WHERE id=:id"),
                {"id": legacy_session_id},
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                purge_future = pool.submit(repository.purge_expired_sessions, 30)
                refresh_transaction.commit()
                assert purge_future.result(timeout=5) == 0
        finally:
            refresh_connection.close()
        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO catalog.published_media
                    (content_hash, content, content_type, attribution,
                     rights_reference, created_at)
                    VALUES (:hash, :content, 'image/png', 'fixture', 'fixture', now())
                """),
                {"hash": media_hash, "content": b"fixture"},
            )
            connection.execute(
                text("""
                    INSERT INTO catalog.published_games
                    (id, source, source_record_id, title, platform, editorial,
                     version, etag, active, cover_hash, updated_at)
                    VALUES (:id, 'fixture', :record_id, :title, 'SNES', '{}'::jsonb,
                            1, :etag, true, :cover_hash, now())
                """),
                [
                    {
                        "id": published_game_id,
                        "record_id": str(index),
                        "title": f"Jogo de fixture {index}",
                        "etag": f'"fixture-{index}"',
                        "cover_hash": media_hash,
                    }
                    for index, published_game_id in enumerate(published_game_ids)
                ],
            )
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
            offer_count = connection.execute(
                text("SELECT count(*) FROM commerce.offers WHERE game_id IS NOT NULL")
            ).scalar_one()
            facts = connection.execute(
                text("""
                    SELECT o.mode, o.price_minor, o.currency, o.demo_rank, o.sandbox,
                           u.state, u.condition_summary, u.defects, u.included_items
                    FROM commerce.physical_units u
                    JOIN commerce.offers o ON o.id=u.offer_id WHERE u.id=:id
                """),
                {"id": unit_id},
            ).one()
        assert 60 <= offer_count <= 100
        assert facts.state == "unavailable"
        assert facts.condition_summary == "Fato editado"
        assert facts.defects == ["Defeito editado"]
        assert facts.included_items == ["Item editado"]
        assert facts.mode == "purchase"
        assert facts.price_minor == 4990
        assert facts.currency == "BRL"
        assert facts.demo_rank == 1
        assert facts.sandbox is True

        seed_main()
        with test_engine.connect() as connection:
            repeated_count = connection.execute(
                text("SELECT count(*) FROM commerce.offers WHERE game_id IS NOT NULL")
            ).scalar_one()
        assert repeated_count == offer_count

        with test_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO commerce.offers
                    (id, game_id, mode, price_minor, currency, condition_summary,
                     demo_rank, sandbox, sku_code, created_at)
                    VALUES (:id, :game_id, 'purchase', 4990, 'BRL', 'Oferta existente',
                            :rank, true, :sku_code, now())
                """),
                [
                    {
                        "id": uuid5(NAMESPACE_URL, f"retrovault:closeout:extra-offer:{index}"),
                        "game_id": uuid5(NAMESPACE_URL, f"retrovault:closeout:extra-game:{index}"),
                        "rank": 1000 + index,
                        "sku_code": f"RV-CLOSEOUT-EXTRA-{index}",
                    }
                    for index in range(41)
                ],
            )

        seed_main()
        with test_engine.connect() as connection:
            over_limit_count = connection.execute(
                text("SELECT count(*) FROM commerce.offers WHERE game_id IS NOT NULL")
            ).scalar_one()
        assert over_limit_count == 101

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
