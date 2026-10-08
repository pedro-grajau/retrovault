"""PostgreSQL integration coverage for idempotent AI operation reservations."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from app.modules.concierge.adapters.postgres_ai_ledger import PostgresAiLedger
from app.modules.concierge.domain.ai_ledger import billing_period_start
from app.platform.config.settings import settings


@pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1",
    reason="PostgreSQL efêmero do compose não está ativo",
)
def test_postgres_ledger_reserves_each_operation_once_per_update() -> None:
    engine = create_engine(settings.database_url)
    connection = engine.connect()
    transaction = connection.begin()

    @contextmanager
    def use_seeded_transaction():
        yield connection

    class TransactionalEngine:
        begin = staticmethod(use_seeded_transaction)

    try:
        ledger = PostgresAiLedger(TransactionalEngine())  # type: ignore[arg-type]
        now = datetime.now(UTC)
        common = {
            "session_id": uuid4(),
            "channel": "telegram",
            "update_id": int(now.timestamp() * 1000) % (2**63 - 1),
            "period_start": billing_period_start(now),
            "model_snapshot": "test-model",
            "prompt_version": "test.v1",
            "workflow_version": "2.3.v1",
            "configuration_version": "test.v1",
            "input_token_limit": 1000,
            "output_token_limit": 100,
            "reserved_cost_usd": Decimal("0.00100000"),
            "monthly_budget_usd": Decimal("25.00"),
            "now": now,
            "expires_at": now + timedelta(minutes=15),
        }

        intent = ledger.reserve(
            **common,
            operation="intent_extraction",
            correlation_id=uuid4(),
        )
        ranking = ledger.reserve(
            **common,
            operation="recommendation_ranking",
            correlation_id=uuid4(),
        )
        intent_retry = ledger.reserve(
            **common,
            operation="intent_extraction",
            correlation_id=uuid4(),
        )
        ranking_retry = ledger.reserve(
            **common,
            operation="recommendation_ranking",
            correlation_id=uuid4(),
        )

        assert intent.allowed and intent.created
        assert ranking.allowed and ranking.created
        assert intent.reservation_id != ranking.reservation_id
        assert not intent_retry.allowed and not intent_retry.created
        assert not ranking_retry.allowed and not ranking_retry.created
        assert intent_retry.reservation_id == intent.reservation_id
        assert ranking_retry.reservation_id == ranking.reservation_id
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()
