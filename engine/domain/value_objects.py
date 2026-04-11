"""Domain value objects for the engine's Clean Architecture layer.

Phase 1 deliverable -- frozen dataclasses with ``__post_init__`` validation.
Each type is referenced by a port signature in ``engine/domain/ports.py``.

Every value object here is immutable (``frozen=True``), validated at
construction time, and has zero external dependencies beyond the Python
standard library.

Audit IDs: CA-01, CA-02.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional


def _require_finite(value, name):
    if value is not None and (math.isnan(value) or math.isinf(value)):
        raise ValueError(f"{name} must be finite, got {value}")


def _require_positive_int(value, name):
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value}")


@dataclass(frozen=True)
class WindowKey:
    """Unique identifier for a 5-minute (or 15-minute) binary-options window."""
    asset: str
    window_ts: int
    duration_secs: int = 300
    def __post_init__(self):
        if not self.asset: raise ValueError("asset must be a non-empty string")
        _require_positive_int(self.window_ts, "window_ts")
        if self.duration_secs not in (300, 900): raise ValueError(f"duration_secs must be 300 or 900, got {self.duration_secs}")
    @property
    def key(self): return f"{self.asset}-{self.window_ts}"
    @property
    def timeframe(self): return "15m" if self.duration_secs == 900 else "5m"

@dataclass(frozen=True)
class Tick:
    """Single price observation from a market feed."""
    source: str; asset: str; price: float; timestamp: float
    def __post_init__(self):
        if not self.source: raise ValueError("source must be a non-empty string")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        _require_finite(self.price, "price")
        if self.price <= 0: raise ValueError(f"price must be positive, got {self.price}")
        _require_finite(self.timestamp, "timestamp")
        if self.timestamp <= 0: raise ValueError(f"timestamp must be positive, got {self.timestamp}")

@dataclass(frozen=True)
class WindowClose:
    """Event emitted when a 5-minute window closes and is ready for evaluation."""
    asset: str; window_ts: int; duration_secs: int; open_price: float; close_ts: float
    latest_tick: Optional[Tick] = None
    def __post_init__(self):
        if not self.asset: raise ValueError("asset must be a non-empty string")
        _require_positive_int(self.window_ts, "window_ts")
        if self.duration_secs not in (300, 900): raise ValueError(f"duration_secs must be 300 or 900, got {self.duration_secs}")
        _require_finite(self.open_price, "open_price")
        if self.open_price <= 0: raise ValueError(f"open_price must be positive, got {self.open_price}")
        _require_finite(self.close_ts, "close_ts")
    @property
    def window_key(self): return WindowKey(asset=self.asset, window_ts=self.window_ts, duration_secs=self.duration_secs)

@dataclass(frozen=True)
class DeltaSet:
    """Per-source delta triple (chainlink / tiingo / binance) for a window."""
    delta_chainlink: Optional[float] = None; delta_tiingo: Optional[float] = None; delta_binance: Optional[float] = None
    def __post_init__(self):
        _require_finite(self.delta_chainlink, "delta_chainlink"); _require_finite(self.delta_tiingo, "delta_tiingo"); _require_finite(self.delta_binance, "delta_binance")
    @property
    def available_count(self): return sum(1 for d in (self.delta_chainlink, self.delta_tiingo, self.delta_binance) if d is not None)
    @property
    def agreeing_sign(self):
        signs = [("UP" if d > 0 else "DOWN") for d in (self.delta_chainlink, self.delta_tiingo, self.delta_binance) if d is not None and d != 0.0]
        if not signs: return None
        if signs.count("UP") >= 2: return "UP"
        if signs.count("DOWN") >= 2: return "DOWN"
        return None

@dataclass(frozen=True)
class SignalEvaluation:
    """One row of the signal_evaluations audit table."""
    window_ts: int; asset: str; timeframe: str; eval_offset: int
    clob_up_bid: Optional[float] = None; clob_up_ask: Optional[float] = None; clob_down_bid: Optional[float] = None; clob_down_ask: Optional[float] = None
    binance_price: Optional[float] = None; tiingo_open: Optional[float] = None; tiingo_close: Optional[float] = None; chainlink_price: Optional[float] = None
    delta_pct: Optional[float] = None; delta_tiingo: Optional[float] = None; delta_binance: Optional[float] = None; delta_chainlink: Optional[float] = None; delta_source: Optional[str] = None
    vpin: Optional[float] = None; regime: Optional[str] = None; clob_spread: Optional[float] = None; clob_mid: Optional[float] = None
    v2_probability_up: Optional[float] = None; v2_direction: Optional[str] = None; v2_agrees: Optional[bool] = None; v2_high_conf: Optional[bool] = None; v2_model_version: Optional[str] = None; v2_quantiles: Optional[str] = None; v2_quantiles_at_close: Optional[str] = None
    gate_vpin_passed: Optional[bool] = None; gate_delta_passed: Optional[bool] = None; gate_cg_passed: Optional[bool] = None; gate_twap_passed: Optional[bool] = None; gate_timesfm_passed: Optional[bool] = None; gate_passed: Optional[bool] = None; gate_failed: Optional[str] = None
    decision: str = "SKIP"
    twap_delta: Optional[float] = None; twap_direction: Optional[str] = None; twap_gamma_agree: Optional[bool] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.timeframe not in ("5m", "15m"): raise ValueError(f"timeframe must be 5m or 15m, got {self.timeframe!r}")
        if not isinstance(self.eval_offset, int) or self.eval_offset < 0: raise ValueError(f"eval_offset must be a non-negative integer, got {self.eval_offset}")
        if self.decision not in ("TRADE", "SKIP"): raise ValueError(f"decision must be TRADE or SKIP, got {self.decision!r}")

@dataclass(frozen=True)
class ClobSnapshot:
    """Point-in-time snapshot of a CLOB order book for both sides of a window."""
    asset: str; timeframe: str; window_ts: int
    up_token_id: Optional[str] = None; down_token_id: Optional[str] = None
    up_best_bid: Optional[float] = None; up_best_ask: Optional[float] = None; up_bid_depth: Optional[float] = None; up_ask_depth: Optional[float] = None
    down_best_bid: Optional[float] = None; down_best_ask: Optional[float] = None; down_bid_depth: Optional[float] = None; down_ask_depth: Optional[float] = None
    up_spread: Optional[float] = None; down_spread: Optional[float] = None; mid_price: Optional[float] = None
    up_bids_top5: tuple = (); up_asks_top5: tuple = (); down_bids_top5: tuple = (); down_asks_top5: tuple = ()
    def __post_init__(self):
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.timeframe not in ("5m", "15m"): raise ValueError(f"timeframe must be 5m or 15m, got {self.timeframe!r}")
        _require_positive_int(self.window_ts, "window_ts")

@dataclass(frozen=True)
class GateAuditRow:
    """Audit row recording which gates ran and their results."""
    window_ts: int; asset: str; timeframe: str; eval_offset: int
    engine_version: str = "v8.0"; delta_source: Optional[str] = None
    open_price: Optional[float] = None; tiingo_open: Optional[float] = None; tiingo_close: Optional[float] = None
    delta_tiingo: Optional[float] = None; delta_binance: Optional[float] = None; delta_chainlink: Optional[float] = None; delta_pct: Optional[float] = None
    vpin: Optional[float] = None; regime: Optional[str] = None
    gate_vpin: Optional[str] = None; gate_delta: Optional[str] = None; gate_cg: Optional[bool] = None; gate_floor: Optional[str] = None; gate_cap: Optional[str] = None
    gate_passed: bool = False; gate_failed: Optional[str] = None; gates_passed_list: Optional[str] = None
    decision: str = "SKIP"; skip_reason: Optional[str] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.timeframe not in ("5m", "15m"): raise ValueError(f"timeframe must be 5m or 15m, got {self.timeframe!r}")
        if not isinstance(self.eval_offset, int) or self.eval_offset < 0: raise ValueError(f"eval_offset must be a non-negative integer, got {self.eval_offset}")
        if self.decision not in ("TRADE", "SKIP"): raise ValueError(f"decision must be TRADE or SKIP, got {self.decision!r}")

@dataclass(frozen=True)
class WindowSnapshot:
    """Full snapshot of a window for backfill and UI hydration."""
    window_ts: int; asset: str; timeframe: str
    open_price: Optional[float] = None; close_price: Optional[float] = None; delta_pct: Optional[float] = None; vpin: Optional[float] = None; regime: Optional[str] = None
    cg_connected: bool = False; cg_oi_usd: Optional[float] = None; cg_oi_delta_pct: Optional[float] = None; cg_liq_long_usd: Optional[float] = None; cg_liq_short_usd: Optional[float] = None; cg_liq_total_usd: Optional[float] = None; cg_long_pct: Optional[float] = None; cg_short_pct: Optional[float] = None; cg_long_short_ratio: Optional[float] = None; cg_top_long_pct: Optional[float] = None; cg_top_short_pct: Optional[float] = None; cg_top_ratio: Optional[float] = None; cg_taker_buy_usd: Optional[float] = None; cg_taker_sell_usd: Optional[float] = None; cg_funding_rate: Optional[float] = None
    direction: Optional[str] = None; confidence: Optional[float] = None; cg_modifier: Optional[float] = None
    trade_placed: bool = False; skip_reason: Optional[str] = None; outcome: Optional[str] = None; pnl_usd: Optional[float] = None; poly_winner: Optional[str] = None; btc_price: Optional[float] = None
    twap_delta_pct: Optional[float] = None; twap_direction: Optional[str] = None; twap_gamma_agree: Optional[bool] = None; twap_agreement_score: Optional[float] = None; twap_confidence_boost: Optional[float] = None; twap_n_ticks: Optional[int] = None; twap_stability: Optional[float] = None; twap_trend_pct: Optional[float] = None; twap_momentum_pct: Optional[float] = None; twap_gamma_gate: Optional[bool] = None; twap_should_skip: Optional[bool] = None; twap_skip_reason: Optional[str] = None
    timesfm_direction: Optional[str] = None; timesfm_confidence: Optional[float] = None; timesfm_predicted_close: Optional[float] = None; timesfm_delta_vs_open: Optional[float] = None; timesfm_spread: Optional[float] = None; timesfm_p10: Optional[float] = None; timesfm_p50: Optional[float] = None; timesfm_p90: Optional[float] = None
    market_best_bid: Optional[float] = None; market_best_ask: Optional[float] = None; market_spread: Optional[float] = None; market_mid_price: Optional[float] = None; market_volume: Optional[float] = None; market_liquidity: Optional[float] = None
    v71_would_trade: Optional[bool] = None; v71_skip_reason: Optional[str] = None; v71_regime: Optional[str] = None
    is_live: bool = False; gamma_up_price: Optional[float] = None; gamma_down_price: Optional[float] = None
    delta_chainlink: Optional[float] = None; delta_tiingo: Optional[float] = None; delta_binance: Optional[float] = None; price_consensus: Optional[str] = None
    engine_version: Optional[str] = None; delta_source: Optional[str] = None; confidence_tier: Optional[str] = None; gates_passed: Optional[str] = None; gate_failed: Optional[str] = None
    shadow_trade_direction: Optional[str] = None; shadow_trade_entry_price: Optional[float] = None
    v2_probability_up: Optional[float] = None; v2_direction: Optional[str] = None; v2_agrees: Optional[bool] = None; v2_model_version: Optional[str] = None; eval_offset: Optional[int] = None; v2_quantiles: Optional[str] = None; v2_quantiles_at_close: Optional[str] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.timeframe not in ("5m", "15m"): raise ValueError(f"timeframe must be 5m or 15m, got {self.timeframe!r}")

@dataclass(frozen=True)
class FillResult:
    """Result of a CLOB order placement (FOK/FAK ladder or GTC)."""
    filled: bool; order_id: Optional[str] = None; fill_price: Optional[float] = None; fill_step: Optional[int] = None; shares: Optional[float] = None; attempts: int = 0; attempted_prices: tuple = (); abort_reason: Optional[str] = None; partial: bool = False; order_type: str = "FAK"
    def __post_init__(self):
        _require_finite(self.fill_price, "fill_price"); _require_finite(self.shares, "shares")
        if self.fill_price is not None and self.fill_price < 0: raise ValueError(f"fill_price cannot be negative, got {self.fill_price}")
        if self.shares is not None and self.shares < 0: raise ValueError(f"shares cannot be negative, got {self.shares}")
        if self.attempts < 0: raise ValueError(f"attempts cannot be negative, got {self.attempts}")
        if self.filled and self.order_id is None: raise ValueError("filled result must have an order_id")

@dataclass(frozen=True)
class WindowMarket:
    """Gamma market lookup result for an (asset, window_ts) pair."""
    asset: str; window_ts: int; condition_id: Optional[str] = None; up_token_id: Optional[str] = None; down_token_id: Optional[str] = None; up_price: Optional[float] = None; down_price: Optional[float] = None; price_source: str = "unknown"
    def __post_init__(self):
        if not self.asset: raise ValueError("asset must be a non-empty string")
        _require_positive_int(self.window_ts, "window_ts"); _require_finite(self.up_price, "up_price"); _require_finite(self.down_price, "down_price")
    @property
    def has_tokens(self): return bool(self.up_token_id and self.down_token_id)

@dataclass(frozen=True)
class OrderBook:
    """Live CLOB order book for a single token."""
    token_id: str; bids: tuple = (); asks: tuple = (); timestamp: Optional[float] = None
    def __post_init__(self):
        if not self.token_id: raise ValueError("token_id must be a non-empty string")
    @property
    def best_bid(self): return self.bids[0][0] if self.bids else None
    @property
    def best_ask(self): return self.asks[0][0] if self.asks else None
    @property
    def spread(self): return (self.best_ask - self.best_bid) if (self.best_bid is not None and self.best_ask is not None) else None

@dataclass(frozen=True)
class PendingTrade:
    """A manual-trade row with status=pending_live from the dashboard."""
    trade_id: int; window_ts: int; asset: str; direction: str; entry_price: float; stake_usd: float; gamma_up_price: Optional[float] = None; gamma_down_price: Optional[float] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.direction not in ("UP", "DOWN"): raise ValueError(f"direction must be UP or DOWN, got {self.direction!r}")
        _require_finite(self.entry_price, "entry_price")
        if self.entry_price <= 0 or self.entry_price >= 1: raise ValueError(f"entry_price must be in (0, 1) range for Polymarket, got {self.entry_price}")
        _require_finite(self.stake_usd, "stake_usd")
        if self.stake_usd <= 0: raise ValueError(f"stake_usd must be positive, got {self.stake_usd}")
    @property
    def clob_side(self): return "YES" if self.direction == "UP" else "NO"

@dataclass(frozen=True)
class TradeDecision:
    """Structured decision output from the gate pipeline."""
    window_ts: int; asset: str; timeframe: str; direction: str; eval_offset: int; entry_price: float; stake_usd: float
    delta_pct: Optional[float] = None; delta_source: Optional[str] = None; vpin: Optional[float] = None; regime: Optional[str] = None; confidence_tier: Optional[str] = None; engine_version: str = "v10.0"; token_id: Optional[str] = None; market_slug: Optional[str] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.timeframe not in ("5m", "15m"): raise ValueError(f"timeframe must be 5m or 15m, got {self.timeframe!r}")
        if self.direction not in ("YES", "NO"): raise ValueError(f"direction must be YES or NO, got {self.direction!r}")
        if not isinstance(self.eval_offset, int) or self.eval_offset < 0: raise ValueError(f"eval_offset must be a non-negative integer, got {self.eval_offset}")
        _require_finite(self.entry_price, "entry_price")
        if self.entry_price <= 0 or self.entry_price >= 1: raise ValueError(f"entry_price must be in (0, 1) for Polymarket, got {self.entry_price}")
        _require_finite(self.stake_usd, "stake_usd")
        if self.stake_usd <= 0: raise ValueError(f"stake_usd must be positive, got {self.stake_usd}")

@dataclass(frozen=True)
class SkipSummary:
    """Consolidated skip summary for a window where all offsets were skipped."""
    window_key: str; asset: str; window_ts: int; n_evals: int; eval_history: tuple = ()
    vpin: Optional[float] = None; delta_pct: Optional[float] = None; regime: Optional[str] = None; direction: Optional[str] = None; confidence: Optional[str] = None
    def __post_init__(self):
        if not self.window_key: raise ValueError("window_key must be a non-empty string")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        _require_positive_int(self.window_ts, "window_ts")
        if self.n_evals < 0: raise ValueError(f"n_evals cannot be negative, got {self.n_evals}")

@dataclass(frozen=True)
class SitrepPayload:
    """5-minute SITREP payload for the heartbeat Telegram message."""
    engine_status: str; paper_mode: bool; is_killed: bool; wallet_balance: float; bankroll: float; starting_bankroll: float; daily_pnl: float; portfolio_value: float
    wins_today: int = 0; losses_today: int = 0; vpin: float = 0.0; vpin_regime: str = "CALM"; btc_price: float = 0.0; binance_connected: bool = False; total_orders: int = 0; resolved_orders: int = 0; open_orders: int = 0; open_positions_value: float = 0.0; drawdown_pct: float = 0.0
    def __post_init__(self):
        _require_finite(self.wallet_balance, "wallet_balance"); _require_finite(self.bankroll, "bankroll"); _require_finite(self.daily_pnl, "daily_pnl"); _require_finite(self.portfolio_value, "portfolio_value"); _require_finite(self.btc_price, "btc_price")
        if self.wins_today < 0: raise ValueError(f"wins_today cannot be negative, got {self.wins_today}")
        if self.losses_today < 0: raise ValueError(f"losses_today cannot be negative, got {self.losses_today}")
    @property
    def win_rate(self):
        t = self.wins_today + self.losses_today; return self.wins_today / t if t > 0 else 0.0

@dataclass(frozen=True)
class WindowOutcome:
    """Outcome of a resolved window (win/loss/push, PnL)."""
    window_ts: int; asset: str; outcome: str; pnl_usd: float; resolved_at: float; direction: Optional[str] = None; token_id: Optional[str] = None; condition_id: Optional[str] = None
    def __post_init__(self):
        _require_positive_int(self.window_ts, "window_ts")
        if not self.asset: raise ValueError("asset must be a non-empty string")
        if self.outcome not in ("WIN", "LOSS", "PUSH", "EXPIRED"): raise ValueError(f"outcome must be WIN/LOSS/PUSH/EXPIRED, got {self.outcome!r}")
        _require_finite(self.pnl_usd, "pnl_usd"); _require_finite(self.resolved_at, "resolved_at")

@dataclass(frozen=True)
class ManualTradeOutcome:
    """Result of processing one pending manual trade."""
    trade_id: int; status: str; order_id: Optional[str] = None; fill_price: Optional[float] = None; token_id: Optional[str] = None; token_source: Optional[str] = None; error: Optional[str] = None
    def __post_init__(self):
        if self.status not in ("executing", "open", "filled", "failed_no_token", "failed_execution", "failed_risk"): raise ValueError(f"invalid manual trade status: {self.status!r}")
        _require_finite(self.fill_price, "fill_price")

@dataclass(frozen=True)
class RiskStatus:
    """Read-only snapshot of the risk manager's current state."""
    current_bankroll: float; peak_bankroll: float; drawdown_pct: float; daily_pnl: float; consecutive_losses: int
    cooldown_until: Optional[str] = None; paper_mode: bool = True; kill_switch_active: bool = False; is_killed: bool = False; polymarket_connected: bool = False; opinion_connected: bool = False
    def __post_init__(self):
        _require_finite(self.current_bankroll, "current_bankroll"); _require_finite(self.peak_bankroll, "peak_bankroll"); _require_finite(self.drawdown_pct, "drawdown_pct"); _require_finite(self.daily_pnl, "daily_pnl")
        if self.drawdown_pct < 0 or self.drawdown_pct > 1: raise ValueError(f"drawdown_pct must be in [0, 1], got {self.drawdown_pct}")
        if self.consecutive_losses < 0: raise ValueError(f"consecutive_losses cannot be negative, got {self.consecutive_losses}")

@dataclass(frozen=True)
class WalletSnapshot:
    """Point-in-time wallet balance snapshot."""
    balance_usdc: float; timestamp: float; source: str = "polymarket_clob"
    def __post_init__(self):
        _require_finite(self.balance_usdc, "balance_usdc")
        if self.balance_usdc < 0: raise ValueError(f"balance_usdc cannot be negative, got {self.balance_usdc}")
        _require_finite(self.timestamp, "timestamp")
        if self.timestamp <= 0: raise ValueError(f"timestamp must be positive, got {self.timestamp}")
        if not self.source: raise ValueError("source must be a non-empty string")

@dataclass(frozen=True)
class HeartbeatRow:
    """One row written to the system_state table by the heartbeat loop."""
    engine_status: str; current_balance: Optional[float] = None; peak_balance: Optional[float] = None; current_drawdown_pct: Optional[float] = None; last_vpin: Optional[float] = None; last_cascade_state: Optional[str] = None; active_positions: int = 0; config: Optional[dict] = field(default=None, hash=False)
    def __post_init__(self):
        if not self.engine_status: raise ValueError("engine_status must be a non-empty string")
        _require_finite(self.current_balance, "current_balance"); _require_finite(self.peak_balance, "peak_balance"); _require_finite(self.current_drawdown_pct, "current_drawdown_pct"); _require_finite(self.last_vpin, "last_vpin")
        if self.active_positions < 0: raise ValueError(f"active_positions cannot be negative, got {self.active_positions}")

@dataclass(frozen=True)
class ResolutionResult:
    """Result of resolving one position against the Polymarket outcome."""
    condition_id: str; outcome: str; pnl_usd: float; token_id: Optional[str] = None; trade_ids_updated: tuple = ()
    def __post_init__(self):
        if not self.condition_id: raise ValueError("condition_id must be a non-empty string")
        if self.outcome not in ("RESOLVED_WIN", "RESOLVED_LOSS"): raise ValueError(f"outcome must be RESOLVED_WIN or RESOLVED_LOSS, got {self.outcome!r}")
        _require_finite(self.pnl_usd, "pnl_usd")

@dataclass(frozen=True)
class PositionOutcome:
    """Position outcome data from the Polymarket API."""
    condition_id: str; outcome: str; size: float; avg_price: float; cur_price: float; value: float; cost: float; pnl: float; token_id: str = ""; asset: str = ""
    def __post_init__(self):
        if not self.condition_id: raise ValueError("condition_id must be a non-empty string")
        if self.outcome not in ("WIN", "LOSS", "OPEN"): raise ValueError(f"outcome must be WIN/LOSS/OPEN, got {self.outcome!r}")
        _require_finite(self.size, "size"); _require_finite(self.avg_price, "avg_price"); _require_finite(self.cur_price, "cur_price"); _require_finite(self.value, "value"); _require_finite(self.cost, "cost"); _require_finite(self.pnl, "pnl")
        if self.size < 0: raise ValueError(f"size cannot be negative, got {self.size}")
