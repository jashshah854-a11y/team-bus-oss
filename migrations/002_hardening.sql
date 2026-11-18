-- Team Bus migration 002: v0.1 -> v0.2 hardening. Idempotent; a no-op on a
-- database built from the v0.2 schema.sql. Applied by tools/migrate.py, which
-- runs the archive migration first, then this file, then api.sql and (when the
-- teambus_agent role exists) grants.sql, all in one transaction.
--
-- Existing installs start in identity_mode = 'trusted' so current clients keep
-- working. Map each member to its own login role, then switch to enforced:
--   UPDATE team_settings SET identity_mode = 'enforced';

CREATE TABLE IF NOT EXISTS team_settings (
    id                 boolean PRIMARY KEY DEFAULT true CHECK (id),
    identity_mode      text NOT NULL DEFAULT 'enforced' CHECK (identity_mode IN ('enforced', 'trusted')),
    inbox_cap          integer NOT NULL DEFAULT 50,
    send_rate_per_hour integer NOT NULL DEFAULT 30,
    lease_minutes      integer NOT NULL DEFAULT 15,
    max_claim_attempts integer NOT NULL DEFAULT 5
);
INSERT INTO team_settings (id, identity_mode) VALUES (true, 'trusted') ON CONFLICT DO NOTHING;

-- Members: authority and login mapping.
ALTER TABLE team_members ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT 'member';
ALTER TABLE team_members ADD COLUMN IF NOT EXISTS db_role text;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_members_role_check') THEN
    ALTER TABLE team_members ADD CONSTRAINT team_members_role_check CHECK (role IN ('member', 'owner', 'service'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_members_db_role_key') THEN
    ALTER TABLE team_members ADD CONSTRAINT team_members_db_role_key UNIQUE (db_role);
  END IF;
  -- New names must be safe as file names (dispatcher spool files). Existing
  -- rows are not checked (NOT VALID); fix any odd names before validating.
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_members_name_check') THEN
    ALTER TABLE team_members ADD CONSTRAINT team_members_name_check
      CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$') NOT VALID;
  END IF;
END $$;

-- Messages: claim fencing, attempt count, failure state.
ALTER TABLE team_messages ADD COLUMN IF NOT EXISTS claim_token uuid;
ALTER TABLE team_messages ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE team_messages ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE team_messages ADD COLUMN IF NOT EXISTS releases integer NOT NULL DEFAULT 0;
ALTER TABLE team_messages DROP CONSTRAINT IF EXISTS team_messages_state_check;
ALTER TABLE team_messages ADD CONSTRAINT team_messages_state_check
  CHECK (state IN ('queued', 'claimed', 'done', 'held', 'failed'));
-- Claims taken by v0.1 clients have no token; give them one so the owner of
-- the lease can still be fenced. The v0.1 holder cannot present it, so it
-- will need to re-claim after the lease runs out.
UPDATE team_messages SET claim_token = gen_random_uuid() WHERE state = 'claimed' AND claim_token IS NULL;

ALTER TABLE team_messages_archive ADD COLUMN IF NOT EXISTS claim_token uuid;
ALTER TABLE team_messages_archive ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0;
ALTER TABLE team_messages_archive ADD COLUMN IF NOT EXISTS last_error text;
ALTER TABLE team_messages_archive ADD COLUMN IF NOT EXISTS releases integer NOT NULL DEFAULT 0;

-- Facts: one current row per key. Reconcile existing duplicate heads first:
-- the newest row stays current, older heads are marked superseded by it.
-- History is untouched; the count is reported as a NOTICE.
DO $$
DECLARE n int;
BEGIN
  WITH heads AS (
    SELECT id, max(id) OVER (PARTITION BY key) AS keep
    FROM team_facts WHERE superseded_by IS NULL)
  UPDATE team_facts f SET superseded_by = h.keep
  FROM heads h WHERE f.id = h.id AND h.id <> h.keep;
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN
    RAISE NOTICE 'teambus 002: reconciled % duplicate current fact(s); the newest per key stays current', n;
  END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS team_facts_one_head_idx ON team_facts (key) WHERE superseded_by IS NULL;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'team_facts_superseded_by_fkey' AND NOT condeferrable) THEN
    ALTER TABLE team_facts ALTER CONSTRAINT team_facts_superseded_by_fkey DEFERRABLE INITIALLY IMMEDIATE;
  END IF;
END $$;

-- Lifetime idempotency registry, backfilled from hot and archived rows so an
-- archived key can never be replayed as new work. Hashes match api.sql.
CREATE TABLE IF NOT EXISTS team_idempotency (
    scope        text NOT NULL,
    key          text NOT NULL,
    payload_hash text NOT NULL,
    ref_id       bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);
INSERT INTO team_idempotency (scope, key, payload_hash, ref_id, created_at)
SELECT 'message', idempotency_key,
       md5(jsonb_build_array(from_member, to_member, kind, body)::text), seq, created_at
FROM (SELECT idempotency_key, from_member, to_member, kind, body, seq, created_at FROM team_messages
      UNION ALL
      SELECT idempotency_key, from_member, to_member, kind, body, seq, created_at FROM team_messages_archive) x
ON CONFLICT DO NOTHING;
INSERT INTO team_idempotency (scope, key, payload_hash, ref_id, created_at)
SELECT 'fact', idempotency_key,
       md5(jsonb_build_array(posted_by, key, kind, body)::text), id, created_at
FROM (SELECT idempotency_key, posted_by, key, kind, body, id, created_at FROM team_facts
      UNION ALL
      SELECT idempotency_key, posted_by, key, kind, body, id, created_at FROM team_facts_archive) x
ON CONFLICT DO NOTHING;

-- Shared to-do list: used by `teambus.py task`, missing from the v0.1 schema.
CREATE TABLE IF NOT EXISTS team_tasks (
    id         bigserial PRIMARY KEY,
    title      text NOT NULL,
    owner      text NOT NULL REFERENCES team_members(name),
    created_by text NOT NULL REFERENCES team_members(name),
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz,
    done_by    text REFERENCES team_members(name)
);
CREATE INDEX IF NOT EXISTS team_tasks_owner_idx ON team_tasks (owner, status, id);

-- Durable wake deliveries.
CREATE TABLE IF NOT EXISTS team_wake_deliveries (
    event_kind      text NOT NULL,
    ref             bigint NOT NULL,
    member          text NOT NULL,
    variant         text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed', 'skipped')),
    attempts        integer NOT NULL DEFAULT 0,
    spooled         boolean NOT NULL DEFAULT false,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    PRIMARY KEY (event_kind, ref, member, variant)
);
CREATE INDEX IF NOT EXISTS team_wake_deliveries_due_idx
    ON team_wake_deliveries (next_attempt_at) WHERE status = 'pending';
