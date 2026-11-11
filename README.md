# Trading

A self-hosted web app for trading options at Schwab with tastylive-style mechanics, built to
run on TrueNAS. See [PLAN.md](PLAN.md) for the design and roadmap.

**Current state: phase 3.** Live positions, watchlist and trade screen from Schwab, plus a
simulated account: order tickets with Schwab's preview, simulated fills against live prices,
rolls, profit-target orders and a trade journal. Orders are never sent to Schwab.

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
