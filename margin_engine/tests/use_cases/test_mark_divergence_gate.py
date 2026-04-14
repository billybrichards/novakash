"""
DQ-07 unit tests — defensive mark-divergence gate in OpenPositionUseCase.

Tests the gate 9.5 check added to `_execute_v4` between the balance query
(gate 9) and the quantile-derived SL/TP math (gate 10). The gate compares
`v4.last_price` (Binance spot from the assembler) against
`exchange.get_mark(side)` and rejects the trade when the divergence
exceeds `v4_max_mark_divergence_bps`.

Agent D recommended this gate in the DQ-05 investigation as a regression
safety rail: the SL/TP ratio math is mathematically consistent regardless
of venue, but a stale spot tick, Hyperliquid basis spike, or cross-region
latency can still produce entries off a bad price anchor. The gate ships
DEFAULT OFF (0.0) so this merge is zero-behavior-change in production.

Test scope (all 4 cases from the DQ-07 plan):
  1. Default OFF (0.0) + 50 bps divergence → gate is a no-op, entry fires
  2. Threshold 20 bps + 25 bps divergence  → gate FAILS, skip reason
     "mark_divergence" logged, no order fires
  3. Threshold 20 bps + 12.5 bps divergence → gate PASSES, entry fires
  4. Threshold 20 bps + get_mark raises      → graceful passthrough,
     entry fires, warning logged

All tests run offline — no real exchange, DB, or HTTP. The same mocking
pattern used by test_open_position_macro_advisory.py is reused here.
"""

from __future__ import annotations

import logging
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.domain.value_objects import (
    Money,
    Price,
    TradeSide,
    Consensus,
    MacroBias,
    Quantiles,
    TimescalePayload,
    V4Snapshot,
)
from margin_engine.domain.ports import FillResult
from margin_engine.application.use_cases.open_position import OpenPositionUseCase
from margin_engine.application.dto import OpenPositionInput


# ──────────────────────────────────────────────────────────────────────────
# Fixtures — snapshot/payload/use-case builders
# ──────────────────────────────────────────────────────────────────────────
#
# These mirror the helpers in test_open_position_macro_advisory.py. Kept
# local (not imported) so the DQ-07 test file is self-contained and
# readable in isolation — test_mark_divergence_gate.py should tell the
# whole story without cross-reading another file.


def _build_payload(last_price_anchor: float = 80000.0) -> TimescalePayload:
    """Tradeable 15m LONG payload anchored around `last_price_anchor`.

    Quantiles are set so SL/TP ratio and fee-wall gates pass cleanly:
      For LONG at last_price=80000:
        sl_pct = 1.25 * (80000 - 79500) / 80000 = 0.00781  (~78 bps)
        tp_pct = 0.85 * (81000 - 80000) / 80000 = 0.01063  (~106 bps)
        win_ratio = tp/sl = 1.36 (> 1.2)
        tp_pct > fee_budget * 1.3 (0.00117 with 45 bps/side)
    """
    return TimescalePayload(
        timescale="15m",
        status="ok",
        probability_up=0.72,
        regime="TRENDING_UP",
        expected_move_bps=20.0,
        window_close_ts=1776400000,
        quantiles_at_close=Quantiles(
            p10=last_price_anchor - 500.0,
            p25=last_price_anchor - 300.0,
            p50=last_price_anchor + 200.0,
            p75=last_price_anchor + 600.0,
            p90=last_price_anchor + 1000.0,
        ),
    )


def _build_snapshot(
    *,
    last_price: float = 80000.0,
    payload: Optional[TimescalePayload] = None,
) -> V4Snapshot:
    if payload is None:
        payload = _build_payload(last_price_anchor=last_price)
    return V4Snapshot(
        asset="BTC",
        ts=1776400000.0,
        last_price=last_price,
        consensus=Consensus(
            safe_to_trade=True,
            safe_to_trade_reason="ok",
            reference_price=last_price,
            max_divergence_bps=0.5,
            source_agreement_score=0.98,
        ),
        macro=MacroBias(
            bias="NEUTRAL",
            confidence=45,
            direction_gate="ALLOW_ALL",
            size_modifier=1.0,
            threshold_modifier=1.0,
            status="ok",
        ),
        timescales={"15m": payload},
    )


def _build_use_case(
    *,
    v4_max_mark_divergence_bps: float,
    exchange_mark_price: Optional[float] = None,
    exchange_mark_raises: bool = False,
    last_price_for_fill: float = 80000.0,
) -> tuple[OpenPositionUseCase, MagicMock]:
    """Build OpenPositionUseCase with the DQ-07 setting and a mocked exchange.

    Args:
        v4_max_mark_divergence_bps: The DQ-07 flag under test.
        exchange_mark_price: If set, get_mark returns Price(this_value).
            Ignored when exchange_mark_raises is True.
        exchange_mark_raises: If True, get_mark raises RuntimeError.
            Verifies the graceful-degradation branch.
        last_price_for_fill: Fill price returned by place_market_order.

    Returns:
        (use_case, exchange_mock) tuple so tests can inspect the mocked
        `place_market_order.called` flag to tell entry-fired from skip.
    """
    exchange = MagicMock()
    exchange.get_balance = AsyncMock(return_value=Money.usd(500.0))

    if exchange_mark_raises:
        exchange.get_mark = AsyncMock(
            side_effect=RuntimeError("simulated transient exchange error"),
        )
    elif exchange_mark_price is not None:
        exchange.get_mark = AsyncMock(
            return_value=Price(value=exchange_mark_price, pair="BTCUSDT"),
        )
    else:
        # Default no-op mock — not expected to be called when flag = 0.0.
        exchange.get_mark = AsyncMock(
            return_value=Price(value=last_price_for_fill, pair="BTCUSDT"),
        )

    exchange.place_market_order = AsyncMock(
        return_value=FillResult(
            order_id="dq07-test-order-1",
            fill_price=Price(last_price_for_fill),
            filled_notional=30.0,
            commission=0.014,
            commission_asset="USDT",
            commission_is_actual=True,
        )
    )

    portfolio = MagicMock()
    portfolio.starting_capital = Money.usd(500.0)
    portfolio.leverage = 3
    portfolio.can_open_position = MagicMock(return_value=(True, "ok"))
    portfolio.positions = []
    portfolio.add_position = MagicMock()

    repo = MagicMock()
    repo.save = AsyncMock()
    alerts = MagicMock()
    alerts.send_trade_opened = AsyncMock()
    alerts.send_error = AsyncMock()

    probability_port = MagicMock()
    signal_port = MagicMock()
    v4_port = MagicMock()

    uc = OpenPositionUseCase(
        input=OpenPositionInput(
            exchange=exchange,
            portfolio=portfolio,
            repository=repo,
            alerts=alerts,
            probability_port=probability_port,
            signal_port=signal_port,
            v4_snapshot_port=v4_port,
            engine_use_v4_actions=True,
            v4_primary_timescale="15m",
            v4_timescales=("15m",),
            v4_entry_edge=0.10,
            v4_min_expected_move_bps=15.0,
            v4_allow_mean_reverting=False,
            v4_macro_mode="advisory",
            v4_macro_hard_veto_confidence_floor=80,
            v4_macro_advisory_size_mult_on_conflict=0.75,
            v4_allow_no_edge_if_exp_move_bps_gte=None,
            # ── The setting under test ──
            v4_max_mark_divergence_bps=v4_max_mark_divergence_bps,
            fee_rate_per_side=0.00045,
            bet_fraction=0.02,
            venue="hyperliquid",
            strategy_version="v2-probability",
        )
    )
    return uc, exchange


# ──────────────────────────────────────────────────────────────────────────
# Four DQ-07 test cases
# ──────────────────────────────────────────────────────────────────────────


class TestMarkDivergenceGate:
    """DQ-07 gate 9.5 — default OFF + threshold FAIL/PASS + graceful degrade."""

    @pytest.mark.asyncio
    async def test_default_off_any_divergence_passes(self, caplog):
        """Case 1 — v4_max_mark_divergence_bps=0.0 (default).

        Even a massive 50 bps divergence must not block the trade: the
        gate branch is not entered at all because the threshold is 0.
        This protects the zero-behavior-change merge contract: shipping
        DQ-07 with no operator action must not alter any production flow.
        """
        uc, exchange = _build_use_case(
            v4_max_mark_divergence_bps=0.0,
            exchange_mark_price=80400.0,  # 50 bps above 80000 — huge
        )
        snap = _build_snapshot(last_price=80000.0)

        with caplog.at_level(logging.DEBUG):
            result = await uc._execute_v4(snap)

        assert result is not None, "Default-off gate must not block the entry"
        assert exchange.place_market_order.called, (
            "Order must fire when gate is default-off"
        )
        # get_mark should NOT have been called — the gate branch is
        # short-circuited by the `> 0` check before the exchange call.
        assert not exchange.get_mark.called, (
            "get_mark should not be queried when gate is default-off"
        )
        # No mark-divergence log messages of any kind
        assert not any(
            "dq07.mark_divergence_gate" in rec.message for rec in caplog.records
        ), "Gate must be silent when default-off"

    @pytest.mark.asyncio
    async def test_threshold_20bps_divergence_25bps_fails(self, caplog):
        """Case 2 — threshold=20, v4=80000, exchange_mark=80200 (25 bps).

        The gate MUST fail with skip reason "mark_divergence" and NO
        order must fire. The divergence of 25 bps exceeds the 20 bps
        threshold, so this is the regression case the gate is designed
        to catch.
        """
        uc, exchange = _build_use_case(
            v4_max_mark_divergence_bps=20.0,
            exchange_mark_price=80200.0,  # 25 bps above 80000
        )
        snap = _build_snapshot(last_price=80000.0)

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        assert result is None or result.position is None, (
            "Gate must reject when divergence exceeds threshold"
        )
        assert not exchange.place_market_order.called, (
            "No order must fire when mark divergence gate fails"
        )
        assert exchange.get_mark.called, (
            "get_mark must have been queried to compute divergence"
        )
        # Structured skip log landed with reason=mark_divergence
        assert any("mark_divergence" in rec.message for rec in caplog.records), (
            "Expected 'mark_divergence' in _log_skip output"
        )
        # And the detailed WARNING with v4_last_price/exchange_mark fired
        assert any(
            "dq07.mark_divergence_gate_failed" in rec.message for rec in caplog.records
        ), "Expected dq07.mark_divergence_gate_failed warning"

    @pytest.mark.asyncio
    async def test_threshold_20bps_divergence_12bps_passes(self, caplog):
        """Case 3 — threshold=20, v4=80000, exchange_mark=80100 (12.5 bps).

        The divergence of 12.5 bps is below the 20 bps threshold, so the
        gate must PASS and the entry must proceed through to the order.
        """
        uc, exchange = _build_use_case(
            v4_max_mark_divergence_bps=20.0,
            exchange_mark_price=80100.0,  # 12.5 bps above 80000
        )
        snap = _build_snapshot(last_price=80000.0)

        with caplog.at_level(logging.DEBUG):
            result = await uc._execute_v4(snap)

        assert result is not None, "Gate must pass when divergence is below threshold"
        assert exchange.place_market_order.called, (
            "Order must fire when mark divergence is within threshold"
        )
        assert exchange.get_mark.called, (
            "get_mark must have been queried to compute divergence"
        )
        # No failure log
        assert not any(
            "dq07.mark_divergence_gate_failed" in rec.message for rec in caplog.records
        )
        assert not any(
            "mark_divergence" in rec.message and "skip" in rec.message.lower()
            for rec in caplog.records
        ), "No skip log should have fired"

    @pytest.mark.asyncio
    async def test_threshold_20bps_exchange_raises_graceful_passthrough(
        self,
        caplog,
    ):
        """Case 4 — threshold=20, exchange.get_mark raises.

        Graceful degradation: a transient exchange error must NOT block
        trades. We log a warning and let the candidate continue through
        to the SL/TP math gate. This matches the "never stall on a
        transient exchange error" philosophy already established in the
        v4 port adapters and continuation path.
        """
        uc, exchange = _build_use_case(
            v4_max_mark_divergence_bps=20.0,
            exchange_mark_raises=True,
        )
        snap = _build_snapshot(last_price=80000.0)

        with caplog.at_level(logging.WARNING):
            result = await uc._execute_v4(snap)

        assert result is not None, (
            "Graceful degradation: transient exchange error must not block"
        )
        assert exchange.place_market_order.called, (
            "Order must fire when get_mark raises (graceful passthrough)"
        )
        # The warning log surfaced the failure for ops monitoring
        assert any("dq07.mark_query_failed" in rec.message for rec in caplog.records), (
            "Expected dq07.mark_query_failed warning on exchange error"
        )
        # No skip log — the graceful path continues, not rejects
        assert not any(
            "dq07.mark_divergence_gate_failed" in rec.message for rec in caplog.records
        ), "No gate-failed log should fire when mark query itself failed"
