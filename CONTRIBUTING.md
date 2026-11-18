# Contributing to Team Bus

Thanks for helping. Team Bus is small on purpose, so the best contributions keep it small.

## Ground rules

- Open an issue before a large change, so we can agree on the shape first.
- Keep the core rules intact: identity comes from the database login, never from arguments or config; writes go through `api.sql`; reading never deletes; the facts board supersedes and never edits.
- Never commit credentials, DSNs, `.pgpass` contents, hostnames or member configs. Use placeholders.

## Development setup

```bash
git clone https://github.com/jashshah854-a11y/team-bus-oss.git
cd team-bus-oss
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install psycopg2-binary
```

Point a `pg-dsn` config at a throwaway Postgres database (Docker works well) and follow the README quickstart.

Run the tests against a throwaway server where the user may create databases and roles:

```bash
TEAMBUS_TEST_DSN="host=localhost port=5432 user=postgres password=... dbname=postgres" python -m unittest discover -s tests -v
```

Database tests skip when neither `TEAMBUS_TEST_DSN` nor `TEAMBUS_PG_BIN` is set. A change to `api.sql` needs a test that connects as a restricted member login (`tests/pgtools.py`), not as the owner, because the owner bypasses row security.

## Pull requests

1. One logical change per pull request, with a short description of why.
2. Say how you tested it: the commands you ran and what they printed.
3. Update `CHANGELOG.md` under "Unreleased".

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By taking part, you agree to it.
