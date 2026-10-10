"""Durable version-aware public event consumption with fencing and retries."""

from uuid import uuid4

from sqlalchemy import Engine, text

from app.platform.outbox.ports import PublicEvent


class PostgresEventReader:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def claim(self, *, consumer: str, limit: int = 20) -> list[PublicEvent]:
        with self.engine.begin() as conn:
            conn.execute(
                text("""INSERT INTO platform.event_consumptions(consumer,event_id)
              SELECT :consumer,e.id FROM platform.outbox_events e
              WHERE (e.topic LIKE 'catalog.%' OR e.topic LIKE 'commerce.availability.%')
                AND NOT EXISTS (
                  SELECT 1 FROM platform.event_consumptions c
                  WHERE c.consumer=:consumer AND c.event_id=e.id
                )
              ON CONFLICT DO NOTHING"""),
                {"consumer": consumer},
            )
            conn.execute(
                text("""UPDATE platform.event_consumptions SET status='dead_letter',error_code='attempts_exhausted',lease_token=NULL,lease_until=NULL
                WHERE consumer=:consumer AND attempts>=5 AND available_at<=now() AND (status='pending' OR (status='delivering' AND lease_until<=now()))"""),
                {"consumer": consumer},
            )
            rows = (
                conn.execute(
                    text("""WITH ready AS (SELECT event_id FROM platform.event_consumptions WHERE consumer=:consumer AND attempts<5 AND available_at<=now() AND (status='pending' OR (status='delivering' AND lease_until<=now())) ORDER BY available_at,event_id LIMIT :limit FOR UPDATE SKIP LOCKED)
              UPDATE platform.event_consumptions c SET status='delivering',attempts=attempts+1,lease_token=:token,lease_until=now()+interval '60 seconds' FROM ready WHERE c.consumer=:consumer AND c.event_id=ready.event_id RETURNING c.event_id,c.lease_token,c.attempts"""),
                    {"consumer": consumer, "limit": limit, "token": uuid4()},
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                e = (
                    conn.execute(
                        text("SELECT * FROM platform.outbox_events WHERE id=:id"),
                        {"id": row["event_id"]},
                    )
                    .mappings()
                    .one()
                )
                result.append(
                    PublicEvent(
                        e["id"],
                        e["topic"],
                        e["aggregate_id"],
                        e["payload"],
                        row["lease_token"],
                        row["attempts"],
                        e["created_at"],
                    )
                )
            return result

    def finish(
        self,
        event: PublicEvent,
        *,
        consumer: str,
        retry: bool = False,
        incompatible: bool = False,
    ) -> None:
        status = (
            "dead_letter"
            if incompatible or (retry and event.attempts >= 5)
            else ("pending" if retry else "delivered")
        )
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """UPDATE platform.event_consumptions SET status=:status,lease_token=NULL,lease_until=NULL,available_at=now()+interval '30 seconds',error_code=:error WHERE consumer=:consumer AND event_id=:id AND lease_token=:token AND lease_until>now()"""
                ),
                {
                    "status": status,
                    "error": "incompatible_version"
                    if incompatible
                    else ("source_unavailable" if retry else None),
                    "consumer": consumer,
                    "id": event.id,
                    "token": event.lease_token,
                },
            )
