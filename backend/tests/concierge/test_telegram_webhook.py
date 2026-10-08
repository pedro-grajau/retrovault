from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from app.modules.concierge.adapters.simulator import InMemorySessionStore
from app.modules.concierge.adapters.telegram_bot import (
    TelegramBotClient,
    TelegramUnavailable,
)
from app.modules.concierge.api import router as concierge_router
from app.modules.concierge.application.context_reference import ContextReferenceService
from app.modules.concierge.application.session_workflow import SessionWorkflow
from app.modules.concierge.domain.session import OutboxReply


class Catalog:
    def __init__(self, game_id=None) -> None:  # type: ignore[no-untyped-def]
        self.game_id = game_id

    def get_game(self, game_id):  # type: ignore[no-untyped-def]
        return object() if game_id == self.game_id else None


class Messenger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.actions: list[tuple[str, str]] = []
        self.fail_next_message = False

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> None:
        self.actions.append((chat_id, action))

    async def send_message(self, chat_id: str, text: str) -> None:
        if self.fail_next_message:
            self.fail_next_message = False
            raise TelegramUnavailable("telegram_delivery_failed")
        self.messages.append((chat_id, text))


@pytest.fixture
def webhook(monkeypatch):  # type: ignore[no-untyped-def]
    game_id = uuid4()
    store = InMemorySessionStore()
    messenger = Messenger()
    checkpointer = InMemorySaver()

    @contextmanager
    def checkpointer_factory():
        yield checkpointer

    service = ContextReferenceService(Catalog(game_id), "test-secret", 1800)
    workflow = SessionWorkflow(store, service, checkpointer_factory)
    monkeypatch.setattr(concierge_router, "_context_references", service)
    monkeypatch.setattr(concierge_router, "_telegram_bot_username", "PixelTestBot")
    monkeypatch.setattr(concierge_router, "_session_store", store)
    monkeypatch.setattr(concierge_router, "_session_workflow", workflow)
    monkeypatch.setattr(concierge_router, "_telegram_messenger", messenger)
    monkeypatch.setattr(
        concierge_router,
        "_telegram_updates",
        concierge_router.TelegramUpdateAdapter(frozenset({12345})),
    )
    monkeypatch.setattr(concierge_router, "_webhook_secret", "test-webhook-secret")
    monkeypatch.setattr(concierge_router, "_telegram_bot_token_configured", True)
    monkeypatch.setattr(concierge_router, "_allowed_user_ids", frozenset({12345}))
    monkeypatch.setattr(concierge_router, "_webhook_max_body_bytes", 65536)
    monkeypatch.setattr(concierge_router, "_typing_threshold_seconds", 3.0)
    monkeypatch.setattr(concierge_router, "_retention_days", 30)
    monkeypatch.setattr(concierge_router, "_retention_checked_at", time.monotonic())
    app = FastAPI()
    app.include_router(concierge_router.router)
    return app, store, messenger, game_id


def telegram_update(
    *,
    update_id: int = 1,
    message_id: int = 1,
    text: str = "Oi, Pixel",
    sent_at: int | None = None,
    user_id: int = 12345,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(time.time()) if sent_at is None else sent_at,
            "text": text,
            "chat": {
                "id": user_id if chat_id is None else chat_id,
                "type": chat_type,
            },
            "from": {"id": user_id, "is_bot": False},
        },
    }


async def post_update(
    app: FastAPI,
    update: object,
    *,
    secret: str = "test-webhook-secret",
    raw: bytes | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
        if raw is not None:
            headers["Content-Type"] = "application/json"
            return await client.post(
                "/api/v1/concierge/telegram/webhook", content=raw, headers=headers
            )
        return await client.post(
            "/api/v1/concierge/telegram/webhook", json=update, headers=headers
        )


@pytest.mark.anyio
async def test_telegram_bot_client_uses_test_transport_without_network_calls() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = TelegramBotClient(
        "123456:bot-secret", transport=httpx.MockTransport(handler)
    )

    await client.send_chat_action("12345")
    await client.send_message("12345", "Oi, Pixel")

    assert len(requests) == 2
    assert requests[0].url.path.endswith("/sendChatAction")
    assert requests[0].read() == b'{"chat_id":"12345","action":"typing"}'
    assert requests[1].url.path.endswith("/sendMessage")
    assert requests[1].read() == b'{"chat_id":"12345","text":"Oi, Pixel"}'


@pytest.mark.anyio
async def test_telegram_bot_client_rejects_provider_errors_without_exposing_token() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "error"})

    client = TelegramBotClient(
        "123456:bot-secret", transport=httpx.MockTransport(handler)
    )

    with pytest.raises(TelegramUnavailable, match="telegram_delivery_failed") as error:
        await client.send_message("12345", "Oi")

    assert "bot-secret" not in str(error.value)


@pytest.mark.anyio
async def test_oversized_telegram_update_is_rejected_before_persistence(webhook) -> None:
    app, store, messenger, _ = webhook

    response = await post_update(app, None, raw=b" " * 65537)

    assert response.status_code == 413
    assert store._sessions == {}
    assert messenger.messages == []


@pytest.mark.anyio
async def test_valid_private_allowlisted_update_starts_session_and_sends_pixel_greeting(
    webhook,
) -> None:
    app, store, messenger, game_id = webhook
    reference, _ = ContextReferenceService(Catalog(game_id), "test-secret", 1800).create_token(
        game_id
    )

    response = await post_update(
        app, telegram_update(text=f"/start {reference}")
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "claimed"}
    assert len(store._sessions) == 1
    session = next(iter(store._sessions.values()))
    assert session["context_game_id"] == game_id
    assert "Pixel" in messenger.messages[0][1]
    assert "IA" in messenger.messages[0][1]
    assert "Sandbox" in messenger.messages[0][1]
    assert messenger.actions == []


@pytest.mark.anyio
async def test_invalid_secret_never_creates_a_session(webhook) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook

    response = await post_update(app, telegram_update(), secret="wrong")

    assert response.status_code == 401
    assert store._sessions == {}
    assert messenger.messages == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "update",
    [
        telegram_update(chat_type="group"),
        telegram_update(user_id=88888),
        telegram_update(chat_id=54321),
        telegram_update(sent_at=int(time.time()) + 300),
        telegram_update(text="x" * 4097),
        telegram_update(update_id=2**63),
        telegram_update(message_id=2**63),
        {"update_id": True, "message": {}},
        {"update_id": 5, "message": {"date": "today"}},
    ],
)
async def test_unsupported_or_invalid_update_is_ignored_without_session(
    webhook, update
) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook

    response = await post_update(app, update)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert store._sessions == {}
    assert messenger.messages == []


@pytest.mark.anyio
async def test_replayed_update_does_not_create_duplicate_reply(webhook) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook
    update = telegram_update(update_id=22)

    first = await post_update(app, update)
    replay = await post_update(app, update)
    altered_replay = await post_update(
        app, telegram_update(update_id=22, message_id=999, text="Mensagem alterada")
    )

    assert first.status_code == replay.status_code == 200
    assert first.json()["status"] == "claimed"
    assert replay.json()["status"] == "duplicate"
    assert altered_replay.json()["status"] == "reconciliation"
    assert len(store._sessions) == 1
    assert len(messenger.messages) == 1
    assert len(store._replay_anomalies) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("start_command", ["/start", "/start@PixelTestBot"])
async def test_context_reference_is_consumed_by_first_valid_start(
    webhook, start_command: str
) -> None:
    app, store, messenger, game_id = webhook
    reference, _ = ContextReferenceService(
        Catalog(game_id), "test-secret", 1800
    ).create_token(game_id)

    first = await post_update(
        app,
        telegram_update(
            update_id=24,
            message_id=1,
            text=f"{start_command} {reference}",
        ),
    )
    store._sessions[("telegram", "12345")]["status"] = "terminal"
    replay = await post_update(
        app,
        telegram_update(update_id=25, message_id=2, text=f"/start {reference}"),
    )

    assert first.status_code == replay.status_code == 200
    assert store._sessions[("telegram", "12345")]["context_game_id"] is None
    assert "link do jogo não pôde ser validado" in messenger.messages[1][1]
    assert len(store._context_reference_uses) == 1


@pytest.mark.anyio
async def test_webhook_ignores_json_integer_exceeding_python_digit_limit(webhook) -> None:
    app, store, messenger, _ = webhook

    response = await post_update(
        app,
        {},
        raw=b'{"update_id":' + (b"9" * 5000) + b"}",
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert store._sessions == {}
    assert messenger.messages == []


@pytest.mark.anyio
async def test_stale_and_out_of_order_updates_only_enter_reconciliation(webhook) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook
    stale = await post_update(
        app,
        telegram_update(update_id=30, sent_at=int(time.time()) - 901),
    )
    first = await post_update(
        app,
        telegram_update(update_id=31, message_id=5),
    )
    out_of_order = await post_update(
        app,
        telegram_update(update_id=32, message_id=4),
    )

    assert stale.json()["status"] == "reconciliation"
    assert first.json()["status"] == "claimed"
    assert out_of_order.json()["status"] == "reconciliation"
    assert len(store._sessions) == 1
    assert len(messenger.messages) == 1


@pytest.mark.anyio
async def test_failed_telegram_send_is_retried_from_durable_pending_reply(webhook) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook
    messenger.fail_next_message = True
    update = telegram_update(update_id=40)

    failed = await post_update(app, update)
    store._messages[("telegram", 40)]["delivery_lease_until"] = (
        datetime.now(UTC) - timedelta(seconds=1)
    )
    retried = await post_update(app, update)

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert retried.json()["status"] == "duplicate"
    assert len(store._sessions) == 1
    assert len(messenger.messages) == 1


@pytest.mark.anyio
async def test_slow_processing_sends_typing_action(webhook, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, store, messenger, _ = webhook
    workflow = concierge_router._session_workflow
    assert workflow is not None
    original = workflow.handle

    def slow_handle(message):  # type: ignore[no-untyped-def]
        time.sleep(0.03)
        return original(message)

    monkeypatch.setattr(workflow, "handle", slow_handle)
    monkeypatch.setattr(concierge_router, "_typing_threshold_seconds", 0.001)

    response = await post_update(app, telegram_update(update_id=50))

    assert response.status_code == 200
    assert messenger.actions == [("12345", "typing")]
    assert len(store._sessions) == 1


@pytest.mark.anyio
async def test_slow_retention_cleanup_is_inside_typing_deadline(webhook, monkeypatch) -> None:
    app, store, messenger, _ = webhook
    original = store.purge_expired_sessions

    def slow_purge(retention_days: int) -> int:
        time.sleep(0.03)
        return original(retention_days)

    monkeypatch.setattr(store, "purge_expired_sessions", slow_purge)
    monkeypatch.setattr(concierge_router, "_retention_checked_at", 0.0)
    monkeypatch.setattr(concierge_router, "_typing_threshold_seconds", 0.001)

    response = await post_update(app, telegram_update(update_id=51))

    assert response.status_code == 200
    assert messenger.actions == [("12345", "typing")]
    assert len(store._sessions) == 1


@pytest.mark.anyio
async def test_deeply_nested_json_is_acknowledged_as_ignored(webhook) -> None:
    app, store, messenger, _ = webhook
    nested = b"[" * 1200 + b"0" + b"]" * 1200

    response = await post_update(app, None, raw=nested)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ignored"}
    assert store._sessions == {}
    assert messenger.messages == []


@pytest.mark.anyio
async def test_oversized_body_is_rejected_before_storage(webhook, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    app, store, _, _ = webhook
    monkeypatch.setattr(concierge_router, "_webhook_max_body_bytes", 64)

    response = await post_update(app, telegram_update(), raw=b" " * 65)

    assert response.status_code == 413
    assert store._sessions == {}


@pytest.mark.anyio
async def test_app_lifespan_redrives_pending_reply_and_marks_it_delivered(
    monkeypatch,
) -> None:
    from app import main as app_main

    reply = OutboxReply(
        id=uuid4(),
        channel="telegram",
        update_id=9901,
        chat_id="12345",
        text="Resposta pendente",
        lease_token=uuid4(),
    )

    class PendingRepository:
        def __init__(self) -> None:
            self.claimed = False
            self.marked = Event()
            self.marked_updates: list[int] = []

        def purge_expired_sessions(self, retention_days: int) -> int:
            return 0

        def claim_pending_replies(self, **_: object) -> list[OutboxReply]:
            if self.claimed:
                return []
            self.claimed = True
            return [reply]

        def mark_reply_delivered(
            self, update_id: int, *, channel: str, lease_token
        ) -> bool:  # type: ignore[no-untyped-def]
            self.marked_updates.append(update_id)
            self.marked.set()
            return update_id == reply.update_id and channel == "telegram"

        def release_reply(self, *args: object, **kwargs: object) -> bool:
            return False

    class RecordingMessenger:
        def __init__(self) -> None:
            self.sent = asyncio.Event()
            self.messages: list[tuple[str, str]] = []

        async def send_message(self, chat_id: str, text: str) -> None:
            self.messages.append((chat_id, text))
            self.sent.set()

    class RecordingAiLedger:
        def __init__(self) -> None:
            self.expire_calls: list[dict[str, object]] = []
            self.purge_cutoffs: list[datetime] = []

        def expire_orphans(self, **kwargs: object) -> int:
            self.expire_calls.append(kwargs)
            return 0

        def purge_expired_metadata(self, *, before: datetime) -> int:
            self.purge_cutoffs.append(before)
            return 0

    repository = PendingRepository()
    messenger = RecordingMessenger()
    ai_ledger = RecordingAiLedger()
    monkeypatch.setattr(app_main, "_session_repository", repository)
    monkeypatch.setattr(app_main, "_telegram_messenger", messenger)
    monkeypatch.setattr(
        app_main,
        "_checkpoint_factory",
        SimpleNamespace(setup=lambda: None),
    )
    monkeypatch.setattr(
        app_main,
        "_ai_ledger_repository",
        ai_ledger,
    )
    monkeypatch.setattr(
        app_main,
        "_handoff_repository",
        SimpleNamespace(claim_pending_handoffs=lambda **_: []),
    )
    monkeypatch.setattr(
        app_main,
        "settings",
        SimpleNamespace(
            pixel_telegram_bot_token=SecretStr("test-token"),
            pixel_telegram_retention_days=30,
            pixel_ai_ledger_retention_days=180,
        ),
    )

    async def in_process_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", in_process_to_thread)

    async with app_main.lifespan(FastAPI()):
        await asyncio.wait_for(messenger.sent.wait(), timeout=3)
        assert await asyncio.to_thread(repository.marked.wait, 3)
        assert ai_ledger.expire_calls
        assert len(ai_ledger.purge_cutoffs) == 1
        assert ai_ledger.expire_calls[0]["limit"] == 100
        assert abs(
            datetime.now(UTC) - ai_ledger.purge_cutoffs[0] - timedelta(days=180)
        ) < timedelta(seconds=2)

    assert messenger.messages == [("12345", "Resposta pendente")]
    assert repository.marked_updates == [reply.update_id]
