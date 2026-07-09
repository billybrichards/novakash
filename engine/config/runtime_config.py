"""
RuntimeConfig — Live-reloadable config that syncs from the trading_configs DB table.

Components read from the singleton `runtime` instance instead of module-level constants.
The orchestrator calls `runtime.sync(db_pool)` every heartbeat (~10s) to pull the
active config for the current mode (paper/live).

Priority: DB active config > env vars > code defaults.

Usage in components:
    from config.runtime_config import runtime
    stake = bankroll * runtime.bet_fraction
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional
from pathlib import Path

import structlog

# Load .env into os.environ here (at module import time, before RuntimeConfig() singleton)
# pydantic-settings loads .env into settings.* but NOT os.environ.
# runtime_config uses os.environ.get() directly — so we must load it ourselves.
try:
    from dotenv import load_dotenv as _load_dotenv

    _env_path = Path(__file__).parent.parent / ".env"
    if _env_path.exists():
        _load_dotenv(
            dotenv_path=str(_env_path), override=False
        )  # override=False: don't clobber existing env
except ImportError:
    pass

log = structlog.get_logger(__name__)


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


# ── Mapping: DB config key → (engine attribute, type converter) ────────────────
# Keys on the left are from trading_configs.config JSON.
# Attributes on the right are what engine components read.
_DB_KEY_MAP: dict[str, tuple[str, type]] = {
    "starting_bankroll": ("starting_bankroll", float),
    "bankroll": ("starting_bankroll", float),  # legacy DB key alias
    "bet_fraction": ("bet_fraction", float),
    "max_position_usd": ("max_position_usd", float),
    "absolute_max_bet": ("max_position_usd", float),
    "max_drawdown_pct": ("max_drawdown_kill", float),
    "daily_loss_limit": ("daily_loss_limit_usd", float),
    "vpin_informed_threshold": ("vpin_informed_threshold", float),
    "vpin_cascade_threshold": ("vpin_cascade_threshold", float),
    "vpin_bucket_size_usd": ("vpin_bucket_size_usd", float),
    "vpin_lookback_buckets": ("vpin_lookback_buckets", int),
    "five_min_vpin_gate": ("five_min_vpin_gate", float),
    "arb_min_spread": ("arb_min_spread", float),
    "arb_max_position": ("arb_max_position", float),
    "arb_max_execution_ms": ("arb_max_execution_ms", int),
    "enable_arb_strategy": ("arb_enabled", bool),
    "cascade_cooldown_seconds": ("cooldown_seconds", int),
    "cascade_min_liq_usd": ("cascade_liq_volume_threshold", float),
    "enable_cascade_strategy": ("cascade_enabled", bool),
    "polymarket_fee_mult": ("polymarket_fee_mult", float),
    "opinion_fee_mult": ("opinion_fee_mult", float),
    "preferred_venue": ("preferred_venue", str),
    "five_min_min_delta_pct": ("five_min_min_delta_pct", float),
    "five_min_cascade_min_delta_pct": ("five_min_cascade_min_delta_pct", float),
    "vpin_cascade_direction_threshold": ("vpin_cascade_direction_threshold", float),
    # SP-04 / SP-05: Strategy port settings (hot-reloadable)
    "V4_FUSION_MODE": ("v4_fusion_mode", str),
    "V10_GATE_MODE": ("v10_gate_mode", str),
    "V4_FUSION_ENABLED": ("v4_fusion_enabled", bool),
    "V10_6_MIN_EVAL_OFFSET_STRATEGY": ("v10_6_min_eval_offset", int),
    "V10_6_MAX_EVAL_OFFSET_STRATEGY": ("v10_6_max_eval_offset", int),
    # SIG-03/SIG-04: V4 DOWN-only strategy (DOWN filter + CLOB sizing)
    "V4_DOWN_ONLY_MODE": ("v4_down_only_mode", str),
    "V4_DOWN_ONLY_ENABLED": ("v4_down_only_enabled", bool),
    # SIG-05: V4 Asian UP strategy (UP-only, Asian session, medium conviction)
    "V4_UP_ASIAN_MODE": ("v4_up_asian_mode", str),
    "V4_UP_ASIAN_ENABLED": ("v4_up_asian_enabled", bool),
    # SP-06: Execution method resolution (hot-reloadable)
    "DEFAULT_EXEC_METHOD": ("default_exec_method", str),
}


class RuntimeConfig:
    """
    Singleton mutable config. All values initialised from env vars,
    then overridden by the active DB trading_config on each sync().

    Thread-safe: sync() writes atomically; readers get a consistent snapshot.
    """

    def __init__(self) -> None:
        # ── Risk ───────────────────────────────────────────────────────────
        self.starting_bankroll: float = _env_float("STARTING_BANKROLL", 29.0)
        self.bet_fraction: float = _env_float("BET_FRACTION", 0.025)
        self.max_position_usd: float = _env_float("MAX_POSITION_USD", 5.0)
        self.max_drawdown_kill: float = _env_float("MAX_DRAWDOWN_KILL", 0.45)
        self.daily_loss_limit_usd: float = _env_float("DAILY_LOSS_LIMIT_USD", 50.0)
        self.daily_loss_limit_pct: float = _env_float("DAILY_LOSS_LIMIT_PCT", 0.10)
        self.min_bet_usd: float = _env_float("MIN_BET_USD", 1.0)
        self.max_open_exposure_pct: float = _env_float("MAX_OPEN_EXPOSURE_PCT", 0.30)
        self.consecutive_loss_cooldown: int = _env_int("CONSECUTIVE_LOSS_COOLDOWN", 3)
        self.cooldown_seconds: int = _env_int("COOLDOWN_SECONDS", 900)

        # ── VPIN ──────────────────────────────────────────────────────────
        self.vpin_bucket_size_usd: float = _env_float("VPIN_BUCKET_SIZE_USD", 500_000)
        self.vpin_lookback_buckets: int = _env_int("VPIN_LOOKBACK_BUCKETS", 50)
        self.vpin_informed_threshold: float = _env_float(
            "VPIN_INFORMED_THRESHOLD", 0.55
        )
        self.vpin_cascade_threshold: float = _env_float("VPIN_CASCADE_THRESHOLD", 0.70)
        self.vpin_cascade_direction_threshold: float = _env_float(
            "VPIN_CASCADE_DIRECTION_THRESHOLD", 0.65
        )

        # ── Cascade ───────────────────────────────────────────────────────
        self.cascade_oi_drop_threshold: float = _env_float(
            "CASCADE_OI_DROP_THRESHOLD", 0.02
        )
        self.cascade_liq_volume_threshold: float = _env_float(
            "CASCADE_LIQ_VOLUME_THRESHOLD", 5e6
        )
        self.cascade_enabled: bool = True

        # ── Arb ───────────────────────────────────────────────────────────
        self.arb_min_spread: float = _env_float("ARB_MIN_SPREAD", 0.015)
        self.arb_max_position: float = _env_float("ARB_MAX_POSITION", 50.0)
        self.arb_max_execution_ms: int = _env_int("ARB_MAX_EXECUTION_MS", 500)
        self.arb_enabled: bool = True

        # ── Fees ──────────────────────────────────────────────────────────
        self.polymarket_fee_mult: float = _env_float("POLYMARKET_FEE_MULT", 0.072)
        self.opinion_fee_mult: float = _env_float("OPINION_FEE_MULT", 0.04)
        self.preferred_venue: str = os.environ.get("PREFERRED_VENUE", "opinion")

        # ── 5-Min (these stay env-only for now — structural, not tunable) ─
        # Read from env first, then .env file fallback
        _five_min_env = os.environ.get("FIVE_MIN_ENABLED", "")
        if not _five_min_env:
            from pathlib import Path

            _env_file = Path(__file__).parent.parent / ".env"
            if _env_file.exists():
                with open(_env_file) as f:
                    for line in f:
                        if line.startswith("FIVE_MIN_ENABLED="):
                            _five_min_env = line.split("=", 1)[1].strip()
                            break
        self.five_min_enabled: bool = _five_min_env.lower() == "true"
        self.five_min_assets: list[str] = os.environ.get(
            "FIVE_MIN_ASSETS", "BTC"
        ).split(",")
        self.five_min_mode: str = os.environ.get("FIVE_MIN_MODE", "safe")
        self.five_min_entry_offset: int = _env_int("FIVE_MIN_ENTRY_OFFSET", 10)
        self.five_min_min_confidence: float = _env_float(
            "FIVE_MIN_MIN_CONFIDENCE", 0.30
        )
        self.five_min_min_delta_pct: float = _env_float("FIVE_MIN_MIN_DELTA_PCT", 0.08)
        self.five_min_cascade_min_delta_pct: float = _env_float(
            "FIVE_MIN_CASCADE_MIN_DELTA_PCT", 0.03
        )
        self.five_min_vpin_gate: float = _env_float("FIVE_MIN_VPIN_GATE", 0.45)
        self.five_min_max_entry_price: float = _env_float(
            "FIVE_MIN_MAX_ENTRY_PRICE", 0.70
        )
        self.fifteen_min_max_entry_price: float = _env_float(
            "FIFTEEN_MIN_MAX_ENTRY_PRICE", 0.70
        )

        # ── Window ────────────────────────────────────────────────────────
        self.poly_window_seconds: int = _env_int("POLY_WINDOW_SECONDS", 300)

        # ── v6.0 TimesFM-Only Strategy ────────────────────────────────────
        # Audit #397 (review fix 397-1): mirror the URL-implies-enabled logic
        # from infrastructure/composition.py here. Without this fix
        # `runtime.timesfm_enabled` stays False even when TIMESFM_URL is set,
        # causing five_min_vpin's forecast gate at five_min_vpin.py:~900 to skip.
        # Logic:
        #   - TIMESFM_ENABLED=false (explicit)               -> False
        #   - TIMESFM_ENABLED=true  + URL unset              -> log error, False
        #   - TIMESFM_ENABLED=true  + URL set                -> True
        #   - TIMESFM_ENABLED unset + URL set                -> True (URL = intent)
        #   - TIMESFM_ENABLED unset + URL unset              -> False
        _timesfm_url_raw = os.environ.get("TIMESFM_URL", "").strip()
        _timesfm_enabled_raw = os.environ.get("TIMESFM_ENABLED", "").strip().lower()
        if _timesfm_enabled_raw == "false":
            self.timesfm_enabled = False
        elif _timesfm_enabled_raw == "true" and not _timesfm_url_raw:
            # Explicit enable but no URL — log and disable.
            try:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    "runtime_config.timesfm_url_missing_skip "
                    "TIMESFM_ENABLED=true but TIMESFM_URL is not set — "
                    "TimesFM gate disabled (audit #397 review fix 1)"
                )
            except Exception:
                pass
            self.timesfm_enabled = False
        elif _timesfm_enabled_raw == "true":
            self.timesfm_enabled = True
        else:
            # No explicit override — enable iff URL configured.
            self.timesfm_enabled = bool(_timesfm_url_raw)
        self.timesfm_url: str = _timesfm_url_raw or "http://3.96.151.28:8080"
        self.timesfm_min_confidence: float = _env_float("TIMESFM_MIN_CONFIDENCE", 0.30)
        self.timesfm_assets: list[str] = os.environ.get("TIMESFM_ASSETS", "BTC").split(
            ","
        )

        # ── Guardrails ────────────────────────────────────────────────────
        # G1: Staggered asset execution
        self.order_stagger_seconds: float = _env_float(
            "ORDER_STAGGER_SECONDS", 1.5
        )  # was 5.0, reduced — FOK fills are near-instant
        # G3: Single best signal mode (only trade the top-scoring asset per window)
        self.single_best_signal: bool = (
            os.environ.get("SINGLE_BEST_SIGNAL", "false").lower() == "true"
        )
        # G4: Order rate limiter
        self.max_orders_per_hour: int = _env_int("MAX_ORDERS_PER_HOUR", 10)
        self.min_order_interval_seconds: float = _env_float(
            "MIN_ORDER_INTERVAL_SECONDS", 4.0
        )

        # ── v8.0: Price source feature flag (env-only, not DB-synced) ────────
        # Controls which feed drives direction signal in five_min_vpin evaluate().
        # Values: 'chainlink' | 'tiingo' | 'binance' | 'consensus'
        # Default: 'chainlink' — Polymarket resolves on Chainlink oracle,
        # so using it for delta aligns prediction with resolution source.
        # Falls back to tiingo -> binance when chainlink unavailable.
        self.delta_price_source: str = os.environ.get(
            "DELTA_PRICE_SOURCE", "chainlink"
        ).lower()

        # ── v8.0 Phase 2: FOK Execution Ladder (env-only, default ON) ───────────
        # FOK_ENABLED: replace GTC single-order with FOK attempt ladder.
        # When true: FOKLadder.execute() used for order placement.
        # When false: legacy GTC/GTD path used (fallback).
        self.fok_enabled: bool = os.environ.get("FOK_ENABLED", "true").lower() == "true"

        # ── v8.0 Phase 3: Gate feature flags (env-only, default OFF) ─────────
        # TWAP_OVERRIDE_ENABLED: allow TWAP+Gamma to override point-delta direction.
        # Disabled: TWAP blocked 12 windows, 8 were winners — net harmful.
        # With Tiingo as delta source, TWAP direction is redundant.
        self.twap_override_enabled: bool = (
            os.environ.get("TWAP_OVERRIDE_ENABLED", "false").lower() == "true"
        )

        # TWAP_GAMMA_GATE_ENABLED: allow TWAP should_skip to return None early.
        # Disabled: gate was blocking more winners than losers.
        self.twap_gamma_gate_enabled: bool = (
            os.environ.get("TWAP_GAMMA_GATE_ENABLED", "false").lower() == "true"
        )

        # TIMESFM_AGREEMENT_ENABLED: allow TimesFM forecast to gate/modify confidence.
        # Disabled: TimesFM accuracy 47.8% — worse than coin flip as a gate.
        # Forecast is still fetched and logged for monitoring when timesfm_enabled=True.
        self.timesfm_agreement_enabled: bool = (
            os.environ.get("TIMESFM_AGREEMENT_ENABLED", "false").lower() == "true"
        )

        # ── v9.0: Source agreement + dynamic caps ───────────────────────────
        # V9_SOURCE_AGREEMENT: CL+TI direction must agree (94.7% WR when agree, 9.1% when disagree)
        self.v9_source_agreement: bool = (
            os.environ.get("V9_SOURCE_AGREEMENT", "false").lower() == "true"
        )
        # V9_CAPS_ENABLED: Two-tier dynamic caps based on empirical agreement WR
        self.v9_caps_enabled: bool = (
            os.environ.get("V9_CAPS_ENABLED", "false").lower() == "true"
        )
        # ORDER_TYPE: FAK (Fill-And-Kill), FOK (Fill-Or-Kill), or GTC
        self.order_type: str = os.environ.get("ORDER_TYPE", "FAK").upper()

        # ── v10: DUNE-gated dynamic pricing ──────────────────────────────
        # V10_DUNE_ENABLED: Use DUNE ML model as confidence gate (replaces VPIN)
        self.v10_dune_enabled: bool = (
            os.environ.get("V10_DUNE_ENABLED", "false").lower() == "true"
        )
        # V10_DUNE_MIN_P: Minimum DUNE P(direction) to trade
        self.v10_dune_min_p: float = float(os.environ.get("V10_DUNE_MIN_P", "0.65"))
        # FIVE_MIN_EVAL_INTERVAL: Seconds between eval ticks (2 = 2s polling, 10 = v9 default)
        self.five_min_eval_interval: int = int(
            os.environ.get("FIVE_MIN_EVAL_INTERVAL", "10")
        )

        # ── v10.6: Sequoia v5 decision surface (DS-01 — eval_offset bounds) ─
        # V10_6_ENABLED: master flag for the v10.6 decision surface.
        # DEFAULTS OFF — merging this PR to develop is zero-behaviour-change
        # in production. Operator flips this on the host to enable. Matches
        # the MARGIN_ENGINE_USE_V4_ACTIONS pattern from margin_engine PR #16.
        self.v10_6_enabled: bool = (
            os.environ.get("V10_6_ENABLED", "false").lower() == "true"
        )
        # V10_6_MIN_EVAL_OFFSET / V10_6_MAX_EVAL_OFFSET: inclusive bounds on
        # ctx.eval_offset for the v10.6 EvalOffsetBoundsGate.
        #
        # ⚠ Namespaced under V10_6_ on purpose: the existing
        # DuneConfidenceGate already reads V10_MIN_EVAL_OFFSET with
        # OPPOSITE semantics (there it acts as a MAXIMUM offset,
        # production value 180/200). Reusing that name here would
        # silently repurpose the variable on flag flip and break trading.
        # See gates.py EvalOffsetBoundsGate docstring for rationale.
        #
        # Defaults (2026-04-20, Billy): widened to [30, 240] to match
        # v5_ensemble and v6_sniper's YAML timing windows. Prior values
        # were 90/180 per V10_6_DECISION_SURFACE_PROPOSAL.md §3.4 but
        # those bounds pre-dated v6_sniper and effectively capped both
        # strategies at T-60..T-180 regardless of YAML. Baking 30/240
        # into the code default makes the wider range durable across
        # deploys (tracked .env was being wiped on git reset).
        #   30  = refuse to trade closer than T-30 to window close
        #  240  = refuse to trade further than T-240 from window close
        self.v10_6_min_eval_offset: int = int(
            os.environ.get("V10_6_MIN_EVAL_OFFSET", "30")
        )
        self.v10_6_max_eval_offset: int = int(
            os.environ.get("V10_6_MAX_EVAL_OFFSET", "240")
        )

        # ── SP-04 / SP-05: Strategy port settings (DB-synced, hot-reloadable) ──
        # These default to env vars and are overridden by DB config on each sync().
        # V10_GATE_MODE / V4_FUSION_MODE: per-window mode for LIVE vs GHOST execution.
        # V4_FUSION_ENABLED: whether V4 fusion strategy is included in evaluation.
        self.use_strategy_port: bool = (
            os.environ.get("ENGINE_USE_STRATEGY_PORT", "false").lower() == "true"
        )
        self.v10_gate_mode: str = os.environ.get("V10_GATE_MODE", "LIVE").upper()
        self.v4_fusion_mode: str = os.environ.get("V4_FUSION_MODE", "GHOST").upper()
        self.v4_fusion_enabled: bool = (
            os.environ.get("V4_FUSION_ENABLED", "false").lower() == "true"
        )
        # SIG-03/SIG-04: V4 DOWN-only strategy (DOWN filter + CLOB sizing).
        # Default enabled=false (matches v4_fusion_enabled pattern — explicit opt-in via DB seed).
        # DB config_seed.py seeds V4_DOWN_ONLY_ENABLED=true so live envs enable it on first sync.
        self.v4_down_only_mode: str = os.environ.get(
            "V4_DOWN_ONLY_MODE", "LIVE"
        ).upper()
        self.v4_down_only_enabled: bool = (
            os.environ.get("V4_DOWN_ONLY_ENABLED", "false").lower() == "true"
        )
        # SIG-05: V4 Asian UP strategy (UP-only, Asian session 23-02 UTC, dist 0.15-0.20).
        # Discovered 2026-04-12: 81-99% WR (5,543 samples). Safe to run alongside v4_down_only.
        self.v4_up_asian_mode: str = os.environ.get("V4_UP_ASIAN_MODE", "LIVE").upper()
        self.v4_up_asian_enabled: bool = (
            os.environ.get("V4_UP_ASIAN_ENABLED", "false").lower() == "true"
        )

        # ── SP-06: Execution method (hot-reloadable, default fak_standard) ──
        # DEFAULT_EXEC_METHOD: env var override for the default execution method.
        #   Value: 'fak_standard' (default) or 'fak_epsilon'
        #   Priority: DB config > env var > 'fak_standard'
        self.default_exec_method: str = os.environ.get(
            "DEFAULT_EXEC_METHOD", "fak_standard"
        ).lower()

        # EPSILON_ENABLED: Global master switch for epsilon ladder.
        #   Must be 'true' for any epsilon ladder to activate (even if DB says so).
        #   This is a kill-switch — prevents accidental epsilon activation on prod.
        self.epsilon_enabled: bool = (
            os.environ.get("EPSILON_ENABLED", "false").lower() == "true"
        )

        # ── PR #464 sister: Polymarket canonical priceToBeat rollout ──────
        # OPEN_PRICE_USE_PRICE_TO_BEAT controls whether downstream consumers
        # (timesfm-service primarily) recompute their delta_* family against
        # the Polymarket-published canonical reference price (set by PR #464
        # on WindowInfo.open_price when source == "polymarket_priceToBeat",
        # propagated to StrategyContext.polymarket_price_to_beat and the
        # V5FeatureBody.polymarket_price_to_beat field of the JSON pushed
        # to /v2/probability).
        #
        # Default OFF — populating the field is zero-behaviour-change for
        # the engine itself: it's pure telemetry on the wire. The flag is
        # READ by the timesfm-service sister-PR, NOT gated on inside the
        # engine. The engine always populates the field when available so
        # the operator can flip the flag on the scorer side without redeploy
        # of the engine.
        #
        # Coordinated rollout:
        #   1. Engine merges PR #464 + this sister change → wire carries
        #      polymarket_price_to_beat. timesfm ignores it (flag off
        #      everywhere). No behaviour change.
        #   2. timesfm sister-PR merges → it READS the field but only USES
        #      it when OPEN_PRICE_USE_PRICE_TO_BEAT=true on its env. Flag
        #      stays default false. No behaviour change.
        #   3. Operator flips OPEN_PRICE_USE_PRICE_TO_BEAT=true on the
        #      scorer host. Single env flip, no code change. Cutover.
        #   4. After bake-in, default flips to true in a follow-up PR and
        #      the override env var is retired.
        #
        # Engine-side reads of this flag should be limited to telemetry /
        # observability (e.g. tagging which path is "expected" downstream).
        # Strategies must NOT change behaviour on this flag — the entire
        # point is to keep the cutover surgical to the scorer's delta math.
        self.open_price_use_price_to_beat: bool = (
            os.environ.get("OPEN_PRICE_USE_PRICE_TO_BEAT", "false").lower() == "true"
        )

        # ── Sync metadata ─────────────────────────────────────────────────
        self._active_config_id: Optional[int] = None
        self._active_config_name: Optional[str] = None
        self._sync_count: int = 0
        self._last_sync_error: Optional[str] = None

    def config_get_exec_method(self, default: str = "fak_standard") -> str:
        """Resolve the effective execution method with full priority chain.
        
        Priority order (highest wins):
          1. DB config `DEFAULT_EXEC_METHOD` column (via sync)
          2. runtime.default_exec_method (env var DEFAULT_EXEC_METHOD)
          3. Passed `default` arg (always 'fak_standard')
        
        Only returns 'fak_epsilon' if runtime.epsilon_enabled is True.
        """
        candidate = self.default_exec_method
        if candidate == "fak_epsilon" and not self.epsilon_enabled:
            candidate = "fak_standard"
        return candidate

    async def sync(self, pool, paper_mode: bool = True) -> bool:
        """
        Pull the active trading_config for the current mode from the DB.
        Overlays DB values onto this instance.

        Returns True if config was updated, False if no change or error.
        """
        # Skip DB sync if env var says so — use pure env var config
        if os.environ.get("SKIP_DB_CONFIG_SYNC") == "true":
            log.info("runtime_config.skip_db_sync", reason="SKIP_DB_CONFIG_SYNC=true")
            return False

        mode = "paper" if paper_mode else "live"

        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, name, config
                    FROM trading_configs
                    WHERE mode = $1 AND is_active = TRUE
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    mode,
                )

            if row is None:
                # No active config for this mode — keep env var defaults
                if self._active_config_id is not None:
                    log.info("runtime_config.no_active_config", mode=mode)
                    self._active_config_id = None
                    self._active_config_name = None
                return False

            config_id = row["id"]
            config_name = row["name"]

            # Skip if same config version already loaded
            if config_id == self._active_config_id:
                return False

            config_data: dict = (
                row["config"]
                if isinstance(row["config"], dict)
                else json.loads(row["config"] or "{}")
            )

            # Apply DB values
            changes = []
            for db_key, (attr, converter) in _DB_KEY_MAP.items():
                if db_key in config_data:
                    try:
                        new_val = converter(config_data[db_key])
                        old_val = getattr(self, attr)
                        if old_val != new_val:
                            setattr(self, attr, new_val)
                            changes.append(f"{attr}: {old_val} → {new_val}")
                    except (ValueError, TypeError) as exc:
                        log.warning(
                            "runtime_config.bad_value",
                            key=db_key,
                            value=config_data[db_key],
                            error=str(exc),
                        )

            self._active_config_id = config_id
            self._active_config_name = config_name
            self._sync_count += 1
            self._last_sync_error = None

            if changes:
                log.info(
                    "runtime_config.synced",
                    config_id=config_id,
                    config_name=config_name,
                    mode=mode,
                    changes=changes,
                )
            else:
                log.info(
                    "runtime_config.loaded",
                    config_id=config_id,
                    config_name=config_name,
                    mode=mode,
                )

            return True

        except Exception as exc:
            self._last_sync_error = str(exc)
            log.error("runtime_config.sync_error", error=str(exc))
            return False

    def snapshot(self) -> dict[str, Any]:
        """Return a dict of all config values for logging/debugging."""
        return {
            "active_config_id": self._active_config_id,
            "active_config_name": self._active_config_name,
            "sync_count": self._sync_count,
            "bet_fraction": self.bet_fraction,
            "max_drawdown_kill": self.max_drawdown_kill,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "vpin_informed_threshold": self.vpin_informed_threshold,
            "vpin_cascade_threshold": self.vpin_cascade_threshold,
            "five_min_vpin_gate": self.five_min_vpin_gate,
            "arb_enabled": self.arb_enabled,
            "cascade_enabled": self.cascade_enabled,
            "preferred_venue": self.preferred_venue,
            # Execution
            "execution_method": self.default_exec_method,
            "epsilon_enabled": self.epsilon_enabled,
            "fok_enabled": self.fok_enabled,
            # Guardrails
            "order_stagger_seconds": self.order_stagger_seconds,
            "single_best_signal": self.single_best_signal,
            "max_orders_per_hour": self.max_orders_per_hour,
            "min_order_interval_seconds": self.min_order_interval_seconds,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
runtime = RuntimeConfig()
