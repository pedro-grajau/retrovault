"""Events only match unambiguous consented targets and notices revalidate facts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.modules.concierge.application.demand_notifications import (
    DemandNotificationService,
)
from app.modules.concierge.domain.demand import DemandNotification
from app.platform.outbox.ports import PublicEvent
from tests.concierge.test_demand import register, setup


class Events:
    def __init__(self, event):
        self.event = event
        self.completed = []

    def claim(self, **kwargs):
        return [self.event]

    def finish(self, event, **kwargs):
        self.completed.append(kwargs)


def event(game, *, state="available", version="availability.v1", occurred_at=None):
    event_id = uuid4()
    return PublicEvent(
        event_id,
        f"commerce.{version}",
        game.id,
        {
            "version": version,
            "event_id": str(event_id),
            "game_id": str(game.id),
            "state": state,
            "mode": "purchase",
            "unit_id": str(uuid4()),
            "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        },
        uuid4(),
        1,
        occurred_at or datetime.now(UTC),
    )


def context():
    service = setup()
    d = register(service)
    game = SimpleNamespace(id=uuid4(), title=d.title, platform=d.platform)
    service.catalog.games = [game]
    return service, d, game


def test_absent_title_published_later_only_unequivocal_match_and_replay_dedup():
    service, d, game = context()
    service.offers.units = 1
    events = Events(event(game))
    worker = DemandNotificationService(
        service, events, public_site_url="https://example.test"
    )
    worker.process_events()
    worker.process_events()
    assert len(service.store.queued) == 1
    assert service.store.rows[d.id].game_id == game.id
    notice = DemandNotification(
        uuid4(), service.store.rows[d.id], "owner", events.event.id, uuid4(), 1
    )
    text = worker.prepare(notice)
    assert "49,90" in text and str(game.id) in text and "Sandbox" in text
    service.offers.units = 0
    assert (
        worker.prepare(notice) is None and service.store.rows[d.id].status == "active"
    )


def test_ambiguous_title_never_notifies():
    service, _, game = context()
    service.offers.units = 1
    service.catalog.games.append(
        SimpleNamespace(id=uuid4(), title=game.title, platform=game.platform)
    )
    events = Events(event(game))
    DemandNotificationService(service, events).process_events()
    assert not service.store.queued
    assert events.completed[-1]["retry"] is True


def test_explicit_sale_ends_only_without_other_eligible_unit():
    service, d, game = context()
    events = Events(event(game, state="sold"))
    worker = DemandNotificationService(service, events)
    service.offers.units = 1
    worker.process_events()
    assert service.store.rows[d.id].status == "active"
    service.offers.units = 0
    worker.process_events()
    assert service.store.rows[d.id].status == "sold" and service.store.sold


def test_temporary_unavailability_does_not_infer_sale():
    service, d, game = context()
    events = Events(event(game, state="unavailable"))
    DemandNotificationService(service, events).process_events()
    assert service.store.rows[d.id].status == "active" and not service.store.sold


def test_historical_sale_before_new_consent_does_not_close_interest():
    service, d, game = context()
    events = Events(
        event(game, state="sold", occurred_at=d.consented_at - timedelta(seconds=1))
    )
    DemandNotificationService(service, events).process_events()
    assert service.store.rows[d.id].status == "active" and not service.store.sold


def test_commerce_failure_retries_unknown_version_dead_letters():
    service, _, game = context()
    service.offers.failed = True
    events = Events(event(game))
    DemandNotificationService(service, events).process_events()
    assert events.completed[-1]["retry"] is True and not service.store.queued
    events.event = event(game, version="availability.v99")
    DemandNotificationService(service, events).process_events()
    assert events.completed[-1]["incompatible"] is True


def test_malformed_payloads_dead_letter_and_subsequent_event_progress():
    from dataclasses import replace

    service, d, game = context()
    service.offers.units = 1
    good = event(game)
    malformed = []
    for key, value in [
        ("mode", {}),
        ("state", []),
        ("unit_id", None),
        ("occurred_at", "yesterday"),
        ("occurred_at", "2026-10-10T12:00:00"),
    ]:
        malformed.append(replace(good, payload={**good.payload, key: value}))
    malformed.append(
        replace(
            good,
            payload={
                key: value for key, value in good.payload.items() if key != "unit_id"
            },
        )
    )
    malformed.append(replace(good, payload=[]))
    events = Events(good)
    events.claim = lambda **kwargs: [*malformed, good]
    DemandNotificationService(service, events).process_events()
    assert all(result.get("incompatible") for result in events.completed[:-1])
    assert not events.completed[-1].get("retry") and service.store.queued == {
        (d.id, good.id)
    }


@pytest.mark.parametrize(
    "outcome", ["success", "failure", "suppressed", "cancelled", "ack_failed"]
)
def test_deliver_demand_transport_paths_are_guarded_off_event_loop(
    monkeypatch, outcome
):
    import asyncio
    import threading
    from contextlib import contextmanager

    import pytest
    from pydantic import SecretStr

    import app.main as main
    from app.modules.concierge.domain.demand import DemandsUnavailable

    service, d, game = context()
    notification = DemandNotification(uuid4(), d, "owner", uuid4(), uuid4(), 1)
    actions = []
    loop_thread = threading.get_ident()

    class Repository:
        @contextmanager
        def delivery_guard(self, notification):
            assert threading.get_ident() != loop_thread
            actions.append("guard")
            yield outcome != "cancelled"

        def retry(self, notification, *, suppressed=False):
            assert threading.get_ident() != loop_thread
            actions.append("suppressed" if suppressed else "retry")

        def complete(self, notification):
            assert threading.get_ident() != loop_thread
            actions.append("complete")
            return outcome != "ack_failed"

    class Notices:
        def prepare(self, notification):
            assert threading.get_ident() != loop_thread
            return None if outcome == "suppressed" else "Sandbox notice"

    class Messenger:
        async def send_message(self, chat_id, text):
            assert threading.get_ident() == loop_thread
            await asyncio.sleep(0)
            actions.append("transport")
            if outcome == "failure":
                raise RuntimeError("synthetic transport failure")

    monkeypatch.setattr(main, "_demand_repository", Repository())
    monkeypatch.setattr(main, "_demand_notifications", Notices())
    monkeypatch.setattr(main, "_telegram_messenger", Messenger())
    monkeypatch.setattr(
        main.settings, "pixel_telegram_bot_token", SecretStr("synthetic")
    )

    async def exercise():
        task = asyncio.create_task(main._deliver_demand(notification))
        # Prove the event loop can keep advancing while the guarded worker runs.
        while not task.done():
            await asyncio.sleep(0.01)
        await task

    def run_delivery():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(exercise())
        finally:
            loop.close()

    if outcome == "ack_failed":
        with pytest.raises(DemandsUnavailable, match="acknowledgment_failed"):
            run_delivery()
    else:
        run_delivery()
    expected = {
        "success": ["guard", "transport", "complete"],
        "failure": ["guard", "transport", "retry"],
        "suppressed": ["guard", "suppressed"],
        "cancelled": ["guard"],
        "ack_failed": ["guard", "transport", "complete"],
    }[outcome]
    assert actions == expected
