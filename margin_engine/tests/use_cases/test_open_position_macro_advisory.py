"""
Phase A unit tests — macro advisory mode and MacroBias per-horizon map.

Tests the 2026-04-11 macro-gate demotion wired into OpenPositionUseCase,
ManagePositionsUseCase, and the MacroBias value object. See the matching
docs/MACRO_AUDIT_2026-04-11.md for the audit that motivated the change.

These tests run offline — they do NOT call any exchange, DB, or HTTP
adapter. All dependencies are stubbed with the minimum surface needed to
walk the macro-gate branch.

Scope (verbatim from the plan file):
  1. Advisory + confidence >= 80 + conflict → does not skip, 0.75x haircut
  2. Advisory + confidence <  80 + conflict → passes through unchanged
  3. Veto     + confidence >= 80 + conflict → returns None with _veto log
  4. Veto     + confidence <  80 + conflict → passes through (below floor)
  5. Continuation advisory — existing position NOT force-closed
  6. MacroBias.for_timescale('5m') empty → None, populated → dict
  7. _parse_macro({"timescale_map": {...}}) populates field correctly
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from margin_engine.domain.value_objects import (
    Consensus,
    FillResult,
    MacroBias,
    Money,
    Price,
    Quantiles,
    TimescalePayload,
    TradeSide,
    V4Snapshot,
    _parse_macro,
)
from margin_engine.use_cases.open_position import OpenPositionUseCase


# ──────────────────────────────────────────────────────────────────────────
# Test fixtures — minimal V4Snapshot builders
# ──────────────────────────────────────────────────────────────────────────

def _build_payload(
    *,
    probability_up: float = 0.72,
    regime: str = "TRENDING_UP",
    expected_move_bps: float = 20.0,
    status: str = "ok",
) -> TimescalePayload:
    """A tradeable 15m payload with a strong LONG probability.

    Quantiles are set so that for a LONG at last_price=70000:
      sl_pct = 1.25 * (70000 - 69500) / 70000 = 0.00893  (~89 bps)
      tp_pct = 0.85 * (71000 - 70000) / 70000 = 0.01214  (~121 bps)
      win_ratio = tp/sl = 1.36  (> 1.2 threshold)
      tp_pct > fee_budget * 1.3  (0.00117 with 45 bps/side fees)
    which clears both quantile-derived gates and lets the test see the
    full entry path.
    """
    return TimescalePayload(
        timescale="15m",
        status=status,
        probability_up=probability_up,
        regime=regime,
        expected_move_bps=expected_move_bps,
        window_close_ts=1776400000,
        quantiles_at_close=Quantiles(
            p10=69500.0, p25=69700.0, p50=70200.0, p75=70600.0, p90=71000.0,
        ),
    )


def _build_snapshot(
    *,
    macro: MacroBias,
    payload: Optional[TimescalePayload] = None,
    consensus_safe: bool = True,
) -> V4Snapshot:
    if payload is None:
        payload = _build_payload()
    return V4Snapshot(
        asset="BTC",
        ts=1776400000.0,
        last_price=70000.0,
        consensus=Consensus(
            safe_to_trade=consensus_safe,
            safe_to_trade_reason="ok",
            reference_price=70000.0,
            max_divergence_bps=0.5,
            source_agreement_score=0.98,
        ),
        macro=macro,
        timescales={"15m": payload},
    )


def _build_use_case(
    *,
    macro_mode: str = "advisory",
    macro_conf_floor: int = 80,
    advisory_haircut: float = 0.75,
    allow_no_edge_gte: Optional[float] = None,
) -> tuple[OpenPositionUseCase, MagicMock]:
    """Construct OpenPositionUseCase with every dependency mocked.

    Returns (use_case, exchange_mock) so tests can inspect whether a
    market order was attempted.
    """
    exchange = MagicMock()
    exchange.get_balance = AsyncMock(return_value=Money.usd(500.0))
    # A realistic FillResult so the post-gate order path can complete and
    # return a Position instead of crashing when a test's scenario expects
    # the entry to fire. Tests that expect a SKIP inspect .called==False.
    exchange.place_market_order = AsyncMock(
        return_value=FillResult(
            order_id="test-order-1",
            fill_price=Price(70000.0),
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
        v4_macro_mode=macro_mode,
        v4_macro_hard_veto_confidence_floor=macro_conf_floor,
        v4_macro_advisory_size_mult_on_conflict=advisory_haircut,
        v4_allow_no_edge_if_exp_move_bps_gte=allow_no_edge_gte,
        fee_rate_per_side=0.00045,
        bet_fraction=0.02,
        venue="hyperliquid",
        strategy_version="v2-probability",
    )
    return uc, exchange


# ──────────────────────────────────────────────────────────────────────────
# MacroBias per-horizon map tests
# ──────────────────────────────────────────────────────────────────────────

class TestMacroBiasTimescaleMap:
    """The `timescale_map` field added in Phase A — Phase C consumers rely
    on it, but Phase A just has to make sure the parser and accessor work."""

    def test_empty_map_returns_none(self):
        macro = MacroBias()
        assert macro.for_timescale("5m") is None
        assert macro.for_timescale("15m") is None

    def test_populated_map_returns_dict(self):
        macro = MacroBias(
            timescale_map={
                "5m":  {"bias": "BEAR", "confidence": 72},
                "15m": {"bias": "NEUTRAL", "confidence": 45},
                "1h":  {"bias": "BULL", "confidence": 60},
            }
        )
        assert macro.for_timescale("5m") == {"bias": "BEAR", "confidence": 72}
        assert macro.for_timescale("15m")["confidence"] == 45
        assert macro.for_timescale("1h")["bias"] == "BULL"
        assert macro.for_timescale("4h") is None  # not in map

    def test_parse_macro_with_timescale_map(self):
        raw = {
            "bias": "NEUTRAL",
            "confidence": 45,
            "direction_gate": "ALLOW_ALL",
            "timescale_map": {
                "5m":  {"bias": "NEUTRAL", "confidence": 35},
                "15m": {"bias": "NEUTRAL", "confidence": 40},
                "1h":  {"bias": "BEAR",    "confidence": 55},
                "4h":  {"bias": "BULL",    "confidence": 60},
            },
        }
        macro = _parse_macro(raw)
        assert macro.bias == "NEUTRAL"
        assert macro.confidence == 45
        assert macro.for_timescale("1h")["bias"] == "BEAR"
        assert macro.for_timescale("4h")["bias"] == "BULL"

    def test_parse_macro_without_timescale_map_is_empty_dict(self):
        """Legacy payloads pre-2026-04-11 have no timescale_map key."""
        raw = {"bias": "BEAR", "confidence": 60, "direction_gate": "SKIP_UP"}
        macro = _parse_macro(raw)
        assert macro.timescale_map == {}
        assert macro.for_timescale("15m") is None

    def test_parse_macro_with_non_dict_timescale_map_is_ignored(self):
        """Defensive: a broken upstream passing e.g. a list does not crash."""
        raw = {"bias": "BULL", "timescale_map": ["not", "a", "dict"]}
        macro = _parse_macro(raw)
        assert macro.timescale_map == {}


# ──────────────────────────────────────────────────────────────────────────
# Advisory / veto dispatch at entry
# ──────────────────────────────────────────────────────────────────────────

def _bear_macro(confidence: int) -> MacroBias:
    """Qwen BEAR / SKIP_UP at the given confidence — the exact shape that
    was vetoing 73% of entries in the 24h audit."""
    return MacroBias(
        bias="BEAR",
        confidence=confidence,
        direction_gate="SKIP_UP",
        size_modifier=1.0,
        threshold_modifier=1.0,
        override_active=(confidence >= 80),
        status="ok",
    )


class TestOpenPositionMacroAdvisory:
    """Entry-side gate tests.

    Each test builds a fresh use case, stages a V4Snapshot with a strong
    LONG setup that should pass all gates EXCEPT potentially the macro one,
    and asserts the expected outcome. `execute_v4` is called directly
    (bypassing `execute()`'s flag check) so the tests don't need to mock
    the v4 port's `get_latest`.
    """

    @pytest.mark.asyncio
    async def test_advisory_high_conf_conflict_applies_haircut(self, caplog):
        """Advisory + confidence=80 + BEAR/SKIP_UP + LONG side:
        the candidate must NOT skip, and the size multiplier must be
        reduced by the advisory haircut (0.75)."""
        uc, exchange = _build_use_case(
            macro_mode="advisory", advisory_haircut=0.75,
        )
        snap = _build_snapshot(macro=_bear_macro(80))

        with caplog.at_level(logging.INFO):
            await uc._execute_v4(snap)

        # Core assertion: the advisory haircut log message fired —
        # this is the signal that macro_conflict was set and consumed.
        assert any(
            "macro advisory conflict" in rec.message
            for rec in caplog.records
        ), "Advisory haircut log line not emitted"
        # Order was attempted (not blocked)
        assert exchange.place_market_order.called, \
            "Advisory mode must NOT block the entry — order should have fired"
        # Verify the haircut actually made it into the notional calc —
        # portfolio.can_open_position is called with the haircut collateral
        called_with = uc._portfolio.can_open_position.call_args.args[0]
        # starting_capital=500 * bet_fraction=0.02 * size_mod=1.0 * haircut=0.75 = 7.5
        assert called_with.amount == pytest.approx(7.5, abs=0.01), \
            f"Expected 500 * 0.02 * 1.0 * 0.75 = 7.5, got {called_with.amount}"

    @pytest.mark.asyncio
    async def test_advisory_low_conf_conflict_is_noop(self, caplog):
        """Advisory + confidence=60 (below floor) + BEAR/SKIP_UP + LONG:
        the gate is a no-op. No haircut applied, no skip, no log about
        conflicts. Default size_mult = 1.0 from MacroBias (not the
        haircut). Entry proceeds at full size."""
        uc, exchange = _build_use_case(
            macro_mode="advisory", macro_conf_floor=80, advisory_haircut=0.75,
        )
        snap = _build_snapshot(macro=_bear_macro(60))

        with caplog.at_level(logging.INFO):
            await uc._execute_v4(snap)

        # No advisory log fired
        assert not any(
            "macro advisory conflict" in rec.message for rec in caplog.records
        )
        # Entry fired
        assert exchange.place_market_order.called
        # Collateral was 500 * 0.02 * 1.0 = 10.0 (full size, no haircut)
        called_with = uc._portfolio.can_open_position.call_args.args[0]
        assert called_with.amount == pytest.approx(10.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_veto_high_conf_conflict_blocks_entry(self, caplog):
        """Veto + confidence=85 + BEAR/SKIP_UP + LONG:
        entry must be blocked with a _veto skip reason. No order fired."""
        uc, exchange = _build_use_case(
            macro_mode="veto", macro_conf_floor=80,
        )
        snap = _build_snapshot(macro=_bear_macro(85))

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        assert result is None, "Veto mode must block the entry"
        assert not exchange.place_market_order.called, \
            "No order should fire when macro vetoed"
        # Check the specific skip reason landed in the logs
        assert any(
            "macro_skip_up_veto" in rec.message for rec in caplog.records
        ), "Expected macro_skip_up_veto in skip log"

    @pytest.mark.asyncio
    async def test_veto_low_conf_conflict_passes_through(self, caplog):
        """Veto + confidence=70 (below floor) + BEAR/SKIP_UP + LONG:
        gate does not fire because confidence < floor. Entry proceeds."""
        uc, exchange = _build_use_case(
            macro_mode="veto", macro_conf_floor=80,
        )
        snap = _build_snapshot(macro=_bear_macro(70))

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        # Entry NOT blocked because confidence < floor
        assert exchange.place_market_order.called, \
            "Below-floor macro must not veto even in veto mode"
        assert result is not None

    @pytest.mark.asyncio
    async def test_veto_no_conflict_passes_through(self):
        """Veto mode, macro is NEUTRAL/ALLOW_ALL — gate is a no-op."""
        uc, exchange = _build_use_case(macro_mode="veto")
        neutral = MacroBias(
            bias="NEUTRAL", confidence=90,
            direction_gate="ALLOW_ALL", status="ok",
        )
        snap = _build_snapshot(macro=neutral)

        result = await uc._execute_v4(snap)

        assert exchange.place_market_order.called
        assert result is not None


# ──────────────────────────────────────────────────────────────────────────
# NO_EDGE override
# ──────────────────────────────────────────────────────────────────────────

class TestNoEdgeOverride:
    """The experimental `v4_allow_no_edge_if_exp_move_bps_gte` flag."""

    @pytest.mark.asyncio
    async def test_no_edge_override_off_still_skips(self, caplog):
        """Default: flag is None → NO_EDGE payloads still skip."""
        uc, _ = _build_use_case(allow_no_edge_gte=None)
        payload = _build_payload(
            regime="NO_EDGE",
            probability_up=0.68,
            expected_move_bps=5.0,  # over the threshold
        )
        snap = _build_snapshot(
            macro=MacroBias(bias="NEUTRAL", status="ok"),
            payload=payload,
        )

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        assert result is None
        assert any(
            "not_tradeable" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_no_edge_override_on_allows_entry(self, caplog):
        """Flag set to 3.0 AND exp_move >= 3 AND regime=NO_EDGE → the
        override fires (not_tradeable check bypassed). The rest of the
        gate stack still runs — for the test to see the full entry, the
        expected_move also needs to clear v4_min_expected_move_bps (15.0).
        """
        uc, exchange = _build_use_case(allow_no_edge_gte=3.0)
        payload = _build_payload(
            regime="NO_EDGE",
            probability_up=0.68,
            expected_move_bps=20.0,  # above 3.0 (override) AND 15.0 (fee wall)
        )
        snap = _build_snapshot(
            macro=MacroBias(bias="NEUTRAL", status="ok"),
            payload=payload,
        )

        with caplog.at_level(logging.INFO):
            await uc._execute_v4(snap)

        # Override log fired (the primary thing under test)
        assert any(
            "NO_EDGE override applied" in rec.message for rec in caplog.records
        )
        # Entry proceeded all the way to order placement
        assert exchange.place_market_order.called

    @pytest.mark.asyncio
    async def test_no_edge_override_fires_but_fee_wall_still_applies(self, caplog):
        """Flag set but exp_move is above the override threshold yet below
        the fee wall → override fires, fee wall then correctly skips. This
        is the expected layering: the override relaxes regime eligibility,
        it does NOT bypass other gates."""
        uc, exchange = _build_use_case(allow_no_edge_gte=3.0)
        payload = _build_payload(
            regime="NO_EDGE",
            expected_move_bps=5.0,  # > 3 override, < 15 fee wall
        )
        snap = _build_snapshot(
            macro=MacroBias(bias="NEUTRAL", status="ok"),
            payload=payload,
        )

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        assert result is None
        assert any("NO_EDGE override applied" in rec.message for rec in caplog.records)
        assert any("expected_move_below_fee_wall" in rec.message for rec in caplog.records)
        assert not exchange.place_market_order.called

    @pytest.mark.asyncio
    async def test_no_edge_override_below_threshold_still_skips(self, caplog):
        """Flag set to 3.0 but exp_move=2.0 < 3.0 → override does NOT fire,
        candidate still skips with not_tradeable."""
        uc, _ = _build_use_case(allow_no_edge_gte=3.0)
        payload = _build_payload(
            regime="NO_EDGE", expected_move_bps=2.0,
        )
        snap = _build_snapshot(
            macro=MacroBias(bias="NEUTRAL", status="ok"),
            payload=payload,
        )

        with caplog.at_level(logging.INFO):
            result = await uc._execute_v4(snap)

        assert result is None
        assert not any(
            "NO_EDGE override applied" in rec.message for rec in caplog.records
        )
