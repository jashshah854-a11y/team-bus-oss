-- Team Bus v0.1 schema for PostgreSQL 13+.
-- Run as a database owner, then run grants.sql after creating teambus_agent.

CREATE TABLE IF NOT EXISTS public.team_members (
    name           text PRIMARY KEY,
    kind           text NOT NULL,              -- hosted-assistant | cli-agent | app-agent
    location       text NOT NULL,              -- e.g. laptop | server | hosted
    machine        text,                       -- host the member runs on, if any
    card           jsonb NOT NULL DEFAULT '{}'::jsonb,  -- A2A Agent Card shape
    heartbeat_at   timestamptz,
    ttl_s          integer NOT NULL DEFAULT 180,
    status         text NOT NULL DEFAULT 'offline',
    inbound_policy text NOT NULL DEFAULT 'accept'
                   CHECK (inbound_policy IN ('accept', 'hold', 'refuse')),
    key_thumbprint text,                       -- member public-key thumbprint (brief 5.4); null in v1
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
                      CHECK (state IN ('queued', 'claimed', 'done', 'held')),
    idempotency_key   text NOT NULL UNIQUE,
    claimed_by        text REFERENCES public.team_members(name),
    claim_lease_until timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    claimed_at        timestamptz,
    done_at           timestamptz
);

CREATE INDEX IF NOT EXISTS team_messages_inbox_idx
    ON public.team_messages (to_member, state, seq);
CREATE INDEX IF NOT EXISTS team_messages_sender_idx
    ON public.team_messages (from_member, created_at);

-- Facts board. One row per post;
-- posting an existing key again supersedes the previous current row
-- (superseded_by points at the replacement), so the board is always the
-- set of rows with superseded_by IS NULL, and full history is preserved.
-- Body text is stored and returned exactly as posted, never summarized.
-- kind = 'fact' (shared truth) | 'directive' (a direction from the owner,
-- relayed verbatim by whichever member received it; the member protocol
-- requires posting it here the moment it is received).
CREATE TABLE IF NOT EXISTS public.team_facts (
    id              bigserial PRIMARY KEY,
    key             text NOT NULL,
    kind            text NOT NULL DEFAULT 'fact'
                    CHECK (kind IN ('fact', 'directive')),
    body            text NOT NULL,
    posted_by       text NOT NULL REFERENCES public.team_members(name),
    idempotency_key text NOT NULL UNIQUE,
    supersedes      bigint REFERENCES public.team_facts(id),
    superseded_by   bigint REFERENCES public.team_facts(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS team_facts_board_idx
    ON public.team_facts (key) WHERE superseded_by IS NULL;
CREATE INDEX IF NOT EXISTS team_facts_feed_idx
    ON public.team_facts (id);

-- No members are seeded. Add only the members your installation needs.
