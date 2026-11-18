# Connecting an AI to the app

The app can be used by an AI assistant through **MCP** (Model Context Protocol), or by any
script through its JSON API. Both use an API key.

## 1. Make a key

Settings → Connection → **AI access**: name the key (for example `claude`), pick a level and
press Create. Copy the key straight away; it is shown once. Only its hash is stored.

| Level | Can |
|---|---|
| Read only | Read positions, chains, scanner, automation, activity and history; analyze and preview trades |
| Read + simulated trades | Also place and cancel orders on the simulated account, run its scanner, accept its proposals |
| Read + simulated and live trades | Also do those on Schwab accounts |

No key can change settings, rules, views (Would own is set only by you), keys, the
simulated account's reset or the Schwab sign-in. A live order from a key passes exactly the
checks a manual one does: the live switches and underlying list, the cash and sweep check
(never margin), the size limits, Schwab's preview and the kill switch. Every order a key places
sends a notification and is marked in the Activity list with the key's name. Revoke a key on the same page; it stops working at once.

Keep the app on your home network or VPN, as it is now. A live key can trade: use read only or
simulated keys anywhere less private.

## 2. Connect an AI (MCP)

The MCP server is at `https://<your app address>/mcp` (streamable HTTP). Send the key as a
header: `Authorization: Bearer trd_...`.

- **Claude Code**:
  `claude mcp add --transport http trading https://<your app address>/mcp --header "Authorization: Bearer trd_..."`
- **Claude desktop or another MCP client**: add a remote (HTTP) MCP server with that URL and
  header.

The AI must be able to reach the address, so run it on a machine on your network or VPN.

Tools: `list_accounts`, `get_snapshot`, `get_portfolio`, `get_watchlist`, `get_market`,
`get_expirations`, `get_chain`, `analyze`, `get_scanner`, `get_automation`, `get_activity`,
`get_trade_history`, `preview_order`, `place_order`, `cancel_order`. Each calls the app's own
API with your key, so it can do what the key can do and nothing more.

Then ask in plain words, for example: "What's on my Joint account and what is near its profit
target?" or "Preview a 16-delta SPY strangle near 45 DTE on the simulated account."

## 3. Scripts (JSON API)

Every `/api/...` route takes the same header:

```
curl -H "Authorization: Bearer trd_..." "https://<your app address>/api/snapshot?account=live"
```

The full list of routes and bodies is at `/api/openapi.json` (needs the key or a signed-in
session). The account is `sim` or a Schwab account key (`live` for the first account,
`live-1234` for others; `list_accounts` or `/api/accounts/in-use` lists them).
