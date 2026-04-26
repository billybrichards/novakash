"""Regression tests for FAK + timing race (live bug 2026-04-26).

Background
----------
v9_lgb_only / v10_lgb_only had ``min_offset_sec: 24`` in their YAML
gate_params. Strategies fired TRADE at T-24, but the FAK ladder takes
10–25s end-to-end (RFQ poll, retries, CLOB submit). Result: every fired
trade hit ``execute_trade.timing_recheck_blocked: eval_offset_past_close``
mid-execution because the window closed before the FAK could fill.

Empirical: 0/30 fills since 15:30 UTC restart.

Fix: bump ``min_offset_sec`` from 24 → 45 in both YAMLs to give the
FAK ladder enough headroom to complete before the window closes.

These tests pin two invariants:

1. The YAML configs themselves expose ``min_offset_sec >= 45`` for
   strategies that fire FAK orders. Anyone bumping it back down will
   re-introduce the race.
2. The ``_recheck_timing_before_execute`` defence-in-depth guard
   correctly flags ``eval_offset_past_close`` when wall clock has
   passed window close, regardless of how the strategy's own gate fired.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from use_cases.execute_trade import _recheck_timing_before_execute
from domain.value_objects import StrategyDecision, WindowKey


CONFIG_DIR = Path(__file__).parent.parent / "strategies" / "configs"
# Strategies that fire FAK orders and need execution headroom.
# v9_ensemble inherits from v9_lgb_only via hook chain.
FAK_LIVE_STRATEGIES = ("v9_lgb_only", "v10_lgb_only")
MIN_SAFE_OFFSET_SEC = 45  # see audit #319 / live incident 2026-04-26


# ── 1. YAML safety floor ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", FAK_LIVE_STRATEGIES)
def test_strategy_min_offset_above_fak_safety_floor(name: str):
    """LIVE strategies that fire FAK orders must allow >=45s execution
    headroom or the FAK ladder cannot complete before window close."""
    yaml_path = CONFIG_DIR / f"{name}.yaml"
    assert yaml_path.exists(), f"missing config: {yaml_path}"
    cfg = yaml.safe_load(yaml_path.read_text())
    min_off = cfg.get("gate_params", {}).get("min_offset_sec")
    assert min_off is not None, (
        f"{name}: min_offset_sec must be set explicitly so the FAK "
        f"safety-floor check can validate it"
    )
    assert min_off >= MIN_SAFE_OFFSET_SEC, (
        f"{name}: min_offset_sec={min_off} is below the FAK safety floor "
        f"({MIN_SAFE_OFFSET_SEC}s). FAK ladder takes 10–25s; firing inside "
        f"that window guarantees ``eval_offset_past_close`` failures. See "
        f"live incident 2026-04-26."
    )


@pytest.mark.parametrize("name", FAK_LIVE_STRATEGIES)
def test_strategy_max_offset_above_min_offset(name: str):
    """Sanity: the entry window must be non-empty."""
    yaml_path = CONFIG_DIR / f"{name}.yaml"
    cfg = yaml.safe_load(yaml_path.read_text())
    gp = cfg.get("gate_params", {})
    min_off = gp.get("min_offset_sec")
    max_off = gp.get("max_offset_sec")
    assert max_off is not None, f"{name}: max_offset_sec must be set"
    assert max_off > min_off, (
        f"{name}: max_offset_sec={max_off} must be > min_offset_sec={min_off}"
    )


# ── 2. _recheck_timing_before_execute defence ──────────────────────────────


def _make_decision(
    min_offset_sec=45,
    strategy_id: str = "v9_lgb_only",
) -> StrategyDecision:
    metadata: dict = {}
    if min_offset_sec is not None:
        metadata["min_offset_sec"] = min_offset_sec
    return StrategyDecision.trade(
        direction="DOWN",
        strategy_id=strategy_id,
        strategy_version="9.1.0-lgb",
        entry_reason="test",
        metadata=metadata,
    )


def _make_key(window_ts: int = 1_777_200_000, timeframe: str = "5m") -> WindowKey:
    return WindowKey(asset="BTC", window_ts=window_ts, timeframe=timeframe)


class TestPastCloseGuard:
    """The ``eval_offset_past_close`` guard MUST fire when wall clock is
    at or past window close — anything else is a stale-surface fill that
    races resolution. This is the bug currently hitting v9_lgb_only."""

    def test_exactly_at_close_blocks(self):
        key = _make_key(window_ts=1_777_200_000)
        # Window closes at 1_777_200_000 + 300 = 1_777_200_300
        result = _recheck_timing_before_execute(
            key,
            _make_decision(),
            now_fn=lambda: 1_777_200_300,
        )
        assert result is not None
        assert "eval_offset_past_close" in result

    def test_past_close_blocks_with_negative_offset(self):
        key = _make_key(window_ts=1_777_200_000)
        result = _recheck_timing_before_execute(
            key,
            _make_decision(),
            now_fn=lambda: 1_777_200_500,  # 200s past close
        )
        assert result is not None
        assert "eval_offset_past_close" in result
        assert "-200" in result  # offset value surfaced for debugging

    def test_inside_window_passes(self):
        """At T-90 (well inside the window), the guard must NOT fire."""
        key = _make_key(window_ts=1_777_200_000)
        result = _recheck_timing_before_execute(
            key,
            _make_decision(),
            now_fn=lambda: 1_777_200_210,  # T-90
        )
        assert result is None

    def test_at_t_minus_24_fak_floor_does_not_block(self):
        """The min-offset floor recheck is intentionally disabled (PR
        7e31330): the strategy's own gate handles min_offset, the
        recheck only catches PAST-close drift. Guarded so the recheck
        cannot mis-fire on a strategy whose gate legitimately fires at
        T-24 even though the YAML safety floor is now 45s — defence in
        depth must NOT regress on this."""
        key = _make_key(window_ts=1_777_200_000)
        # Decision says min_offset_sec=45 (post-fix). Wall clock is at
        # T-24 (24s before close). Guard should NOT block — the past-close
        # check is the only enforced gate at this layer.
        result = _recheck_timing_before_execute(
            key,
            _make_decision(min_offset_sec=45),
            now_fn=lambda: 1_777_200_276,
        )
        assert result is None, (
            "The execute-time recheck must only enforce past-close, not "
            "min-offset drift. Re-enabling drift here will cause false "
            "blocks for any strategy whose gate fires near min_offset."
        )


class TestDegenerateInputs:
    """The recheck must fail OPEN (return None) on degenerate inputs so a
    malformed key cannot silently block every legitimate trade."""

    def test_synthetic_zero_window_ts_returns_none(self):
        """If somehow a key with window_ts=0 reaches the recheck (e.g.
        synthetic test path or future bug), the helper must fail OPEN."""

        class _FakeKey:
            asset = "BTC"
            window_ts = 0
            timeframe = "5m"
            duration_secs = 0

        result = _recheck_timing_before_execute(
            _FakeKey(),  # type: ignore[arg-type]
            _make_decision(),
            now_fn=lambda: 1_777_200_500,
        )
        assert result is None

    def test_unknown_timeframe_uses_5m_fallback(self):
        """Unknown timeframe → 5m default, recheck still works."""
        key = WindowKey(
            asset="BTC", window_ts=1_777_200_000, timeframe="5m"
        )
        # Override duration_secs to simulate an old-format key
        result = _recheck_timing_before_execute(
            key, _make_decision(), now_fn=lambda: 1_777_200_500
        )
        assert result is not None  # 200s past close → block


class TestMinOffsetMetadataReadButNotEnforced:
    """The recheck still parses ``min_offset_sec`` from metadata for
    forward-compat (and so it's available for logging), but it must not
    cause a block — that path was deliberately disabled to avoid
    false-positive races against 3-tick entry confirmation."""

    def test_aggressive_min_offset_does_not_block_inside_window(self):
        """Even with min_offset_sec=120, a wall-clock T-30 must NOT block.
        The strategy gate is the authority on min_offset; the recheck is
        only the past-close guard."""
        key = _make_key(window_ts=1_777_200_000)
        result = _recheck_timing_before_execute(
            key,
            _make_decision(min_offset_sec=120),
            now_fn=lambda: 1_777_200_270,  # T-30
        )
        assert result is None

    def test_metadata_default_when_absent(self):
        """No min_offset_sec in metadata, no kwarg → default constant
        loaded; the past-close guard still works."""
        d = StrategyDecision.trade(
            direction="UP",
            strategy_id="x",
            strategy_version="1",
            entry_reason="test",
            metadata={},
        )
        key = _make_key(window_ts=1_777_200_000)
        # Past close → still blocks
        result = _recheck_timing_before_execute(
            key, d, now_fn=lambda: 1_777_200_400
        )
        assert result is not None
        assert "eval_offset_past_close" in result
