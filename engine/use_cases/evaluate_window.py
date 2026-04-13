"""EvaluateWindowUseCase -- Phase 3 extraction from five_min_vpin.py.

Feature flag: ENGINE_USE_CLEAN_EVALUATE_WINDOW=true activates this path.
Default is false (off) -- the legacy path in five_min_vpin.py runs unchanged.
Audit IDs: CA-01, CA-02, CA-03.
"""

from __future__ import annotations
import asyncio, json, os, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import structlog
from config.constants import FIVE_MIN_EVAL_OFFSETS
from config.runtime_config import runtime
from data.feeds.polymarket_5min import WindowInfo
from data.models import MarketState
from signals.gates import (
    GateContext,
    GatePipeline,
    PipelineResult,
    EvalOffsetBoundsGate,
    SourceAgreementGate,
    DeltaMagnitudeGate,
    TakerFlowGate,
    CGConfirmationGate,
    DuneConfidenceGate,
    SpreadGate,
    DynamicCapGate,
)

log = structlog.get_logger(__name__)

_CAP_T240 = float(os.environ.get("V81_CAP_T240", "0.55"))
_CAP_T180 = float(os.environ.get("V81_CAP_T180", "0.60"))
_CAP_T120 = float(os.environ.get("V81_CAP_T120", "0.65"))
_CAP_T60 = float(os.environ.get("V81_CAP_T60", "0.73"))


def _get_v81_cap(offset: int) -> float:
    if offset >= 180:
        return _CAP_T240
    if offset >= 120:
        return _CAP_T180
    if offset >= 80:
        return _CAP_T120
    return _CAP_T60


@dataclass
class FiveMinSignal:
    window: object  # WindowInfo
    current_price: float
    current_vpin: float
    delta_pct: float
    confidence: str
    direction: str
    cg_modifier: float = 0.0
    entry_reason: str = "v8_standard"
    v81_entry_cap: float = 0.73
    _v10_gate_data: dict = field(default_factory=dict, repr=False)


@dataclass
class EvaluateWindowResult:
    signal: Optional[FiveMinSignal]
    window_snapshot: dict
    skip_reason: Optional[str] = None


class EvaluateWindowUseCase:
    def __init__(
        self,
        *,
        db_client=None,
        alerter=None,
        cg_enhanced=None,
        cg_feeds=None,
        timesfm_client=None,
        timesfm_v2=None,
        twap_tracker=None,
        tick_recorder=None,
        vpin_calculator=None,
        claude_evaluator=None,
        fetch_current_price_fn=None,
        fetch_fresh_gamma_fn=None,
    ):
        self._db = db_client
        self._alerter = alerter
        self._cg_enhanced = cg_enhanced
        self._cg_feeds = cg_feeds or {}
        self._timesfm = timesfm_client
        self._timesfm_v2 = timesfm_v2
        self._twap = twap_tracker
        self._tick_recorder = tick_recorder
        self._vpin = vpin_calculator
        self._claude_eval = claude_evaluator
        self._fetch_current_price = fetch_current_price_fn
        self._fetch_fresh_gamma_price = fetch_fresh_gamma_fn
        self._traded_windows: set[str] = set()
        self._last_executed_window: Optional[str] = None
        self._window_eval_history: dict[str, list] = {}
        self._last_skip_reason: str = ""
        self._log = log.bind(component="evaluate_window_uc")

    async def load_traded_windows(self, hours: int = 2) -> None:
        if self._db:
            try:
                self._traded_windows = await self._db.load_recent_traded_windows(
                    hours=hours
                )
            except Exception:
                pass

    def mark_traded(self, window_key: str) -> None:
        self._traded_windows.add(window_key)
        self._last_executed_window = window_key

    def was_traded(self, window_key: str) -> bool:
        return window_key in self._traded_windows

    async def execute(
        self, window: WindowInfo, state: MarketState
    ) -> EvaluateWindowResult:
        window_key = f"{window.asset}-{window.window_ts}"
        eval_offset = getattr(window, "eval_offset", None)
        if window_key in self._traded_windows:
            return EvaluateWindowResult(
                signal=None, window_snapshot={}, skip_reason="already_traded"
            )
        if window.asset == "BTC":
            current_price = float(state.btc_price) if state.btc_price else None
        else:
            current_price = (
                await self._fetch_current_price(window.asset)
                if self._fetch_current_price
                else None
            )
        if current_price is None:
            return EvaluateWindowResult(
                signal=None, window_snapshot={}, skip_reason="no_current_price"
            )
        open_price = window.open_price
        if open_price is None:
            return EvaluateWindowResult(
                signal=None, window_snapshot={}, skip_reason="no_open_price"
            )
        delta_binance = (current_price - open_price) / open_price * 100
        _tiingo_open = _tiingo_close = delta_tiingo = None
        _tiingo_candle_source = "none"
        _tiingo_api_key = os.environ.get(
            "TIINGO_API_KEY", "3f4456e457a4184d76c58a1320d8e1b214c3ab16"
        )
        try:
            import aiohttp

            _ts_s = datetime.fromtimestamp(window.window_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            _ts_e = datetime.fromtimestamp(
                window.window_ts + 300, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (
                f"https://api.tiingo.com/tiingo/crypto/prices?tickers={window.asset.lower()}usd"
                f"&startDate={_ts_s}&endDate={_ts_e}&resampleFreq=5min&token={_tiingo_api_key}"
            )
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as r:
                    if r.status == 200:
                        d = await r.json()
                        if d and isinstance(d, list) and len(d) > 0:
                            pd = d[0].get("priceData", [])
                            if pd:
                                _tiingo_open = float(pd[0].get("open", 0) or 0) or None
                                _tiingo_close = (
                                    float(pd[-1].get("close", 0) or 0) or None
                                )
                                if _tiingo_open and _tiingo_close and _tiingo_open > 0:
                                    delta_tiingo = (
                                        (_tiingo_close - _tiingo_open)
                                        / _tiingo_open
                                        * 100
                                    )
                                    _tiingo_candle_source = "rest_candle"
        except Exception:
            pass
        if delta_tiingo is None and self._db:
            try:
                p = await self._db.get_latest_tiingo_price(window.asset)
                if p:
                    _tiingo_open = open_price
                    _tiingo_close = p
                    delta_tiingo = (p - open_price) / open_price * 100
                    _tiingo_candle_source = "db_tick"
            except Exception:
                pass
        _chainlink_price = None
        if self._db:
            try:
                _chainlink_price = await self._db.get_latest_chainlink_price(
                    window.asset
                )
            except Exception:
                pass
        delta_chainlink = (
            ((_chainlink_price - open_price) / open_price * 100)
            if _chainlink_price
            else None
        )
        _dirs = []
        if delta_binance is not None:
            _dirs.append("UP" if delta_binance > 0 else "DOWN")
        if delta_chainlink is not None:
            _dirs.append("UP" if delta_chainlink > 0 else "DOWN")
        if delta_tiingo is not None:
            _dirs.append("UP" if delta_tiingo > 0 else "DOWN")
        if len(_dirs) >= 2:
            _up = _dirs.count("UP")
            _dn = _dirs.count("DOWN")
            if _up == len(_dirs) or _dn == len(_dirs):
                price_consensus = "AGREE"
            elif abs(_up - _dn) >= 1 and len(_dirs) >= 3:
                price_consensus = "MIXED"
            elif len(_dirs) == 2 and _up != _dn:
                price_consensus = "MIXED"
            else:
                price_consensus = "DISAGREE"
        else:
            price_consensus = "AGREE"
        _delta_source = runtime.delta_price_source
        if _delta_source == "tiingo" and delta_tiingo is not None:
            delta_pct = delta_tiingo
            _psu = f"tiingo_{_tiingo_candle_source}"
        elif _delta_source == "tiingo" and delta_chainlink is not None:
            delta_pct = delta_chainlink
            _psu = "chainlink_fallback"
        elif _delta_source == "chainlink" and delta_chainlink is not None:
            delta_pct = delta_chainlink
            _psu = "chainlink"
        elif _delta_source == "consensus":
            if price_consensus != "AGREE":
                return EvaluateWindowResult(
                    signal=None, window_snapshot={}, skip_reason="consensus_disagree"
                )
            delta_pct = (
                delta_tiingo
                if delta_tiingo is not None
                else (delta_chainlink if delta_chainlink is not None else delta_binance)
            )
            _psu = "consensus"
        else:
            if delta_tiingo is not None:
                delta_pct = delta_tiingo
                _psu = f"tiingo_{_tiingo_candle_source}"
            elif delta_chainlink is not None:
                delta_pct = delta_chainlink
                _psu = "chainlink"
            else:
                delta_pct = delta_binance
                _psu = "binance"
        current_vpin = self._vpin.current_vpin if self._vpin else 0.0
        twap_result = None
        if self._twap:
            twap_result = self._twap.evaluate(
                asset=window.asset,
                window_ts=window.window_ts,
                current_price=current_price,
                gamma_up_price=window.up_price,
                gamma_down_price=window.down_price,
            )
            self._twap.cleanup_window(window.asset, window.window_ts)
        if current_vpin >= runtime.vpin_cascade_direction_threshold:
            _snap_regime = "CASCADE"
        elif current_vpin >= runtime.vpin_informed_threshold:
            _snap_regime = "TRANSITION"
        elif current_vpin >= 0.45:
            _snap_regime = "NORMAL"
        else:
            _snap_regime = "CALM"
        _v10 = os.environ.get("V10_DUNE_ENABLED", "false").lower() == "true"
        signal = None
        if _v10:
            signal = await self._run_v10_pipeline(
                window=window,
                state=state,
                window_key=window_key,
                eval_offset=eval_offset,
                current_price=current_price,
                open_price=open_price,
                current_vpin=current_vpin,
                delta_pct=delta_pct,
                delta_binance=delta_binance,
                delta_chainlink=delta_chainlink,
                delta_tiingo=delta_tiingo,
                _tiingo_close=_tiingo_close,
                _psu=_psu,
                _snap_regime=_snap_regime,
                twap_result=twap_result,
            )
            if signal is not None:
                return EvaluateWindowResult(
                    signal=signal, window_snapshot={}, skip_reason=None
                )
        tf = "15m" if window.duration_secs == 900 else "5m"
        cg = None
        if self._cg_feeds:
            af = self._cg_feeds.get(window.asset, self._cg_feeds.get("BTC"))
            if af and af.connected:
                cg = af.snapshot
        elif self._cg_enhanced and self._cg_enhanced.connected:
            cg = self._cg_enhanced.snapshot
        ws = self._build_snapshot(
            window,
            tf,
            open_price,
            current_price,
            delta_pct,
            delta_chainlink,
            delta_tiingo,
            delta_binance,
            price_consensus,
            current_vpin,
            _snap_regime,
            cg,
            signal,
            twap_result,
            _tiingo_open,
            _tiingo_close,
            _psu,
        )
        self._compute_gates(
            ws,
            current_vpin,
            delta_pct,
            signal,
            delta_tiingo,
            delta_binance,
            delta_chainlink,
        )
        if self._fetch_fresh_gamma_price:
            try:
                u, d, _ = await self._fetch_fresh_gamma_price(
                    f"btc-updown-5m-{window.window_ts}"
                )
                if u is not None:
                    ws["gamma_up_price"] = u
                    ws["gamma_down_price"] = d
            except Exception:
                pass
        if _chainlink_price:
            ws["chainlink_open"] = _chainlink_price
        if _tiingo_open:
            ws["tiingo_open"] = _tiingo_open
        if self._db:
            try:
                c = await self._db.get_latest_clob_prices(window.asset)
                if c:
                    for k in (
                        "clob_up_bid",
                        "clob_up_ask",
                        "clob_down_bid",
                        "clob_down_ask",
                    ):
                        ws[k] = c.get(k)
            except Exception:
                pass
            try:
                m = await self._db.get_latest_macro_signal()
                if m:
                    for k in ("macro_bias", "macro_confidence"):
                        ws[k] = m.get(k, "")
            except Exception:
                pass
            try:
                await self._db.write_window_snapshot(ws)
            except Exception:
                pass
        if signal is None:
            _skip = (
                self._last_skip_reason
                or f"No signal (VPIN {current_vpin:.3f}, delta {delta_pct:+.4f}%)"
            )
            self._last_skip_reason = ""
            ws["skip_reason"] = _skip
            self._append_skip_history(
                window_key=window_key,
                eval_offset=eval_offset,
                _skip_reason=_skip,
                current_vpin=current_vpin,
                delta_pct=delta_pct,
                window_snapshot=ws,
                _snap_regime=_snap_regime,
                delta_chainlink=delta_chainlink,
                delta_tiingo=delta_tiingo,
            )
            self._cleanup_stale_history()
            return EvaluateWindowResult(
                signal=None, window_snapshot=ws, skip_reason=_skip
            )
        if not ws.get("_cap_passed", True) or not ws.get("_floor_passed", True):
            self._last_skip_reason = "price gate"
            signal = None
        if signal is not None and self._db:
            try:
                c = await self._db.get_latest_clob_prices(window.asset)
                if c:
                    _ask = (
                        c.get("clob_up_ask")
                        if signal.direction == "UP"
                        else c.get("clob_down_ask")
                    )
                    _cap = _get_v81_cap(eval_offset) if eval_offset else 0.73
                    if runtime.fok_enabled:
                        if _ask and _ask < 0.30:
                            self._last_skip_reason = f"CLOB FLOOR"
                            signal = None
                    else:
                        if _ask and _ask > _cap:
                            self._last_skip_reason = f"CLOB CAP"
                            signal = None
                        elif _ask and _ask < 0.30:
                            self._last_skip_reason = f"CLOB FLOOR"
                            signal = None
            except Exception:
                pass
        if signal is None:
            _skip = self._last_skip_reason or "CLOB price gate"
            self._last_skip_reason = ""
            self._cleanup_stale_history()
            return EvaluateWindowResult(
                signal=None, window_snapshot=ws, skip_reason=_skip
            )
        self._traded_windows.add(window_key)
        self._last_executed_window = window_key
        return EvaluateWindowResult(signal=signal, window_snapshot=ws, skip_reason=None)

    async def _run_v10_pipeline(
        self,
        *,
        window,
        state,
        window_key,
        eval_offset,
        current_price,
        open_price,
        current_vpin,
        delta_pct,
        delta_binance,
        delta_chainlink,
        delta_tiingo,
        _tiingo_close,
        _psu,
        _snap_regime,
        twap_result,
    ):
        if window_key in self._traded_windows:
            return None
        from signals.v2_feature_body import build_v5_feature_body

        _cg = self._cg_enhanced.snapshot if self._cg_enhanced is not None else None
        _twap_d = twap_result.twap_delta_pct if twap_result is not None else None
        _v5 = build_v5_feature_body(
            eval_offset=eval_offset,
            vpin=current_vpin,
            delta_pct=delta_pct,
            twap_delta=_twap_d,
            binance_price=current_price,
            chainlink_price=None,
            tiingo_close=_tiingo_close,
            delta_binance=delta_binance,
            delta_chainlink=delta_chainlink,
            delta_tiingo=delta_tiingo,
            regime=_snap_regime,
            delta_source=_psu,
        )
        ctx = GateContext(
            delta_chainlink=delta_chainlink,
            delta_tiingo=delta_tiingo,
            delta_binance=delta_binance,
            delta_pct=delta_pct,
            vpin=current_vpin,
            regime=_snap_regime,
            asset=window.asset,
            eval_offset=eval_offset,
            window_ts=window.window_ts,
            cg_snapshot=_cg,
            twap_delta=_twap_d,
            tiingo_close=_tiingo_close,
            current_price=current_price,
            delta_source=_psu,
            prev_v2_probability_up=None,
            v5_features=_v5,
        )
        pipeline = GatePipeline(
            [
                EvalOffsetBoundsGate(),
                SourceAgreementGate(),
                DeltaMagnitudeGate(),
                TakerFlowGate(),
                CGConfirmationGate(),
                DuneConfidenceGate(dune_client=self._timesfm_v2),
                SpreadGate(),
                DynamicCapGate(),
            ]
        )
        pr = await pipeline.evaluate(ctx)
        if pr.passed:
            direction = pr.direction
            confidence = (
                "HIGH"
                if (
                    ctx.dune_probability_up
                    and max(ctx.dune_probability_up, 1 - ctx.dune_probability_up) > 0.75
                )
                else "MODERATE"
            )
            _ot = os.environ.get("ORDER_TYPE", "FAK").upper()
            _cgt = ""
            if ctx.cg_threshold_modifier != 0:
                _cgt = f"_CG{ctx.cg_threshold_modifier:+.02f}"
            elif ctx.cg_bonus > 0:
                _cgt = f"_CGB{ctx.cg_bonus:.02f}"
            signal = FiveMinSignal(
                window=window,
                current_price=current_price,
                current_vpin=current_vpin,
                delta_pct=delta_pct,
                confidence=confidence,
                direction=direction,
                cg_modifier=ctx.cg_threshold_modifier,
                entry_reason=f"v10_DUNE_{_snap_regime}_T{ctx.eval_offset}_{_ot}{_cgt}",
                v81_entry_cap=pr.cap or 0.65,
            )
            self._traded_windows.add(window_key)
            self._last_executed_window = window_key
            signal._v10_gate_data = self._extract_v10_gate_data(pr, _snap_regime)
            if self._db:
                try:
                    await self._db.write_signal_evaluation(
                        {
                            "window_ts": window.window_ts,
                            "asset": window.asset,
                            "timeframe": "5m",
                            "eval_offset": ctx.eval_offset,
                            "delta_pct": delta_pct,
                            "vpin": current_vpin,
                            "regime": _snap_regime,
                            "decision": "TRADE",
                            "gate_passed": True,
                            "gate_failed": None,
                            "v2_probability_up": ctx.dune_probability_up,
                            "v2_direction": direction,
                        }
                    )
                except Exception:
                    pass
                try:
                    await self._db.write_window_snapshot(
                        {
                            "window_ts": window.window_ts,
                            "asset": window.asset,
                            "timeframe": "5m",
                            "open_price": open_price,
                            "close_price": current_price,
                            "delta_pct": delta_pct,
                            "vpin": current_vpin,
                            "regime": _snap_regime,
                            "direction": direction,
                            "confidence": confidence,
                            "trade_placed": True,
                            "engine_version": "v10.3",
                            "eval_offset": ctx.eval_offset,
                            "v2_probability_up": ctx.dune_probability_up,
                            "v2_direction": direction,
                        }
                    )
                except Exception:
                    pass
                try:
                    asyncio.create_task(
                        self._db.write_gate_audit(
                            {
                                "window_ts": window.window_ts,
                                "asset": window.asset,
                                "timeframe": "5m",
                                "engine_version": "v10.3",
                                "direction": direction,
                                "delta_pct": delta_pct,
                                "vpin": current_vpin,
                                "regime": _snap_regime,
                                "gate_passed": True,
                                "decision": "TRADE",
                                "eval_offset": ctx.eval_offset,
                            }
                        )
                    )
                except Exception:
                    pass
            return signal
        else:
            self._last_skip_reason = pr.skip_reason or "v10 gate failed"
            if self._db:
                try:
                    await self._db.write_signal_evaluation(
                        {
                            "window_ts": window.window_ts,
                            "asset": window.asset,
                            "timeframe": "5m",
                            "eval_offset": eval_offset,
                            "delta_pct": delta_pct,
                            "vpin": current_vpin,
                            "regime": _snap_regime,
                            "decision": "SKIP",
                            "gate_passed": False,
                            "gate_failed": pr.failed_gate or "unknown",
                            "v2_probability_up": ctx.dune_probability_up,
                        }
                    )
                except Exception:
                    pass
            return None

    def _extract_v10_gate_data(self, pr, regime):
        d = {}
        for g in pr.gate_results:
            if g.gate_name == "dune_confidence":
                d["v103_dune_p"] = g.data.get("dune_p")
                d["v103_threshold"] = g.data.get("threshold")
            elif g.gate_name == "taker_flow":
                d["v103_taker_status"] = (
                    "both_opposing"
                    if g.data.get("taker_opposing") and g.data.get("smart_opposing")
                    else "opposing"
                    if g.data.get("taker_opposing")
                    else "aligned"
                    if g.data.get("taker_aligned")
                    else "neutral"
                )
                d["v103_taker_buy_pct"] = g.data.get("buy_pct", 50)
            elif g.gate_name == "cg_confirmation":
                d["v103_cg_confirms"] = g.data.get("confirms", 0)
            elif g.gate_name == "spread_gate":
                d["v103_spread_pct"] = g.data.get("spread_pct")
        d["v103_gate_results"] = [
            {"name": g.gate_name, "passed": g.passed, "reason": g.reason[:60]}
            for g in pr.gate_results
        ]
        return d

    def _build_snapshot(
        self,
        window,
        tf,
        open_price,
        current_price,
        delta_pct,
        delta_chainlink,
        delta_tiingo,
        delta_binance,
        price_consensus,
        current_vpin,
        _snap_regime,
        cg,
        signal,
        twap_result,
        _tiingo_open,
        _tiingo_close,
        _psu,
    ):
        _dir = signal.direction if signal else ("UP" if delta_pct > 0 else "DOWN")
        return {
            "window_ts": window.window_ts,
            "asset": window.asset,
            "timeframe": tf,
            "open_price": open_price,
            "close_price": current_price,
            "delta_pct": delta_pct,
            "delta_chainlink": delta_chainlink,
            "delta_tiingo": delta_tiingo,
            "delta_binance": delta_binance,
            "price_consensus": price_consensus,
            "vpin": current_vpin,
            "regime": _snap_regime,
            "btc_price": current_price,
            "cg_connected": cg.connected if cg else False,
            "direction": _dir,
            "confidence": signal.confidence if signal else None,
            "trade_placed": False,
            "skip_reason": None,
            "engine_version": "v8.0",
            "delta_source": _psu,
            "chainlink_open": None,
            "tiingo_open": _tiingo_open,
            "tiingo_close": _tiingo_close,
            "gamma_up_price": float(window.up_price)
            if window.up_price is not None
            else None,
            "gamma_down_price": float(window.down_price)
            if window.down_price is not None
            else None,
            "shadow_trade_direction": _dir,
            "shadow_trade_entry_price": (
                window.up_price if _dir == "UP" else window.down_price
            )
            if (window.up_price and window.down_price)
            else None,
            "twap_delta_pct": twap_result.twap_delta_pct if twap_result else None,
        }

    def _compute_gates(
        self,
        ws,
        current_vpin,
        delta_pct,
        signal,
        delta_tiingo,
        delta_binance,
        delta_chainlink,
    ):
        from config.runtime_config import runtime as _rt

        _vp = current_vpin >= _rt.five_min_vpin_gate
        _dt = (
            _rt.five_min_cascade_min_delta_pct
            if current_vpin >= _rt.vpin_cascade_direction_threshold
            else _rt.five_min_min_delta_pct
        )
        _dp = abs(delta_pct) >= _dt if delta_pct is not None else False
        _fp = True
        _cp = True
        _imp = (
            signal.direction
            if signal
            else ("UP" if delta_pct and delta_pct > 0 else "DOWN")
        )
        if ws.get("gamma_up_price") and ws.get("gamma_down_price"):
            _ep = ws["gamma_up_price"] if _imp == "UP" else ws["gamma_down_price"]
            if _ep < 0.30:
                _fp = False
            elif _ep > 0.73:
                _cp = False
        _gp = []
        _gf = None
        for n, p in [
            ("vpin", _vp),
            ("delta", _dp),
            ("cg", True),
            ("floor", _fp),
            ("cap", _cp),
        ]:
            if p:
                _gp.append(n)
            elif _gf is None:
                _gf = n
        _sa = sum(
            1
            for d in [delta_tiingo, delta_binance, delta_chainlink]
            if d is not None
            and ((d > 0 and _imp == "UP") or (d < 0 and _imp == "DOWN"))
        )
        if _sa >= 3 and current_vpin >= 0.65 and abs(delta_pct or 0) >= 0.05:
            ct = "DECISIVE"
        elif _sa >= 2 and current_vpin >= 0.55:
            ct = "HIGH"
        elif _vp and _dp:
            ct = "MODERATE"
        elif _vp:
            ct = "LOW"
        else:
            ct = "NONE"
        ws["gates_passed"] = ",".join(_gp)
        ws["gate_failed"] = _gf
        ws["confidence_tier"] = ct
        ws["_cap_passed"] = _cp
        ws["_floor_passed"] = _fp

    def _append_skip_history(
        self,
        *,
        window_key,
        eval_offset,
        _skip_reason,
        current_vpin,
        delta_pct,
        window_snapshot,
        _snap_regime,
        delta_chainlink,
        delta_tiingo,
    ):
        if window_key not in self._window_eval_history:
            self._window_eval_history[window_key] = []
        self._window_eval_history[window_key].append(
            {
                "offset": eval_offset,
                "skip_reason": _skip_reason,
                "vpin": current_vpin,
                "delta_pct": delta_pct,
                "regime": _snap_regime,
            }
        )

    def _cleanup_stale_history(self):
        now = time.time()
        for k in [k for k in self._window_eval_history if "-" in k]:
            try:
                if now - int(k.split("-", 1)[1]) > 600:
                    self._window_eval_history.pop(k, None)
            except Exception:
                pass

    async def _send_trade_alert(self, **kw):
        pass

    def _fire_claude_eval(self, **kw):
        pass

    async def _apply_v81_early_entry_gate(
        self,
        *,
        window,
        eval_offset,
        signal,
        current_vpin,
        delta_pct,
        twap_result,
        current_price,
        _tiingo_close,
        delta_binance,
        delta_chainlink,
        delta_tiingo,
        _snap_regime,
        _price_source_used,
        window_snapshot,
    ):
        # v8.1 early entry gate -- delegates to v2.2 scorer
        if self._timesfm_v2 is None:
            return signal
        _v81_cap = _get_v81_cap(eval_offset)
        _v8_dir = signal.direction
        try:
            from signals.v2_feature_body import build_v5_feature_body

            f = build_v5_feature_body(
                eval_offset=float(eval_offset),
                vpin=current_vpin,
                delta_pct=delta_pct,
                twap_delta=(twap_result.twap_delta_pct if twap_result else None),
                clob_up_price=window.up_price,
                clob_down_price=window.down_price,
                binance_price=current_price,
                chainlink_price=window_snapshot.get("chainlink_open"),
                tiingo_close=_tiingo_close,
                delta_binance=delta_binance,
                delta_chainlink=delta_chainlink,
                delta_tiingo=delta_tiingo,
                regime=_snap_regime,
                delta_source=_price_source_used,
                prev_v2_probability_up=window_snapshot.get("v2_probability_up"),
            )
            r = await self._timesfm_v2.score_with_features(
                asset=window.asset, seconds_to_close=eval_offset, features=f
            )
            if not r or "probability_up" not in r:
                raise RuntimeError("v2.2 invalid")
            _p = float(r["probability_up"])
            _d = "UP" if _p > 0.5 else "DOWN"
            _hi = _p > 0.65 or _p < 0.35
            _ag = _d == _v8_dir
            window_snapshot["v2_probability_up"] = round(_p, 4)
            window_snapshot["v2_direction"] = _d
            window_snapshot["v2_agrees"] = _ag
            if not _hi:
                signal = None
                self._last_skip_reason = f"v2.2 LOW conf ({_p:.2f}) at T-{eval_offset}"
            elif not _ag:
                signal = None
                self._last_skip_reason = f"v2.2 DISAGREES at T-{eval_offset}"
            elif eval_offset >= 120:
                if current_vpin < 0.65:
                    signal = None
                    self._last_skip_reason = f"v8.1: not CASCADE at T-{eval_offset}"
                elif abs(delta_pct) < 0.05 if delta_pct else True:
                    signal = None
                    self._last_skip_reason = f"v8.1: delta weak at T-{eval_offset}"
                else:
                    signal.confidence = "DECISIVE"
                    signal.entry_reason = f"v2.2_early_T{eval_offset}"
                    signal.v81_entry_cap = _v81_cap
            else:
                if current_vpin < 0.55:
                    signal = None
                    self._last_skip_reason = f"v8.1.2: NORMAL at T-{eval_offset}"
                else:
                    signal.entry_reason = f"v2.2_confirmed_T{eval_offset}"
                    signal.v81_entry_cap = _v81_cap
        except Exception as e:
            signal = None
            self._last_skip_reason = (
                f"v8.1: v2.2 unavailable at T-{eval_offset}: {str(e)[:50]}"
            )
        return signal
