"""
Engine Constants

All numerical parameters for signals, execution, and risk management.
Reads from environment variables with sensible defaults.
Runtime config in DB can further override them.
"""

import os

def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))

def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))

# ── Polymarket / Window ──
POLY_WINDOW_SECONDS: int = _env_int("POLY_WINDOW_SECONDS", 300)

# ── Fee Multipliers ──
POLYMARKET_CRYPTO_FEE_MULT: float = _env_float("POLYMARKET_FEE_MULT", 0.072)
OPINION_CRYPTO_FEE_MULT: float = _env_float("OPINION_FEE_MULT", 0.04)

# ── VPIN (Volume-Synchronized PIN) ──
VPIN_BUCKET_SIZE_USD: float = _env_float("VPIN_BUCKET_SIZE_USD", 50_000)
VPIN_LOOKBACK_BUCKETS: int = _env_int("VPIN_LOOKBACK_BUCKETS", 50)
VPIN_INFORMED_THRESHOLD: float = _env_float("VPIN_INFORMED_THRESHOLD", 0.55)
VPIN_CASCADE_THRESHOLD: float = _env_float("VPIN_CASCADE_THRESHOLD", 0.70)

# ── Cascade Detector ──
CASCADE_OI_DROP_THRESHOLD: float = _env_float("CASCADE_OI_DROP_THRESHOLD", 0.02)
CASCADE_LIQ_VOLUME_THRESHOLD: float = _env_float("CASCADE_LIQ_VOLUME_THRESHOLD", 5e6)

# ── Risk Management ──
MAX_DRAWDOWN_KILL: float = _env_float("MAX_DRAWDOWN_KILL", 0.45)
BET_FRACTION: float = _env_float("BET_FRACTION", 0.025)
MIN_BET_USD: float = _env_float("MIN_BET_USD", 2.0)
MAX_OPEN_EXPOSURE_PCT: float = _env_float("MAX_OPEN_EXPOSURE_PCT", 0.30)
DAILY_LOSS_LIMIT_PCT: float = _env_float("DAILY_LOSS_LIMIT_PCT", 0.10)
CONSECUTIVE_LOSS_COOLDOWN: int = _env_int("CONSECUTIVE_LOSS_COOLDOWN", 3)
COOLDOWN_SECONDS: int = _env_int("COOLDOWN_SECONDS", 900)

# ── Sub-$1 Arbitrage ──
ARB_MIN_SPREAD: float = _env_float("ARB_MIN_SPREAD", 0.015)
ARB_MAX_POSITION: float = _env_float("ARB_MAX_POSITION", 50.0)
ARB_MAX_EXECUTION_MS: int = _env_int("ARB_MAX_EXECUTION_MS", 500)

# ── 5-Minute Polymarket Trading ──
FIVE_MIN_ENABLED: bool = os.environ.get("FIVE_MIN_ENABLED", "false").lower() == "true"
FIVE_MIN_ASSETS: list[str] = os.environ.get("FIVE_MIN_ASSETS", "BTC").split(",")
FIVE_MIN_MODE: str = os.environ.get("FIVE_MIN_MODE", "safe")
FIVE_MIN_ENTRY_OFFSET: int = _env_int("FIVE_MIN_ENTRY_OFFSET", 60)  # seconds before close (legacy — use FIVE_MIN_EVAL_OFFSETS for multi-window)

# Multi-offset evaluation: comma-separated list of T-minus values
# v10: Dynamic offset generation from FIVE_MIN_EVAL_INTERVAL env var.
# Default interval=2 → 91 offsets (T-240, T-238, ..., T-62, T-60) = 2s polling.
# Set interval=10 for v9 behavior (18 offsets). Set to 1 for 1s polling.
# Override with explicit FIVE_MIN_EVAL_OFFSETS for custom offsets.
_eval_interval = int(os.environ.get("FIVE_MIN_EVAL_INTERVAL", "2"))
_eval_offsets_explicit = os.environ.get("FIVE_MIN_EVAL_OFFSETS", "")
if _eval_offsets_explicit:
    _eval_offsets_raw = _eval_offsets_explicit
else:
    _eval_offsets_raw = ",".join(str(x) for x in range(240, 59, -_eval_interval))
FIVE_MIN_EVAL_OFFSETS: list[int] = sorted(
    [int(x.strip()) for x in _eval_offsets_raw.split(",") if x.strip().isdigit()],
    reverse=True,  # largest offset first (earliest in window)
)
FIVE_MIN_MIN_CONFIDENCE: float = _env_float("FIVE_MIN_MIN_CONFIDENCE", 0.30)
FIVE_MIN_MIN_DELTA_PCT: float = _env_float("FIVE_MIN_MIN_DELTA_PCT", 0.001)  # skip below this (matches backtest)
