-- Team Bus grants. Create a restricted `teambus_agent` login role, then run
-- this file as the database owner after schema.sql.
-- Least privilege: row DML on the team tables only, plus sequence use.
-- No DELETE (the bus is an audit trail; rows are never deleted),
-- no DDL, nothing on any other table.

GRANT SELECT, INSERT, UPDATE ON public.team_members  TO teambus_agent;
GRANT SELECT, INSERT, UPDATE ON public.team_messages TO teambus_agent;
GRANT SELECT, INSERT, UPDATE ON public.team_facts    TO teambus_agent;
GRANT USAGE, SELECT ON SEQUENCE public.team_messages_seq_seq TO teambus_agent;
GRANT USAGE, SELECT ON SEQUENCE public.team_facts_id_seq     TO teambus_agent;

-- Archive tables: agents may read history, never move or remove rows.
-- `teambus.py compact` runs as the database owner, not as teambus_agent.
GRANT SELECT ON public.team_messages_archive TO teambus_agent;
GRANT SELECT ON public.team_facts_archive    TO teambus_agent;
