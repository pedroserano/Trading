# Connecting an AI to the app

The app can be used by an AI assistant through **MCP** (Model Context Protocol), or by any
script through its JSON API. Both use an API key you make in Settings.

## Why connect an AI

The app shows numbers and follows rules. An AI connected to it can read the same numbers,
answer questions about them, and do the legwork of building and checking trades. It sees your
real positions and chains, not a pasted screenshot. Some examples:

**A quick look at the book**
- "Summarise my Joint account: what's open, what's near its profit target, what's tested, and
  what reaches 21 DTE this week."
- "Which positions are losing the most theta to gamma right now, and why?"
- "Is my beta-weighted delta inside my band? If not, which positions are pushing it out?"

**Finding and checking trades**
- "Go through the scanner's candidates and explain, in plain words, why each one qualifies.
  Which would you skip and why?"
- "Show me the SPY chain near 45 DTE and preview a 16-delta strangle on the simulated account.
  What's the buying power, the probability of profit and the break-evens?"
- "Compare an iron condor and a strangle on IWM for the same expiration: credit, buying power
  and return on capital."

**Managing what's open**
- "My AAPL covered call is in the money with 9 days left. What are my choices, and what would
  rolling up and out to the next monthly cost or pay?"
- "Which trades does automanage want to roll or close today, and is anything waiting for my
  OK?"
- "Is there an ex-dividend date coming up on anything I'm short calls on?"

**Reviewing and learning**
- "Go through my closed trades from the last three months. Where did the losses come from:
  earnings, tested sides, or holding past 21 DTE?"
- "What's my win rate on strangles compared with iron condors, and the average days held?"
- "Explain this trade's payoff and Greeks as if I'm new to options."

**Practice before going live**
- Give the AI a simulated-trades key and let it place and manage trades on the simulated
  account. You compare its choices with your own before ever giving it live access.

**Your own tools**
- A script that checks the book each morning and sends you a summary.
- A spreadsheet or notebook that pulls positions and history through the JSON API.

The AI never changes your settings, rules or views. It works inside the same limits as you do
on the order ticket.

## 1. Make a key

Settings → Connection → **AI access**: name the key (for example `claude`), pick a level and
press Create. Copy the key straight away; it is shown once. Only its hash is stored.

| Level | Can |
|---|---|
| Read only | Read positions, chains, scanner, automation, activity and history; analyze and preview trades |
| Read + simulated trades | Also place and cancel orders on the simulated account, run its scanner, accept its proposals |
| Read + simulated and live trades | Also do those on Schwab accounts |

No key can change any of these:
- settings or rules;
- views (Would own is set only by you);
- keys;
- the simulated account's reset;
- the Schwab sign-in.

A live order from a key passes exactly the checks a manual one does:
- the live switches and underlying list;
- the cash and sweep check (never margin);
- the size limits;
- Schwab's preview;
- the kill switch.

Every order a key places sends a notification and is marked in the Activity list with the key's
name. Revoke a key on the same page; it stops working at once.

Start with a read-only key. Move to simulated trades once you trust what it suggests. Give
live access last, if ever.

## 2. Connect Claude through Tailscale

This assumes the app is already served on your tailnet as in
[DEPLOY-TRUENAS.md](DEPLOY-TRUENAS.md#tailscale-optional-a-trusted-certificate-and-access-away-from-home),
at an address like `https://truenas-scale.rainbow-chickadee.ts.net` (yours is on the Tailscale
Machines page). That address has a trusted certificate and only devices on your tailnet can
reach it, which is what the AI connection needs.

The MCP server is that address plus `/mcp`:

```
https://truenas-scale.rainbow-chickadee.ts.net/mcp
```

Claude has to connect **from a computer that is on your tailnet**: install Tailscale on it and
sign in to the same account. Check by opening the app's address in that computer's browser.

### Claude Code (terminal, VS Code, JetBrains)

On the computer with Tailscale on:

```
claude mcp add --transport http trading https://truenas-scale.rainbow-chickadee.ts.net/mcp \
  --header "Authorization: Bearer trd_..."
```

Run `claude mcp list` to check it shows as connected. Then, in Claude Code, ask anything from
the examples above. Add `--scope user` to the command to have it in every project.

### Claude desktop app (Mac or Windows)

The desktop app reaches your tailnet through a small local bridge, `mcp-remote`, which needs
[Node.js](https://nodejs.org) installed. On the computer with Tailscale on:

1. In Claude desktop: Settings → Developer → **Edit Config**. This opens
   `claude_desktop_config.json`.
2. Add the app under `mcpServers` (keep anything already there):

   ```json
   {
     "mcpServers": {
       "trading": {
         "command": "npx",
         "args": [
           "-y", "mcp-remote",
           "https://truenas-scale.rainbow-chickadee.ts.net/mcp",
           "--header", "Authorization:${TRADING_KEY}"
         ],
         "env": { "TRADING_KEY": "Bearer trd_..." }
       }
     }
   }
   ```

   The key goes in `env` without spaces around the colon in the header. This avoids a
   known problem with spaces in arguments on Windows.
3. Quit Claude desktop completely and open it again. The tools icon in the chat box lists
   `trading` and its tools.

The config file holds the key in plain text. Keep that computer's account private, and use a
read-only or simulated key on a shared computer.

### Claude on the web and phone

claude.ai's custom connectors are called from Anthropic's servers, not from your device, so
they can't reach an address that is only on your tailnet. To use them you would have to put the
app on the public internet (for example with Tailscale Funnel). This guide doesn't recommend
that for an app that can trade. Use Claude Code or Claude desktop on a computer on your
tailnet instead.

### If it doesn't connect

- **Can't reach the address**: open `https://<your ts.net name>/login` on the same computer.
  If it doesn't load, Tailscale isn't on or `tailscale serve` isn't set on the NAS (see
  DEPLOY-TRUENAS.md).
- **401 Unknown API key**: the key was mistyped or revoked. Make a new one.
- **429**: too many wrong keys in a minute; wait a minute.
- **403 can't trade on 'live'**: the key's level doesn't cover that account, which is
  intended. Make a key with the level you want.
- **An order refused with a reason**: the app's own checks stopped it, as they would on the
  order ticket. The reason says which (live list, cash, size, preview).

### Tools

`list_accounts`, `get_snapshot`, `get_portfolio`, `get_watchlist`, `get_market`,
`get_expirations`, `get_chain`, `analyze`, `get_scanner`, `get_automation`, `get_activity`,
`get_trade_history`, `preview_order`, `place_order`, `cancel_order`. Each calls the app's own
API with your key, so it can do what the key can do and nothing more. Claude is told to preview
every order and show it to you before placing it. Its MCP permission prompt also asks you
before each tool call unless you allow that tool.

## 3. Scripts (JSON API)

Every `/api/...` route takes the same header:

```
curl -H "Authorization: Bearer trd_..." \
  "https://truenas-scale.rainbow-chickadee.ts.net/api/snapshot?account=live"
```

The full list of routes and bodies is at `/api/openapi.json` (needs the key or a signed-in
session). The account is `sim` or a Schwab account key: `live` for the first account and
`live-1234` for others. `list_accounts` or `/api/accounts/in-use` lists them.
