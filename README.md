# Trading

A self-hosted web app for trading options at Schwab with tastylive-style mechanics, built to
run on TrueNAS. See [PLAN.md](PLAN.md) for the design and roadmap.

**Current state: phase 0.** The app signs in to Schwab, stores the token encrypted, and shows
the linked account's balances. It cannot place orders.

- Deploy: [docs/DEPLOY-TRUENAS.md](docs/DEPLOY-TRUENAS.md)
- Backend: `backend/` (Python, FastAPI, schwab-py)
- Stack definition: `deploy/`

## Development

```sh
cd backend
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
