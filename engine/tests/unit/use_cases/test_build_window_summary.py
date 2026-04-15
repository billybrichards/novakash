"""Unit tests for BuildWindowSummaryUseCase (PR C).

Covers the three concrete fixes from the audit that the use case must
preserve:

1. LIVE-only actionable buckets (GHOST goes to shadow).
2. exec-too-late vs outside-window split (must not conflate).
3. "Already traded this window" earlier-trade surfacing.

Plus the formatter adapter round-trip.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

from adapters.alert.window_summary_formatter import format_window_summary
from domain.value_objects import StrategyDecision
from use_cases.build_window_summary import BuildWindowSummaryUseCase


# ── Helpers ───────────────────────────────────────────────────────────────


def _decision(sid, action="SKIP", direction=None, skip_reason=None, confidence=None):
    return StrategyDecision(
        action=action,
        direction=direction,
        confidence=confidence,
        confidence_score=None,
        entry_cap=None,
        collateral_pct=None,
        strategy_id=sid,
        strategy_version="test",
        entry_reason="",
        skip_reason=skip_reason,
        metadata={},
    )


def _cfg(mode, timing=None, pre_gate_hook=None, timescale=None):
    gates = []
    if timing is not None:
        gates.append({"type": "timing", "params": {"min_offset": timing[0], "max_offset": timing[1]}})
    return SimpleNamespace(
        mode=mode,
        gates=gates,
        version="1.0.0",
        pre_gate_hook=pre_gate_hook,
        timescale=timescale,
    )


def _prior_record(sid, eval_offset, action="TRADE", skip_reason=None):
    return SimpleNamespace(
        strategy_id=sid,
        action=action,
        eval_offset=eval_offset,
        skip_reason=skip_reason,
    )


# ── Test cases ────────────────────────────────────────────────────────────


def test_live_only_in_actionable_buckets():
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision("v10_gate", action="TRADE", direction="DOWN", confidence="HIGH"),
        _decision("v4_ghost", action="TRADE", direction="UP"),
    ]
    configs = {
        "v10_gate": _cfg("LIVE"),
        "v4_ghost": _cfg("GHOST"),
    }
    ctx = uc.execute(
        window_ts=1, eval_offset=120, timescale="5m",
        open_price=100.0, current_price=101.0,
        sources_agree="YES (DOWN)",
        decisions=decisions, configs=configs,
    )
    assert len(ctx.eligible) == 1
    assert ctx.eligible[0].strategy_id == "v10_gate"
    # Ghost TRADE does NOT appear in eligible — collapsed into shadow
    assert all(r.strategy_id != "v4_ghost" for r in ctx.eligible)
    assert len(ctx.ghost_shadow) == 1
    assert ctx.ghost_shadow[0].strategy_id == "v4_ghost"


def test_exec_too_late_vs_outside_window_split():
    uc = BuildWindowSummaryUseCase()
    decisions = [
        # Custom exec-safety hook: "too late (<70s)" — not an outside-window
        _decision("v4_fusion",
                  skip_reason="polymarket: timing=optimal T-62 -- too late (<70s), skip"),
        # YAML timing gate: "outside [...]"
        _decision("v15m_fusion",
                  skip_reason="timing: T-62 outside [240, 480]"),
        # Custom hook variant: "timing=late -- outside window" — MUST go to off_window,
        # not exec-timing (was the bug pre-PR-A).
        _decision("v4_downonly",
                  skip_reason="polymarket: timing=late -- outside window"),
    ]
    configs = {
        "v4_fusion": _cfg("LIVE"),
        "v15m_fusion": _cfg("LIVE", timing=(240, 480)),
        "v4_downonly": _cfg("LIVE"),
    }
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="5m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
    )
    # v4_fusion has no timing bounds → exec_timing bucket (window-state unknown)
    assert [r.strategy_id for r in ctx.blocked_exec_timing] == ["v4_fusion"]
    # v15m_fusion has bounds (240,480); at T-62 window closed → window_expired
    assert [r.strategy_id for r in ctx.window_expired] == ["v15m_fusion"]
    v15m_text = ctx.window_expired[0].text
    assert "T-480..T-240" in v15m_text
    # v4_downonly has no bounds → off_window (YAML-style outside-window reason)
    off_ids = [r.strategy_id for r in ctx.off_window]
    assert off_ids == ["v4_downonly"]


def test_already_traded_earlier_suppresses_off_window():
    uc = BuildWindowSummaryUseCase()
    # LIVE strategy that's now OFF-WINDOW at T-62, but had TRADED earlier at T-312
    decisions = [_decision("v15m_gate", skip_reason="timing: T-62 outside [200, 400]")]
    configs = {"v15m_gate": _cfg("LIVE", timing=(200, 400))}
    prior = [_prior_record("v15m_gate", eval_offset=312)]
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="15m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
        prior_decisions=prior,
    )
    # Must NOT show as off-window — show as "already traded at T-312"
    assert ctx.off_window == ()
    assert len(ctx.already_traded) == 1
    assert "T-312" in ctx.already_traded[0].text


def test_prior_trades_at_equal_or_smaller_offset_ignored():
    # Trades at offsets <= current must not count — those are AFTER or AT now.
    uc = BuildWindowSummaryUseCase()
    decisions = [_decision("v10_gate", skip_reason="some other reason")]
    configs = {"v10_gate": _cfg("LIVE")}
    prior = [
        _prior_record("v10_gate", eval_offset=62),  # equal to now
        _prior_record("v10_gate", eval_offset=30),  # after now
    ]
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="5m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
        prior_decisions=prior,
    )
    assert ctx.already_traded == ()
    assert len(ctx.blocked_signal) == 1


def test_window_expired_reframes_too_late_at_final_offset():
    """At T-62, strategy window T-180..T-70 has passed. 'too late (<70s)'
    is circular — reclassify into window_expired with dominant prior skip."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v4_fusion",
            skip_reason="polymarket: timing=optimal T-62 -- too late (<70s), skip",
        ),
    ]
    configs = {"v4_fusion": _cfg("LIVE", timing=(70, 180))}
    prior = [
        _prior_record(
            "v4_fusion", 180, action="SKIP",
            skip_reason="confidence: dist=0.074 < 0.12",
        ),
        _prior_record(
            "v4_fusion", 150, action="SKIP",
            skip_reason="confidence: dist=0.081 < 0.12",
        ),
        _prior_record(
            "v4_fusion", 120, action="SKIP",
            skip_reason="confidence: dist=0.095 < 0.12",
        ),
    ]
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="5m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
        prior_decisions=prior,
    )
    assert ctx.blocked_exec_timing == ()
    assert len(ctx.window_expired) == 1
    body = ctx.window_expired[0].text
    assert "window T-180..T-70 expired" in body
    assert "×3" in body
    assert "T-180" in body and "T-150" in body and "T-120" in body


def test_window_expired_with_no_prior_skips_shows_never_evaluated():
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v4_fusion",
            skip_reason="polymarket: timing=optimal T-62 -- too late (<70s), skip",
        ),
    ]
    configs = {"v4_fusion": _cfg("LIVE", timing=(70, 180))}
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="5m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
        prior_decisions=(),
    )
    assert len(ctx.window_expired) == 1
    assert "never evaluated in-window" in ctx.window_expired[0].text


def test_window_expired_prior_too_late_reasons_filtered():
    """Prior 'too late' skips must NOT feed the dominant-reason summary."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v4_fusion",
            skip_reason="polymarket: too late (<70s), skip",
        ),
    ]
    configs = {"v4_fusion": _cfg("LIVE", timing=(70, 180))}
    prior = [
        _prior_record(
            "v4_fusion", 180, action="SKIP",
            skip_reason="polymarket: too late (<70s), skip",
        ),
        _prior_record(
            "v4_fusion", 150, action="SKIP",
            skip_reason="confidence: dist=0.08 < 0.12",
        ),
    ]
    ctx = uc.execute(
        window_ts=1, eval_offset=62, timescale="5m",
        open_price=None, current_price=None,
        sources_agree="",
        decisions=decisions, configs=configs,
        prior_decisions=prior,
    )
    assert len(ctx.window_expired) == 1
    body = ctx.window_expired[0].text
    assert "confidence" in body
    assert "×1" in body


def test_formatter_roundtrip_has_all_sections():
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision("v10_gate", action="TRADE", direction="DOWN", confidence="HIGH"),
        _decision("v4_basic", skip_reason="confidence: dist=0.074 < 0.12"),
        _decision("v4_fusion", skip_reason="polymarket: too late (<70s), skip"),
        _decision("v15m_fusion", skip_reason="timing: T-62 outside [240, 480]"),
        _decision("v15m_gate", skip_reason="timing: T-62 outside [200, 400]"),
        _decision("v4_up_asian", action="TRADE", direction="UP"),
    ]
    configs = {
        "v10_gate": _cfg("LIVE"),
        "v4_basic": _cfg("LIVE"),
        "v4_fusion": _cfg("LIVE"),
        "v15m_fusion": _cfg("LIVE", timing=(240, 480)),
        "v15m_gate": _cfg("LIVE", timing=(200, 400)),
        "v4_up_asian": _cfg("GHOST"),
    }
    prior = [_prior_record("v15m_gate", eval_offset=312)]
    ctx = uc.execute(
        window_ts=1_700_000_000, eval_offset=62, timescale="5m",
        open_price=74_252.10, current_price=74_430.80,
        sources_agree="YES (DOWN)",
        decisions=decisions, configs=configs,
        prior_decisions=prior,
    )
    text = format_window_summary(ctx)
    # Header
    assert "Open $74,252.10" in text
    assert "Now $74,430.80" in text
    assert "T-62" in text
    assert "YES (DOWN)" in text
    # Every section title
    assert "Eligible now (tradable):" in text
    assert "Blocked by signal:" in text
    assert "Blocked by execution timing:" in text
    # v15m_fusion (timing 240..480) at T-62 → window_expired, not off_window
    assert "Window expired without trade:" in text
    assert "Already traded this window:" in text
    assert "Inactive (GHOST shadow):" in text
    # Already-traded wins over off-window for v15m_gate
    assert "traded at T-312" in text


def test_formatter_omits_empty_sections():
    uc = BuildWindowSummaryUseCase()
    # Only a single TRADE, nothing else
    decisions = [_decision("v10_gate", action="TRADE", direction="UP")]
    configs = {"v10_gate": _cfg("LIVE")}
    ctx = uc.execute(
        window_ts=1, eval_offset=120, timescale="5m",
        open_price=100.0, current_price=101.0,
        sources_agree="",
        decisions=decisions, configs=configs,
    )
    text = format_window_summary(ctx)
    assert "Eligible now (tradable):" in text
    assert "Blocked by signal:" not in text
    assert "Blocked by execution timing:" not in text
    assert "Off-window this offset:" not in text
    assert "Inactive (GHOST shadow):" not in text


# ── Poly v2 synthetic bounds (v4_fusion / v15m_fusion family) ─────────────


def test_v4_fusion_poly_v2_hook_synthetic_bounds_5m():
    """v4_fusion has gates=[] + pre_gate_hook=evaluate_polymarket_v2 + timescale=5m.
    At T-62 (< 70), the skip reason is 'polymarket: timing=early T-62 -- too
    late (<70s), skip' which the old code bucketed as blocked_exec_timing
    (circular). With synthetic bounds (70, 180), the reclassifier recognises
    the window has closed and surfaces the real prior blocker."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v4_fusion",
            skip_reason="polymarket: timing=early T-62 -- too late (<70s), skip",
        ),
    ]
    configs = {
        "v4_fusion": _cfg(
            "LIVE",
            pre_gate_hook="evaluate_polymarket_v2",
            timescale="5m",
        ),
    }
    # Prior in-window blocker at T-120 — the real reason
    prior = [
        _prior_record(
            "v4_fusion",
            eval_offset=120,
            action="SKIP",
            skip_reason="consensus not safe_to_trade",
        ),
    ]
    ctx = uc.execute(
        window_ts=1_700_000_000,
        eval_offset=62,
        timescale="5m",
        open_price=74_252.10,
        current_price=74_430.80,
        sources_agree="NO (mixed)",
        decisions=decisions,
        configs=configs,
        prior_decisions=prior,
    )
    # Must reclassify to window_expired (not blocked_exec_timing)
    assert len(ctx.window_expired) == 1
    assert len(ctx.blocked_exec_timing) == 0
    assert "window T-180..T-70 expired" in ctx.window_expired[0].text
    assert "consensus not safe_to_trade" in ctx.window_expired[0].text


def test_v15m_fusion_poly_v2_hook_synthetic_bounds():
    """v15m_fusion has same YAML shape but timescale=15m → bounds (180, 250)."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v15m_fusion",
            skip_reason="polymarket: timing=late -- outside window",
        ),
    ]
    configs = {
        "v15m_fusion": _cfg(
            "LIVE",
            pre_gate_hook="evaluate_polymarket_v2",
            timescale="15m",
        ),
    }
    # At T-120, we're past the 15m strategy's lower bound (180) — window closed
    ctx = uc.execute(
        window_ts=1,
        eval_offset=120,
        timescale="15m",
        open_price=100.0,
        current_price=101.0,
        sources_agree="",
        decisions=decisions,
        configs=configs,
        prior_decisions=[],
    )
    assert len(ctx.window_expired) == 1
    assert "window T-250..T-180 expired" in ctx.window_expired[0].text
    # No prior skips → "never evaluated in-window" fallback
    assert "never evaluated in-window" in ctx.window_expired[0].text


def test_poly_v2_hook_in_window_still_surfaces_signal_blocker():
    """If eval_offset is still inside synthetic bounds, the reclassifier
    should NOT fire — real skip reason (confidence) goes to blocked_signal."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v4_fusion",
            skip_reason="polymarket: p_up=0.610 dist=0.110 < 0.12 threshold",
        ),
    ]
    configs = {
        "v4_fusion": _cfg(
            "LIVE",
            pre_gate_hook="evaluate_polymarket_v2",
            timescale="5m",
        ),
    }
    # T-120 is inside (70, 180) → window still open
    ctx = uc.execute(
        window_ts=1,
        eval_offset=120,
        timescale="5m",
        open_price=100.0,
        current_price=101.0,
        sources_agree="",
        decisions=decisions,
        configs=configs,
        prior_decisions=[],
    )
    assert len(ctx.window_expired) == 0
    assert len(ctx.blocked_signal) == 1
    assert "dist=0.110 < 0.12" in ctx.blocked_signal[0].text


def test_poly_v2_hook_unknown_timescale_returns_no_bounds():
    """Synthetic bounds only defined for 5m and 15m. Other timescales fall
    back to None — old 'too late' path preserved (no worse than pre-PR)."""
    uc = BuildWindowSummaryUseCase()
    decisions = [
        _decision(
            "v_mystery",
            skip_reason="polymarket: timing=optimal T-15 -- too late (<20s), skip",
        ),
    ]
    configs = {
        "v_mystery": _cfg(
            "LIVE",
            pre_gate_hook="evaluate_polymarket_v2",
            timescale="30m",  # unsupported
        ),
    }
    ctx = uc.execute(
        window_ts=1,
        eval_offset=15,
        timescale="30m",
        open_price=100.0,
        current_price=101.0,
        sources_agree="",
        decisions=decisions,
        configs=configs,
        prior_decisions=[],
    )
    assert len(ctx.window_expired) == 0
    assert len(ctx.blocked_exec_timing) == 1
