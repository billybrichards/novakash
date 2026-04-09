"""
Lightweight HTTP status server for the margin engine.

Runs alongside the main trading loop on a configurable port (default 8090).
Exposes read-only state for the Hub proxy to consume.

GET /status  — portfolio state, open/closed positions, P&L
GET /health  — liveness probe
"""
from __future__ import annotations

import logging
from aiohttp import web
from margin_engine.domain.entities.portfolio import Portfolio

logger = logging.getLogger(__name__)


def _position_to_dict(p) -> dict:
    """Serialize a Position entity to JSON-friendly dict."""
    return {
        "id": p.id,
        "asset": p.asset,
        "side": p.side.value,
        "state": p.state.value,
        "entry_price": p.entry_price.value if p.entry_price else None,
        "notional": p.notional.amount if p.notional else None,
        "collateral": p.collateral.amount if p.collateral else None,
        "entry_signal_score": p.entry_signal_score,
        "entry_timescale": p.entry_timescale,
        "unrealised_pnl": 0.0,  # needs current price — set by caller
        "realised_pnl": p.realised_pnl,
        "exit_reason": p.exit_reason.value if p.exit_reason else None,
        "opened_at": p.opened_at,
        "closed_at": p.closed_at,
        "hold_duration_s": p.hold_duration_s,
    }


class StatusServer:
    """
    Thin read-only HTTP API for dashboard consumption.

    Usage:
        server = StatusServer(portfolio, exchange, port=8090)
        await server.start()
        # ... trading loop ...
        await server.stop()
    """

    def __init__(self, portfolio: Portfolio, exchange, port: int = 8090):
        self._portfolio = portfolio
        self._exchange = exchange
        self._port = port
        self._app = web.Application()
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/health", self._handle_health)
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("Status server listening on :%d", self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        portfolio = self._portfolio

        # Get current price for unrealised P&L
        current_price = None
        try:
            current_price = await self._exchange.get_current_price("BTCUSDT")
        except Exception:
            pass

        open_pos = portfolio.open_positions
        closed_pos = [p for p in portfolio.positions if p.state.value == "CLOSED"]

        positions = []
        for p in portfolio.positions:
            d = _position_to_dict(p)
            if current_price and p.state.value == "OPEN":
                d["unrealised_pnl"] = p.unrealised_pnl(current_price)
            positions.append(d)

        balance = portfolio.starting_capital.amount
        try:
            balance = await self._exchange.get_balance()
        except Exception:
            pass

        return web.json_response({
            "portfolio": {
                "balance": balance,
                "exposure": portfolio.total_exposure,
                "leverage": portfolio.leverage,
                "is_active": portfolio.is_active,
                "kill_switch": portfolio._kill_switch,
                "paper_mode": hasattr(self._exchange, "_balance"),  # PaperExchangeAdapter has _balance
                "daily_pnl": portfolio._daily_pnl,
                "consecutive_losses": portfolio._consecutive_losses,
            },
            "positions": positions,
            "stats": {
                "open_count": len(open_pos),
                "closed_count": len(closed_pos),
                "total_realised_pnl": portfolio.total_realised_pnl,
                "win_rate": portfolio.win_rate,
            },
        })
