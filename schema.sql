-- Team Bus v0.2 schema for PostgreSQL 13+.
-- Fresh install, as the database owner:
--   psql "$OWNER_URL" -f schema.sql -f api.sql
--   (create the teambus_agent group role and one login role per member, see README)
--   psql "$OWNER_URL" -f grants.sql
-- Existing v0.1 installs: run tools/migrate.py instead of this file.

-- preflight:begin
-- Refuse to install into a schema that other roles can write to. With CREATE on
-- public, any login could plant a table, trigger, function or operator that the
-- owner's code then runs (privilege escalation), or an overload that makes every
-- bus function ambiguous. The same block runs first in schema.sql, api.sql and
-- tools/migrate.py, before any table is touched.
DO $preflight$
DECLARE
  ref_owner name := COALESCE(
    (SELECT r.rolname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
     WHERE c.oid = to_regclass('public.team_members')), current_user);
  t text;
  o name;
BEGIN
  IF has_schema_privilege('public', 'public', 'CREATE') THEN
    RAISE EXCEPTION 'teambus: every role may CREATE in schema public; run '
      '"REVOKE CREATE ON SCHEMA public FROM PUBLIC" first (grant CREATE back to specific '
      'roles that need it). Nothing was changed.';
  END IF;
  FOREACH t IN ARRAY ARRAY['team_settings', 'team_members', 'team_messages', 'team_facts',
      'team_idempotency', 'team_tasks', 'team_wake_deliveries', 'team_messages_archive',
      'team_facts_archive']
  LOOP
    SELECT r.rolname INTO o FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
    WHERE c.oid = to_regclass('public.' || t);
    IF o IS NOT NULL AND o <> ref_owner THEN
      RAISE EXCEPTION 'teambus: public.% is owned by %, not by %; refusing to trust it. '
        'Nothing was changed.', t, o, ref_owner;
    END IF;
  END LOOP;
END $preflight$;
-- preflight:end

-- One row of installation settings. identity_mode:
--   enforced  a login role must be mapped to a member (team_members.db_role);
--             the config file cannot change who you are. Default for new installs.
--   trusted   v0.1 behaviour: the config's member name is believed. Only for a
--             fleet whose clients fully trust each other, or during migration.
CREATE TABLE IF NOT EXISTS public.team_settings (
    id                 boolean PRIMARY KEY DEFAULT true CHECK (id),
    identity_mode      text NOT NULL DEFAULT 'enforced'
                       CHECK (identity_mode IN ('enforced', 'trusted')),
    inbox_cap          integer NOT NULL DEFAULT 50,   -- flood fuse per recipient (0 = off)
    send_rate_per_hour integer NOT NULL DEFAULT 30,   -- loop fuse per sender (0 = off)
    lease_minutes      integer NOT NULL DEFAULT 15,   -- claim lease; renew for longer work
    max_claim_attempts integer NOT NULL DEFAULT 5     -- after this many failed attempts a message is 'failed'
);
INSERT INTO public.team_settings (id) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS public.team_members (
    name           text PRIMARY KEY
                   CONSTRAINT team_members_name_check CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$'),
    kind           text NOT NULL,              -- hosted-assistant | cli-agent | app-agent | service | human
    location       text NOT NULL,              -- e.g. laptop | server | hosted
    machine        text,                       -- host the member runs on, if any
    card           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- A2A Agent Card shape
    heartbeat_at   timestamptz,
    ttl_s          integer NOT NULL DEFAULT 180,
    status         text NOT NULL DEFAULT 'offline',
    inbound_policy text NOT NULL DEFAULT 'accept'
                   CHECK (inbound_policy IN ('accept', 'hold', 'refuse')),
    key_thumbprint text,                       -- member public-key thumbprint; null in v1
    -- Authority. member: ordinary agent. owner: may post directives and manage
    -- other members' policy. service: may read all mail (dispatcher, deadman).
    role           text NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'owner', 'service')),
    -- The Postgres login role this member connects as. Unique: one login, one member.
    db_role        text UNIQUE,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.team_messages (
    seq               bigserial PRIMARY KEY,
    from_member       text NOT NULL REFERENCES public.team_members(name),
    to_member         text NOT NULL REFERENCES public.team_members(name),
    body              text NOT NULL,
    kind              text NOT NULL DEFAULT 'message',  -- message | ask_user
    state             text NOT NULL DEFAULT 'queued'
                      CONSTRAINT team_messages_state_check
                      CHECK (state IN ('queued', 'claimed', 'done', 'held', 'failed')),
    idempotency_key   text NOT NULL UNIQUE,
    claimed_by        text REFERENCES public.team_members(name),
    claim_lease_until timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    claimed_at        timestamptz,
    done_at           timestamptz,
    claim_token       uuid,                    -- fences one claim attempt; required by done/renew/fail
    attempts          integer NOT NULL DEFAULT 0,
    last_error        text,
    releases          integer NOT NULL DEFAULT 0   -- times released from held/failed; part of the wake variant
);

CREATE INDEX IF NOT EXISTS team_messages_inbox_idx
    ON public.team_messages (to_member, state, seq);
CREATE INDEX IF NOT EXISTS team_messages_sender_idx
    ON public.team_messages (from_member, created_at);

-- Facts board. One row per post; posting an existing key again supersedes the
-- previous current row (superseded_by points at the replacement), so the board
-- is the set of rows with superseded_by IS NULL, and full history is kept.
-- kind = 'fact' (shared truth) | 'directive' (only members with role 'owner').
CREATE TABLE IF NOT EXISTS public.team_facts (
    id              bigserial PRIMARY KEY,
    key             text NOT NULL,
    kind            text NOT NULL DEFAULT 'fact'
                    CHECK (kind IN ('fact', 'directive')),
    body            text NOT NULL,
    posted_by       text NOT NULL REFERENCES public.team_members(name),
    idempotency_key text NOT NULL UNIQUE,
    -- supersedes is a plain id, not a foreign key: once `teambus.py compact`
    -- moves an old version to team_facts_archive, the current row still
    -- points at it, and `teambus.py history <key>` follows it across both.
    supersedes      bigint,
    -- Deferrable so teambus_post_fact can retire the old head before the new
    -- row exists, which keeps "one current row per key" true at every moment.
    superseded_by   bigint REFERENCES public.team_facts(id) DEFERRABLE INITIALLY IMMEDIATE,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS team_facts_board_idx
    ON public.team_facts (key) WHERE superseded_by IS NULL;
-- At most one current fact per key, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS team_facts_one_head_idx
    ON public.team_facts (key) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS team_facts_feed_idx
    ON public.team_facts (id);

-- Lifetime duplicate protection. Independent of hot and archive tables, and
-- never compacted, so an archived message or fact cannot be replayed as new.
-- payload_hash detects the same key reused for a different payload.
CREATE TABLE IF NOT EXISTS public.team_idempotency (
    scope        text NOT NULL,               -- 'message' | 'fact'
    key          text NOT NULL,
    payload_hash text NOT NULL,
    ref_id       bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope, key)
);

-- Shared to-do list (`teambus.py task` / `tasks`).
CREATE TABLE IF NOT EXISTS public.team_tasks (
    id         bigserial PRIMARY KEY,
    title      text NOT NULL,
    owner      text NOT NULL REFERENCES public.team_members(name),
    created_by text NOT NULL REFERENCES public.team_members(name),
    status     text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    note       text,
    created_at timestamptz NOT NULL DEFAULT now(),
    done_at    timestamptz,
    done_by    text REFERENCES public.team_members(name)
);
CREATE INDEX IF NOT EXISTS team_tasks_owner_idx ON public.team_tasks (owner, status, id);

-- Durable wake deliveries, written by the dispatcher. One row per event, per
-- recipient, per variant (a held message that is released, or a message that
-- is re-queued after a failed attempt, is a new variant and wakes again).
-- A failed wake is retried with backoff instead of being forgotten.
CREATE TABLE IF NOT EXISTS public.team_wake_deliveries (
    event_kind      text NOT NULL,            -- 'message' | 'fact'
    ref             bigint NOT NULL,          -- team_messages.seq or team_facts.id
    member          text NOT NULL,
    variant         text NOT NULL DEFAULT '', -- state:attempts:releases, e.g. 'queued:0:0', 'expired:1:0'
    status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'delivered', 'failed', 'skipped')),
    attempts        integer NOT NULL DEFAULT 0,
    spooled         boolean NOT NULL DEFAULT false,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    delivered_at    timestamptz,
    PRIMARY KEY (event_kind, ref, member, variant)
);
CREATE INDEX IF NOT EXISTS team_wake_deliveries_due_idx
    ON public.team_wake_deliveries (next_attempt_at) WHERE status = 'pending';

-- No members are seeded. Add only the members your installation needs.

-- Archive tables for `teambus.py compact`: finished rows move here, never
-- deleted. Same columns as the hot tables plus archived_at.
CREATE TABLE IF NOT EXISTS public.team_messages_archive (
    seq               bigint PRIMARY KEY,
    from_member       text NOT NULL,
    to_member         text NOT NULL,
    body              text NOT NULL,
    kind              text NOT NULL,
    state             text NOT NULL,
    idempotency_key   text NOT NULL,
    claimed_by        text,
    claim_lease_until timestamptz,
    created_at        timestamptz NOT NULL,
    claimed_at        timestamptz,
    done_at           timestamptz,
    claim_token       uuid,
    attempts          integer NOT NULL DEFAULT 0,
    last_error        text,
    releases          integer NOT NULL DEFAULT 0,
    archived_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS team_messages_archive_from_idx ON public.team_messages_archive (from_member, seq);
CREATE INDEX IF NOT EXISTS team_messages_archive_to_idx   ON public.team_messages_archive (to_member, seq);

CREATE TABLE IF NOT EXISTS public.team_facts_archive (
    id              bigint PRIMARY KEY,
    key             text NOT NULL,
    kind            text NOT NULL,
    body            text NOT NULL,
    posted_by       text NOT NULL,
    idempotency_key text NOT NULL,
    supersedes      bigint,
    superseded_by   bigint,
    created_at      timestamptz NOT NULL,
    archived_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS team_facts_archive_key_idx ON public.team_facts_archive (key, id);
