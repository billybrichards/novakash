"""Unit tests for EvaluateWindowUseCase (Phase 3)."""
from __future__ import annotations
import asyncio, os, sys, time
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

_engine = os.path.join(os.path.dirname(__file__), "..", "..", "..")
if _engine not in sys.path:
    sys.path.insert(0, _engine)

def _make_window(asset="BTC", window_ts=1712345600, duration_secs=300,
        open_price=84000.0, up_price=0.55, down_price=0.45, eval_offset=60):
    from data.feeds.polymarket_5min import WindowInfo, WindowState
    w = WindowInfo(window_ts=window_ts, asset=asset, duration_secs=duration_secs,
        state=WindowState.CLOSING, open_price=open_price, up_price=up_price, down_price=down_price)
    w.eval_offset = eval_offset
    return w

def _make_state(btc_price=84100.0):
    from data.models import MarketState
    return MarketState(btc_price=Decimal(str(btc_price)))

def _make_db():
    db = AsyncMock()
    db.get_latest_tiingo_price = AsyncMock(return_value=84050.0)
    db.get_latest_chainlink_price = AsyncMock(return_value=84080.0)
    db.get_latest_clob_prices = AsyncMock(return_value={"clob_up_bid": 0.52, "clob_up_ask": 0.54, "clob_down_bid": 0.44, "clob_down_ask": 0.46})
    db.get_latest_macro_signal = AsyncMock(return_value={"macro_bias": "NEUTRAL", "macro_confidence": "0.5", "macro_gate": "", "macro_reasoning": ""})
    db.write_window_snapshot = AsyncMock()
    db.write_signal_evaluation = AsyncMock()
    db.write_gate_audit = AsyncMock()  # retired no-op — kept to avoid AttributeError in legacy call sites
    db.write_window_prediction = AsyncMock()
    db.update_window_skip_reason = AsyncMock()
    db.load_recent_traded_windows = AsyncMock(return_value=set())
    return db

def _make_vpin(vpin=0.55):
    m = MagicMock()
    m.current_vpin = vpin
    return m

def _make_uc(**kw):
    from use_cases.evaluate_window import EvaluateWindowUseCase
    defaults = {"db_client": _make_db(), "vpin_calculator": _make_vpin()}
    defaults.update(kw)
    return EvaluateWindowUseCase(**defaults)

class TestEvaluateWindowUseCase:
    @pytest.mark.asyncio
    async def test_already_traded_returns_skip(self):
        uc = _make_uc()
        uc._traded_windows.add("BTC-1712345600")
        result = await uc.execute(_make_window(), _make_state())
        assert result.signal is None
        assert result.skip_reason == "already_traded"

    @pytest.mark.asyncio
    async def test_no_current_price_returns_skip(self):
        uc = _make_uc(fetch_current_price_fn=AsyncMock(return_value=None))
        result = await uc.execute(_make_window(asset="ETH"), _make_state())
        assert result.signal is None
        assert result.skip_reason == "no_current_price"

    @pytest.mark.asyncio
    async def test_no_open_price_returns_skip(self):
        uc = _make_uc()
        result = await uc.execute(_make_window(open_price=None), _make_state())
        assert result.signal is None
        assert result.skip_reason == "no_open_price"

    @pytest.mark.asyncio
    async def test_mark_traded_and_was_traded(self):
        uc = _make_uc()
        assert not uc.was_traded("BTC-1712345600")
        uc.mark_traded("BTC-1712345600")
        assert uc.was_traded("BTC-1712345600")

    @pytest.mark.asyncio
    async def test_load_traded_windows_from_db(self):
        db = _make_db()
        db.load_recent_traded_windows = AsyncMock(return_value={"BTC-111", "BTC-222"})
        uc = _make_uc(db_client=db)
        await uc.load_traded_windows(hours=2)
        assert uc.was_traded("BTC-111")
        assert uc.was_traded("BTC-222")
        assert not uc.was_traded("BTC-333")

    def test_v10_pipeline_passes_without_dune_client(self):
        # Without a DUNE client, DuneConfidenceGate passes by default.
        # The pipeline passes all 8 gates and returns a TRADE signal.
        with patch.dict(os.environ, {"V10_DUNE_ENABLED": "true"}):
            uc = _make_uc()
            result = asyncio.get_event_loop().run_until_complete(uc.execute(_make_window(), _make_state()))
            assert result.signal is not None
            assert result.signal.direction is not None

    def test_v10_trade_with_mock_pipeline(self):
        mock_pr = MagicMock()
        mock_pr.passed = True
        mock_pr.direction = "UP"
        mock_pr.cap = 0.55
        mock_pr.dune_p = 0.72
        mock_pr.gate_results = []
        mock_pr.failed_gate = None
        mock_pr.skip_reason = None
        with patch.dict(os.environ, {"V10_DUNE_ENABLED": "true"}):
            uc = _make_uc()
            with patch("use_cases.evaluate_window.GatePipeline.evaluate", return_value=mock_pr):
                result = asyncio.get_event_loop().run_until_complete(uc.execute(_make_window(), _make_state()))
        assert result.signal is not None
        assert result.signal.direction == "UP"

    def test_cleanup_stale_history(self):
        uc = _make_uc()
        old_ts = int(time.time()) - 1800
        uc._window_eval_history[f"BTC-{old_ts}"] = [{"offset": 60}]
        fresh_ts = int(time.time()) - 60
        uc._window_eval_history[f"BTC-{fresh_ts}"] = [{"offset": 60}]
        uc._cleanup_stale_history()
        assert f"BTC-{old_ts}" not in uc._window_eval_history
        assert f"BTC-{fresh_ts}" in uc._window_eval_history

    def test_append_skip_history(self):
        uc = _make_uc()
        uc._append_skip_history(window_key="BTC-1712345600", eval_offset=60,
            _skip_reason="test", current_vpin=0.55, delta_pct=0.03,
            window_snapshot={}, _snap_regime="TRANSITION",
            delta_chainlink=0.04, delta_tiingo=0.03)
        assert "BTC-1712345600" in uc._window_eval_history
        assert uc._window_eval_history["BTC-1712345600"][0]["skip_reason"] == "test"

    def test_extract_v10_gate_data(self):
        from signals.gates import GateResult, PipelineResult
        uc = _make_uc()
        pr = PipelineResult(passed=True, direction="UP", cap=0.55, dune_p=0.72,
            gate_results=[
                GateResult(passed=True, gate_name="dune_confidence", data={"dune_p": 0.72, "threshold": 0.65}),
                GateResult(passed=True, gate_name="taker_flow", data={"taker_aligned": True, "buy_pct": 65}),
                GateResult(passed=True, gate_name="cg_confirmation", data={"confirms": 2}),
                GateResult(passed=True, gate_name="spread_gate", data={"spread_pct": 0.03})])
        d = uc._extract_v10_gate_data(pr, "CASCADE")
        assert d["v103_dune_p"] == 0.72
        assert d["v103_taker_status"] == "aligned"
        assert d["v103_cg_confirms"] == 2
        assert len(d["v103_gate_results"]) == 4

    def test_compute_gates_decisive(self):
        uc = _make_uc()
        ws = {"gamma_up_price": 0.55, "gamma_down_price": 0.45}
        uc._compute_gates(ws, 0.70, 0.06, None, 0.05, 0.04, 0.06)
        assert ws["confidence_tier"] == "DECISIVE"
        assert "vpin" in ws["gates_passed"]

    def test_compute_gates_floor_fail(self):
        uc = _make_uc()
        ws = {"gamma_up_price": 0.25, "gamma_down_price": 0.75}
        uc._compute_gates(ws, 0.55, 0.10, None, 0.10, 0.09, 0.10)
        assert ws["_floor_passed"] is False
        assert ws["gate_failed"] == "floor"

class TestFeatureFlagDelegation:
    def test_constructor_accepts_evaluate_use_case(self):
        from strategies.five_min_vpin import FiveMinVPINStrategy
        import inspect
        sig = inspect.signature(FiveMinVPINStrategy.__init__)
        assert "evaluate_use_case" in sig.parameters
