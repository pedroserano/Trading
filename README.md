# Trading

A self-hosted web app for trading options at Schwab with tastytrade-style mechanics: sell
premium in high IV around 45 DTE, manage winners at a profit target and everything at 21 DTE,
and keep buying power, delta and theta inside set bounds. It runs as a Docker Compose stack
on TrueNAS. [PLAN.md](PLAN.md) has the design, the phases and what is still open.

## What it does

- **Positions:** live Schwab positions grouped into trade chains, with credit including rolls,
  rule flags (profit target, 21 DTE, tested, loss, earnings) and a History panel per trade.
  A header shows net liq, buying power against the VIX ceiling, beta-weighted delta and theta
  against their portfolio targets, and the data and sign-in status.
- **Portfolio:** buying power by underlying and sector, delta by position, a theta ladder and
  a calendar of what reaches 21 DTE.
- **Watchlist and Trade:** IV rank, expected move, price extremes and earnings dates; a chain
  with quick builders (strangle, straddle, verticals, iron condor, jade lizard, covered call,
  ratios), payoff chart, and the portfolio after the trade.
- **Orders:** tickets for opening, closing and rolling, checked by server-side guards and
  Schwab's preview, on a simulated account or the Schwab account.
- **Automation:** automanage per trade (profit targets, 21 DTE rolls, tested-side repairs,
  earnings exits) and a scanner that finds new trades, each at Off, Notify, Propose or Auto.
- **Activity and Dashboard:** working orders, the journal, an order check against
  thinkorswim, and results from the imported trading history plus everything since.

## Safety switches

Orders go to Schwab only when `LIVE_TRADING=true` is set in `.env`; there is no second switch
in Settings. Live orders are also limited to an allowlist of underlyings (or any underlying, if
ticked), a contract cap per leg and a buying-power cap per trade, and each one you send needs
Schwab's preview and a second confirm.

Every account ticked on Schwab's consent screen at sign-in is used at once, named as in Schwab
(the header picker switches between them): each has its own positions, trade history,
automanage levels, scanner level and Schwab limits, while the rules are shared. The
trading-history workbook can be imported on Settings, Connection: Joint rows go to the first
account, Individual rows to the account named Individual.

Automation places Schwab orders by itself only with `LIVE_AUTO=true` in `.env` as well; without
it, Auto is not offered for the Schwab account. Each account has its own automanage levels and
scanner level (Settings, Automation and Scanner), and the top of both tabs lists what runs by
itself on each account. Automation also stops for
the day at a daily loss limit, holds while prices are stale or the Schwab sign-in is about to
lapse, and can be paused from the header.

## Layout

- `backend/`: Python 3, FastAPI and schwab-py; the server-rendered Settings page is in
  `backend/app/templates/`.
- `frontend/`: React and TypeScript, built with Vite and served by the backend.
- `deploy/`: the Compose stack (app, PostgreSQL, Caddy) and an example `.env`.
- `docs/DEPLOY-TRUENAS.md`: installing on TrueNAS, every setting in `.env`, and the Schwab
  sign-in.

## Development

```sh
# Backend: tests and lint, as CI runs them
cd backend
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .

# Frontend: type check and build
cd frontend
npm ci
npm run build
```

For a local preview without Schwab, set `DEMO_MODE=true`: the app then shows a simulated
account with demo prices. During frontend work, run the backend on port 8000 and `npm run
dev`; Vite proxies the API calls.
