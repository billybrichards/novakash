"""
Lightweight HTTP status server for the margin engine.

Runs alongside the main trading loop on a configurable port (default 8090).
Exposes read-only state for the Hub proxy to consume.

GET /status   — portfolio state, open/closed positions, P&L, execution context
GET /health   — liveness probe
GET /logs     — recent log lines (paginated)
GET /history  — paginated closed-position history for the Trade Timeline tab
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

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

    def __init__(
        self,
        portfolio: Portfolio,
        exchange,
        port: int = 8090,
        log_repo=None,
        position_repo=None,
        execution_info_fn: Optional[Callable[[], dict]] = None,
    ):
        """
        position_repo: optional PgPositionRepository — when set, /history is enabled
        execution_info_fn: optional callable returning the freshest execution
            context dict (venue, fees, price feed health, strategy). Built in
            main.py so it can capture live state like price_feed.is_healthy.
        """
        self._portfolio = portfolio
        self._exchange = exchange
        self._port = port
        self._log_repo = log_repo
        self._position_repo = position_repo
        self._execution_info_fn = execution_info_fn
        self._app = web.Application()
        self._app.router.add_get("/status", self._handle_status)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/logs", self._handle_logs)
        self._app.router.add_get("/history", self._handle_history)
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

        # Reference mid price for display only — NOT used for P&L maths,
        # that goes through exchange.get_unrealised_pnl which uses the
        # close-side bid/ask and factors in fees + borrow.
        current_price_val = None
        try:
            price_obj = await self._exchange.get_current_price("BTCUSDT")
            current_price_val = price_obj.value if hasattr(price_obj, 'value') else float(price_obj)
        except Exception:
            pass

        open_pos = portfolio.open_positions
        closed_pos = [p for p in portfolio.positions if p.state.value == "CLOSED"]

        positions = []
        for p in portfolio.positions:
            d = _position_to_dict(p)
            if p.state.value == "OPEN":
                # Use the exchange port so the dashboard shows the same
                # net unrealised P&L that stops and TP evaluation use.
                try:
                    d["unrealised_pnl"] = await self._exchange.get_unrealised_pnl(p)
                except Exception:
                    # Fall back to raw gross if the exchange call fails —
                    # better to show *something* on the dashboard than nothing.
                    if current_price_val:
                        d["unrealised_pnl"] = p.unrealised_pnl(current_price_val)
            positions.append(d)

        balance = portfolio.starting_capital.amount
        try:
            bal = await self._exchange.get_balance()
            balance = bal.amount if hasattr(bal, 'amount') else float(bal)
        except Exception:
            pass

        # Execution context — built fresh on every /status call so values
        # like price_feed.healthy and last_price_age_s are current. Falls
        # back to a minimal block if main.py didn't supply the closure
        # (back-compat for callers that haven't been updated yet).
        execution_block: dict
        if self._execution_info_fn is not None:
            try:
                execution_block = self._execution_info_fn()
            except Exception as e:
                logger.warning("execution_info_fn raised: %s", e)
                execution_block = {"venue": "unknown", "error": str(e)}
        else:
            # Legacy fallback — preserves the old behavior of inferring
            # paper mode from the exchange adapter type. New deployments
            # should always pass execution_info_fn.
            execution_block = {
                "venue": "binance",
                "paper_mode": hasattr(self._exchange, "_balance"),
            }

        return web.json_response({
            "portfolio": {
                "balance": balance,
                "exposure": portfolio.total_exposure,
                "leverage": portfolio.leverage,
                "is_active": portfolio.is_active,
                "kill_switch": portfolio._kill_switch,
                # Kept for backward compat with frontends that read this
                # field directly. New code should read execution.paper_mode.
                "paper_mode": execution_block.get(
                    "paper_mode",
                    hasattr(self._exchange, "_balance"),
                ),
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
            "execution": execution_block,
        })

    async def _handle_history(self, request: web.Request) -> web.Response:
        """
        Paginated history of closed positions for the Trade Timeline tab.

        Query params (all optional):
            limit       — int, capped at 100, default 25
            offset      — int >= 0, default 0
            side        — "LONG" | "SHORT"
            outcome     — "win" | "loss"
            exit_reason — comma-separated list (e.g. "TAKE_PROFIT,STOP_LOSS")

        Response shape:
            {"rows": [...], "total": int, "limit": int, "offset": int}
        """
        if not self._position_repo:
            return web.json_response(
                {"error": "position repo not configured on this engine"},
                status=503,
            )

        try:
            limit = min(max(int(request.query.get("limit", "25")), 1), 100)
        except ValueError:
            return web.json_response({"error": "invalid limit"}, status=400)
        try:
            offset = max(int(request.query.get("offset", "0")), 0)
        except ValueError:
            return web.json_response({"error": "invalid offset"}, status=400)

        side = request.query.get("side")
        if side and side not in ("LONG", "SHORT"):
            return web.json_response({"error": "side must be LONG or SHORT"}, status=400)

        outcome = request.query.get("outcome")
        if outcome and outcome not in ("win", "loss"):
            return web.json_response({"error": "outcome must be win or loss"}, status=400)

        exit_reason = request.query.get("exit_reason")  # repository validates internally

        try:
            rows = await self._position_repo.get_closed_history(
                limit=limit,
                offset=offset,
                side=side,
                outcome=outcome,
                exit_reason=exit_reason,
            )
            total = await self._position_repo.get_closed_history_count(
                side=side,
                outcome=outcome,
                exit_reason=exit_reason,
            )
        except Exception as e:
            logger.exception("history query failed: %s", e)
            return web.json_response(
                {"error": "history query failed", "detail": str(e)},
                status=500,
            )

        return web.json_response({
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
        })

    async def _handle_logs(self, request: web.Request) -> web.Response:
        if not self._log_repo:
            return web.json_response({"error": "log persistence not configured"}, status=503)
        limit = int(request.query.get("limit", "100"))
        level = request.query.get("level")
        since = int(request.query.get("since_minutes", "60"))
        rows = await self._log_repo.query(limit=limit, level=level, since_minutes=since)
        return web.json_response({"logs": rows, "count": len(rows)})
