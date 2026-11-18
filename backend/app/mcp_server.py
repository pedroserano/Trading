"""The app as an MCP server, for AI assistants.

Mounted at /mcp (streamable HTTP). Every tool calls the app's own /api route in-process with
the caller's API key, so a tool can do exactly what that key can do through the API: the
same levels, guards, live gate and cash-only rule. Nothing here decides what is allowed.
"""

import json
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

INSTRUCTIONS = """\
Options trading app for Schwab accounts plus a simulated account ("sim").
Start with list_accounts. Account keys are "sim" or a Schwab account key such as "live".
Your API key has a level: read (look only), sim (also trade the simulated account) or live
(also trade Schwab accounts). Live orders still go through the user's live list, the cash and
sweep check (never margin), Schwab's preview and the kill switch; a refusal explains why.
Always preview_order before place_order and show the user the preview.
Option leg symbols use Schwab's format, e.g. "SPY   261218P00550000"
(root padded to 6, YYMMDD, P/C, strike x1000 to 8 digits); get_chain lists them.
"""


def build(app) -> tuple[MCPServer, Any]:
    """The MCP server and its ASGI app (already behind the API key check)."""
    server = MCPServer("trading", instructions=INSTRUCTIONS)
    transport = httpx.ASGITransport(app=app)

    async def call(ctx: Context, method: str, path: str, **kw) -> str:
        headers = {"authorization": (ctx.headers or {}).get("authorization", "")}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://app", headers=headers, timeout=60
        ) as client:
            r = await client.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ToolError(f"HTTP {r.status_code}: {detail}")
        return json.dumps(r.json(), default=str)

    @server.tool()
    async def list_accounts(ctx: Context) -> str:
        """The accounts: "sim" and each Schwab account key with its name. Needs: read."""
        return await call(ctx, "GET", "/api/accounts")

    @server.tool()
    async def get_snapshot(ctx: Context, account: str = "live") -> str:
        """Positions grouped by underlying and strategy, with Greeks, P&L and balances for an
        account. Needs: read."""
        return await call(ctx, "GET", "/api/snapshot", params={"account": account})

    @server.tool()
    async def get_portfolio(ctx: Context, account: str = "live") -> str:
        """Portfolio risk: Greeks totals, buying power, expirations, concentration. Needs: read."""
        return await call(ctx, "GET", "/api/portfolio", params={"account": account})

    @server.tool()
    async def get_watchlist(ctx: Context) -> str:
        """Watchlist symbols with IV rank, expected move and the user's views. Needs: read."""
        return await call(ctx, "GET", "/api/watchlist")

    @server.tool()
    async def get_market(ctx: Context) -> str:
        """Index and VIX levels. Needs: read."""
        return await call(ctx, "GET", "/api/market")

    @server.tool()
    async def get_expirations(ctx: Context, symbol: str) -> str:
        """Option expirations for a symbol, with days to expiration. Needs: read."""
        return await call(ctx, "GET", f"/api/trade/{symbol}/expirations")

    @server.tool()
    async def get_chain(ctx: Context, symbol: str, expiration: str) -> str:
        """The option chain for one expiration (YYYY-MM-DD): strikes, bid/ask, Greeks, IV and
        each contract's symbol. Needs: read."""
        return await call(
            ctx, "GET", f"/api/trade/{symbol}/chain", params={"expiration": expiration}
        )

    @server.tool()
    async def analyze(ctx: Context, request: dict) -> str:
        """Payoff, breakevens, probability of profit and portfolio-after figures for a
        proposed trade. request is the app's analyze body: {"symbol", "legs": [{"put_call",
        "strike", "expiration", "quantity", "price"}], "account"}. Needs: read."""
        return await call(ctx, "POST", "/api/analyze", json=request)

    @server.tool()
    async def get_scanner(ctx: Context, account: str = "sim") -> str:
        """The scanner's current candidates and what it would trade. Needs: read."""
        return await call(ctx, "GET", "/api/scanner", params={"account": account})

    @server.tool()
    async def get_automation(ctx: Context, account: str = "sim") -> str:
        """Automanage state: trades it watches, proposals and recent actions. Needs: read."""
        return await call(ctx, "GET", "/api/automation", params={"account": account})

    @server.tool()
    async def get_activity(ctx: Context, account: str = "live") -> str:
        """Recent orders and fills for an account. Needs: read."""
        return await call(ctx, "GET", "/api/activity", params={"account": account})

    @server.tool()
    async def get_trade_history(ctx: Context, account: str = "live") -> str:
        """Trade chains (opens, rolls, closes) with their realised P&L. Needs: read."""
        return await call(ctx, "GET", "/api/chains", params={"account": account})

    @server.tool()
    async def preview_order(ctx: Context, order: dict) -> str:
        """Check an order without placing it: guard errors, live blocks, warnings, price and
        Schwab's preview. order: {"account", "intent": "open"|"close"|"roll"|"adjust",
        "legs": [{"symbol", "quantity" (+buy/-sell), "effect": "open"|"close"}],
        "limit_net" (optional here; the preview returns the natural, mid and far prices in
        the same sign convention to choose it from), "duration": "DAY"|"GTC"}. Needs: read."""
        return await call(ctx, "POST", "/api/orders/preview", json=order)

    @server.tool()
    async def place_order(ctx: Context, order: dict) -> str:
        """Place an order (same body as preview_order; limit_net is required). Needs: sim for
        "sim", live for a Schwab account. Always preview first and confirm with the user."""
        return await call(ctx, "POST", "/api/orders", json=order)

    @server.tool()
    async def cancel_order(ctx: Context, order_id: str, account: str) -> str:
        """Cancel a working order. Needs: sim for "sim", live for a Schwab account."""
        return await call(
            ctx, "POST", f"/api/orders/{order_id}/cancel", params={"account": account}
        )

    asgi = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        # The key check below guards the endpoint; the app is reached by its LAN or VPN name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",  # noqa: S104 - only selects the security default, binds nothing
    )

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            check = app.state.check_key
            request = Request(scope)
            try:
                rec = await check(request)
            except Exception as e:  # HTTPException from the key check
                status = getattr(e, "status_code", 401)
                await JSONResponse({"detail": getattr(e, "detail", "Unauthorized")}, status)(
                    scope, receive, send
                )
                return
            if rec is None:
                await JSONResponse({"detail": "Send Authorization: Bearer <API key>"}, 401)(
                    scope, receive, send
                )
                return
        await asgi(scope, receive, send)

    return server, guarded
