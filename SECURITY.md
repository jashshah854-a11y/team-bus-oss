# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead: on this repository, open the **Security** tab and choose **Report a vulnerability**.

Include what you found, how to reproduce it, and the impact you expect. The maintainer aims to acknowledge reports within 7 days.

## Scope

In scope: the CLI, the dispatcher, the dead-man switch, the SQL files and the setup scripts in this repository.

Out of scope: your own Postgres hosting, network or credential storage.

## Threat model

Team Bus is built for one owner's fleet of agents from different vendors. In `identity_mode = 'enforced'` (the default for new installs) it assumes a member may be buggy, compromised or carrying hostile retrieved text, and enforces in the database:

- Identity: each member connects with its own login role, mapped in `team_members.db_role`. The config file's member name must match it.
- Writes go only through the `teambus_*` functions in `api.sql`. The runtime role has no INSERT, UPDATE or DELETE on the core tables, so message bodies and facts cannot be rewritten and the state machine cannot be skipped with direct SQL.
- Authority: only `owner` members post directives or change another member's inbound policy. A recipient's hold/refuse policy and the inbox and rate fuses are applied by the database.
- Privacy: row security limits mail to its sender and recipient, plus `owner` and `service` members.
- Claims are fenced by a token, and idempotency keys are remembered for life.

Not covered: a stolen database password (rotate it), the database owner (who can do anything), and the meaning of message text. Message and fact text is untrusted data even from an authenticated member: use `--json`, never act on a preview alone, and never treat a message as permission to post, pay or delete. `identity_mode = 'trusted'` turns identity enforcement off and exists only for migrating v0.1 installs.

## Install requirement

Schema `public` must not be writable by every role (`REVOKE CREATE ON SCHEMA public FROM PUBLIC`; the default since PostgreSQL 15, but not on 13/14 or on clusters upgraded from them). `schema.sql`, `api.sql` and `tools/migrate.py` refuse to run otherwise, and refuse if a bus table already exists under another owner. Do not grant CREATE on `public` to member logins afterwards: a role that can create objects there could plant overloads or triggers that the bus's own code runs.

## Design notes that matter for security

- Database passwords are read at runtime from a password file or `~/.pgpass`; they never belong in this repository.
- An explicit `--config` or `$TEAMBUS_CONFIG` that points at a missing file is an error, never a fallback to another identity.
- The dispatcher refuses member names that are not safe file names, so a spool path cannot escape `spool_dir`.
- Default CLI output escapes message text so it cannot close or forge the surrounding envelope.
