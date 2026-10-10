"""Real PostgreSQL consent, concurrency, fencing, retention and rollback tests."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.modules.concierge.adapters.postgres_demands import PostgresDemandRepository
from app.platform.config.settings import settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_DB_TESTS") != "1", reason="requires PostgreSQL integration service"
)


@pytest.fixture
def repository():
    engine = create_engine(settings.database_url)
    repo = PostgresDemandRepository(engine)
    marker = uuid4().hex
    yield repo, marker
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM concierge.demands WHERE owner_key=:owner"),
            {"owner": marker},
        )
    engine.dispose()


def propose(repo, owner, update=1):
    return repo.propose(
        owner_key=owner,
        channel="simulator",
        chat_id="sim-owner",
        session_id=uuid4(),
        update_id=update,
        correlation_id=uuid4(),
        title="Chrono Trigger",
        platform="SNES",
        mode="purchase",
        game_id=None,
        context={"version": "demand.v1"},
    )


def active(repo, owner):
    d = propose(repo, owner)
    return repo.confirm(d.id, owner_key=owner, channel="simulator", update_id=2)


def test_real_concurrent_consents_across_sessions_reuse_one_active_record(repository):
    repo, owner = repository
    proposals = [propose(repo, owner, 10 + i) for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(
            pool.map(
                lambda item: (
                    repo.confirm(
                        item[1].id,
                        owner_key=owner,
                        channel="simulator",
                        update_id=100 + item[0],
                    ).id
                ),
                enumerate(proposals),
            )
        )
    assert len(set(ids)) == 1
    assert (
        len(
            [
                d
                for d in repo.list_owned(owner_key=owner, channel="simulator")
                if d.status == "active"
            ]
        )
        == 1
    )
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM concierge.demand_history h JOIN concierge.demands d ON d.id=h.demand_id WHERE d.owner_key=:owner AND h.action='consented'"
                ),
                {"owner": owner},
            ).scalar_one()
            == 1
        )


def test_record_keeps_utc_channel_session_correlation_context_and_retention_independent(
    repository,
):
    repo, owner = repository
    d = active(repo, owner)
    assert d.consented_at is not None
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM concierge.demands WHERE id=:id"), {"id": d.id}
            )
            .mappings()
            .one()
        )
        assert (
            row["session_id"]
            and row["correlation_id"]
            and row["channel"] == "simulator"
        )
        assert (
            row["created_at"].utcoffset() == timedelta(0)
            and row["context"]["version"] == "demand.v1"
        )
    assert repo.cancel(d.id, owner_key="intruder", channel="simulator") is False
    assert repo.cancel(d.id, owner_key=owner, channel="telegram") is False
    assert repo.cancel(d.id, owner_key=owner, channel="simulator") is True
    assert repo.cancel(d.id, owner_key=owner, channel="simulator") is True
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE concierge.demands SET closed_at=now()-interval '31 days' WHERE id=:id"
            ),
            {"id": d.id},
        )
    repo.purge()
    with repo.engine.connect() as conn:
        row = (
            conn.execute(
                text("SELECT * FROM concierge.demands WHERE id=:id"), {"id": d.id}
            )
            .mappings()
            .one()
        )
        assert (
            row["chat_id"] is None
            and row["context"] == {}
            and row["close_reason"] == "owner_cancelled"
        )


def test_delivery_fencing_reclaim_and_first_ack_closes_other_notices(repository):
    repo, owner = repository
    d = active(repo, owner)
    game = uuid4()
    event = uuid4()
    for event_id in (event, event, uuid4()):
        repo.enqueue(d.id, event_id=event_id, game_id=game)
    claimed = repo.claim(now=datetime.now(UTC), channel="simulator")
    claimed = [n for n in claimed if n.demand.id == d.id]
    assert len(claimed) == 2
    first = claimed[0]
    with repo.delivery_guard(first) as valid:
        assert valid
        assert repo.complete(first)
    assert repo.complete(first) is False
    with repo.delivery_guard(claimed[1]) as valid:
        assert valid is False
    assert repo.cancel(d.id, owner_key=owner, channel="simulator") is True
    assert not [
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d.id
    ]


def test_cancel_before_send_invalidates_lease_and_cancel_during_transport_serializes(
    repository,
):
    repo, owner = repository
    d = active(repo, owner)
    repo.enqueue(d.id, event_id=uuid4(), game_id=uuid4())
    n = next(
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d.id
    )
    started, release = Event(), Event()

    def transport():
        with repo.delivery_guard(n) as valid:
            assert valid
            started.set()
            assert release.wait(5)
            assert repo.complete(n)

    with ThreadPoolExecutor(max_workers=2) as pool:
        send = pool.submit(transport)
        assert started.wait(5)
        cancel = pool.submit(repo.cancel, d.id, owner_key=owner, channel="simulator")
        assert not cancel.done()
        release.set()
        send.result(timeout=5)
        assert cancel.result(timeout=5)
    d2 = propose(repo, owner, 20)
    d2 = repo.confirm(d2.id, owner_key=owner, channel="simulator", update_id=21)
    repo.enqueue(d2.id, event_id=uuid4(), game_id=uuid4())
    n2 = next(
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d2.id
    )
    assert repo.cancel(d2.id, owner_key=owner, channel="simulator")
    with repo.delivery_guard(n2) as valid:
        assert valid is False
    assert repo.complete(n2) is False


def test_stale_token_cannot_complete_and_retry_limit_is_dead_letter(repository):
    repo, owner = repository
    d = active(repo, owner)
    repo.enqueue(d.id, event_id=uuid4(), game_id=uuid4())
    stale = next(
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d.id
    )
    with repo.engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE concierge.demand_outbox SET lease_until=now()-interval '1 second' WHERE id=:id"
            ),
            {"id": stale.id},
        )
    fresh = next(
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d.id
    )
    assert stale.lease_token != fresh.lease_token and not repo.complete(stale)
    for _ in range(4):
        repo.retry(fresh)
        with repo.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE concierge.demand_outbox SET available_at=now() WHERE id=:id"
                ),
                {"id": fresh.id},
            )
        claimed = [
            n
            for n in repo.claim(now=datetime.now(UTC), channel="simulator")
            if n.demand.id == d.id
        ]
        if claimed:
            fresh = claimed[0]
    with repo.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT status FROM concierge.demand_outbox WHERE id=:id"),
                {"id": fresh.id},
            ).scalar_one()
            == "dead_letter"
        )


def test_expired_lease_cannot_be_reclaimed_while_guarded_transport_can_complete(
    repository,
):
    repo, owner = repository
    d = active(repo, owner)
    repo.enqueue(d.id, event_id=uuid4(), game_id=uuid4())
    n = next(
        n
        for n in repo.claim(now=datetime.now(UTC), channel="simulator")
        if n.demand.id == d.id
    )
    with repo.delivery_guard(n) as valid:
        assert valid
        with repo.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE concierge.demand_outbox SET lease_until=now()-interval '1 second' WHERE id=:id"
                ),
                {"id": n.id},
            )
        assert not [
            item
            for item in repo.claim(now=datetime.now(UTC), channel="simulator")
            if item.demand.id == d.id
        ]
        assert repo.complete(n)
    assert (
        repo.get_owned(d.id, owner_key=owner, channel="simulator").status == "notified"
    )


def test_notification_crash_reclaims_dead_letter_after_five_attempts(repository):
    repo, owner = repository
    d = active(repo, owner)
    repo.enqueue(d.id, event_id=uuid4(), game_id=uuid4())
    for attempt in range(1, 6):
        n = next(
            n
            for n in repo.claim(now=datetime.now(UTC), channel="simulator")
            if n.demand.id == d.id
        )
        assert n.attempts == attempt
        with repo.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE concierge.demand_outbox SET lease_until=now()-interval '1 second' WHERE id=:id"
                ),
                {"id": n.id},
            )
    assert not [
        item
        for item in repo.claim(now=datetime.now(UTC), channel="simulator")
        if item.demand.id == d.id
    ]
    with repo.engine.connect() as conn:
        row = conn.execute(
            text("SELECT status,attempts FROM concierge.demand_outbox WHERE id=:id"),
            {"id": n.id},
        ).one()
        assert row == ("dead_letter", 5)
