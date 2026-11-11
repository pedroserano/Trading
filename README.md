# Trading

A self-hosted web app for trading options at Schwab with tastylive-style mechanics, built to
run on TrueNAS. See [PLAN.md](PLAN.md) for the design and roadmap.

**Current state: phase 4, steps 1 and 1b.** Live positions with trade chains and roll
history, a watchlist, a trade screen, a simulated account with order tickets, the imported
trading-history spreadsheet reconciled with Schwab, and a dashboard of what worked. Orders are
never sent to Schwab.

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
