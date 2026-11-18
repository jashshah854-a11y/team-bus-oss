-- Team Bus v0.2 grants. Run as the database owner after schema.sql and api.sql.
--
-- teambus_agent is a NOLOGIN group role. Each member connects with its own
-- login role, granted teambus_agent and mapped in team_members.db_role:
--   CREATE ROLE teambus_agent NOLOGIN;
--   CREATE ROLE alice_bus LOGIN PASSWORD '...' IN ROLE teambus_agent;
--   UPDATE team_members SET db_role = 'alice_bus' WHERE name = 'alice';
--
-- Least privilege: read the tables, write only through the teambus_* functions
-- (api.sql), which enforce identity, authority and the state machine.
-- No INSERT/UPDATE/DELETE on the core tables, no DDL.

GRANT SELECT ON public.team_settings, public.team_members, public.team_messages,
                public.team_facts, public.team_tasks,
                public.team_messages_archive, public.team_facts_archive
  TO teambus_agent;

-- Dispatcher bookkeeping. Row security (api.sql) limits it to owner/service members.
GRANT SELECT, INSERT, UPDATE ON public.team_wake_deliveries TO teambus_agent;

-- Remove v0.1 direct-write grants if they exist (no-ops on a fresh install).
REVOKE INSERT, UPDATE, DELETE ON public.team_members, public.team_messages, public.team_facts,
                                 public.team_idempotency, public.team_tasks, public.team_settings
  FROM teambus_agent;
REVOKE USAGE, SELECT ON SEQUENCE public.team_messages_seq_seq, public.team_facts_id_seq
  FROM teambus_agent;

DO $$
DECLARE f text;
BEGIN
  FOR f IN SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
           WHERE n.nspname = 'public' AND p.proname LIKE 'teambus\_%'
  LOOP
    EXECUTE 'GRANT EXECUTE ON FUNCTION ' || f || ' TO teambus_agent';
  END LOOP;
END $$;
