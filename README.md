# Trading

A self-hosted web app for trading options at Schwab with tastylive-style mechanics, built to
run on TrueNAS. See [PLAN.md](PLAN.md) for the design and roadmap.

**Current state: phase 2.** Read-only: the app shows live positions with tastylive-style
flags, a watchlist with IV rank, and a trade screen with an option chain, strategy builders
and payoff analysis. It cannot place orders.

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
