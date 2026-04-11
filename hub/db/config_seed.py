"""
CFG-02 — Seed data for the config_keys table.

Single source of truth for the 142 DB-managed config keys inventoried in
docs/CONFIG_MIGRATION_PLAN.md §4. Each row is a (service, key) tuple plus
metadata: type, default value, description, category, restart_required,
editable_via_ui.

Adding a new config key:
  1. Add it to the appropriate _SERVICE_KEYS list below
  2. Bump the count in CONFIG_MIGRATION_PLAN.md §4.7
  3. Re-deploy the hub — seed_config_keys() runs at startup and idempotently
     UPSERTs the new row

The seed is idempotent: re-running it does NOT wipe operator-set
current_value rows in config_values. The ON CONFLICT clause only updates
description / type / category / etc. — i.e. the developer-owned schema
fields, not the operator-owned current value.

SECRET EXCLUSION:
  Per CONFIG_MIGRATION_PLAN.md §10.4 and §2.3 rule 1, secrets are NEVER
  DB-managed. Any key matching SECRET_PATTERN is rejected by
  validate_seed() and seed_config_keys() refuses to start. This is a hard
  gate — not a warning — to prevent a future seed entry from accidentally
  exposing API keys via the /api/v58/config endpoint.
"""

from __future__ import annotations

import re
from typing import Optional

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


# ─── Seed row dataclass-ish dict ──────────────────────────────────────────────
#
# Each list entry is a 7-tuple:
#   (service, key, type, default_value, description, category, restart_required)
#
# Tuple was chosen over a dict for compactness — there are 142 of these and
# every keystroke counts when reading them top-to-bottom. The seed loader
# converts to dicts before INSERT.

# Regex per CONFIG_MIGRATION_PLAN.md §10.4. Any seed key matching this
# pattern is REJECTED — secrets do NOT belong in the DB.
SECRET_PATTERN = re.compile(
    r".*_(API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|PASSPHRASE|FUNDER_ADDRESS|WALLET_KEY)$"
)


# ─── Engine — 88 keys (per plan §4.1.2 + §4.1.3) ──────────────────────────────
#
# Categories:
#   - sizing      → bankroll, position caps, fractions
#   - thresholds  → VPIN gates, regime gates, delta gates
#   - gates       → on/off behavioural toggles + gate-class flags
#   - execution   → order types, FOK/TWAP, stagger, intervals
#   - strategy    → 5m / 15m / timesfm / arb master flags + per-strategy params
#   - venue       → fees, preferred venue routing
#   - infrastructure → reconciler, sync intervals, alerts (still tunable but more
#                      'plumbing' than 'strategy')

_ENGINE_KEYS: list[tuple] = [
    # ── Existing _DB_KEY_MAP keys (already DB-managed via runtime_config.sync) ──
    ("engine", "STARTING_BANKROLL", "float", "500.0",
     "Initial bankroll in USD. Used as the base for Kelly position sizing.",
     "sizing", False),
    ("engine", "BET_FRACTION", "float", "0.025",
     "Kelly fraction per trade. 0.025 = 2.5% of bankroll. Higher = more aggressive compounding.",
     "sizing", False),
    ("engine", "MAX_POSITION_USD", "float", "500.0",
     "Hard cap on any single position in USD, regardless of Kelly fraction.",
     "sizing", False),
    ("engine", "MAX_DRAWDOWN_KILL", "float", "0.45",
     "If equity drops this fraction below peak, the engine halts and waits for manual reset.",
     "sizing", False),
    ("engine", "DAILY_LOSS_LIMIT_USD", "float", "50.0",
     "Maximum USD loss allowed in a single trading day before the engine pauses until midnight UTC.",
     "sizing", False),
    ("engine", "DAILY_LOSS_LIMIT_PCT", "float", "0.10",
     "Maximum percentage equity loss in a single day before the engine pauses.",
     "sizing", False),
    ("engine", "MIN_BET_USD", "float", "2.0",
     "Floor on bet size — trades below this are skipped to avoid Polymarket dust limits.",
     "sizing", False),
    ("engine", "MAX_OPEN_EXPOSURE_PCT", "float", "0.30",
     "Maximum fraction of bankroll that can be open across all positions at once.",
     "sizing", False),
    ("engine", "CONSECUTIVE_LOSS_COOLDOWN", "int", "3",
     "Number of consecutive losses that triggers a cooldown period.",
     "sizing", False),
    ("engine", "COOLDOWN_SECONDS", "int", "900",
     "Duration in seconds of the cooldown after consecutive losses.",
     "sizing", False),
    ("engine", "VPIN_BUCKET_SIZE_USD", "float", "500000",
     "Volume per VPIN bucket in USD. Larger buckets smooth the signal but lag in low-volume periods.",
     "thresholds", False),
    ("engine", "VPIN_LOOKBACK_BUCKETS", "int", "50",
     "Number of buckets in the rolling VPIN window.",
     "thresholds", False),
    ("engine", "VPIN_INFORMED_THRESHOLD", "float", "0.55",
     "VPIN level above which informed-trader pressure is detected.",
     "thresholds", False),
    ("engine", "VPIN_CASCADE_THRESHOLD", "float", "0.70",
     "VPIN level above which a liquidation cascade is potentially in progress.",
     "thresholds", False),
    ("engine", "VPIN_CASCADE_DIRECTION_THRESHOLD", "float", "0.65",
     "VPIN directional gate for cascade trades.",
     "thresholds", False),
    ("engine", "CASCADE_OI_DROP_THRESHOLD", "float", "0.02",
     "Open-interest drop fraction that triggers a cascade signal.",
     "thresholds", False),
    ("engine", "CASCADE_LIQ_VOLUME_THRESHOLD", "float", "5000000",
     "Minimum liquidation volume in USD before a cascade signal fires.",
     "thresholds", False),
    ("engine", "ARB_MIN_SPREAD", "float", "0.015",
     "Minimum cross-venue spread to trigger an arbitrage trade.",
     "thresholds", False),
    ("engine", "ARB_MAX_POSITION", "float", "50.0",
     "Maximum USD per arbitrage leg. Total exposure per arb is 2x this value.",
     "sizing", False),
    ("engine", "ARB_MAX_EXECUTION_MS", "int", "500",
     "Maximum milliseconds to execute both legs of an arb. Trade is abandoned if exceeded.",
     "execution", False),
    ("engine", "POLYMARKET_FEE_MULT", "float", "0.072",
     "Effective Polymarket round-trip fee multiplier (read-only — set by the platform).",
     "venue", False),
    ("engine", "OPINION_FEE_MULT", "float", "0.04",
     "Effective Opinion Markets round-trip fee multiplier.",
     "venue", False),
    ("engine", "PREFERRED_VENUE", "enum", "opinion",
     "Default venue when both platforms have equal spread. Opinion is preferred for lower fees.",
     "venue", False),
    ("engine", "FIVE_MIN_MIN_DELTA_PCT", "float", "0.08",
     "Minimum delta percentage required to take a 5m trade in NORMAL or TRANSITION regime.",
     "thresholds", False),
    ("engine", "FIVE_MIN_CASCADE_MIN_DELTA_PCT", "float", "0.03",
     "Minimum delta percentage required to take a 5m trade during a CASCADE regime.",
     "thresholds", False),
    ("engine", "FIVE_MIN_VPIN_GATE", "float", "0.45",
     "VPIN gate threshold for 5m entries.",
     "thresholds", False),

    # ── runtime_config.py keys not yet in _DB_KEY_MAP ──
    ("engine", "FIVE_MIN_ENABLED", "bool", "false",
     "Master toggle for the 5-minute Polymarket strategy. False = no 5m trades fire.",
     "strategy", False),
    ("engine", "FIVE_MIN_ASSETS", "string", "BTC",
     "CSV list of assets the 5m strategy trades.",
     "strategy", False),
    ("engine", "FIVE_MIN_MODE", "enum", "safe",
     "5m strategy mode: flat / safe / degen.",
     "strategy", False),
    ("engine", "FIVE_MIN_ENTRY_OFFSET", "int", "10",
     "Entry trigger offset (seconds) for 5m windows.",
     "execution", False),
    ("engine", "FIVE_MIN_MIN_CONFIDENCE", "float", "0.30",
     "Minimum conviction score required for a 5m entry.",
     "thresholds", False),
    ("engine", "FIVE_MIN_MAX_ENTRY_PRICE", "float", "0.70",
     "Maximum Polymarket price (probability) at which a 5m trade may be entered.",
     "thresholds", False),
    ("engine", "FIFTEEN_MIN_MAX_ENTRY_PRICE", "float", "0.70",
     "Maximum Polymarket price at which a 15m trade may be entered.",
     "thresholds", False),
    ("engine", "POLY_WINDOW_SECONDS", "int", "300",
     "Polymarket window duration in seconds. Structural but tunable.",
     "execution", False),
    ("engine", "ORDER_STAGGER_SECONDS", "float", "1.5",
     "Per-asset order stagger delay (G1 gate).",
     "execution", False),
    ("engine", "SINGLE_BEST_SIGNAL", "bool", "false",
     "G3 gate: only act on the highest-conviction signal in a window.",
     "gates", False),
    ("engine", "MAX_ORDERS_PER_HOUR", "int", "10",
     "G4 rate limit on orders per hour.",
     "execution", False),
    ("engine", "MIN_ORDER_INTERVAL_SECONDS", "float", "4.0",
     "G4 minimum interval between consecutive orders.",
     "execution", False),
    ("engine", "DELTA_PRICE_SOURCE", "enum", "tiingo",
     "Source for the delta calculation: tiingo / binance / chainlink / consensus.",
     "thresholds", False),
    ("engine", "FOK_ENABLED", "bool", "true",
     "Enable the FOK ladder execution path.",
     "execution", False),
    ("engine", "TWAP_OVERRIDE_ENABLED", "bool", "false",
     "Enable the TWAP override gate.",
     "gates", False),
    ("engine", "TWAP_GAMMA_GATE_ENABLED", "bool", "false",
     "Enable the TWAP gamma gate.",
     "gates", False),
    ("engine", "TIMESFM_AGREEMENT_ENABLED", "bool", "false",
     "Require TimesFM agreement on direction before entering a trade.",
     "gates", False),
    ("engine", "TIMESFM_ENABLED", "bool", "false",
     "Master toggle for the TimesFM strategy.",
     "strategy", False),
    ("engine", "TIMESFM_MIN_CONFIDENCE", "float", "0.30",
     "Minimum TimesFM conviction required to act on a forecast.",
     "thresholds", False),
    ("engine", "TIMESFM_ASSETS", "string", "BTC",
     "CSV list of assets the TimesFM strategy trades.",
     "strategy", False),
    ("engine", "V2_EARLY_ENTRY_ENABLED", "bool", "true",
     "Enable v2 early-entry path (T-180 to T-120).",
     "gates", False),
    ("engine", "FIFTEEN_MIN_ENABLED", "bool", "false",
     "Master toggle for the 15-minute Polymarket strategy.",
     "strategy", False),
    ("engine", "FIFTEEN_MIN_ASSETS", "string", "BTC,ETH,SOL",
     "CSV list of assets the 15m strategy trades.",
     "strategy", False),
    ("engine", "V9_SOURCE_AGREEMENT", "bool", "false",
     "Enable v9 source-agreement gate.",
     "gates", False),
    ("engine", "V9_CAPS_ENABLED", "bool", "false",
     "Enable v9 dynamic confidence-based caps.",
     "gates", False),
    ("engine", "ORDER_TYPE", "enum", "FAK",
     "Order type: FAK / FOK / GTC.",
     "execution", False),
    ("engine", "RECONCILER_ENABLED", "bool", "true",
     "Enable the trade reconciler loop.",
     "infrastructure", False),
    ("engine", "POLY_FILLS_SYNC_INTERVAL_S", "float", "300",
     "Interval (seconds) between Polymarket fills sync runs.",
     "infrastructure", False),
    ("engine", "POLY_FILLS_LOOKBACK_HOURS", "float", "2",
     "Lookback window in hours for the Polymarket fills sync.",
     "infrastructure", False),

    # ── v8.1 / v9 pricing caps (inline in five_min_vpin.py) ──
    ("engine", "V81_CAP_T240", "float", "0.55",
     "v8.1 entry price cap at T-240 seconds.",
     "thresholds", False),
    ("engine", "V81_CAP_T180", "float", "0.60",
     "v8.1 entry price cap at T-180 seconds.",
     "thresholds", False),
    ("engine", "V81_CAP_T120", "float", "0.65",
     "v8.1 entry price cap at T-120 seconds.",
     "thresholds", False),
    ("engine", "V81_CAP_T60", "float", "0.73",
     "v8.1 entry price cap at T-60 seconds.",
     "thresholds", False),
    ("engine", "V9_CAP_EARLY", "float", "0.55",
     "v9 entry price cap during the early window.",
     "thresholds", False),
    ("engine", "V9_CAP_GOLDEN", "float", "0.65",
     "v9 entry price cap during the golden window.",
     "thresholds", False),
    ("engine", "V9_VPIN_EARLY", "float", "0.65",
     "v9 VPIN gate during the early window.",
     "thresholds", False),
    ("engine", "V9_VPIN_LATE", "float", "0.45",
     "v9 VPIN gate during the late window.",
     "thresholds", False),
    ("engine", "FOK_PRICE_CAP", "float", "0.73",
     "Maximum price at which the FOK ladder will place an order.",
     "execution", False),
    ("engine", "PRICE_FLOOR", "float", "0.30",
     "Minimum price at which the FOK ladder will place an order.",
     "execution", False),
    ("engine", "FOK_PI_BONUS_CENTS", "float", "0.0314",
     "FOK price improvement bonus in cents.",
     "execution", False),
    ("engine", "ABSOLUTE_MAX_BET", "float", "32.0",
     "Hard absolute cap on per-trade bet size, applied after Kelly + venue caps.",
     "sizing", False),

    # ── v10 / v10.6 decision surface gates (inline in signals/gates.py) ──
    # restart_required=TRUE because gates capture env at __init__ time.
    ("engine", "V10_6_ENABLED", "bool", "false",
     "Master flag for the v10.6 decision surface (DS-01). Enables EvalOffsetBoundsGate.",
     "gates", True),
    ("engine", "V10_6_MIN_EVAL_OFFSET", "int", "90",
     "Minimum evaluation offset (seconds) for the v10.6 EvalOffsetBoundsGate.",
     "gates", True),
    ("engine", "V10_6_MAX_EVAL_OFFSET", "int", "180",
     "Maximum evaluation offset (seconds) for the v10.6 EvalOffsetBoundsGate.",
     "gates", True),
    ("engine", "V10_MIN_DELTA_PCT", "float", "0.0",
     "Global delta-magnitude floor in DeltaMagnitudeGate.",
     "gates", True),
    ("engine", "V10_TRANSITION_MIN_DELTA", "float", "0.0",
     "Delta-magnitude floor specific to the TRANSITION regime.",
     "gates", True),
    ("engine", "V10_DUNE_MIN_P", "float", "0.65",
     "Base DUNE confidence floor in DuneConfidenceGate.",
     "gates", True),
    ("engine", "V10_OFFSET_PENALTY_MAX", "float", "0.06",
     "Maximum offset-based penalty applied by DuneConfidenceGate.",
     "gates", True),
    ("engine", "V10_OFFSET_PENALTY_EARLY_MAX", "float", "0.0",
     "Early-entry offset penalty.",
     "gates", True),
    ("engine", "V10_EARLY_ENTRY_MIN_CONF", "float", "0.90",
     "Minimum confidence required for an early-entry trade.",
     "gates", True),
    ("engine", "V10_DOWN_PENALTY", "float", "0.0",
     "DOWN-side calibration penalty.",
     "gates", True),
    ("engine", "V10_CASCADE_MIN_CONF", "float", "0.90",
     "Minimum confidence for a cascade-regime entry.",
     "gates", True),
    ("engine", "V10_CASCADE_CONF_BONUS", "float", "0.05",
     "Confidence bonus applied during a cascade regime.",
     "gates", True),
    ("engine", "V10_TRANSITION_MIN_P", "float", "0.70",
     "Minimum probability gate for the TRANSITION regime.",
     "gates", True),
    ("engine", "V10_CASCADE_MIN_P", "float", "0.72",
     "Minimum probability gate for the CASCADE regime.",
     "gates", True),
    ("engine", "V10_NORMAL_MIN_P", "float", "0.65",
     "Minimum probability gate for the NORMAL regime.",
     "gates", True),
    ("engine", "V10_LOW_VOL_MIN_P", "float", "0.65",
     "Minimum probability gate for the LOW_VOL regime.",
     "gates", True),
    ("engine", "V10_TRENDING_MIN_P", "float", "0.72",
     "Minimum probability gate for the TRENDING regime.",
     "gates", True),
    ("engine", "V10_CALM_MIN_P", "float", "0.72",
     "Minimum probability gate for the CALM regime.",
     "gates", True),
    ("engine", "V10_MIN_EVAL_OFFSET", "int", "200",
     "Global maximum offset limit. NOTE: misnamed — this is the MAX, not MIN. See plan §10.6.",
     "gates", True),
    ("engine", "V10_NORMAL_MIN_OFFSET", "int", "0",
     "Minimum offset for NORMAL regime entries.",
     "gates", True),
    ("engine", "V10_TRANSITION_MAX_DOWN_OFFSET", "int", "0",
     "Maximum offset for DOWN-direction entries during the TRANSITION regime.",
     "gates", True),
    ("engine", "V10_DUNE_MODEL", "enum", "oak",
     "Model identifier for the DUNE scorer.",
     "gates", True),
    ("engine", "V10_DUNE_ENABLED", "bool", "false",
     "Master toggle for the DUNE confidence gate.",
     "gates", True),
    ("engine", "V10_CG_TAKER_GATE", "bool", "false",
     "Master toggle for the Coinglass taker-flow gate.",
     "gates", True),
    ("engine", "V10_CG_TAKER_OPPOSING_PCT", "float", "55",
     "Coinglass taker-flow opposing-side percentage threshold.",
     "gates", True),
    ("engine", "V10_CG_SMART_OPPOSING_PCT", "float", "52",
     "Coinglass smart-money opposing percentage threshold.",
     "gates", True),
    ("engine", "V10_CG_TAKER_OPPOSING_PENALTY", "float", "0.05",
     "Penalty applied when taker flow opposes the trade direction.",
     "gates", True),
    ("engine", "V10_CG_TAKER_ALIGNED_BONUS", "float", "0.02",
     "Bonus applied when taker flow aligns with the trade direction.",
     "gates", True),
    ("engine", "V10_CG_MAX_AGE_MS", "int", "120000",
     "Maximum staleness (ms) for Coinglass data before the gate is skipped.",
     "gates", True),
    ("engine", "V10_CG_CONFIRM_BONUS", "float", "0.03",
     "Bonus applied by CGConfirmationGate when 2/3 sources confirm.",
     "gates", True),
    ("engine", "V10_CG_ZERO_CONFIRM_PENALTY", "float", "0.02",
     "Penalty applied when zero CG sources confirm the direction.",
     "gates", True),
    ("engine", "V10_CG_CONFIRM_MIN", "int", "2",
     "Minimum number of CG confirmations required for the bonus.",
     "gates", True),
    ("engine", "V10_MAX_SPREAD_PCT", "float", "8",
     "Maximum Polymarket spread percentage allowed by SpreadGate.",
     "gates", True),
    ("engine", "V10_CAP_SCALE_BASE", "float", "0.48",
     "Base value for confidence-scaled position cap.",
     "gates", True),
    ("engine", "V10_CAP_SCALE_CEILING", "float", "0.72",
     "Ceiling for confidence-scaled position cap.",
     "gates", True),
    ("engine", "V10_CAP_SCALE_MIN_CONF", "float", "0.65",
     "Minimum confidence at which cap scaling begins.",
     "gates", True),
    ("engine", "V10_CAP_SCALE_MAX_CONF", "float", "0.88",
     "Confidence at which cap scaling reaches its ceiling.",
     "gates", True),
    ("engine", "V10_DUNE_CAP_FLOOR", "float", "0.35",
     "Floor on the DUNE-derived position cap.",
     "gates", True),
    ("engine", "V10_EARLY_ENTRY_CAP_MAX", "float", "0.63",
     "Maximum cap for early-entry trades.",
     "gates", True),
    ("engine", "V10_EARLY_ENTRY_OFFSET", "int", "180",
     "Offset (seconds) at which early entry is allowed.",
     "gates", True),
    ("engine", "FIVE_MIN_EVAL_INTERVAL", "int", "10",
     "Evaluation tick interval (seconds) for the 5m strategy.",
     "execution", False),
    ("engine", "V11_POLY_SPOT_ONLY_CONSENSUS", "bool", "false",
     "DQ-01 spot-only consensus mode for SourceAgreementGate. False = v11.1 2/3 majority.",
     "gates", True),
    ("engine", "TELEGRAM_ALERTS_PAPER", "bool", "true",
     "Route paper-mode trade alerts to Telegram.",
     "infrastructure", False),
    ("engine", "TELEGRAM_ALERTS_LIVE", "bool", "false",
     "Route live-mode trade alerts to Telegram.",
     "infrastructure", False),
]


# ─── margin_engine — 41 keys (per plan §4.2.2) ────────────────────────────────

_MARGIN_ENGINE_KEYS: list[tuple] = [
    ("margin_engine", "MARGIN_PAPER_FEE_RATE", "float", "0.001",
     "Binance paper-mode fee rate.",
     "venue", False),
    ("margin_engine", "MARGIN_PAPER_SPREAD_BPS", "float", "2.0",
     "Binance paper-mode spread in basis points.",
     "venue", False),
    ("margin_engine", "MARGIN_HYPERLIQUID_PAPER_FEE_RATE", "float", "0.00045",
     "Hyperliquid paper-mode fee rate.",
     "venue", False),
    ("margin_engine", "MARGIN_HYPERLIQUID_PAPER_SPREAD_BPS", "float", "1.0",
     "Hyperliquid paper-mode spread in basis points.",
     "venue", False),
    ("margin_engine", "MARGIN_PAPER_FEE_RATE_OVERRIDE", "float", "",
     "Explicit fee rate override (overrides venue default if set).",
     "venue", False),
    ("margin_engine", "MARGIN_PAPER_SPREAD_BPS_OVERRIDE", "float", "",
     "Explicit spread override in basis points (overrides venue default if set).",
     "venue", False),
    ("margin_engine", "MARGIN_HYPERLIQUID_ASSET", "string", "BTC",
     "Hyperliquid trading asset.",
     "strategy", False),
    ("margin_engine", "MARGIN_HYPERLIQUID_POLL_INTERVAL_S", "float", "2.0",
     "Hyperliquid price poll cadence in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_HYPERLIQUID_PRICE_FRESHNESS_S", "float", "15.0",
     "Hyperliquid price staleness threshold in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_ENGINE_USE_V4_ACTIONS", "bool", "false",
     "Master toggle for the v4 fusion-surface gate stack.",
     "gates", False),
    ("margin_engine", "MARGIN_V4_PRIMARY_TIMESCALE", "enum", "15m",
     "Primary timescale for v4 decision surface.",
     "strategy", False),
    ("margin_engine", "MARGIN_V4_TIMESCALES", "string", "5m,15m,1h,4h",
     "CSV of timescales requested from the v4 fusion service.",
     "strategy", False),
    ("margin_engine", "MARGIN_V4_STRATEGY", "string", "fee_aware_15m",
     "Active v4 strategy key.",
     "strategy", False),
    ("margin_engine", "MARGIN_V4_POLL_INTERVAL_S", "float", "2.0",
     "v4 fusion service poll cadence in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_V4_FRESHNESS_S", "float", "10.0",
     "v4 fusion service staleness threshold in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_V4_ENTRY_EDGE", "float", "0.10",
     "Entry conviction threshold for v4 trades.",
     "thresholds", False),
    ("margin_engine", "MARGIN_V4_CONTINUATION_MIN_CONVICTION", "float", "0.10",
     "Minimum conviction required for a v4 continuation entry.",
     "thresholds", False),
    ("margin_engine", "MARGIN_V4_CONTINUATION_MAX", "int", "",
     "Optional cap on consecutive v4 continuation entries.",
     "execution", False),
    ("margin_engine", "MARGIN_V4_MIN_EXPECTED_MOVE_BPS", "float", "15.0",
     "Minimum expected price move in basis points (fee wall).",
     "thresholds", False),
    ("margin_engine", "MARGIN_V4_ALLOW_MEAN_REVERTING", "bool", "false",
     "Allow mean-reverting v4 trades.",
     "gates", False),
    ("margin_engine", "MARGIN_V4_EVENT_EXIT_SECONDS", "int", "120",
     "Force-exit window for event-driven v4 trades.",
     "execution", False),
    ("margin_engine", "MARGIN_V4_MACRO_MODE", "enum", "advisory",
     "Macro overlay mode: veto / advisory.",
     "gates", False),
    ("margin_engine", "MARGIN_V4_MACRO_HARD_VETO_CONFIDENCE_FLOOR", "int", "80",
     "Confidence floor required to apply a macro hard veto.",
     "gates", False),
    ("margin_engine", "MARGIN_V4_MACRO_ADVISORY_SIZE_MULT_ON_CONFLICT", "float", "0.75",
     "Size multiplier applied when macro advisory conflicts with the trade.",
     "sizing", False),
    ("margin_engine", "MARGIN_V4_ALLOW_NO_EDGE_IF_EXP_MOVE_BPS_GTE", "float", "",
     "Override that allows NO_EDGE trades if expected move exceeds this bps threshold.",
     "gates", False),
    ("margin_engine", "MARGIN_V4_MAX_MARK_DIVERGENCE_BPS", "float", "0.0",
     "DQ-07 defensive mark-divergence gate threshold (basis points). 0.0 = disabled.",
     "gates", False),
    ("margin_engine", "MARGIN_REGIME_THRESHOLD", "float", "0.0",
     "v3 regime magnitude threshold.",
     "thresholds", False),
    ("margin_engine", "MARGIN_REGIME_TIMESCALE", "enum", "1h",
     "Regime detection timescale.",
     "strategy", False),
    ("margin_engine", "MARGIN_SIGNAL_THRESHOLD", "float", "0.50",
     "Legacy signal-strength gate threshold.",
     "thresholds", False),
    ("margin_engine", "MARGIN_PROBABILITY_ASSET", "string", "BTC",
     "v2 probability service asset.",
     "strategy", False),
    ("margin_engine", "MARGIN_PROBABILITY_TIMESCALE", "enum", "15m",
     "v2 probability timescale.",
     "strategy", False),
    ("margin_engine", "MARGIN_PROBABILITY_SECONDS_TO_CLOSE", "int", "480",
     "Seconds to window close used in v2 probability requests.",
     "execution", False),
    ("margin_engine", "MARGIN_PROBABILITY_POLL_INTERVAL_S", "float", "30.0",
     "v2 probability poll cadence in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_PROBABILITY_FRESHNESS_S", "float", "120.0",
     "v2 probability staleness threshold in seconds.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_PROBABILITY_MIN_CONVICTION", "float", "0.20",
     "Minimum v2 conviction required to act on a forecast.",
     "thresholds", False),
    ("margin_engine", "MARGIN_STARTING_CAPITAL", "float", "500.0",
     "Starting paper-mode capital.",
     "sizing", False),
    ("margin_engine", "MARGIN_LEVERAGE", "int", "3",
     "Fixed leverage for margin trades.",
     "sizing", False),
    ("margin_engine", "MARGIN_BET_FRACTION", "float", "0.02",
     "Per-trade bet fraction (Kelly-style).",
     "sizing", False),
    ("margin_engine", "MARGIN_MAX_OPEN_POSITIONS", "int", "1",
     "Maximum concurrent open positions.",
     "sizing", False),
    ("margin_engine", "MARGIN_MAX_EXPOSURE_PCT", "float", "0.20",
     "Maximum fraction of capital open across positions.",
     "sizing", False),
    ("margin_engine", "MARGIN_DAILY_LOSS_LIMIT_PCT", "float", "0.10",
     "Daily loss limit as a fraction of capital.",
     "sizing", False),
    ("margin_engine", "MARGIN_CONSECUTIVE_LOSS_COOLDOWN", "int", "3",
     "Consecutive losses that trigger a cooldown.",
     "sizing", False),
    ("margin_engine", "MARGIN_COOLDOWN_SECONDS", "int", "600",
     "Cooldown duration in seconds.",
     "sizing", False),
    ("margin_engine", "MARGIN_STOP_LOSS_PCT", "float", "0.006",
     "Stop loss percentage.",
     "execution", False),
    ("margin_engine", "MARGIN_TAKE_PROFIT_PCT", "float", "0.005",
     "Take profit percentage.",
     "execution", False),
    ("margin_engine", "MARGIN_TRAILING_STOP_PCT", "float", "0.003",
     "Trailing stop percentage.",
     "execution", False),
    ("margin_engine", "MARGIN_MAX_HOLD_SECONDS", "int", "900",
     "Maximum position hold duration in seconds.",
     "execution", False),
    ("margin_engine", "MARGIN_SIGNAL_REVERSAL_THRESHOLD", "float", "-10.0",
     "Legacy signal reversal threshold.",
     "thresholds", False),
    ("margin_engine", "MARGIN_TRADING_TIMESCALES", "string", "15m",
     "CSV list of timescales used for trading decisions.",
     "strategy", False),
    ("margin_engine", "MARGIN_TELEGRAM_ENABLED", "bool", "true",
     "Master toggle for Telegram alerts.",
     "infrastructure", False),
    ("margin_engine", "MARGIN_TICK_INTERVAL_S", "float", "2.0",
     "Position management tick cadence in seconds.",
     "infrastructure", False),
]


# ─── hub — 0 DB-managed keys (per plan §4.3) ──────────────────────────────────
#
# The hub authors config but does not consume DB-backed config in v1.
# This list is intentionally empty — placeholder for CFG-09 future tunables.

_HUB_KEYS: list[tuple] = []


# ─── data-collector — 7 keys (per plan §4.4) ─────────────────────────────────

_DATA_COLLECTOR_KEYS: list[tuple] = [
    ("data-collector", "MIN_REQUEST_INTERVAL", "float", "0.25",
     "Minimum interval between Polymarket gamma API requests in seconds.",
     "infrastructure", False),
    ("data-collector", "BACKOFF_BASE", "float", "1.0",
     "Base for exponential backoff on API errors (seconds).",
     "infrastructure", False),
    ("data-collector", "BACKOFF_MAX", "float", "60.0",
     "Maximum backoff cap on consecutive API errors (seconds).",
     "infrastructure", False),
    ("data-collector", "POLL_INTERVAL", "int", "1",
     "Main loop poll interval in seconds.",
     "infrastructure", False),
    ("data-collector", "RESOLUTION_DELAY", "int", "30",
     "Delay (seconds) between window close and resolution check.",
     "execution", False),
    ("data-collector", "ASSETS", "string", "BTC,ETH,SOL,XRP",
     "CSV list of assets to collect data for.",
     "strategy", False),
    ("data-collector", "TIMEFRAMES", "string", "5m,15m",
     "CSV list of timeframes to collect.",
     "strategy", False),
]


# ─── macro-observer — 6 keys (per plan §4.5) ──────────────────────────────────

_MACRO_OBSERVER_KEYS: list[tuple] = [
    ("macro-observer", "POLL_INTERVAL", "int", "60",
     "Main loop cadence in seconds.",
     "infrastructure", False),
    ("macro-observer", "QWEN_MODEL", "enum", "qwen35-122b-abliterated",
     "Qwen model slug for macro inference.",
     "strategy", False),
    ("macro-observer", "QWEN_MAX_TOKENS", "int", "1536",
     "Maximum completion tokens for Qwen requests.",
     "infrastructure", False),
    ("macro-observer", "QWEN_TIMEOUT_S", "float", "60",
     "HTTP timeout for Qwen requests in seconds.",
     "infrastructure", False),
    ("macro-observer", "ANTHROPIC_MODEL", "enum", "claude-sonnet-4-6",
     "Claude model slug used as the fallback macro inferencer.",
     "strategy", False),
    ("macro-observer", "EVAL_INTERVAL", "int", "60",
     "Evaluation cadence in seconds.",
     "infrastructure", False),
]


# ─── timesfm — 0 DB-managed, 0 in v1 (read-only display covered by hub later) ──
#
# Per plan §4.6, timesfm-service has no .env reads — its config is hardcoded
# in Python constructors. v1 ships zero rows; CFG-13 (out of scope) will
# add the read-only display path. This list is intentionally empty.

_TIMESFM_KEYS: list[tuple] = []


# Combined list — used by seed_config_keys() and the validator.
ALL_SEED_KEYS: list[tuple] = (
    _ENGINE_KEYS
    + _MARGIN_ENGINE_KEYS
    + _HUB_KEYS
    + _DATA_COLLECTOR_KEYS
    + _MACRO_OBSERVER_KEYS
    + _TIMESFM_KEYS
)


def validate_seed(rows: list[tuple]) -> None:
    """Reject seed lists that contain secrets or duplicates.

    Raises:
        ValueError: If any row's key matches SECRET_PATTERN, or if a
            (service, key) tuple appears more than once.
    """
    seen: set[tuple[str, str]] = set()
    for row in rows:
        service, key = row[0], row[1]
        if SECRET_PATTERN.match(key):
            raise ValueError(
                f"config_seed: refusing to seed secret-like key '{service}.{key}' "
                f"— secrets stay in .env per CONFIG_MIGRATION_PLAN.md §10.4"
            )
        pair = (service, key)
        if pair in seen:
            raise ValueError(
                f"config_seed: duplicate (service, key) tuple {pair} in seed list"
            )
        seen.add(pair)


def seed_summary() -> dict[str, int]:
    """Return per-service seed counts. Used by tests and the migration log."""
    counts: dict[str, int] = {}
    for row in ALL_SEED_KEYS:
        service = row[0]
        counts[service] = counts.get(service, 0) + 1
    return counts


async def seed_config_keys(session: AsyncSession) -> dict[str, int]:
    """Idempotently seed all 142 config_keys rows.

    Re-running this is safe: existing rows have their description / type /
    default / category fields refreshed, but their associated config_values
    rows are NOT touched. This is the same UPSERT-on-developer-fields,
    leave-operator-fields-alone pattern that lets the developer add a new
    key to the seed list without wiping operator-set values for other keys.

    Returns:
        Per-service count of keys upserted (for the migration log).

    Raises:
        ValueError: If validate_seed() fails.
    """
    validate_seed(ALL_SEED_KEYS)

    upsert_sql = text("""
        INSERT INTO config_keys (
            service, key, type, default_value, description,
            category, restart_required, editable_via_ui
        )
        VALUES (
            :service, :key, :type, :default_value, :description,
            :category, :restart_required, TRUE
        )
        ON CONFLICT (service, key) DO UPDATE SET
            type = EXCLUDED.type,
            default_value = EXCLUDED.default_value,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            restart_required = EXCLUDED.restart_required,
            updated_at = NOW()
    """)

    for row in ALL_SEED_KEYS:
        service, key, vtype, default_value, description, category, restart_required = row
        await session.execute(upsert_sql, {
            "service": service,
            "key": key,
            "type": vtype,
            "default_value": default_value,
            "description": description,
            "category": category,
            "restart_required": restart_required,
        })

    counts = seed_summary()
    log.info("config_seed.upserted", counts=counts, total=len(ALL_SEED_KEYS))
    return counts
