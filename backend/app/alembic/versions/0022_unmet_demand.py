"""Add consented demands, consumer leases and transactional availability events."""
from alembic import op
revision = '0022_unmet_demand'
down_revision = '0021_handoff_context'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE concierge.demands (
          id uuid PRIMARY KEY, owner_key text NOT NULL, channel text NOT NULL CHECK (channel IN ('telegram','simulator')),
          chat_id text, session_id uuid NOT NULL, source_update_id bigint NOT NULL,
          correlation_id uuid NOT NULL, title text NOT NULL, platform text NOT NULL,
          title_key text NOT NULL, platform_key text NOT NULL,
          mode text NOT NULL CHECK (mode IN ('purchase','rental')), game_id uuid,
          context jsonb NOT NULL, status text NOT NULL CHECK (status IN ('proposed','active','cancelled','notified','sold')),
          created_at timestamptz NOT NULL DEFAULT now(), consented_at timestamptz,
          closed_at timestamptz, close_reason text,
          UNIQUE (owner_key,channel,source_update_id)
        );
        CREATE UNIQUE INDEX demands_active_owner ON concierge.demands(owner_key,channel,title_key,platform_key,mode) WHERE status='active';
        CREATE TABLE concierge.demand_history (
          id bigserial PRIMARY KEY, demand_id uuid NOT NULL REFERENCES concierge.demands(id) ON DELETE CASCADE,
          action text NOT NULL, event_id uuid, occurred_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE concierge.demand_command_effects (
          channel text NOT NULL, owner_key text NOT NULL, update_id bigint NOT NULL,
          demand_id uuid NOT NULL REFERENCES concierge.demands(id) ON DELETE CASCADE,
          PRIMARY KEY(channel,owner_key,update_id)
        );
        CREATE TABLE concierge.demand_outbox (
          id uuid PRIMARY KEY, demand_id uuid NOT NULL REFERENCES concierge.demands(id) ON DELETE CASCADE,
          event_id uuid NOT NULL, status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivering','delivered','suppressed','cancelled','dead_letter')),
          attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
          lease_token uuid, lease_until timestamptz, delivered_at timestamptz,
          UNIQUE(demand_id,event_id)
        );
        CREATE TABLE platform.event_consumptions (
          consumer text NOT NULL, event_id uuid NOT NULL REFERENCES platform.outbox_events(id),
          status text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
          lease_token uuid, lease_until timestamptz, available_at timestamptz NOT NULL DEFAULT now(),
          error_code text, PRIMARY KEY(consumer,event_id)
        );
        ALTER TABLE commerce.physical_units DROP CONSTRAINT physical_units_state_check;
        ALTER TABLE commerce.physical_units ADD CONSTRAINT physical_units_state_check CHECK(state IN ('available','unavailable','sold'));
        CREATE FUNCTION commerce.emit_availability_event() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE offer_record record; event_id uuid;
        BEGIN
          IF TG_OP = 'UPDATE' AND NEW.state = OLD.state AND NEW.offer_id = OLD.offer_id THEN RETURN NEW; END IF;
          SELECT game_id,mode INTO offer_record FROM commerce.offers WHERE id=NEW.offer_id;
          event_id := gen_random_uuid();
          INSERT INTO platform.outbox_events(id,topic,aggregate_type,aggregate_id,payload,created_at,available_at)
          VALUES(event_id,'commerce.availability.v1','game',offer_record.game_id,
            jsonb_build_object('version','availability.v1','event_id',event_id,'game_id',offer_record.game_id,
              'mode',offer_record.mode,'unit_id',NEW.id,'state',NEW.state,'occurred_at',now()),now(),now());
          RETURN NEW;
        END $$;
        CREATE TRIGGER units_availability_event AFTER INSERT OR UPDATE ON commerce.physical_units
          FOR EACH ROW EXECUTE FUNCTION commerce.emit_availability_event();
    """)


def downgrade() -> None:
    op.execute("""
      DROP TRIGGER units_availability_event ON commerce.physical_units;
      DROP FUNCTION commerce.emit_availability_event();
      UPDATE commerce.physical_units SET state='unavailable' WHERE state='sold';
      ALTER TABLE commerce.physical_units DROP CONSTRAINT physical_units_state_check;
      ALTER TABLE commerce.physical_units ADD CONSTRAINT physical_units_state_check CHECK(state IN ('available','unavailable'));
      DROP TABLE platform.event_consumptions;
      DROP TABLE concierge.demand_outbox;
      DROP TABLE concierge.demand_command_effects;
      DROP TABLE concierge.demand_history;
      DROP TABLE concierge.demands;
    """)
