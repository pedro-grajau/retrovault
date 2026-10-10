"""Demand transactions and independently retained contact data."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from threading import local
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.modules.concierge.domain.demand import (
    Demand,
    DemandNotification,
    DemandsUnavailable,
    normalize,
)


class PostgresDemandRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._delivery = local()

    @staticmethod
    def _demand(row: Any) -> Demand:
        return Demand(
            row["id"],
            row["owner_key"],
            row["channel"],
            row["title"],
            row["platform"],
            row["mode"],
            row["game_id"],
            row["status"],
            row["created_at"],
            row["consented_at"],
        )

    @contextmanager
    def _transaction(self):
        try:
            with self.engine.begin() as connection:
                yield connection
        except SQLAlchemyError as exc:
            raise DemandsUnavailable("demand_storage_unavailable") from exc

    def propose(
        self,
        *,
        owner_key: str,
        channel: str,
        chat_id: str,
        session_id: UUID,
        update_id: int,
        correlation_id: UUID,
        title: str,
        platform: str,
        mode: str,
        game_id: UUID | None,
        context: dict[str, object],
    ) -> Demand:
        params = {
            "id": uuid4(),
            "owner_key": owner_key,
            "channel": channel,
            "chat_id": chat_id,
            "session_id": session_id,
            "update_id": update_id,
            "correlation_id": correlation_id,
            "title": title,
            "platform": platform,
            "title_key": normalize(title),
            "platform_key": normalize(platform),
            "mode": mode,
            "game_id": game_id,
            "context": json.dumps(context),
        }
        with self._transaction() as conn:
            row = (
                conn.execute(
                    text("""INSERT INTO concierge.demands(id,owner_key,channel,chat_id,session_id,source_update_id,correlation_id,title,platform,title_key,platform_key,mode,game_id,context,status)
                VALUES(:id,:owner_key,:channel,:chat_id,:session_id,:update_id,:correlation_id,:title,:platform,:title_key,:platform_key,:mode,:game_id,CAST(:context AS jsonb),'proposed')
                ON CONFLICT(owner_key,channel,source_update_id) DO UPDATE SET source_update_id=excluded.source_update_id RETURNING *"""),
                    params,
                )
                .mappings()
                .one()
            )
            return self._demand(row)

    def confirm(
        self, demand_id: UUID, *, owner_key: str, channel: str, update_id: int
    ) -> Demand | None:
        p = {
            "id": demand_id,
            "owner": owner_key,
            "channel": channel,
            "update": update_id,
        }
        with self._transaction() as conn:
            conn.execute(
                text(
                    "SELECT pg_advisory_xact_lock(hashtextextended(:owner || :channel,0))"
                ),
                p,
            )
            effect = (
                conn.execute(
                    text(
                        """SELECT d.* FROM concierge.demand_command_effects e JOIN concierge.demands d ON d.id=e.demand_id WHERE e.owner_key=:owner AND e.channel=:channel AND e.update_id=:update"""
                    ),
                    p,
                )
                .mappings()
                .first()
            )
            if effect:
                return self._demand(effect)
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM concierge.demands WHERE id=:id AND owner_key=:owner AND channel=:channel FOR UPDATE"
                    ),
                    p,
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] not in ("active", "proposed"):
                return None
            active = (
                conn.execute(
                    text(
                        """SELECT * FROM concierge.demands WHERE owner_key=:owner AND channel=:channel AND title_key=:title AND platform_key=:platform AND mode=:mode AND status='active' """
                    ),
                    {
                        "owner": owner_key,
                        "channel": channel,
                        "title": row["title_key"],
                        "platform": row["platform_key"],
                        "mode": row["mode"],
                    },
                )
                .mappings()
                .first()
            )
            if active is None:
                row = (
                    conn.execute(
                        text(
                            "UPDATE concierge.demands SET status='active',consented_at=now() WHERE id=:id RETURNING *"
                        ),
                        p,
                    )
                    .mappings()
                    .one()
                )
                conn.execute(
                    text(
                        "INSERT INTO concierge.demand_history(demand_id,action) VALUES(:id,'consented')"
                    ),
                    p,
                )
            elif active["id"] != demand_id:
                conn.execute(
                    text(
                        "UPDATE concierge.demands SET status='cancelled',closed_at=now(),close_reason='duplicate_active' WHERE id=:id"
                    ),
                    p,
                )
                row = active
            conn.execute(
                text(
                    "INSERT INTO concierge.demand_command_effects(channel,owner_key,update_id,demand_id) VALUES(:channel,:owner,:update,:effect_id)"
                ),
                {**p, "effect_id": row["id"]},
            )
            return self._demand(row)

    def cancel(self, demand_id: UUID, *, owner_key: str, channel: str) -> bool:
        with self._transaction() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": str(demand_id)},
            )
            row = conn.execute(
                text(
                    "SELECT status FROM concierge.demands WHERE id=:id AND owner_key=:owner AND channel=:channel FOR UPDATE"
                ),
                {"id": demand_id, "owner": owner_key, "channel": channel},
            ).first()
            if row is None:
                return False
            if row[0] in ("active", "proposed"):
                conn.execute(
                    text(
                        "UPDATE concierge.demands SET status='cancelled',closed_at=now(),close_reason='owner_cancelled' WHERE id=:id"
                    ),
                    {"id": demand_id},
                )
                conn.execute(
                    text(
                        "UPDATE concierge.demand_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE demand_id=:id AND status IN ('pending','delivering')"
                    ),
                    {"id": demand_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO concierge.demand_history(demand_id,action) VALUES(:id,'cancelled')"
                    ),
                    {"id": demand_id},
                )
            return True

    def get_owned(
        self, demand_id: UUID, *, owner_key: str, channel: str
    ) -> Demand | None:
        with self._transaction() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT * FROM concierge.demands WHERE id=:id AND owner_key=:owner AND channel=:channel"
                    ),
                    {"id": demand_id, "owner": owner_key, "channel": channel},
                )
                .mappings()
                .first()
            )
            return self._demand(row) if row is not None else None

    def list_owned(self, *, owner_key: str, channel: str) -> list[Demand]:
        with self._transaction() as conn:
            rows = conn.execute(
                text(
                    "SELECT * FROM concierge.demands WHERE owner_key=:owner AND channel=:channel AND status IN ('active','proposed') ORDER BY created_at DESC LIMIT 101"
                ),
                {"owner": owner_key, "channel": channel},
            ).mappings()
            return [self._demand(row) for row in rows]

    def active(self) -> list[Demand]:
        with self._transaction() as conn:
            return [
                self._demand(row)
                for row in conn.execute(
                    text(
                        "SELECT * FROM concierge.demands WHERE status='active' ORDER BY created_at,id"
                    )
                ).mappings()
            ]

    def enqueue(self, demand_id: UUID, *, event_id: UUID, game_id: UUID) -> None:
        with self._transaction() as conn:
            conn.execute(
                text(
                    "UPDATE concierge.demands SET game_id=:game WHERE id=:id AND status='active' AND (game_id IS NULL OR game_id=:game)"
                ),
                {"id": demand_id, "game": game_id},
            )
            conn.execute(
                text(
                    """INSERT INTO concierge.demand_outbox(id,demand_id,event_id) SELECT :new,:id,:event FROM concierge.demands WHERE id=:id AND status='active' ON CONFLICT(demand_id,event_id) DO NOTHING"""
                ),
                {"new": uuid4(), "id": demand_id, "event": event_id},
            )

    def close_sold(self, demand_id: UUID, *, event_id: UUID) -> None:
        with self._transaction() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": str(demand_id)},
            )
            row = conn.execute(
                text(
                    "UPDATE concierge.demands SET status='sold',closed_at=now(),close_reason='explicit_sale_without_eligible_unit' WHERE id=:id AND status='active' RETURNING id"
                ),
                {"id": demand_id},
            ).first()
            if row:
                conn.execute(
                    text(
                        "UPDATE concierge.demand_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE demand_id=:id AND status IN ('pending','delivering')"
                    ),
                    {"id": demand_id},
                )
                conn.execute(
                    text(
                        "INSERT INTO concierge.demand_history(demand_id,action,event_id) VALUES(:id,'sold',:event)"
                    ),
                    {"id": demand_id, "event": event_id},
                )

    def claim(
        self, *, now: datetime, limit: int = 20, channel: str | None = None
    ) -> list[DemandNotification]:
        with self._transaction() as conn:
            conn.execute(
                text("""UPDATE concierge.demand_outbox SET status='dead_letter',lease_token=NULL,lease_until=NULL
                WHERE attempts>=5 AND available_at<=:now AND (status='pending' OR (status='delivering' AND lease_until<=:now))
                AND pg_try_advisory_xact_lock(hashtextextended(demand_id::text,0))"""),
                {"now": now},
            )
            rows = (
                conn.execute(
                    text("""WITH ready AS (SELECT o.id FROM concierge.demand_outbox o JOIN concierge.demands d ON d.id=o.demand_id
              WHERE o.attempts<5 AND pg_try_advisory_xact_lock(hashtextextended(d.id::text,0)) AND (CAST(:channel AS text) IS NULL OR d.channel=CAST(:channel AS text)) AND d.status='active' AND d.chat_id IS NOT NULL AND o.available_at<=:now AND (o.status='pending' OR (o.status='delivering' AND o.lease_until<=:now))
              ORDER BY o.available_at,o.id LIMIT :limit FOR UPDATE OF o SKIP LOCKED)
              UPDATE concierge.demand_outbox o SET status='delivering',lease_token=:token,lease_until=:now+interval '30 seconds',attempts=attempts+1 FROM ready WHERE o.id=ready.id RETURNING o.*"""),
                    {"now": now, "limit": limit, "token": uuid4(), "channel": channel},
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                d = (
                    conn.execute(
                        text("SELECT * FROM concierge.demands WHERE id=:id"),
                        {"id": row["demand_id"]},
                    )
                    .mappings()
                    .one()
                )
                result.append(
                    DemandNotification(
                        row["id"],
                        self._demand(d),
                        d["chat_id"],
                        row["event_id"],
                        row["lease_token"],
                        row["attempts"],
                    )
                )
            return result

    @contextmanager
    def delivery_guard(self, notification: DemandNotification):
        # The same lock fences cancellation/sale while transport is in flight.
        with self._transaction() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": str(notification.demand.id)},
            )
            valid = conn.execute(
                text(
                    """SELECT 1 FROM concierge.demand_outbox o JOIN concierge.demands d ON d.id=o.demand_id WHERE o.id=:id AND o.lease_token=:token AND o.status='delivering' AND o.lease_until>now() AND d.status='active'"""
                ),
                {"id": notification.id, "token": notification.lease_token},
            ).first()
            previous = getattr(self._delivery, "notification", None)
            self._delivery.notification = (
                (notification.id, notification.lease_token) if valid else None
            )
            try:
                yield bool(valid)
            finally:
                self._delivery.notification = previous

    def complete(self, notification: DemandNotification) -> bool:
        with self._transaction() as conn:
            row = conn.execute(
                text(
                    """UPDATE concierge.demand_outbox SET status='delivered',delivered_at=now(),lease_token=NULL,lease_until=NULL WHERE id=:id AND status='delivering' AND lease_token=:token AND (lease_until>now() OR :guarded) RETURNING demand_id"""
                ),
                {
                    "id": notification.id,
                    "token": notification.lease_token,
                    "guarded": getattr(self._delivery, "notification", None)
                    == (notification.id, notification.lease_token),
                },
            ).first()
            if not row:
                return False
            conn.execute(
                text(
                    "UPDATE concierge.demands SET status='notified',closed_at=now(),close_reason='first_transport_confirmed_notice' WHERE id=:id AND status='active'"
                ),
                {"id": notification.demand.id},
            )
            conn.execute(
                text(
                    "UPDATE concierge.demand_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE demand_id=:demand AND id<>:id AND status IN ('pending','delivering')"
                ),
                {"demand": notification.demand.id, "id": notification.id},
            )
            conn.execute(
                text(
                    "INSERT INTO concierge.demand_history(demand_id,action,event_id) VALUES(:id,'notified',:event)"
                ),
                {"id": notification.demand.id, "event": notification.event_id},
            )
            return True

    def retry(
        self, notification: DemandNotification, *, suppressed: bool = False
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                text(
                    """UPDATE concierge.demand_outbox SET status=:status,available_at=now()+interval '30 seconds',lease_token=NULL,lease_until=NULL WHERE id=:id AND status='delivering' AND lease_token=:token"""
                ),
                {
                    "status": "suppressed"
                    if suppressed
                    else ("dead_letter" if notification.attempts >= 5 else "pending"),
                    "id": notification.id,
                    "token": notification.lease_token,
                },
            )

    def purge(self, *, contact_days: int = 30, audit_days: int = 180) -> None:
        if contact_days < 1 or audit_days < contact_days:
            raise ValueError("invalid_demand_retention")
        with self._transaction() as conn:
            conn.execute(
                text(
                    "UPDATE concierge.demands SET chat_id=NULL,context='{}'::jsonb WHERE closed_at<now()-(:days*interval '1 day')"
                ),
                {"days": contact_days},
            )
            conn.execute(
                text(
                    "DELETE FROM concierge.demands WHERE closed_at<now()-(:days*interval '1 day')"
                ),
                {"days": audit_days},
            )
            conn.execute(
                text(
                    "UPDATE concierge.demands SET status='cancelled',closed_at=now(),close_reason='consent_expired' WHERE status='proposed' AND created_at<now()-interval '1 day'"
                )
            )
