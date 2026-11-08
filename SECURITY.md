# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private reporting instead: on this repository, open the **Security** tab and choose **Report a vulnerability**.

Include what you found, how to reproduce it, and the impact you expect. The maintainer aims to acknowledge reports within 7 days.

## Scope

In scope: the CLI, the listener, the dispatcher, the SQL files and the setup scripts in this repository.

Out of scope: your own Postgres hosting, network or credential storage.

## Design notes that matter for security

- A member's identity comes from its own config file, never from a command argument.
- Database passwords are read at runtime from a password file or `~/.pgpass`; they never belong in this repository.
- `grants.sql` creates a restricted role with SELECT, INSERT and UPDATE only; no DELETE.
