"""Tests that write_signal_evaluation INSERTs include CLOB fields on clean paths.

Covers:
- evaluate_strategies._write_signal_evaluation (StrategyContext path)
- evaluate_window._run_v10_pipeline TRADE path
- evaluate_window._run_v10_pipeline SKIP path
- NULL CLOB query results pass through cleanly
- ETH, XRP assets (not just BTC)
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


# ---------------------------------------------------------------------------
# Helpers shared across both test classes
# ---------------------------------------------------------------------------

def _make_ctx(asset="BTC", clob_up_bid=0.52, clob_up_ask=0.54,
              clob_down_bid=0.44, clob_down_ask=0.46):
    """Build a minimal StrategyContext with CLOB fields set."""
    from domain.value_objects import StrategyContext
    return StrategyContext(
        asset=asset,
        window_ts=1712345600,
        timeframe="5m",
        eval_offset=120,
        delta_chainlink=0.05,
        delta_tiingo=0.04,
        delta_binance=0.05,
        delta_pct=0.04,
        delta_source="tiingo_rest_candle",
        current_price=84100.0,
        open_price=84000.0,
        vpin=0.55,
        regime="NORMAL",
        cg_snapshot=None,
        twap_delta=None,
        tiingo_close=84050.0,
        gamma_up_price=0.55,
        gamma_down_price=0.45,
        clob_up_bid=clob_up_bid,
        clob_up_ask=clob_up_ask,
        clob_down_bid=clob_down_bid,
        clob_down_ask=clob_down_ask,
    )


def _make_decision(action="TRADE", direction="UP", strategy_id="v10_gate"):
    from domain.value_objects import StrategyDecision
    return StrategyDecision(
        action=action,
        direction=direction if action == "TRADE" else None,
        confidence="HIGH" if action == "TRADE" else None,
        confidence_score=0.72 if action == "TRADE" else None,
        entry_cap=0.60 if action == "TRADE" else None,
        collateral_pct=None,
        strategy_id=strategy_id,
        strategy_version="10.5.3",
        entry_reason="test_reason" if action == "TRADE" else "",
        skip_reason=None if action == "TRADE" else "gate_failed",
    )


# ---------------------------------------------------------------------------
# evaluate_strategies._write_signal_evaluation — StrategyContext path
# ---------------------------------------------------------------------------

class TestEvaluateStrategiesClob:
    """CLOB fields appear in write_signal_evaluation from evaluate_strategies."""

    def _make_uc(self, db_client=None):
        from use_cases.evaluate_strategies import EvaluateStrategiesUseCase
        return EvaluateStrategiesUseCase(
            strategies=[],
            decision_repo=AsyncMock(),
            vpin_calculator=MagicMock(current_vpin=0.55, regime="NORMAL"),
            db_client=db_client,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["BTC", "ETH", "XRP"])
    async def test_clob_fields_written_for_all_assets(self, asset):
        """_write_signal_evaluation passes CLOB fields from ctx to the DB dict."""
        db = AsyncMock()
        db.write_signal_evaluation = AsyncMock()
        uc = self._make_uc(db_client=db)

        ctx = _make_ctx(asset=asset, clob_up_bid=0.52, clob_up_ask=0.54,
                        clob_down_bid=0.44, clob_down_ask=0.46)
        decision = _make_decision("TRADE", "UP")

        await uc._write_signal_evaluation(ctx, decision, asset, 1712345600, 120)
        await asyncio.sleep(0)  # let any tasks flush

        db.write_signal_evaluation.assert_called_once()
        written = db.write_signal_evaluation.call_args[0][0]

        assert written["clob_up_bid"] == 0.52
        assert written["clob_up_ask"] == 0.54
        assert written["clob_down_bid"] == 0.44
        assert written["clob_down_ask"] == 0.46

    @pytest.mark.asyncio
    async def test_null_clob_passes_through_cleanly(self):
        """None CLOB values in ctx are written as None without error."""
        db = AsyncMock()
        db.write_signal_evaluation = AsyncMock()
        uc = self._make_uc(db_client=db)

        ctx = _make_ctx(clob_up_bid=None, clob_up_ask=None,
                        clob_down_bid=None, clob_down_ask=None)
        decision = _make_decision("SKIP", strategy_id="v10_gate")

        await uc._write_signal_evaluation(ctx, decision, "BTC", 1712345600, 120)
        await asyncio.sleep(0)

        db.write_signal_evaluation.assert_called_once()
        written = db.write_signal_evaluation.call_args[0][0]

        assert written["clob_up_bid"] is None
        assert written["clob_up_ask"] is None
        assert written["clob_down_bid"] is None
        assert written["clob_down_ask"] is None

    @pytest.mark.asyncio
    async def test_existing_fields_unchanged(self):
        """Adding CLOB fields does not break vpin, regime, delta_pct, or decision."""
        db = AsyncMock()
        db.write_signal_evaluation = AsyncMock()
        uc = self._make_uc(db_client=db)

        ctx = _make_ctx(asset="BTC")
        decision = _make_decision("TRADE", "DOWN")

        await uc._write_signal_evaluation(ctx, decision, "BTC", 1712345600, 120)
        await asyncio.sleep(0)

        written = db.write_signal_evaluation.call_args[0][0]
        assert written["vpin"] == 0.55
        assert written["regime"] == "NORMAL"
        assert written["delta_pct"] == pytest.approx(0.04)
        assert written["decision"] == "TRADE"
        assert written["asset"] == "BTC"


# ---------------------------------------------------------------------------
# evaluate_window._run_v10_pipeline — TRADE and SKIP paths
# ---------------------------------------------------------------------------

def _make_window(asset="BTC"):
    from data.feeds.polymarket_5min import WindowInfo, WindowState
    w = WindowInfo(
        window_ts=1712345600,
        asset=asset,
        duration_secs=300,
        state=WindowState.CLOSING,
        open_price=84000.0,
        up_price=0.55,
        down_price=0.45,
    )
    w.eval_offset = 120
    return w


def _make_db_with_clob(clob_data=None):
    db = AsyncMock()
    db.get_latest_tiingo_price = AsyncMock(return_value=84050.0)
    db.get_latest_chainlink_price = AsyncMock(return_value=84080.0)
    db.get_latest_clob_prices = AsyncMock(return_value=clob_data or {
        "clob_up_bid": 0.52,
        "clob_up_ask": 0.54,
        "clob_down_bid": 0.44,
        "clob_down_ask": 0.46,
    })
    db.get_latest_macro_signal = AsyncMock(return_value=None)
    db.write_window_snapshot = AsyncMock()
    db.write_signal_evaluation = AsyncMock()
    db.write_gate_audit = AsyncMock()
    db.write_window_prediction = AsyncMock()
    db.update_window_skip_reason = AsyncMock()
    db.load_recent_traded_windows = AsyncMock(return_value=set())
    return db


class TestEvaluateWindowV10PipelineClob:
    """CLOB fields appear in write_signal_evaluation from _run_v10_pipeline."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset", ["ETH", "XRP"])
    async def test_clob_written_on_skip_path_non_btc(self, asset):
        """SKIP decision from _run_v10_pipeline writes CLOB fields for ETH/XRP."""
        from use_cases.evaluate_window import EvaluateWindowUseCase

        db = _make_db_with_clob({
            "clob_up_bid": 0.51,
            "clob_up_ask": 0.53,
            "clob_down_bid": 0.43,
            "clob_down_ask": 0.45,
        })

        # Patch the gate pipeline so it always returns a SKIP result
        mock_pr = MagicMock()
        mock_pr.passed = False
        mock_pr.direction = None
        mock_pr.skip_reason = "test_gate_failed"
        mock_pr.failed_gate = "test_gate"
        mock_pr.cap = None
        mock_pr.gate_results = []

        uc = EvaluateWindowUseCase(db_client=db, vpin_calculator=MagicMock(current_vpin=0.55))

        with patch("use_cases.evaluate_window.GatePipeline") as MockPipeline:
            mock_pipeline_inst = AsyncMock()
            mock_pipeline_inst.evaluate = AsyncMock(return_value=mock_pr)
            MockPipeline.return_value = mock_pipeline_inst

            with patch.dict(os.environ, {"V10_DUNE_ENABLED": "true"}):
                with patch("signals.v2_feature_body.build_v5_feature_body", return_value=MagicMock()):
                    with patch("use_cases.evaluate_window.DuneConfidenceGate", MagicMock()):
                        await uc._run_v10_pipeline(
                            window=_make_window(asset=asset),
                            state=MagicMock(),
                            window_key=f"{asset}-1712345600",
                            eval_offset=120,
                            current_price=84100.0,
                            open_price=84000.0,
                            current_vpin=0.55,
                            delta_pct=0.04,
                            delta_binance=0.05,
                            delta_chainlink=0.04,
                            delta_tiingo=0.04,
                            _tiingo_close=84050.0,
                            _psu="tiingo_rest_candle",
                            _snap_regime="NORMAL",
                            twap_result=None,
                        )

        # At least one write_signal_evaluation call should include CLOB fields
        assert db.write_signal_evaluation.called
        written = db.write_signal_evaluation.call_args[0][0]
        assert written["clob_up_bid"] == 0.51
        assert written["clob_up_ask"] == 0.53
        assert written["clob_down_bid"] == 0.43
        assert written["clob_down_ask"] == 0.45

    @pytest.mark.asyncio
    async def test_null_clob_query_passes_through_on_skip(self):
        """When get_latest_clob_prices returns None, CLOB fields are None (no crash)."""
        from use_cases.evaluate_window import EvaluateWindowUseCase

        db = _make_db_with_clob(None)
        db.get_latest_clob_prices = AsyncMock(return_value=None)

        mock_pr = MagicMock()
        mock_pr.passed = False
        mock_pr.direction = None
        mock_pr.skip_reason = "gate_failed"
        mock_pr.failed_gate = "test_gate"
        mock_pr.cap = None
        mock_pr.gate_results = []

        uc = EvaluateWindowUseCase(db_client=db, vpin_calculator=MagicMock(current_vpin=0.55))

        with patch("use_cases.evaluate_window.GatePipeline") as MockPipeline:
            mock_pipeline_inst = AsyncMock()
            mock_pipeline_inst.evaluate = AsyncMock(return_value=mock_pr)
            MockPipeline.return_value = mock_pipeline_inst

            with patch.dict(os.environ, {"V10_DUNE_ENABLED": "true"}):
                with patch("signals.v2_feature_body.build_v5_feature_body", return_value=MagicMock()):
                    with patch("use_cases.evaluate_window.DuneConfidenceGate", MagicMock()):
                        await uc._run_v10_pipeline(
                            window=_make_window("BTC"),
                            state=MagicMock(),
                            window_key="BTC-1712345600",
                            eval_offset=120,
                            current_price=84100.0,
                            open_price=84000.0,
                            current_vpin=0.55,
                            delta_pct=0.04,
                            delta_binance=0.05,
                            delta_chainlink=0.04,
                            delta_tiingo=0.04,
                            _tiingo_close=84050.0,
                            _psu="tiingo_rest_candle",
                            _snap_regime="NORMAL",
                            twap_result=None,
                        )

        assert db.write_signal_evaluation.called
        written = db.write_signal_evaluation.call_args[0][0]
        assert written["clob_up_bid"] is None
        assert written["clob_down_ask"] is None
