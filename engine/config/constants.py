"""
Engine Constants

All numerical parameters for signals, execution, and risk management.
These are defaults; runtime config in DB can override them.
"""

# ── Polymarket / Window ──
POLY_WINDOW_SECONDS: int = 300  # Look-back window for arb price validity

# ── Fee Multipliers ──
POLYMARKET_CRYPTO_FEE_MULT: float = 0.072   # 7.2% effective fee on crypto markets
OPINION_CRYPTO_FEE_MULT: float = 0.04       # 4.0% effective fee on Opinion

# ── VPIN (Volume-Synchronized PIN) ──
VPIN_BUCKET_SIZE_USD: float = 50_000        # Each volume bucket = $50k traded notional
VPIN_LOOKBACK_BUCKETS: int = 50             # Rolling window of 50 buckets
VPIN_INFORMED_THRESHOLD: float = 0.55      # VPIN > 0.55 → elevated informed flow
VPIN_CASCADE_THRESHOLD: float = 0.70       # VPIN > 0.70 → cascade-level informed flow

# ── Cascade Detector ──
CASCADE_OI_DROP_THRESHOLD: float = 0.02     # OI must drop ≥ 2% for cascade signal
CASCADE_LIQ_VOLUME_THRESHOLD: float = 5e6   # Liquidation volume must exceed $5M

# ── Risk Management ──
MAX_DRAWDOWN_KILL: float = 0.45             # Kill switch at 45% drawdown from peak
BET_FRACTION: float = 0.025                 # Kelly fraction: 2.5% of bankroll per bet
MIN_BET_USD: float = 2.0                    # Minimum bet size (Polymarket minimum)
MAX_OPEN_EXPOSURE_PCT: float = 0.30         # Max 30% of bankroll in open positions
DAILY_LOSS_LIMIT_PCT: float = 0.10         # Stop trading after 10% daily loss
CONSECUTIVE_LOSS_COOLDOWN: int = 3          # Cool down after N consecutive losses
COOLDOWN_SECONDS: int = 900                 # 15-minute cooldown duration

# ── Sub-$1 Arbitrage ──
ARB_MIN_SPREAD: float = 0.015              # Minimum net spread after fees to trade
ARB_MAX_POSITION: float = 50.0             # Max USD per arb position
ARB_MAX_EXECUTION_MS: int = 500            # Max time to execute both legs (ms)
