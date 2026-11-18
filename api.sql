-- Team Bus v0.2 database API. Run as the database owner after schema.sql
-- (fresh install) or through tools/migrate.py (existing install). Safe to re-run.
--
-- Every write goes through a SECURITY DEFINER function below. The runtime role
-- (teambus_agent) gets SELECT on the tables and EXECUTE on these functions, not
-- INSERT/UPDATE (see grants.sql), so the rules here cannot be bypassed with
-- direct SQL:
--   * who you are comes from your Postgres login (public.team_members.db_role), not
--     from your config file (identity_mode = 'enforced');
--   * only role 'owner' posts directives or changes another member's policy;
--   * message bodies and facts are append-only;
--   * a recipient's hold/refuse policy and the inbox/rate fuses are applied
--     by the database;
--   * one current fact per key; duplicate keys are detected for life;
--   * a claim is fenced by a token that done/renew/fail must present.
-- Reads of mail are limited by row-level security to sender and recipient
-- (plus 'owner'/'service' members). Database owners and superusers bypass all
-- of this, as they own the data anyway.

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

-- ------------------------------------------------------------------ identity

CREATE OR REPLACE FUNCTION public.teambus_is_admin() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  -- current_user here is the function owner (the schema owner).
  SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = session_user AND rolsuper)
      OR pg_has_role(session_user, current_user, 'MEMBER');
$$;

CREATE OR REPLACE FUNCTION public.teambus_mode() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE((SELECT identity_mode FROM public.team_settings WHERE id), 'enforced');
$$;

-- The member mapped to the current login, or NULL.
CREATE OR REPLACE FUNCTION public.teambus_reader() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT name FROM public.team_members WHERE db_role = session_user;
$$;

-- May the current login read every member's mail?
CREATE OR REPLACE FUNCTION public.teambus_can_read_all() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT public.teambus_is_admin()
      OR public.teambus_mode() = 'trusted'
      OR EXISTS (SELECT 1 FROM public.team_members
                 WHERE db_role = session_user AND role IN ('owner', 'service'));
$$;

-- Resolve the acting member. p_claimed is the member name from the client's
-- config. A mapped login must match it; an unmapped login is believed only
-- for admins or in trusted mode.
CREATE OR REPLACE FUNCTION public.teambus_actor(p_claimed text) RETURNS text
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  mapped text;
BEGIN
  SELECT name INTO mapped FROM public.team_members WHERE db_role = session_user;
  IF mapped IS NOT NULL THEN
    IF p_claimed IS NOT NULL AND p_claimed <> mapped THEN
      RAISE EXCEPTION 'config says member "%", but database login "%" is member "%"',
        p_claimed, session_user, mapped USING ERRCODE = '42501';
    END IF;
    RETURN mapped;
  END IF;
  IF public.teambus_is_admin() OR public.teambus_mode() = 'trusted' THEN
    IF p_claimed IS NULL OR NOT EXISTS (SELECT 1 FROM public.team_members WHERE name = p_claimed) THEN
      RAISE EXCEPTION '"%" is not on the roster', p_claimed USING ERRCODE = '42501';
    END IF;
    RETURN p_claimed;
  END IF;
  RAISE EXCEPTION 'database login "%" is not mapped to a member (identity_mode=enforced); '
    'the owner must set public.team_members.db_role', session_user USING ERRCODE = '42501';
END $$;

CREATE OR REPLACE FUNCTION public.teambus_role_of(p_member text) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT role FROM public.team_members WHERE name = p_member;
$$;

CREATE OR REPLACE FUNCTION public.teambus_whoami(p_claimed text)
RETURNS TABLE (member text, role text, identity_mode text, admin boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
BEGIN
  RETURN QUERY SELECT me, public.teambus_role_of(me), public.teambus_mode(), public.teambus_is_admin();
END $$;

-- ------------------------------------------------------------------ messages

CREATE OR REPLACE FUNCTION public.teambus_send(p_claimed text, p_to text, p_body text,
                                               p_key text, p_kind text DEFAULT 'message')
RETURNS TABLE (seq bigint, state text, duplicate boolean, recipient_live boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
  me text := public.teambus_actor(p_claimed);
  s public.team_settings%ROWTYPE;
  pol text;
  st text;
  h text;
  reg public.team_idempotency%ROWTYPE;
  new_seq bigint;
BEGIN
  IF p_key IS NULL OR length(p_key) = 0 THEN
    RAISE EXCEPTION 'an idempotency key is required' USING ERRCODE = '22023';
  END IF;
  IF p_kind NOT IN ('message', 'ask_user') THEN
    RAISE EXCEPTION 'unknown message kind "%"', p_kind USING ERRCODE = '22023';
  END IF;
  SELECT m.inbound_policy INTO pol FROM public.team_members m WHERE m.name = p_to;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown recipient "%"', p_to USING ERRCODE = '22023';
  END IF;
  IF p_to = me THEN
    RAISE EXCEPTION 'cannot send to yourself' USING ERRCODE = '22023';
  END IF;

  -- Lifetime dedupe, before any fuse: a retry of a send that already landed
  -- must report the original, even if the fuses would now refuse it.
  h := md5(jsonb_build_array(me, p_to, p_kind, p_body)::text);
  INSERT INTO public.team_idempotency (scope, key, payload_hash) VALUES ('message', p_key, h)
  ON CONFLICT DO NOTHING;
  IF NOT FOUND THEN
    SELECT * INTO reg FROM public.team_idempotency r WHERE r.scope = 'message' AND r.key = p_key;
    IF reg.payload_hash <> h THEN
      RAISE EXCEPTION 'idempotency key "%" was already used for a different message', p_key
        USING ERRCODE = '23505';
    END IF;
    RETURN QUERY
      SELECT x.seq, x.state, true, NULL::boolean FROM (
        SELECT m.seq, m.state FROM public.team_messages m WHERE m.seq = reg.ref_id
        UNION ALL
        SELECT a.seq, a.state FROM public.team_messages_archive a WHERE a.seq = reg.ref_id) x
      LIMIT 1;
    RETURN;
  END IF;

  IF pol = 'refuse' THEN
    RAISE EXCEPTION '% refuses inbound messages (inbound_policy=refuse)', p_to USING ERRCODE = '42501';
  END IF;
  st := CASE WHEN pol = 'hold' THEN 'held' ELSE 'queued' END;
  SELECT * INTO s FROM public.team_settings WHERE id;
  IF COALESCE(s.inbox_cap, 0) > 0 AND (SELECT count(*) FROM public.team_messages m
      WHERE m.to_member = p_to AND m.state IN ('queued', 'claimed', 'held')) >= s.inbox_cap THEN
    RAISE EXCEPTION '% inbox is at the %-message unread cap; send refused', p_to, s.inbox_cap
      USING ERRCODE = '53400';
  END IF;
  IF COALESCE(s.send_rate_per_hour, 0) > 0 AND (SELECT count(*) FROM public.team_messages m
      WHERE m.from_member = me AND m.created_at > now() - interval '1 hour') >= s.send_rate_per_hour THEN
    RAISE EXCEPTION 'sender rate limit (%/hour) reached; send refused', s.send_rate_per_hour
      USING ERRCODE = '53400';
  END IF;

  INSERT INTO public.team_messages (from_member, to_member, body, kind, state, idempotency_key)
  VALUES (me, p_to, p_body, p_kind, st, p_key) RETURNING public.team_messages.seq INTO new_seq;
  UPDATE public.team_idempotency r SET ref_id = new_seq WHERE r.scope = 'message' AND r.key = p_key;
  RETURN QUERY
    SELECT new_seq, st, false,
           COALESCE((SELECT m.heartbeat_at > now() - (m.ttl_s * interval '1 second')
                     FROM public.team_members m WHERE m.name = p_to), false);
END $$;

-- Lease the next queued message (or one exact seq / one sender's next).
-- Expired claims are returned to the queue first, so a crashed holder loses nothing;
-- an expiry counts as a failed attempt, so a message that keeps crashing its worker
-- parks as 'failed' after max_claim_attempts instead of cycling forever.
CREATE OR REPLACE FUNCTION public.teambus_claim(p_claimed text, p_from text DEFAULT NULL,
                                                p_seq bigint DEFAULT NULL)
RETURNS TABLE (seq bigint, from_member text, kind text, body text, claim_token uuid,
               lease_until timestamptz, attempt integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
  me text := public.teambus_actor(p_claimed);
  lease int := COALESCE((SELECT lease_minutes FROM public.team_settings WHERE id), 15);
  maxa int := COALESCE((SELECT max_claim_attempts FROM public.team_settings WHERE id), 5);
  r record;
  tok uuid := gen_random_uuid();
BEGIN
  UPDATE public.team_messages m SET state = CASE WHEN m.attempts >= maxa THEN 'failed' ELSE 'queued' END,
         claimed_by = NULL, claim_lease_until = NULL, claimed_at = NULL, claim_token = NULL,
         last_error = 'claim lease expired'
  WHERE m.to_member = me AND m.state = 'claimed' AND m.claim_lease_until < now();

  SELECT m.seq, m.from_member, m.kind, m.body INTO r FROM public.team_messages m
  WHERE m.to_member = me AND m.state = 'queued'
    AND (p_from IS NULL OR m.from_member = p_from)
    AND (p_seq IS NULL OR m.seq = p_seq)
  ORDER BY m.seq LIMIT 1 FOR UPDATE SKIP LOCKED;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  UPDATE public.team_messages m SET state = 'claimed', claimed_by = me, claimed_at = now(),
         claim_lease_until = now() + lease * interval '1 minute', claim_token = tok,
         attempts = m.attempts + 1
  WHERE m.seq = r.seq;
  RETURN QUERY SELECT m.seq, m.from_member, m.kind, m.body, m.claim_token, m.claim_lease_until, m.attempts
               FROM public.team_messages m WHERE m.seq = r.seq;
END $$;

-- Extend a live claim. Fails if the claim was lost (expired and re-claimed).
CREATE OR REPLACE FUNCTION public.teambus_renew(p_claimed text, p_seq bigint, p_token uuid,
                                                p_minutes integer DEFAULT NULL)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  mins int := COALESCE(p_minutes, (SELECT lease_minutes FROM public.team_settings WHERE id), 15);
  v_until timestamptz;
BEGIN
  IF mins < 1 OR mins > 1440 THEN
    RAISE EXCEPTION 'lease minutes must be between 1 and 1440' USING ERRCODE = '22023';
  END IF;
  UPDATE public.team_messages m SET claim_lease_until = now() + mins * interval '1 minute'
  WHERE m.seq = p_seq AND m.to_member = me AND m.state = 'claimed' AND m.claim_token = p_token
  RETURNING m.claim_lease_until INTO v_until;
  IF v_until IS NULL THEN
    RAISE EXCEPTION 'seq % is not claimed by this token (lease lost or finished)', p_seq
      USING ERRCODE = '55000';
  END IF;
  RETURN v_until;
END $$;

-- Finish a message. With a token: the token must be the current claim.
-- Without a token: only an unclaimed queued message (claim-and-finish in one step).
CREATE OR REPLACE FUNCTION public.teambus_done(p_claimed text, p_seq bigint, p_token uuid DEFAULT NULL)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  v_at timestamptz;
BEGIN
  IF p_token IS NULL THEN
    UPDATE public.team_messages m SET state = 'done', done_at = now(), claimed_by = me,
           claimed_at = COALESCE(m.claimed_at, now()), attempts = m.attempts + 1
    WHERE m.seq = p_seq AND m.to_member = me AND m.state = 'queued'
    RETURNING m.done_at INTO v_at;
  ELSE
    UPDATE public.team_messages m SET state = 'done', done_at = now(), claim_lease_until = NULL
    WHERE m.seq = p_seq AND m.to_member = me AND m.state = 'claimed' AND m.claim_token = p_token
    RETURNING m.done_at INTO v_at;
  END IF;
  IF v_at IS NULL THEN
    RAISE EXCEPTION 'seq % is not an open message for % with that claim (already done, claimed '
      'under another token, held, or not yours)', p_seq, me USING ERRCODE = '55000';
  END IF;
  RETURN v_at;
END $$;

-- Give up on a claimed message: back to the queue, or 'failed' after
-- max_claim_attempts so it stops cycling and shows up for an operator.
CREATE OR REPLACE FUNCTION public.teambus_fail(p_claimed text, p_seq bigint, p_token uuid, p_reason text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  maxa int := COALESCE((SELECT max_claim_attempts FROM public.team_settings WHERE id), 5);
  st text;
BEGIN
  UPDATE public.team_messages m
  SET state = CASE WHEN m.attempts >= maxa THEN 'failed' ELSE 'queued' END,
      claimed_by = NULL, claimed_at = NULL, claim_lease_until = NULL, claim_token = NULL,
      last_error = left(p_reason, 2000)
  WHERE m.seq = p_seq AND m.to_member = me AND m.state = 'claimed' AND m.claim_token = p_token
  RETURNING m.state INTO st;
  IF st IS NULL THEN
    RAISE EXCEPTION 'seq % is not claimed by this token', p_seq USING ERRCODE = '55000';
  END IF;
  RETURN st;
END $$;

-- Release a held or failed message back to the queue. The recipient or an owner.
CREATE OR REPLACE FUNCTION public.teambus_release(p_claimed text, p_seq bigint)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  is_boss boolean := public.teambus_role_of(me) = 'owner' OR public.teambus_is_admin();
  st text;
BEGIN
  UPDATE public.team_messages m SET state = 'queued', releases = m.releases + 1,
         attempts = CASE WHEN m.state = 'failed' THEN 0 ELSE m.attempts END
  WHERE m.seq = p_seq AND m.state IN ('held', 'failed') AND (m.to_member = me OR is_boss)
  RETURNING m.state INTO st;
  IF st IS NULL THEN
    RAISE EXCEPTION 'seq % is not a held or failed message you may release', p_seq USING ERRCODE = '55000';
  END IF;
  RETURN st;
END $$;

-- Unread counts for `who`; counts are not secret even when bodies are.
CREATE OR REPLACE FUNCTION public.teambus_unread_counts()
RETURNS TABLE (member text, unread bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT m.name, (SELECT count(*) FROM public.team_messages x
                  WHERE x.to_member = m.name AND x.state IN ('queued', 'held'))
  FROM public.team_members m ORDER BY m.name;
$$;

-- ------------------------------------------------------------------ members

-- Heartbeat. A member beats for itself; an owner or service may beat for a
-- named member (the dispatcher keeps its watched member fresh). p_min_ttl
-- raises the presence TTL so a slow, healthy heartbeat does not read as offline.
CREATE OR REPLACE FUNCTION public.teambus_heartbeat(p_claimed text, p_member text DEFAULT NULL,
                                                    p_min_ttl integer DEFAULT NULL)
RETURNS timestamptz
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  target text := COALESCE(p_member, me);
  v_at timestamptz;
BEGIN
  IF target <> me AND NOT (public.teambus_role_of(me) IN ('owner', 'service') OR public.teambus_is_admin()) THEN
    RAISE EXCEPTION '% may not heartbeat for %', me, target USING ERRCODE = '42501';
  END IF;
  UPDATE public.team_members m SET heartbeat_at = now(), status = 'online', updated_at = now(),
         ttl_s = GREATEST(m.ttl_s, COALESCE(p_min_ttl, 0))
  WHERE m.name = target RETURNING m.heartbeat_at INTO v_at;
  IF v_at IS NULL THEN
    RAISE EXCEPTION '"%" is not on the roster', target USING ERRCODE = '22023';
  END IF;
  RETURN v_at;
END $$;

CREATE OR REPLACE FUNCTION public.teambus_set_policy(p_claimed text, p_member text, p_policy text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  pol text;
BEGIN
  IF p_member <> me AND NOT (public.teambus_role_of(me) = 'owner' OR public.teambus_is_admin()) THEN
    RAISE EXCEPTION 'only % or an owner may change %''s inbound policy', p_member, p_member
      USING ERRCODE = '42501';
  END IF;
  UPDATE public.team_members m SET inbound_policy = p_policy, updated_at = now()
  WHERE m.name = p_member RETURNING m.inbound_policy INTO pol;
  IF pol IS NULL THEN
    RAISE EXCEPTION 'no such member %', p_member USING ERRCODE = '22023';
  END IF;
  RETURN pol;
END $$;

-- ------------------------------------------------------------------ facts

-- Post a fact or directive. Writers to one key are serialized with an advisory
-- lock (which also covers a key that has no row yet), the old head is retired
-- before the new row is inserted, and team_facts_one_head_idx enforces the rule.
CREATE OR REPLACE FUNCTION public.teambus_post_fact(p_claimed text, p_key text, p_kind text,
                                                    p_body text, p_idem text)
RETURNS TABLE (id bigint, supersedes bigint, duplicate boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
#variable_conflict use_column
DECLARE
  me text := public.teambus_actor(p_claimed);
  h text;
  reg public.team_idempotency%ROWTYPE;
  prev bigint;
  prev_kind text;
  new_id bigint;
BEGIN
  IF p_idem IS NULL OR length(p_idem) = 0 THEN
    RAISE EXCEPTION 'an idempotency key is required' USING ERRCODE = '22023';
  END IF;
  IF p_kind NOT IN ('fact', 'directive') THEN
    RAISE EXCEPTION 'unknown fact kind "%"', p_kind USING ERRCODE = '22023';
  END IF;
  IF p_kind = 'directive' AND NOT (public.teambus_role_of(me) = 'owner' OR public.teambus_is_admin()) THEN
    RAISE EXCEPTION 'only an owner may post a directive (% has role %)', me, public.teambus_role_of(me)
      USING ERRCODE = '42501';
  END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('teambus.fact:' || p_key, 0::bigint));

  h := md5(jsonb_build_array(me, p_key, p_kind, p_body)::text);
  INSERT INTO public.team_idempotency (scope, key, payload_hash) VALUES ('fact', p_idem, h)
  ON CONFLICT DO NOTHING;
  IF NOT FOUND THEN
    SELECT * INTO reg FROM public.team_idempotency r WHERE r.scope = 'fact' AND r.key = p_idem;
    IF reg.payload_hash <> h THEN
      RAISE EXCEPTION 'idempotency key "%" was already used for a different fact', p_idem
        USING ERRCODE = '23505';
    END IF;
    RETURN QUERY SELECT reg.ref_id, NULL::bigint, true;
    RETURN;
  END IF;

  SELECT f.id, f.kind INTO prev, prev_kind FROM public.team_facts f
  WHERE f.key = p_key AND f.superseded_by IS NULL FOR UPDATE;
  IF prev_kind = 'directive' AND NOT (public.teambus_role_of(me) = 'owner' OR public.teambus_is_admin()) THEN
    RAISE EXCEPTION 'only an owner may replace or retire the directive "%"', p_key
      USING ERRCODE = '42501';
  END IF;
  new_id := nextval(pg_get_serial_sequence('public.team_facts', 'id'));
  SET CONSTRAINTS public.team_facts_superseded_by_fkey DEFERRED;
  IF prev IS NOT NULL THEN
    UPDATE public.team_facts f SET superseded_by = new_id WHERE f.id = prev;
  END IF;
  INSERT INTO public.team_facts (id, key, kind, body, posted_by, idempotency_key, supersedes)
  VALUES (new_id, p_key, p_kind, p_body, me, p_idem, prev);
  SET CONSTRAINTS public.team_facts_superseded_by_fkey IMMEDIATE;
  UPDATE public.team_idempotency r SET ref_id = new_id WHERE r.scope = 'fact' AND r.key = p_idem;
  RETURN QUERY SELECT new_id, prev, false;
END $$;

-- ------------------------------------------------------------------ tasks

CREATE OR REPLACE FUNCTION public.teambus_task_add(p_claimed text, p_owner text, p_title text)
RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  tid bigint;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.team_members WHERE name = p_owner) THEN
    RAISE EXCEPTION '"%" is not on the roster', p_owner USING ERRCODE = '22023';
  END IF;
  INSERT INTO public.team_tasks (title, owner, created_by) VALUES (p_title, p_owner, me) RETURNING public.team_tasks.id INTO tid;
  RETURN tid;
END $$;

-- The task's owner, its creator, or an owner-role member may close it.
CREATE OR REPLACE FUNCTION public.teambus_task_done(p_claimed text, p_id bigint, p_note text)
RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  me text := public.teambus_actor(p_claimed);
  boss boolean := public.teambus_role_of(me) = 'owner' OR public.teambus_is_admin();
  t text;
BEGIN
  UPDATE public.team_tasks k SET status = 'done', done_at = now(), done_by = me, note = COALESCE(p_note, k.note)
  WHERE k.id = p_id AND k.status = 'open' AND (k.owner = me OR k.created_by = me OR boss)
  RETURNING k.title INTO t;
  RETURN t;   -- NULL: no open task with that id that you may close
END $$;

-- ------------------------------------------------------------------ row security

ALTER TABLE public.team_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_messages_read ON public.team_messages;
CREATE POLICY team_messages_read ON public.team_messages FOR SELECT
  USING (public.teambus_can_read_all() OR from_member = public.teambus_reader() OR to_member = public.teambus_reader());

ALTER TABLE public.team_messages_archive ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_messages_archive_read ON public.team_messages_archive;
CREATE POLICY team_messages_archive_read ON public.team_messages_archive FOR SELECT
  USING (public.teambus_can_read_all() OR from_member = public.teambus_reader() OR to_member = public.teambus_reader());

-- Wake deliveries: dispatcher bookkeeping, for owner/service members only.
ALTER TABLE public.team_wake_deliveries ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS team_wake_deliveries_service ON public.team_wake_deliveries;
CREATE POLICY team_wake_deliveries_service ON public.team_wake_deliveries FOR ALL
  USING (public.teambus_can_read_all()) WITH CHECK (public.teambus_can_read_all());

-- Only the owner may call these; grants.sql gives them to teambus_agent.
DO $$
DECLARE f text;
BEGIN
  FOR f IN SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' AND p.proname LIKE 'teambus\_%'
  LOOP
    EXECUTE 'REVOKE ALL ON FUNCTION ' || f || ' FROM PUBLIC';
  END LOOP;
END $$;
