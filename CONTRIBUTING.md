# Contributing to Team Bus

Thanks for helping. Team Bus is small on purpose, so the best contributions keep it small.

## Ground rules

- Open an issue before a large change, so we can agree on the shape first.
- Keep the core rules intact: identity comes from config, never from arguments; reading never deletes; the facts board supersedes and never edits.
- Never commit credentials, DSNs, `.pgpass` contents, hostnames or member configs. Use placeholders.

## Development setup

```bash
git clone https://github.com/jashshah854-a11y/team-bus-oss.git
cd team-bus-oss
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install psycopg2-binary
```

Point a `pg-dsn` config at a throwaway Postgres database (Docker works well) and follow the README quickstart.

## Pull requests

1. One logical change per pull request, with a short description of why.
2. Say how you tested it: the commands you ran and what they printed.
3. Update `CHANGELOG.md` under "Unreleased".

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). By taking part, you agree to it.
