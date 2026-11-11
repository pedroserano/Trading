# Plan: self-hosted options trading app for Schwab, built around tasty mechanics

Status: draft for review. Nothing is built yet.

## 1. What this is

A single-user web app that runs as a Docker Compose stack on TrueNAS, connects to one
Schwab login through the Schwab Trader API, and is organised around the tastylive way of
trading short premium: find high-IV underlyings, sell around 45 DTE, manage winners at a
profit target, manage everything at 21 DTE, and keep portfolio buying power, delta and
theta inside set bounds.

The app assists and the trader decides. Every order is previewed and confirmed by a
click. No automated order entry in v1 (see section 9 for what could come later).

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

**Portfolio**
- Buying power in use against a VIX-scaled ceiling. Commonly cited tastylive tiers are 25% under VIX 15, 30% at 15 to 20, 35% at 20 to 30, 40% at 30 to 40, 50% above 40. One third-party source I found states different figures, so treat these as defaults to check against tastylive's own material.
- Per-position size: buying power effect as a share of net liq, default cap 5% undefined risk, 3% defined risk.
- Beta-weighted delta to SPY, shown in deltas and as a dollar move per 1% SPY move.
- Daily theta as a share of net liq, target band 0.1% to 0.3%.
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
- Every order goes through preview, then an explicit confirmation showing the final legs, price, and max loss.
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
| 4. Live trading | Guards, audit log, reconciliation, GTC profit-taking orders, cancel and replace. Start with one-lot defined-risk trades. | A month of small live trades with zero reconciliation mismatches. |
| 4a. SPY-only first year | LIVE mode restricted to SPY by an allowlist setting, one-lot default and a low per-order buying power cap. Other underlyings stay available in SIM. | A year of SPY trading reviewed in the journal before the allowlist is widened. |
| 5. Refinement | Portfolio screen, performance stats, mobile layout, earnings data, optional one-click "manage at 21 DTE" batch tickets. | Ongoing. |

Automation beyond one-click tickets (for example auto-closing at 50%) is deliberately left
out. A resting GTC limit order at Schwab does that job without the app needing to be running.

## 11. Open questions

Decided:

- First year of live trading is small size in SPY only. The app enforces this with an underlying allowlist rather than relying on discipline. SPY also sidesteps the IV rank wait, since VIX history gives its rank from day one.
- The app targets one chosen account out of several under the Schwab login (section 4, account targeting).

- TrueNAS 25.10.7.
- One Schwab brokerage account. The data model still keys everything by account so more can be added.
- Equity and index options only. No futures options.
- Notifications by Pushover and email. Home-network access only at first.

- Margin account at the highest options approval level, so undefined-risk strategies (strangles, straddles, ratios, naked puts and calls) are in the builders.
- No tastytrade account. IV rank comes from Schwab data only.
- A Schwab developer app already exists. Reuse it rather than recreating it: the key and secret are shown in the app's details in the portal, and a new app waits for approval again.

Status: phase 0 complete (running on TrueNAS, connected to Schwab). Phase 1 complete: running on real Schwab data and checked against thinkorswim. Phase 2 built and deployed; its market-hours check against thinkorswim is pending. Phase 3 built: order tickets (open, close, roll) with Schwab preview, server-side guards, a simulated broker with fills against live quotes, GTC profit targets, expiry settlement, and a trade journal with statistics. Its exit test is two weeks of simulated trading with the order JSON checked against orders built in thinkorswim.

Still open: nothing blocks phase 1. The remaining unknowns are listed at the end of section 2 and are resolved by testing against the live API.

## Sources

- Schwab API behaviour: [schwab-py getting started](https://schwab-py.readthedocs.io/en/stable/getting-started.html), [schwab-py order templates](https://schwab-py.readthedocs.io/en/latest/order-templates.html), [Schwabdev orders](https://tylerebowers.github.io/Schwabdev/pages/orders.html), [unofficial guide to Schwab's Trader APIs](https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57), [rate limits and token expiry](https://mylinedchart.com/resources/articles/schwab-api-rate-limits-and-token-expiry), [7-day rule](https://jorgai.com/blog/schwab-trader-api-explained-2026-what-replaced-the-td-ameritrade-and-thinkorswim-apis-what-an-app-can-do-in-your-account-and-the-7-day-rule), [Schwab IV rank definition](https://help.streetsmart.schwab.com/edge/1.70/Content/Options%20Chain%20Column%20Descriptions.htm)
- Tasty mechanics: [tastylive trade entry and exit](https://www.tastylive.com/shows/best-practices/episodes/trade-entry-and-exit-07-08-2019), [managing 21 days early](https://www.tastylive.com/shows/best-practices/episodes/managing-21-days-early-02-25-2020), [capital allocation and volatility](https://www.tastylive.com/news-insights/How-Capital-Allocation-Impacts-Volatility), [VIX-based buying power (third party)](https://help.optionsautotrader.com/en/articles/82-how-the-vix-based-buying-power-works), [tastytrade custom grouping](https://tastyworks.freshdesk.com/support/solutions/articles/43000704699-custom-grouping)
- TrueNAS: [Install Custom App screens, 25.04](https://www.truenas.com/docs/scale/25.04/scaleuireference/apps/installcustomappscreens/), [Docker on TrueNAS with compose includes](https://technotim.live/posts/truenas-docker-pro)
- Projects reviewed: linked in section 3.
