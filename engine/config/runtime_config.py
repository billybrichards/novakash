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

import structlog

log = structlog.get_logger(__name__)


def _env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


# ── Mapping: DB config key → (engine attribute, type converter) ────────────────
# Keys on the left are from trading_configs.config JSON.
# Attributes on the right are what engine components read.
_DB_KEY_MAP: dict[str, tuple[str, type]] = {
    "starting_bankroll":        ("starting_bankroll", float),
    "bet_fraction":             ("bet_fraction", float),
    "max_position_usd":         ("max_position_usd", float),
    "max_drawdown_pct":         ("max_drawdown_kill", float),
    "daily_loss_limit":         ("daily_loss_limit_usd", float),
    "vpin_informed_threshold":  ("vpin_informed_threshold", float),
    "vpin_cascade_threshold":   ("vpin_cascade_threshold", float),
    "vpin_bucket_size_usd":     ("vpin_bucket_size_usd", float),
    "vpin_lookback_buckets":    ("vpin_lookback_buckets", int),
    "five_min_vpin_gate":       ("five_min_vpin_gate", float),
    "arb_min_spread":           ("arb_min_spread", float),
    "arb_max_position":         ("arb_max_position", float),
    "arb_max_execution_ms":     ("arb_max_execution_ms", int),
    "enable_arb_strategy":      ("arb_enabled", bool),
    "cascade_cooldown_seconds": ("cooldown_seconds", int),
    "cascade_min_liq_usd":      ("cascade_liq_volume_threshold", float),
    "enable_cascade_strategy":  ("cascade_enabled", bool),
    "polymarket_fee_mult":      ("polymarket_fee_mult", float),
    "opinion_fee_mult":         ("opinion_fee_mult", float),
    "preferred_venue":          ("preferred_venue", str),
    "five_min_min_delta_pct":   ("five_min_min_delta_pct", float),
    "five_min_cascade_min_delta_pct": ("five_min_cascade_min_delta_pct", float),
    "vpin_cascade_direction_threshold": ("vpin_cascade_direction_threshold", float),
}


class RuntimeConfig:
    """
    Singleton mutable config. All values initialised from env vars,
    then overridden by the active DB trading_config on each sync().

    Thread-safe: sync() writes atomically; readers get a consistent snapshot.
    """

    def __init__(self) -> None:
        # ── Risk ───────────────────────────────────────────────────────────
        self.starting_bankroll: float = _env_float("STARTING_BANKROLL", 500.0)
        self.bet_fraction: float = _env_float("BET_FRACTION", 0.025)
        self.max_position_usd: float = _env_float("MAX_POSITION_USD", 500.0)
        self.max_drawdown_kill: float = _env_float("MAX_DRAWDOWN_KILL", 0.45)
        self.daily_loss_limit_usd: float = _env_float("DAILY_LOSS_LIMIT_USD", 50.0)
        self.daily_loss_limit_pct: float = _env_float("DAILY_LOSS_LIMIT_PCT", 0.10)
        self.min_bet_usd: float = _env_float("MIN_BET_USD", 2.0)
        self.max_open_exposure_pct: float = _env_float("MAX_OPEN_EXPOSURE_PCT", 0.30)
        self.consecutive_loss_cooldown: int = _env_int("CONSECUTIVE_LOSS_COOLDOWN", 3)
        self.cooldown_seconds: int = _env_int("COOLDOWN_SECONDS", 900)

        # ── VPIN ──────────────────────────────────────────────────────────
        self.vpin_bucket_size_usd: float = _env_float("VPIN_BUCKET_SIZE_USD", 500_000)
        self.vpin_lookback_buckets: int = _env_int("VPIN_LOOKBACK_BUCKETS", 50)
        self.vpin_informed_threshold: float = _env_float("VPIN_INFORMED_THRESHOLD", 0.55)
        self.vpin_cascade_threshold: float = _env_float("VPIN_CASCADE_THRESHOLD", 0.70)
        self.vpin_cascade_direction_threshold: float = _env_float("VPIN_CASCADE_DIRECTION_THRESHOLD", 0.65)

        # ── Cascade ───────────────────────────────────────────────────────
        self.cascade_oi_drop_threshold: float = _env_float("CASCADE_OI_DROP_THRESHOLD", 0.02)
        self.cascade_liq_volume_threshold: float = _env_float("CASCADE_LIQ_VOLUME_THRESHOLD", 5e6)
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
        self.five_min_enabled: bool = os.environ.get("FIVE_MIN_ENABLED", "false").lower() == "true"
        self.five_min_assets: list[str] = os.environ.get("FIVE_MIN_ASSETS", "BTC").split(",")
        self.five_min_mode: str = os.environ.get("FIVE_MIN_MODE", "safe")
        self.five_min_entry_offset: int = _env_int("FIVE_MIN_ENTRY_OFFSET", 10)
        self.five_min_min_confidence: float = _env_float("FIVE_MIN_MIN_CONFIDENCE", 0.30)
        self.five_min_min_delta_pct: float = _env_float("FIVE_MIN_MIN_DELTA_PCT", 0.08)
        self.five_min_cascade_min_delta_pct: float = _env_float("FIVE_MIN_CASCADE_MIN_DELTA_PCT", 0.03)
        self.five_min_vpin_gate: float = _env_float("FIVE_MIN_VPIN_GATE", 0.45)

        # ── Window ────────────────────────────────────────────────────────
        self.poly_window_seconds: int = _env_int("POLY_WINDOW_SECONDS", 300)

        # ── Guardrails ────────────────────────────────────────────────────
        # G1: Staggered asset execution
        self.order_stagger_seconds: float = _env_float("ORDER_STAGGER_SECONDS", 5.0)
        # G3: Single best signal mode (only trade the top-scoring asset per window)
        self.single_best_signal: bool = os.environ.get("SINGLE_BEST_SIGNAL", "false").lower() == "true"
        # G4: Order rate limiter
        self.max_orders_per_hour: int = _env_int("MAX_ORDERS_PER_HOUR", 10)
        self.min_order_interval_seconds: float = _env_float("MIN_ORDER_INTERVAL_SECONDS", 4.0)

        # ── Sync metadata ─────────────────────────────────────────────────
        self._active_config_id: Optional[int] = None
        self._active_config_name: Optional[str] = None
        self._sync_count: int = 0
        self._last_sync_error: Optional[str] = None

    async def sync(self, pool, paper_mode: bool = True) -> bool:
        """
        Pull the active trading_config for the current mode from the DB.
        Overlays DB values onto this instance.

        Returns True if config was updated, False if no change or error.
        """
        # Skip DB sync if env var says so — use pure env var config
        if os.environ.get('SKIP_DB_CONFIG_SYNC') == 'true':
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

            config_data: dict = row["config"] if isinstance(row["config"], dict) else json.loads(row["config"] or "{}")

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
            # Guardrails
            "order_stagger_seconds": self.order_stagger_seconds,
            "single_best_signal": self.single_best_signal,
            "max_orders_per_hour": self.max_orders_per_hour,
            "min_order_interval_seconds": self.min_order_interval_seconds,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────
runtime = RuntimeConfig()
