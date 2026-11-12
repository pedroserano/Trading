# Plan: self-hosted options trading app for Schwab, built around tasty mechanics

Status: draft for review. Nothing is built yet.

## 1. What this is

A single-user web app that runs as a Docker Compose stack on TrueNAS, connects to one
Schwab login through the Schwab Trader API, and is organised around the tastylive way of
trading short premium: find high-IV underlyings, sell around 45 DTE, manage winners at a
profit target, manage everything at 21 DTE, and keep portfolio buying power, delta and
theta inside set bounds.

By default the app assists and the trader decides: every order is previewed and confirmed
by a click. From phase 4, each position can be handed to automanage, and from phase 5 the
app can also find and open trades itself, both under the same rules and limits, both off
until switched on, and both proven on the simulated account first (section 11).

## 2. Constraints found in research

These shape the design more than any framework choice.

| Constraint | Consequence for the design |
|---|---|
| Schwab refresh tokens last about 7 days and cannot be extended; access tokens last about 30 minutes. | A weekly browser re-login is unavoidable. The app needs a first-class re-auth page, a token-age indicator in the header, and a notification 48 hours before expiry. |
| The OAuth callback URL must be HTTPS and must match the registered string exactly. | Use the paste-the-redirect-URL flow, which works on a headless NAS, and register `https://127.0.0.1:8182`. No public exposure of the NAS is needed. |
| Schwab appears to allow one valid sign-in per app and limits streamer sessions per account. | Exactly one backend process owns the token and the single streamer connection, and fans data out to browser tabs. Never one Schwab stream per tab. |
| The API has no IV rank field. | Compute IV rank ourselves from a stored daily IV history per underlying (section 6). |
| The API has no paper trading and no historical option prices. | Build a simulated broker for dry runs. Backtesting is out of scope. |
| Schwab's order JSON for multi-leg orders is intricate, and library templates cover only singles and verticals. | Write our own strategy-to-order builder with heavy unit tests, and run every order through Schwab's preview endpoint before sending. |
| Order requests (place, replace, cancel) are throttled per account at a figure set on the app registration, from 0 to 120 a minute. Reading orders is unthrottled. No market data limit is stated. | Central rate limiter in the Schwab client, streaming preferred over polling, limits kept in config. Check the app's order throttle is above zero before phase 4. |
| Order entry through the API covers only the `EQUITY` and `OPTION` asset types. | Matches the chosen scope. Whether cash-settled index options such as SPX are accepted as `OPTION` is untested and gets checked with a preview call before any index product is added. |
| The target NAS runs TrueNAS 25.10.7, which runs apps as plain Docker Compose ("Install via YAML") and requires a top-level `services` key. | Ship a standard compose file. No Kubernetes or Helm work. |

Confirmed from Schwab's own portal documentation (OAuth guide, order samples, both API
specifications, streamer guide): the 30-minute and 7-day token lifetimes, the HTTPS callback
rule, the limit of one streamer connection per user, the order throttle above, and the
existence of a preview-order endpoint. The registered app has both products, shows
"Ready For Use", and uses the callback `https://127.0.0.1:8182`.

Still not documented in what has been reviewed, and to be settled by testing in phase 0:
the streamer's symbol limit (the guide confirms one exists, error code 19, without a number),
the streamer field numbering for options and account activity, and the order JSON for
strangles, iron condors and rolls. The portal's samples stop at a two-leg vertical, sent as
`NET_DEBIT` or `NET_CREDIT` with an `orderLegCollection`. schwab-py's source covers the field
numbering, and its order-capture script covers the JSON.

## 3. What others have built

No existing project covers this use case. Each of these is useful for one idea.

| Project | What it is | What to take from it |
|---|---|---|
| [alexgolec/schwab-py](https://github.com/alexgolec/schwab-py) | The established Python wrapper (MIT, successor to tda-api). Separate HTTP and streaming clients, order builder. | Candidate client library. Its record-an-order-then-generate-code script is the fastest way to learn the exact JSON for strangles, condors and rolls. |
| [tylerebowers/Schwabdev](https://github.com/tylerebowers/Schwabdev) | Most-starred Python wrapper (MIT). Sync and async, automatic token handling, streamer auto start and stop with market hours, auto restart. | Candidate client library. Its docs include an iron condor sent as one `NET_CREDIT` order. |
| [BardsWork/schwab-gateway](https://github.com/BardsWork/schwab-gateway) | FastAPI service holding one token for a whole network, with a WebSocket relay. Read-only. | The paste-URL re-auth page, expiry monitor, and one-token-owner pattern. Its warning: it has no authentication, so ours must. |
| [ashwinurao/edge-trader-for-schwab](https://github.com/ashwinurao/edge-trader-for-schwab) | Click-to-trade day-trading dashboard, FastAPI and SQLite. | Preview before send, a LIVE/STALE/DISCONNECTED light that disables order buttons on stale data, no automatic order retries, and a guard against closing one leg of a spread by accident. |
| [kalebhoffer/optiondesk](https://github.com/kalebhoffer/optiondesk) | Automated intraday long-option bot with paper broker. | Paper mode by default, live mode behind two separate switches, risk gates as a separate module, and a SQLite journal of every decision. |
| [jttyeung/Trading_Dashboard_App](https://github.com/jttyeung/Trading_Dashboard_App) | Read-only Next.js portfolio dashboard fed by a separate Schwab bridge, private over Tailscale. | Tailscale for remote access and a secret-scanning pre-commit hook. |
| [yomikoye/optionable](https://github.com/yomikoye/optionable) | Self-hosted wheel tracker. | Roll chains: linking each roll to its predecessor so a position's full credit history is tracked. |
| [EazyDuz1t/EzOptions-Schwab](https://github.com/EazyDuz1t/EzOptions-Schwab) | Flask and Plotly GEX dashboard on Schwab data. | Evidence that Schwab chain data is good enough for live Greek analytics. |

The gap this project fills: none of them combines Schwab order entry for multi-leg short
premium, position grouping with roll history, and tasty-style portfolio rules.

## 4. Architecture

```
Browser (React SPA)
   |  HTTPS: REST + one WebSocket
Caddy (TLS, reverse proxy)
   |
Backend (FastAPI, one process)
   |-- Schwab client: token store, rate limiter, REST
   |-- Streamer manager: one Schwab WebSocket, subscription ref-counting
   |-- Domain: positions, strategy grouping, Greeks, IV rank, rules engine
   |-- Order service: builder, preview, guards, submit, audit log
   |-- Scheduler: IV snapshots, token expiry alerts, reconciliation
   |
PostgreSQL (trades, roll chains, IV history, audit log, settings)
```

### Backend: Python 3.12, FastAPI, asyncio

Python because both mature Schwab libraries are Python and the numerical work (Greeks,
probability, beta weighting) is simplest there. One process, because one process must own
the token and the stream.

Client library recommendation: **schwab-py**, wrapped behind our own `BrokerClient`
interface so it can be swapped. It is the thinner wrapper, built on httpx with a separate
async streaming client, which suits an asyncio service. Schwabdev is the fallback and has
better stream lifecycle handling out of the box. The phase 0 spike confirmed schwab-py 1.5.1 covers what the
backend needs: sign-in from a pasted redirect address, custom token storage (used for
encryption at rest), and an async client.

The `BrokerClient` interface gets two implementations: `SchwabBroker` and `SimBroker`.
`SimBroker` uses live market data and fills simulated orders at mid minus a configurable
slippage. It is what the app runs against until live trading is deliberately switched on.

### Account targeting

The Schwab login may hold several accounts; the app trades exactly one. Two layers enforce that:

1. At the Schwab consent screen during sign-in, tick only the account the app should see. Schwab then issues a token that cannot reach the others at all. This is the strong control, and it is repeated at each weekly re-login.
2. On first run the app lists the accounts the token can see (`GET /accounts/accountNumbers`, which returns each number with the hashed value the API uses in URLs), and the user picks one. That hash is stored, every order and account call goes through one function that uses only it, and the server refuses to start in LIVE mode if the token exposes more accounts than the one selected unless an explicit override is set.

The header shows the last four digits of the active account at all times.

### Weekly Schwab sign-in

The 7-day refresh token cannot be extended, so the aim is a 30-second sign-in that never lapses by surprise.

- **One-tap flow.** A second callback URL pointing at the NAS (for example `https://trading.lan/auth/callback`) is added to the Schwab app alongside `https://127.0.0.1:8182`; Schwab accepts several, comma-separated, within 255 characters. Schwab redirects the browser to that address and the backend captures the code, checks the `state` value it issued, and exchanges it for tokens. The address only needs to resolve on the home network.
- **Fallback.** The paste-the-redirect-URL page stays, using the `127.0.0.1` callback. It is the only flow until the new callback is approved, since editing the app may put it back through Schwab's approval, and until the NAS address has a certificate the browser trusts (Caddy's internal CA installed on the phone and computer, or a real certificate through a DNS challenge on an owned domain).
- **Scheduled reminder.** A sign-in at any time restarts the 7-day clock. The app sends a notification with a sign-in link at a chosen weekly time (default Sunday 18:00 Eastern), again at 48 and 24 hours before expiry if still not done, and immediately if a refresh fails, which is what a Schwab password change causes.
- **Lapse behaviour.** If the token expires, the app shows a full-width banner, disables order entry, and keeps the last known positions marked stale. Orders already resting at Schwab, including GTC profit-taking orders, are unaffected.
- **Not done.** No automated login with stored Schwab credentials.

### Database: PostgreSQL

Postgres rather than SQLite because the IV history and tick-derived snapshots grow, and
concurrent writes from the streamer and scheduler are simpler. SQLite would work for a
lighter first version; Postgres costs one more container. Migrations with Alembic.

Core tables: `accounts`, `positions_snapshot`, `strategies` (a grouped trade),
`strategy_legs`, `roll_links`, `orders`, `fills`, `iv_history`, `watchlist`,
`rule_settings`, `alerts`, `audit_log`.

### Frontend: React, TypeScript, Vite

- Data: TanStack Query for REST, one WebSocket with a small store (Zustand) for live quotes.
- Tables: AG Grid Community or TanStack Table. Positions and chains are dense, live-updating grids, and this is the most important UI choice.
- Charts: TradingView Lightweight Charts for price, and a custom SVG or canvas component for the payoff curve.
- Styling: Tailwind with a small set of headless components. Dark theme first.

Built to static files and served by the backend or Caddy, so there is no Node runtime in production.

## 5. Tasty mechanics as configurable rules

Defaults follow tastylive's published research. Every number is a setting, because an
expert will have their own.

**Entry**
- IV rank above 30 preferred (highlight above 50). IV percentile shown alongside.
- Target 45 DTE: pick the monthly expiration nearest 45 days, within 30 to 60.
- Strike selection by delta: 16 delta for strangles, 30 delta shorts for defined risk, 20 delta shorts with wings for iron condors. Collect about one third of the width on verticals and condors.
- Liquidity filter: bid-ask width, open interest, and volume on the chosen strikes.
- Earnings and ex-dividend dates flagged inside the trade window.
- Assignment risk: SPY options are American-style and physically settled, so short in-the-money calls are flagged ahead of the quarterly ex-dividend date, and any short option in the money near expiration is flagged. An assignment that arrives as a stock position is recognised and attached to the strategy it came from.

**Management**
- Profit target: 50% of max profit for strangles, verticals and condors; 25% for straddles. Offer a GTC closing order at the target the moment the opening order fills.
- 21 DTE: every position at or under 21 DTE is flagged to close or roll.
- Tested side: flag when a short strike's delta passes a threshold (default 30) or is breached; suggest rolling the untested side toward the money, or rolling out in time for a credit.
- Loss guide: flag undefined-risk trades at 2x credit received.
- Rolls must be for a net credit by default; a debit roll needs an explicit override.
- Guts to wings: an inverted strangle, or a straddle or narrow strangle with a short past 0.70 delta, closes the guts and sells a new 16-delta strangle in the same expiration, paying at most the in-the-money value plus a small allowance.

**Portfolio**
- Buying power in use against a VIX-scaled ceiling. Commonly cited tastylive tiers are 25% under VIX 15, 30% at 15 to 20, 35% at 20 to 30, 40% at 30 to 40, 50% above 40. One third-party source I found states different figures, so treat these as defaults to check against tastylive's own material.
- Per-position size: buying power effect as a share of net liq, default cap 5% undefined risk, 3% defined risk.
- Beta-weighted delta to SPY, shown in deltas and as a dollar move per 1% SPY move.
- Daily theta as a share of net liq, target band 0.1% to 0.3%.
- Delta bias band for beta-weighted delta as a share of net liq (neutral, bullish or bearish). Bands are corrected by what the scanner opens and by notifications, never by automatic hedge trades.
- Concentration by underlying and by sector.

The rules engine is a pure function: portfolio state plus settings in, a list of flags
out. That makes it testable and keeps it from ever touching orders directly.

## 6. Derived data we must compute

- **IV rank and percentile.** Nightly job stores a 30-day constant-maturity ATM IV for every watchlist and held underlying, interpolated from the two expirations bracketing 30 days. Rank is (current minus 52-week low) over (high minus low). A year of history has to accumulate, so seed it: for index products use the public vol indices (VIX, VXN, RVX, and the CBOE single-stock vol indices where they exist); for others show "building, N days" until enough history exists. The IV source sits behind an `IvProvider` interface so an external source can fill the gap (options below).

  Schwab is the only IV source in v1. No tastytrade account and no paid feed. How the one-year gap is handled:

  - Index and ETF underlyings (SPX, SPY, QQQ, IWM, and others with a CBOE vol index) get a true IV rank from day one, because Schwab serves price history for the vol indices (VIX, VXN, RVX and similar).
  - Single stocks start collecting on the day they join the watchlist, so the watchlist should be loaded in phase 1, well before trading starts. Until 252 trading days exist the app shows the rank over the history it has, labelled with the day count, alongside two measures available immediately from Schwab data: IV against 30-day historical volatility (from Schwab price history), and the IV percentile within the current chain's term structure.
  - Optional later shortcut, only if the wait proves annoying: a one-month backfill from a paid source. [IVolatility](https://stockmarketstack.com/tools/ivolatility-api) (from $79 a month, history to 2005) is the candidate; [ORATS](https://orats.com/data-api) ($199 a month listed) is more than needed. The IV store sits behind an `IvProvider` interface so a backfill is an import, not a redesign.

- **Greeks.** Use Schwab's per-contract Greeks from the chain and the `LEVELONE_OPTIONS` stream. Recompute with our own model (Black-Scholes, with an American approximation for equity options) as a cross-check and for what-if analysis. One researcher found Schwab's reported IV inconsistent with its prices, so the cross-check has real value.
- **Probability of profit, expected move, breakevens** from the chain.
- **Beta weighting.** Betas computed from daily price history against SPY, refreshed weekly.
- **Buying power effect.** For defined risk it is computable (width minus credit). For undefined risk, use Schwab's order preview response as the source of truth before entry, and Schwab's reported maintenance requirement afterwards.
- **Strategy grouping.** Schwab reports flat legs. Auto-detect strategies from fills that arrive in one order, let the user regroup manually, and persist the grouping. tastytrade's own platform loses custom groups on rolls; ours should carry the group through a roll via `roll_links`.

### Trade chains and roll history

Each position is tracked as a trade chain, the way tastytrade shows a position's roll history:
one chain from the opening order, through every roll and adjustment, to the final close,
expiration or assignment.

- **Events.** Each chain records every order that touched it: open, roll, adjust, close,
  expire, assign. Each event keeps its legs and fill prices, the net credit or debit, and
  commissions.
- **Running totals.** The chain shows the net credit taken in so far including all rolls,
  realized P/L on legs already closed, open P/L on the legs still held, total P/L, days in
  the trade, and the number of rolls. The Positions screen's credit and % of max profit use
  the chain's cumulative credit, so a strangle rolled twice for credits shows its true
  breakeven and profit.
- **Chain view.** From any position, a History panel shows the timeline: each roll with its
  date, the strikes and expiration it moved from and to, the credit for that roll, and the
  running total, ending in the current open legs. Closed chains appear in the journal with the
  same view and feed the results by strategy.
- **Simulated account.** Already works this way since phase 3: every simulated order belongs
  to a chain. Phase 4 adds the History panel.
- **Schwab account.** Orders placed through the app are attached to their chain when placed.
  Orders placed elsewhere, such as in thinkorswim, are picked up from Schwab's transaction
  history (polled every few minutes, and back-filled once as far as Schwab's history goes) and
  linked by rule: an order that only opens starts a chain; an order that closes legs of a
  chain and opens new legs on the same underlying is a roll of that chain; expirations and
  assignments close or convert legs. Shares received by assignment stay in the chain, so a
  put assignment followed by covered calls reads as one trade. When a link is ambiguous (for
  example a close and a separate open minutes apart), the app proposes it and a one-click
  Link or Split corrects it.
- **Commissions.** Taken from Schwab's transactions for the real account; $0.45 per contract
  for the simulated account (a setting).

### Trading history from the Google Sheet, and the analysis dashboard

The trading history kept in the "Options P/L - Auto" Google Sheet since July 2020 is imported
into the app, reconciled with Schwab, and analysed alongside every trade the app records.

**What the sheet contains** (reviewed November 2026):

- **Trades tab:** one row per leg. Columns A–P hold the opening: underlying, strike, strategy
  code, expiration, date, stock price at the time, quantity in shares, premium, commission,
  breakeven, max loss and a buying power estimate. Q is the account. R is the status: Closed,
  Rolled, Expired, Assigned or Open. S–Y hold the closing: date, quantity, price, commission,
  cost, P/L, % return and days held. Multi-leg P/L and cost basis (AB–AC) are filled for a few
  combined trades.
- **Strategy codes:** P and C are short puts and calls, P+ and C+ are long; Vert P, Vert C, IC
  and STR are spreads entered as one row with a net price; a blank code is stock.
- **Summary tab:** P/L by underlying, and monthly option income by year for each account.
- **Accounts:** Joint (3,840 rows, July 2020 onwards), Individual (963 rows, from 2023) and
  Traditional IRA (32 rows, from 2025).
- **Joint rows by type:** 3,335 single-leg options, 103 spreads with net prices only, 99 stock,
  292 futures options (/ES, /NQ), and about 15 rows with missing or unusual fields to review.

**Writing new trades into the sheet: not recommended.** It is possible: the app could append
rows to the Trades tab through Google's Sheets API with a Google service account, copying the
row formulas. But it is fragile. A moved column, renamed tab or edited formula would make the
app write to the wrong place. Rows already entered by hand would need matching to avoid
duplicates. The app would also hold a Google credential with write access to the sheet.
Instead:

- **Export in the sheet's format.** A button on the dashboard downloads any period's trades
  as rows laid out exactly like the Trades tab (columns A–Y, values only), to paste in if you
  want to keep the sheet going.
- **Direct writing** can still be added later (phase 6) if the export proves too manual.

**Import and reconciliation (Joint account):**

- **Upload.** The sheet is uploaded as an .xlsx file on the Settings page; the app needs no
  Google access. Uploading again later is safe: rows already imported are recognised and only
  new or changed rows are added.
- **Conversion.** Each single-leg row becomes an opening fill and a closing, expiration or
  assignment fill. Rows marked Rolled are linked to the leg opened the same day on the same
  underlying, so roll chains match how they were traded. Spreads entered as one row are kept
  with their net prices. Stock rows become share positions. Futures options are kept as
  history for the P/L figures but are not reconciled, since the app does not trade futures.
- **Reconciliation with Schwab.** Where the sheet overlaps Schwab's transaction history (up to
  a year), each sheet leg is matched to a Schwab fill by contract, side, quantity and date (one
  trading day either way). Matched legs use Schwab's exact prices, times and fees, and keep the
  sheet's roll links as a hint. Legs only in the sheet, or only at Schwab, appear on a review
  list with Accept and Ignore, since they usually mean another account's trade or a typo.
- **Before the overlap,** the sheet is the only record, and its trades are labelled "from
  sheet" wherever they appear.
- **Other accounts.** Individual and Traditional IRA rows can be imported the same way as
  history-only accounts for the dashboard, without reconciliation, if wanted.

**Dashboard: what worked and what did not.** A Dashboard page over every closed chain: the
imported history, Schwab trades since, and optionally the simulated account.

- **Overview:** cumulative P/L, P/L by month and year (the Summary tab's monthly income table),
  by underlying and by strategy; win rate, average winner and loser, largest losses.
- **Breakdowns.** For each factor below, closed trades are bucketed and each bucket shows the
  number of trades, win rate, total and average P/L, P/L per day held, and average share of
  the credit kept:

  | Factor | Sheet history | Trades from now on |
  |---|---|---|
  | Strategy, underlying, DTE at entry, days held, number of rolls, day of week | Yes | Yes |
  | How it was managed: closed early (at what % of max profit), rolled, held to expiration, assigned | Yes | Yes |
  | Size: buying power estimate and contracts; as a share of net liq | Estimate and contracts only | Yes |
  | Distance out of the money at entry (strike against the stock price recorded in the sheet), as a stand-in for delta | Yes | Yes, plus actual delta |
  | VIX at entry and its change during the trade | Yes, from Schwab's VIX price history | Yes |
  | Underlying move during the trade | Yes, from Schwab's daily price history | Yes |
  | IV rank at entry | SPY and SPX only, from VIX history | Yes |
  | Probability of profit at entry | Estimate for SPY and SPX only, using VIX as the volatility | Yes, captured at the order |
  | Days to earnings at entry and whether earnings fell inside the trade | If the earnings source provides past report dates | Yes |

- **Why.** A factor table ranks the buckets that did meaningfully better or worse than the
  overall result, with their sample sizes, so a small sample is not mistaken for a pattern.
  A losers report lists the worst trades with every factor value side by side. A mechanics
  check compares trades that followed the rules (entry near 45 DTE, managed at 50% or by 21
  DTE) with those that did not. These show what went with good and bad results; they cannot
  prove why.
- **Monthly totals and the Summary tab.** The dashboard counts the Joint account only in
  every year, and counts expired legs on their expiration date. The Summary tab's monthly
  table sums every account for 2020-2023 (Joint only from 2024) and leaves out legs that
  expired without a closing date, so some months differ; the dashboard's figures equal the
  sum of the Trades tab's P/L column for Joint rows by month.
- **From now on,** every order placed through the app records a snapshot of the conditions:
  IV, IV rank, VIX, delta, probability of profit, size against net liq, days to earnings. So
  the analysis gets more complete with every trade.

## 7. User interface

Design principles for an expert user: dense rather than spacious, numbers over prose,
nothing explained that an options trader already knows, every frequent action reachable
from the keyboard, and no modal that is not an order confirmation.

Persistent header on every screen: net liq, buying power used against the VIX ceiling,
beta-weighted delta, theta, VIX, data status light, token days remaining, and a
SIM or LIVE badge that cannot be missed.

**Screens**

1. **Positions** (home). Grouped by underlying, then strategy. Columns: strategy, DTE, quantity, credit received including rolls, current mark, P/L open, percent of max profit, delta, theta, short-strike deltas, buying power, IV rank. Rule flags appear as coloured chips on the row (50% reached, 21 DTE, tested, earnings). Row actions: Close, Roll, Set GTC, Adjust. Toggle between underlying grouping and a flat list sorted by DTE or by flag.
2. **Trade** (one underlying). Left: chain centred on 45 DTE with delta, bid, ask, mid, IV, open interest; click bid to sell, ask to buy. Right: strategy quick-builders (strangle, straddle, put vertical, call vertical, iron condor, jade lizard, covered call, cash-secured put, ratio) that pre-select strikes by the configured deltas. Below: payoff curve with expected-move shading, plus max profit, max loss, breakevens, probability of profit, buying power effect and the portfolio's delta and theta after the trade.
3. **Order ticket.** Legs, limit price with a slider from natural to mid to opposite side, and nudge buttons in tick steps. Preview calls Schwab and shows commission, buying power effect and any Schwab warnings. Confirm sends. Keyboard: arrows adjust price, Enter previews, Ctrl+Enter sends.
4. **Roll ticket.** Opened from a position. Shows the closing legs, proposes the same strikes in the next monthly cycle, displays net credit or debit for the roll and the new cumulative credit and breakevens. Sent as one order.
5. **Watchlist and scanner.** Sortable by IV rank, with IV percentile, expected move, liquidity grade, earnings date and existing exposure. One click opens Trade.
6. **Portfolio.** Buying power by underlying and sector, beta-weighted delta by position, theta ladder, expiration calendar showing what crosses 21 DTE each week.
7. **Activity.** Working orders with cancel and replace, fills, and the journal: every strategy from open through rolls to close, with realised P/L and stats by strategy type and underlying (win rate, average days held, P/L per day).
8. **Settings.** Rule thresholds, Schwab connection and re-auth, notifications, SIM or LIVE switch.

Mobile: Positions, alerts and closing orders should work on a phone. Full chain trading is desktop-first.

## 8. Safety and security

This app can move real money and holds credentials that can too.

**Order safety**
- SIM is the default. LIVE requires both an environment variable on the container and a setting in the UI.
- Every manual order goes through preview, then an explicit confirmation showing the final legs, price, and max loss. Automated orders go through the same preview and guards, within the limits in section 11, and each one is notified and logged.
- Hard guards, rejected server-side: max contracts per order, max buying power effect per order, limit price outside a band around mid, orders while data is stale, market orders on multi-leg strategies, selling to open something that leaves a naked position not selected deliberately.
- No automatic retries. An order with an unknown outcome blocks further orders on that underlying until reconciled against Schwab.
- Reconcile positions and working orders against Schwab on startup and every few minutes. Schwab is always the source of truth.
- Append-only audit log of every order request, preview response, and broker response.

**Security**
- The app has its own login (single user, strong password hashed with argon2, TOTP second factor, short session lifetime). schwab-gateway and edge-trader both ship with no login, which is not acceptable for something that places orders.
- Schwab app key, secret and tokens are encrypted at rest with a key supplied as a Docker secret, stored on an encrypted ZFS dataset.
- Never port-forward the app to the internet. v1 is reachable on the home network only. Remote access is a later, optional step: Tailscale is the simplest route (free personal tier, an official TrueNAS app, and a phone app), and adding it needs no change to this app.
- TLS from Caddy with an internal certificate authority or a DNS-challenge certificate.
- Secret scanning in pre-commit and CI, and no credentials in the repository.

## 9. Deployment on TrueNAS

- Target: TrueNAS 25.10.7 (Docker-based apps). The compose file uses the top-level `services` key that 25.10 requires.
- Three containers: `backend`, `postgres`, `caddy`. Images built by GitHub Actions and published to GHCR, so the NAS only pulls.
- Datasets: `apps/trading/postgres`, `apps/trading/config`, `apps/trading/backups`, with a ZFS snapshot task on each and a nightly `pg_dump` into `backups`.
- Install through Apps, Discover, Custom App, Install via YAML. The custom app holds a two-line stub that pulls in the real compose file from a dataset with `include:`, which keeps the real file under version control.
- Health checks on all three containers, container time zone set to US Eastern, market calendar (including holidays and early closes) from `pandas_market_calendars` or `exchange_calendars`.
- Notifications: Pushover as the default (one-time app purchase of about $5, one HTTPS call to send, works away from home with no extra setup), with email over SMTP as a second channel. Events: token expiring, rule flags, fills, stream down. Messages carry symbols and flags but no account numbers or balances, since they pass through a third party. The notifier is an interface, so ntfy or others can be added.

## 10. Phases

| Phase | Scope | Exit test |
|---|---|---|
| 0. Setup | Register the Schwab developer app (approval takes days and status must read "Ready For Use"), repo scaffold, CI, compose skeleton, library spike. | Container on TrueNAS completes OAuth and prints account balances. |
| 1. Read-only portfolio | Token lifecycle, paste-URL and one-tap sign-in with weekly reminder, accounts and positions, quotes refreshed every 5 seconds and pushed to the browser (Schwab streaming deferred to phase 2, once the REST path is proven on real data), strategy grouping, Positions screen, header metrics, app login. | Positions screen matches thinkorswim for P/L, delta and theta across a full trading day. |
| 2. Analysis | Schwab streaming for quotes and account activity, chain view, payoff curve, Greeks cross-check, IV history job and IV rank, watchlist, beta weighting, rules engine and flags, notifications. | Flags fire correctly against a recorded set of test portfolios. |
| 3. Simulated trading | Order builder for all listed strategies, preview against Schwab, `SimBroker`, order and roll tickets, journal, roll chains. | Two weeks of simulated trading with builder output checked against orders built by hand in thinkorswim. |
| 4. Live trading and automanage | Live order placement through Schwab with the phase 3 guards and preview, audit log table, reconciliation, cancel and replace, GTC profit targets resting at Schwab. Trade chains for the Schwab account from transaction history, and the History panel. Then per-position automanage (section 11), first on the simulated account, then live. Start live with one-lot defined-risk trades. | A month of small live trades with zero reconciliation mismatches, and every roll from that month shown correctly in its chain. Automanage: two weeks on the simulated account with every action reviewed, then two weeks live in Notify and Propose modes before any Auto. |
| 4 (cont.). History import and dashboard | Sheet upload and conversion, reconciliation with Schwab and the review list, Dashboard page with the breakdowns above, export in the sheet's format, entry snapshots on every order. Built after trade chains and before automanage. | Every Joint row from the sheet either imported or on the review list with a reason; overlap totals within a few dollars of Schwab's; before Schwab's record, the dashboard's monthly totals equal the sheet's own P/L column (Y) by closing month. |
| 4a. SPY-only first year | LIVE mode restricted to SPY by an allowlist setting, one-lot default and a low per-order buying power cap. Other underlyings stay available in SIM. | A year of SPY trading reviewed in the journal before the allowlist is widened. |
| 5. Opportunity scanner and automated opening | Scanner over the watchlist using the entry rules, ranked trade ideas, then Propose and Auto modes for opening trades (section 11), simulated account first. | A month of the scanner in Ideas mode on the simulated account, compared with what you would have chosen; then a month of Auto on the simulated account inside all limits, before Auto is allowed on the Schwab account. |
| 6. Refinement | Portfolio screen (built), mobile layout, earnings data (built: Finnhub and manual dates), more strategy builders (covered call and ratios built). | Ongoing. |

## 11. Automation

Automation reuses everything built for manual trading: the rules engine decides what is due,
the same ticket code builds the order, the same guards and Schwab preview check it, and the
same journal records it. The automation layer only decides when to act and how far to chase
a price. Nothing is automated that could not also be done by hand from a ticket.

### Levels

Every automated behaviour has four levels, chosen per rule:

| Level | What happens |
|---|---|
| Off | Nothing. |
| Notify | A Pushover notification and a flag on the position. |
| Propose | A ready ticket waits on the position and in a Pending list; the notification opens it. Placing it is one tap. |
| Auto | The app places the order itself, notifies you, and logs why. |

An optional grace period sits between Propose and Auto: the app notifies you that it will
place the order in, say, 10 minutes unless you cancel, then places it.

### Phase 4: automanage per position

Each open position (each trade chain) gets an Automanage switch, off by default. Turning it on
shows the plan the app will follow for that position under the current rules, for example:

- Profit target: a GTC limit to close at 50% of max profit, resting at Schwab.
- At 21 DTE: roll to the monthly nearest 45 DTE at the same strikes if that is a credit; if
  not, close.
- Tested (short strike at 30 delta or in the money): notify.
- Loss at 2x credit received: notify.

Each line can be changed for that position, so one strangle can be fully automatic while
another only notifies. Defaults are set on the Settings page.

Default levels and actions, all editable:

| Rule | Default level | Action |
|---|---|---|
| Profit target (50%, 25% for straddles and iron flies) | Auto | GTC limit close at Schwab, placed right after the opening fill and re-placed after every roll at the new cumulative credit. Lives at the broker, so it works even when the app is down. |
| 21 DTE, undefined risk | Propose | Roll out to the monthly nearest 45 DTE at the same strikes for a net credit; close if no credit is available or the chain has already been rolled 3 times. |
| 21 DTE, defined risk | Propose | Close. Rolling for a credit is an option. |
| Tested short strike | Notify | For strangles, a proposed roll of the untested side toward 30 delta, same expiration, for a credit. |
| Loss at 2x credit (undefined risk) | Notify | Proposed close. |
| Expiration day with an in-the-money short | Propose | Close before 15:30 ET to avoid assignment. |

How an automatic order is worked:

- **Timing.** Only between 09:45 and 15:30 ET on full trading days, never in the first or last
  15 minutes, and not after 12:30 on early-close days.
- **Liquidity.** Skipped and retried later if the bid-ask spread of the order is wider than a
  set share of its mid.
- **Price.** Limit orders only. Start at mid; every few minutes move one cent toward natural,
  up to a maximum concession (default 25% of the distance from mid to natural); then cancel
  and notify. Never a market order.
- **Checks.** Every order passes the same guards and Schwab preview as a manual one. A rejection
  stops automation for that position and notifies you.
- **Four legs per order.** Schwab and thinkorswim accept at most four legs in one order. A roll
  closes and reopens every leg, so an iron condor is rolled one side at a time (two 4-leg
  orders); positions with more than four legs are closed by side.
- **One closing order per contract.** A new closing or rolling order replaces any working
  closing order on the same contracts, such as a resting profit target. Live, this uses
  Schwab's replace-order call; the simulated account marks the old order Replaced.
- **Explanation.** Each automatic action is logged with the rule, the numbers that triggered it,
  the quotes used and every price change, and appears in the position's History panel.

### Phase 5: opportunity scanner and automated opening

The scanner runs over the watchlist every 15 minutes in the trading window and proposes new
trades that follow the entry mechanics:

- **Candidate filter.** IV rank at or above 30 (preferring 50 and above). Liquid at the money
  (spread and open interest thresholds). The monthly expiration nearest 45 DTE, within 30 to 60.
  No ex-dividend date inside the trade for short calls. No earnings inside the earnings
  buffer (below).
- **Strategy choice.** From the strategies you enable, by IV regime: high IV rank, strangles at
  16 delta or iron condors at 20 delta short strikes; moderate, defined-risk spreads at 30
  delta collecting about a third of the width; low IV rank, no new short-premium trade. Each
  needs a minimum credit and probability of profit.
- **Portfolio fit.** A trade is only proposed if, afterwards, buying power used stays under the
  VIX-based ceiling, the trade's buying power is within the per-position guide (3% defined,
  5% undefined), beta-weighted delta moves toward neutral or stays inside a set band, and theta
  stays within its target band. At most one open chain per underlying and expiration, and new
  trades are spread across expirations rather than stacked in one.
- **Earnings buffer.** A setting, "avoid earnings within N days" (default 7). A single stock
  with an earnings date within N days from today is not proposed or opened. A second
  setting, on by default, also excludes any trade whose expiration falls after the next
  earnings date, so a position opened today does not sit through a report. When no earnings
  date is known for a single stock, it is treated as possibly within the buffer: Ideas mode
  shows it marked "earnings unknown", and Propose and Auto skip it. Index products and broad
  ETFs (SPY, QQQ, IWM and similar) have no earnings and are exempt.
- **Earnings source.** Schwab's API does not appear to provide earnings dates, so a separate
  source sits behind an `EarningsProvider` interface, fetched once a day for the watchlist and
  held positions and stored with the date it was fetched. Candidates to evaluate when phase 5
  starts: Alpha Vantage's earnings calendar (one request returns about three months for all
  US companies, which fits a free daily allowance) and Finnhub's earnings calendar, then
  Nasdaq's public calendar as a fallback. Their current free-tier terms need checking at that
  point. A manual date per symbol on the Settings page overrides the source. Positions also
  get a flag when an earnings date falls before their expiration.
- **Ranking.** Candidates are scored on IV rank, credit per dollar of buying power, probability
  of profit, liquidity and how much they improve the portfolio's delta and theta.

**Settings.** Every number the scanner and automation use is adjustable on the Settings
page under Management rules, next to the existing management thresholds: entry IV rank,
DTE window and target, short-strike deltas per strategy, spread widths, minimum credit
and probability of profit, liquidity thresholds, the earnings buffer, buying power and
delta limits, which strategies are enabled, scan frequency and trading window, daily caps
and the daily loss limit, the price-stepping rules, and each automanage default. You will
add further settings of your own when phase 5 starts.

Modes, chosen per account:

- **Ideas.** A ranked list on a Scanner screen; each idea opens a ready ticket.
- **Propose.** The best idea becomes a pending ticket and a notification.
- **Auto.** The app opens the trade itself, with automanage switched on for it, so every
  automatically opened trade is also automatically managed. Together with phase 4, this is
  full automation within the rules.

Limits for Auto opening, all enforced on the server:

- A master switch per account, off by default. Turning on Auto for the Schwab account also
  needs an environment variable on the container, like live trading itself.
- Daily caps: new trades per day, buying power added per day, and automated orders per day.
- The underlying allowlist (SPY only in the first year).
- A daily loss limit that pauses all automation for the rest of the day.

### Kill switch and automatic pauses

A Pause automation switch sits in the header for each account. Automation also pauses itself,
and notifies you, when:

- Data is stale or the stream and REST polling both fail.
- The Schwab sign-in has less than a day left.
- Positions or working orders at Schwab do not match what the app expects.
- An order is rejected or its outcome is unknown.
- The daily loss limit or any daily cap is reached.

### Validation

Historical option prices are not available from Schwab, so the rules cannot be backtested
here. They are validated forward instead: every automated behaviour runs on the simulated
account first, against live prices, and the journal's results by strategy show how the
mechanics are doing before anything is switched on for real money.

Fully automatic trading of a real account is a decision with real risk. Before enabling Auto
for the Schwab account, check Schwab's Trader API terms for automated order entry and the
order throttle set on the app registration.

## 12. Open questions

Decided:

- The scanner never opens a single-stock trade with earnings inside a user-set number of days, or with an unknown earnings date (section 11, earnings buffer).
- Live trading started small in SPY only, enforced by an underlying allowlist. The allowlist can now be widened to any underlying in Settings (decided November 2026), with the per-leg contract cap and per-trade buying-power cap still applying. SPY also sidesteps the IV rank wait, since VIX history gives its rank from day one.
- The app targets one chosen account out of several under the Schwab login (section 4, account targeting).

- TrueNAS 25.10.7.
- One Schwab brokerage account. The data model still keys everything by account so more can be added.
- Equity and index options only. No futures options.
- Notifications by Pushover and email. Home-network access only at first.

- Margin account at the highest options approval level, so undefined-risk strategies (strangles, straddles, ratios, naked puts and calls) are in the builders.
- No tastytrade account. IV rank comes from Schwab data only.
- A Schwab developer app already exists. Reuse it rather than recreating it: the key and secret are shown in the app's details in the portal, and a new app waits for approval again.

Status: phase 0 complete (running on TrueNAS, connected to Schwab). Phase 1 complete: running on real Schwab data and checked against thinkorswim. Phase 2 complete: built, deployed and checked against thinkorswim during market hours. Phase 3 built: order tickets (open, close, roll) with Schwab preview, server-side guards, a simulated broker with fills against live quotes, GTC profit targets, expiry settlement, and a trade journal with statistics. Its exit test, two weeks of simulated trading with the order JSON checked against orders built in thinkorswim, is under way. Phase 4 step 1 built: trade chains for the Schwab account from its transaction history (a year back-filled, then every five minutes and on account activity), roll linking with Link and Split corrections, positions grouped by chain with credit including rolls, and a History panel on every position. History import and dashboard built: the sheet's 3,840 Joint rows import in about a second (18 rows unusable, each with a reason), its own P/L figures are used before Schwab's record so the totals equal the sheet's $268,301.97, and the Dashboard page breaks results down by 19 factors. The upload has since been removed from Settings: the imported rows stay in the database and keep feeding the Dashboard, and new trades are recorded by the app from Schwab. Phase 4 step 2 built: automanage on the simulated account. Each trade has an Automanage switch with a per-rule level (Off, Notify, Propose, Auto), defaults and the stepping limits are on Settings under Automanage, and the Positions page shows a Running/Paused switch, proposals waiting for one tap, and each trade's plan and recent actions. It acts only in its window during market hours, pauses itself on stale prices or at the daily order cap, and every order goes through the same ticket and guards as a manual one. Its exit test is two weeks on the simulated account with every action reviewed; live orders (step 3) come after. Phase 4 step 3 built: live orders through Schwab behind two switches (LIVE_TRADING=true in .env and the Live trading switch in Settings), limited to an allowlist (SPY), one contract per leg and 5% of net liq per trade, with Schwab's preview required, a second confirm, replace instead of duplicate closing orders, GTC profit targets placed at Schwab on fill, and an audit log; automanage runs on the live account at most at Propose, unless Auto is allowed (below). Management rules gained an IV-rank gate on rolling (close instead below 25) and no roll cap by default. Phase 5 built: the scanner opens premium the tastytrade way (IV rank 30+, strangle from 50, otherwise iron condor at a third of width, monthly near 45 DTE, 16 delta), skips thin markets, held underlyings and single stocks while no earnings date source exists, sizes against buying power and the VIX ceiling, and follows Notify/Propose/Auto per account (live at most Propose unless Auto is allowed). Earnings dates: from Finnhub's earnings calendar when FINNHUB_API_KEY is set, and from dates entered in Settings (which win); refreshed every six hours for watched, held and live-allowed stocks. The scanner opens a single stock only when its next report is known and more than the buffer (7 sessions) away; automanage's earnings rule closes a trade that holds through a report a set number of sessions before the last session ahead of it (bmo: the day before; amc: the day itself); Watchlist and Positions flag the next report with its date and before open/after close. Price extremes: the Watchlist shows each symbol's place in its 52-week range, its recent move against the move implied volatility expected, and today's move against its average, with a down/up badge at an extreme; the scanner leans its strangle or iron condor against an extreme (the favoured side's short strike at 25 delta) and ranks those trades first. Thresholds are under Management rules: opening trades. Trade screen chart: a year of daily candles (Schwab price history) with the live price on the last bar, the account's strikes in the symbol as labelled lines (shorts solid, longs dashed), the 52-week high and low, the expected move to the selected expiration as a cone, and the next earnings report; 1M/3M/6M/1Y ranges. Market strip on Positions: price and day's move for /ES, /NQ, /RTY, /YM, VIX, /CL, /GC, /SI, /ZB, /ZN, /6E, /6J and /BTC every 15 seconds, asked for by root so Schwab rolls the contract; if a root is not resolved the front contract is worked out from the market's cycle, and failing that a cash index or ETF stands in, marked with an asterisk. Tested side: the untested side rolls closer to the tested side's delta (or a fixed one) for a credit, iron condors roll their untested vertical at the same width, at most 2 such rolls per expiration, a lone short option in the money can roll out in time for a credit (off by default), and going inverted stays its own switch. An inverted strangle buys the guts and sells the wings (Tom Sosnoff's re-centring): one four-leg order in the same expiration closing the inverted shorts and selling a new 16-delta strangle, paying at most the inversion width plus an allowance ($0.25 a unit). The same repair covers a straddle, and a strangle whose strikes are within 1% of the price, once a short reaches 0.70 delta (both adjustable, on by default); it pays at most the shorts' in-the-money value plus the allowance, and below that delta a straddle is left to the 25% profit target and 21 DTE. Choosing a margin type saves at once. Portfolio targets (Settings, Management rules): a delta bias (neutral, bullish or bearish; choosing one saves at once and sets the band) with a band for beta-weighted delta as a share of net liq (SPY deltas times SPY's price over net liq; our own defaults: neutral -5% to +5%, bullish 0 to +15%, bearish -15% to 0), and a daily theta band at tastytrade's 0.1% to 0.3% of net liq. The snapshot carries both bands; outside one the header turns amber and a notification goes out once a day per band and side; the order ticket warns about a trade that moves delta or theta further out of its band; the scanner leans new trades back toward the delta band (before the price-extreme lean), only proposes a trade that would push delta further out, and opens nothing while theta is at its maximum or a trade would take it over. No hedge trade is ever placed for a band. The Settings page explains each of these the tastytrade way. Phase 6 so far: a Portfolio screen (buying power by underlying and by sector, sectors from a built-in ETF table and Finnhub's company profile for stocks; beta-weighted delta by position; a theta ladder by expiration; and a weekly calendar of what reaches 21 DTE). The Trade screen adds a covered call (against shares held; the shares count in the payoff, not the order) and put and call 1x2 ratios, and buying power is estimated the Reg T way for spreads, ratios and covered calls. Safeguards: a daily loss limit (2% of net liq) stops automation for the rest of the day; automation holds while the Schwab sign-in has less than a day left; an automatic order waits while its bid/ask is wider than 25% of mid; early-close days end the window at 12:30 ET; a pause switch sits in the header; while paused or holding, the scanner only proposes. Scanner: no single-stock trade through its next report, probability of profit at least 55%, ranking by IV rank, credit per dollar of buying power and POP, one trade per underlying and expiration, at most 10% of net liq added a day, and optional rescans every N minutes. Live widened: the allowlist can be replaced by Any underlying (Settings, Live trading; the live scanner then uses the watchlist), and Auto on the Schwab account is possible behind two more switches, LIVE_AUTO=true in .env and Allow Auto in Settings, on top of LIVE_TRADING and the Live trading switch; without them live automation proposes. Both are off until switched on. The Trade screen's tickets follow the account chosen in the header. Order check built for the thinkorswim comparison: the Activity page (Schwab account) logs every order on the account from the last 30 days, rebuilds each with the app's own order builder from the same legs and price, and lists any field that differs, with both JSON bodies side by side.

Still open:

1. The phase 3 two-week simulated run, with order JSON compared against thinkorswim (in progress).
2. Ex-dividend warnings for short calls (section 5, assignment risk) and the matching scanner filter.

Settled since: the market-hours check against thinkorswim is done; the Schwab app's order
throttle is above zero; earnings dates come from Finnhub (FINNHUB_API_KEY) plus dates
entered in Settings.

## Sources

- Schwab API behaviour: [schwab-py getting started](https://schwab-py.readthedocs.io/en/stable/getting-started.html), [schwab-py order templates](https://schwab-py.readthedocs.io/en/latest/order-templates.html), [Schwabdev orders](https://tylerebowers.github.io/Schwabdev/pages/orders.html), [unofficial guide to Schwab's Trader APIs](https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57), [rate limits and token expiry](https://mylinedchart.com/resources/articles/schwab-api-rate-limits-and-token-expiry), [7-day rule](https://jorgai.com/blog/schwab-trader-api-explained-2026-what-replaced-the-td-ameritrade-and-thinkorswim-apis-what-an-app-can-do-in-your-account-and-the-7-day-rule), [Schwab IV rank definition](https://help.streetsmart.schwab.com/edge/1.70/Content/Options%20Chain%20Column%20Descriptions.htm)
- Tasty mechanics: [tastylive trade entry and exit](https://www.tastylive.com/shows/best-practices/episodes/trade-entry-and-exit-07-08-2019), [managing 21 days early](https://www.tastylive.com/shows/best-practices/episodes/managing-21-days-early-02-25-2020), [capital allocation and volatility](https://www.tastylive.com/news-insights/How-Capital-Allocation-Impacts-Volatility), [VIX-based buying power (third party)](https://help.optionsautotrader.com/en/articles/82-how-the-vix-based-buying-power-works), [tastytrade custom grouping](https://tastyworks.freshdesk.com/support/solutions/articles/43000704699-custom-grouping)
- TrueNAS: [Install Custom App screens, 25.04](https://www.truenas.com/docs/scale/25.04/scaleuireference/apps/installcustomappscreens/), [Docker on TrueNAS with compose includes](https://technotim.live/posts/truenas-docker-pro)
- Projects reviewed: linked in section 3.
