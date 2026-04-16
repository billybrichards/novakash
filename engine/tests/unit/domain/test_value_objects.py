"""Unit tests for engine.domain.value_objects -- Phase 1 (CA-01, CA-02)."""
import math, pytest
from domain.value_objects import (ClobSnapshot, DeltaSet, FillResult, GateAuditRow, HeartbeatRow, ManualTradeOutcome, OrderBook, PendingTrade, PositionOutcome, ResolutionResult, RiskStatus, SignalEvaluation, SitrepPayload, SkipSummary, Tick, TradeDecision, WalletSnapshot, WindowClose, WindowKey, WindowMarket, WindowOutcome, WindowSnapshot)

class TestWindowKey:
    def test_happy(self): wk = WindowKey(asset="BTC", window_ts=1712345678); assert wk.key == "BTC-1712345678" and wk.timeframe == "5m"
    def test_15m(self): assert WindowKey(asset="BTC", window_ts=1, duration_secs=900).timeframe == "15m"
    def test_empty_asset(self):
        with pytest.raises(ValueError, match="asset"): WindowKey(asset="", window_ts=1)
    def test_zero_ts(self):
        with pytest.raises(ValueError, match="window_ts"): WindowKey(asset="BTC", window_ts=0)
    def test_neg_ts(self):
        with pytest.raises(ValueError, match="window_ts"): WindowKey(asset="BTC", window_ts=-1)
    def test_bad_dur(self):
        with pytest.raises(ValueError, match="duration_secs"): WindowKey(asset="BTC", window_ts=1, duration_secs=600)
    def test_frozen(self):
        with pytest.raises(AttributeError): WindowKey(asset="BTC", window_ts=1).asset = "ETH"
class TestTick:
    def test_happy(self): assert Tick(source="b", asset="B", price=97500.0, timestamp=1.0).price == 97500.0
    def test_empty_source(self):
        with pytest.raises(ValueError, match="source"): Tick(source="", asset="B", price=1.0, timestamp=1.0)
    def test_zero_price(self):
        with pytest.raises(ValueError, match="price must be positive"): Tick(source="b", asset="B", price=0.0, timestamp=1.0)
    def test_nan_price(self):
        with pytest.raises(ValueError, match="price must be finite"): Tick(source="b", asset="B", price=float("nan"), timestamp=1.0)
    def test_inf_price(self):
        with pytest.raises(ValueError, match="price must be finite"): Tick(source="b", asset="B", price=float("inf"), timestamp=1.0)
    def test_zero_ts(self):
        with pytest.raises(ValueError, match="timestamp must be positive"): Tick(source="b", asset="B", price=1.0, timestamp=0.0)
class TestWindowClose:
    def test_happy(self): assert WindowClose(asset="BTC", window_ts=1, duration_secs=300, open_price=97500.0, close_ts=1.0).window_key == WindowKey(asset="BTC", window_ts=1)
    def test_zero_open(self):
        with pytest.raises(ValueError, match="open_price"): WindowClose(asset="BTC", window_ts=1, duration_secs=300, open_price=0.0, close_ts=1.0)
    def test_bad_dur(self):
        with pytest.raises(ValueError, match="duration_secs"): WindowClose(asset="BTC", window_ts=1, duration_secs=60, open_price=1.0, close_ts=1.0)
class TestDeltaSet:
    def test_all_none(self): assert DeltaSet().available_count == 0 and DeltaSet().agreeing_sign is None
    def test_agree_up(self): assert DeltaSet(delta_chainlink=0.05, delta_tiingo=0.03).agreeing_sign == "UP"
    def test_agree_down(self): assert DeltaSet(delta_chainlink=-0.05, delta_binance=-0.02).agreeing_sign == "DOWN"
    def test_nan(self):
        with pytest.raises(ValueError, match="delta_chainlink must be finite"): DeltaSet(delta_chainlink=float("nan"))
class TestSignalEvaluation:
    def test_happy(self): assert SignalEvaluation(window_ts=1, asset="BTC", timeframe="5m", eval_offset=60).decision == "SKIP"
    def test_bad_tf(self):
        with pytest.raises(ValueError, match="timeframe"): SignalEvaluation(window_ts=1, asset="BTC", timeframe="1h", eval_offset=0)
    def test_neg_off(self):
        with pytest.raises(ValueError, match="eval_offset"): SignalEvaluation(window_ts=1, asset="BTC", timeframe="5m", eval_offset=-1)
    def test_bad_dec(self):
        with pytest.raises(ValueError, match="decision"): SignalEvaluation(window_ts=1, asset="BTC", timeframe="5m", eval_offset=0, decision="HOLD")
class TestClobSnapshot:
    def test_happy(self): assert ClobSnapshot(asset="BTC", timeframe="5m", window_ts=1).asset == "BTC"
    def test_bad_tf(self):
        with pytest.raises(ValueError, match="timeframe"): ClobSnapshot(asset="BTC", timeframe="1h", window_ts=1)
class TestGateAuditRow:
    def test_happy(self): assert GateAuditRow(window_ts=1, asset="BTC", timeframe="5m", eval_offset=60).decision == "SKIP"
    def test_bad_dec(self):
        with pytest.raises(ValueError, match="decision"): GateAuditRow(window_ts=1, asset="BTC", timeframe="5m", eval_offset=0, decision="X")
class TestWindowSnapshot:
    def test_happy(self): assert not WindowSnapshot(window_ts=1, asset="BTC", timeframe="5m").is_live
    def test_bad_tf(self):
        with pytest.raises(ValueError, match="timeframe"): WindowSnapshot(window_ts=1, asset="BTC", timeframe="1d")
class TestFillResult:
    def test_filled(self): assert FillResult(filled=True, order_id="x", fill_price=0.65, shares=10.0, attempts=1).filled
    def test_neg_price(self):
        with pytest.raises(ValueError, match="fill_price cannot be negative"): FillResult(filled=False, fill_price=-0.5)
    def test_neg_attempts(self):
        with pytest.raises(ValueError, match="attempts cannot be negative"): FillResult(filled=False, attempts=-1)
    def test_filled_no_oid(self):
        with pytest.raises(ValueError, match="filled result must have an order_id"): FillResult(filled=True, fill_price=0.65)
    def test_nan_price(self):
        with pytest.raises(ValueError, match="fill_price must be finite"): FillResult(filled=False, fill_price=float("nan"))
class TestWindowMarket:
    def test_has_tokens(self): assert WindowMarket(asset="BTC", window_ts=1, up_token_id="a", down_token_id="b").has_tokens
    def test_nan(self):
        with pytest.raises(ValueError, match="up_price must be finite"): WindowMarket(asset="BTC", window_ts=1, up_price=float("nan"))
class TestOrderBook:
    def test_happy(self): ob = OrderBook(token_id="x", bids=((0.64, 100.0),), asks=((0.66, 50.0),)); assert ob.spread == pytest.approx(0.02)
    def test_empty(self): assert OrderBook(token_id="x").spread is None
    def test_empty_token(self):
        with pytest.raises(ValueError, match="token_id"): OrderBook(token_id="")
class TestPendingTrade:
    def test_up(self): assert PendingTrade(trade_id=1, window_ts=1, asset="BTC", direction="UP", entry_price=0.65, stake_usd=4.0).clob_side == "YES"
    def test_bad_dir(self):
        with pytest.raises(ValueError, match="direction"): PendingTrade(trade_id=1, window_ts=1, asset="BTC", direction="X", entry_price=0.5, stake_usd=4.0)
    def test_price_high(self):
        with pytest.raises(ValueError, match="entry_price"): PendingTrade(trade_id=1, window_ts=1, asset="BTC", direction="UP", entry_price=1.5, stake_usd=4.0)
    def test_nan_price(self):
        with pytest.raises(ValueError, match="entry_price must be finite"): PendingTrade(trade_id=1, window_ts=1, asset="BTC", direction="UP", entry_price=float("nan"), stake_usd=4.0)
class TestTradeDecision:
    def test_happy(self): assert TradeDecision(window_ts=1, asset="BTC", timeframe="5m", direction="YES", eval_offset=60, entry_price=0.65, stake_usd=4.0).engine_version == "v10.0"
    def test_bad_dir(self):
        with pytest.raises(ValueError, match="direction"): TradeDecision(window_ts=1, asset="BTC", timeframe="5m", direction="UP", eval_offset=0, entry_price=0.5, stake_usd=4.0)
    def test_neg_off(self):
        with pytest.raises(ValueError, match="eval_offset"): TradeDecision(window_ts=1, asset="BTC", timeframe="5m", direction="YES", eval_offset=-5, entry_price=0.5, stake_usd=4.0)
class TestSkipSummary:
    def test_happy(self): assert SkipSummary(window_key="B-1", asset="BTC", window_ts=1, n_evals=19).n_evals == 19
    def test_empty_key(self):
        with pytest.raises(ValueError, match="window_key"): SkipSummary(window_key="", asset="BTC", window_ts=1, n_evals=0)
    def test_neg_evals(self):
        with pytest.raises(ValueError, match="n_evals"): SkipSummary(window_key="B-1", asset="BTC", window_ts=1, n_evals=-1)
class TestSitrepPayload:
    def _m(self, **kw):
        d = dict(engine_status="r", paper_mode=False, is_killed=False, wallet_balance=500.0, bankroll=500.0, starting_bankroll=500.0, daily_pnl=10.0, portfolio_value=510.0); d.update(kw); return SitrepPayload(**d)
    def test_wr(self): assert self._m(wins_today=5, losses_today=3).win_rate == pytest.approx(5/8)
    def test_nan(self):
        with pytest.raises(ValueError, match="wallet_balance"): self._m(wallet_balance=float("nan"))
    def test_neg_w(self):
        with pytest.raises(ValueError, match="wins_today"): self._m(wins_today=-1)
class TestWindowOutcome:
    def test_happy(self): assert WindowOutcome(window_ts=1, asset="BTC", outcome="WIN", pnl_usd=2.5, resolved_at=1.0).outcome == "WIN"
    def test_bad(self):
        with pytest.raises(ValueError, match="outcome"): WindowOutcome(window_ts=1, asset="BTC", outcome="DRAW", pnl_usd=0.0, resolved_at=1.0)
class TestManualTradeOutcome:
    def test_happy(self): assert ManualTradeOutcome(trade_id=1, status="open").status == "open"
    def test_bad(self):
        with pytest.raises(ValueError, match="invalid manual trade status"): ManualTradeOutcome(trade_id=1, status="unknown")
class TestRiskStatus:
    def test_happy(self): assert not RiskStatus(current_bankroll=500.0, peak_bankroll=550.0, drawdown_pct=0.09, daily_pnl=-5.0, consecutive_losses=1).is_killed
    def test_dd_high(self):
        with pytest.raises(ValueError, match="drawdown_pct"): RiskStatus(current_bankroll=500.0, peak_bankroll=550.0, drawdown_pct=1.5, daily_pnl=0.0, consecutive_losses=0)
    def test_nan_br(self):
        with pytest.raises(ValueError, match="current_bankroll"): RiskStatus(current_bankroll=float("nan"), peak_bankroll=550.0, drawdown_pct=0.0, daily_pnl=0.0, consecutive_losses=0)
class TestWalletSnapshot:
    def test_happy(self): assert WalletSnapshot(balance_usdc=500.0, timestamp=1.0).source == "polymarket_clob"
    def test_neg(self):
        with pytest.raises(ValueError, match="balance_usdc"): WalletSnapshot(balance_usdc=-1.0, timestamp=1.0)
    def test_empty_src(self):
        with pytest.raises(ValueError, match="source"): WalletSnapshot(balance_usdc=500.0, timestamp=1.0, source="")
class TestHeartbeatRow:
    def test_happy(self): assert HeartbeatRow(engine_status="running", active_positions=3).active_positions == 3
    def test_empty(self):
        with pytest.raises(ValueError, match="engine_status"): HeartbeatRow(engine_status="")
    def test_nan_bal(self):
        with pytest.raises(ValueError, match="current_balance"): HeartbeatRow(engine_status="r", current_balance=float("nan"))
class TestResolutionResult:
    def test_happy(self): assert ResolutionResult(condition_id="c", outcome="RESOLVED_WIN", pnl_usd=3.5).pnl_usd == 3.5
    def test_bad_outcome(self):
        with pytest.raises(ValueError, match="outcome"): ResolutionResult(condition_id="c", outcome="WIN", pnl_usd=3.5)
class TestPositionOutcome:
    def test_happy(self): assert PositionOutcome(condition_id="c", outcome="WIN", size=10.0, avg_price=0.65, cur_price=1.0, value=10.0, cost=6.5, pnl=3.5).pnl == 3.5
    def test_bad_out(self):
        with pytest.raises(ValueError, match="outcome"): PositionOutcome(condition_id="c", outcome="EXPIRED", size=10.0, avg_price=0.65, cur_price=1.0, value=10.0, cost=6.5, pnl=3.5)
    def test_neg_size(self):
        with pytest.raises(ValueError, match="size"): PositionOutcome(condition_id="c", outcome="WIN", size=-1.0, avg_price=0.65, cur_price=1.0, value=10.0, cost=6.5, pnl=3.5)
