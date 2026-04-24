"""Runtime override cache for strategy mode + gate_params (audit #291).

Reads the ``strategy_runtime_overrides`` DB table (see
``hub/db/migrations/versions/20260424_01_strategy_runtime_overrides.sql``)
and returns an effective override for each ``strategy_id`` that is
**layered on top** of YAML + env in the same semantic order as
``reference_config_layering.md``:

    YAML defaults → env vars → DB runtime override → final

Design goals:

* **Zero config-reload latency** — operator flips GHOST↔LIVE via a hub
  API PATCH, engine picks it up on the next cache refresh (<= 30s),
  no engine restart / rsync / deploy required. This is the whole
  point of the table (cycle time 15-30 min → 5-30 s).

* **Thread-safe**, callable from inside ``_evaluate_one`` (runs on the
  engine's asyncio main loop). Lookups are O(1) dict reads.

* **Idempotent** — two concurrent refreshes produce identical state,
  refreshing with no DB change is a no-op beyond a DEBUG log line.

* **Graceful degradation** — DB pool unavailable, table missing, or
  connection error all fall through to "no override" rather than
  500-ing the evaluation loop. The engine must keep trading on the
  YAML baseline if the override surface goes dark.

* **Audit-trail logging** — every cache refresh that CHANGES state
  (added, removed, or mutated row) logs at INFO. Refreshes with no
  delta log at DEBUG. Startup logs once at INFO regardless.

Usage (from ``engine/strategies/registry.py``)::

    from strategies.runtime_override import get_runtime_override_manager

    mgr = get_runtime_override_manager(db_pool)  # singleton
    await mgr.start()  # kicks off periodic refresh

    # Per-evaluation:
    effective_mode = mgr.get_effective_mode(name, config.mode)
    effective_params = mgr.get_effective_params(name, config.gate_params)

The manager is a module-level singleton because the registry instance
lifecycle is tied to the engine process but the refresh task needs
persistent state across all evaluate calls. Tests patch
``_get_singleton()`` or construct ``RuntimeOverrideManager`` directly.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

try:
    import structlog  # type: ignore[import]
    _log = structlog.get_logger(__name__)
except Exception:  # pragma: no cover — tests without structlog
    import logging
    _log = logging.getLogger(__name__)


# Cache refresh interval — matches the "<= 35s end-to-end override latency"
# promise in the audit-task post-merge validation plan. Exposed as a module
# constant so tests can patch it without monkey-patching the manager.
DEFAULT_REFRESH_INTERVAL_SEC: float = 30.0


@dataclass(frozen=True)
class RuntimeOverride:
    """One row from strategy_runtime_overrides.

    ``mode`` may be None to mean "inherit YAML mode". ``params`` is a
    (possibly partial) dict of gate_params that will be **shallow-merged**
    on top of the YAML gate_params — i.e. YAML keys absent from
    ``params`` survive. Deep-merge is intentionally NOT performed; see the
    module docstring for the rationale (operators should set the full
    nested value for any param they override).
    """
    strategy_id: str
    mode: Optional[str]
    params: Optional[Dict[str, Any]]


@dataclass
class _CacheState:
    """Snapshot of the override table at a point in time.

    ``loaded_at`` is a monotonic clock timestamp; callers can use it to
    check staleness without racing on ``time.time()``.
    """
    overrides: Dict[str, RuntimeOverride] = field(default_factory=dict)
    loaded_at: float = 0.0
    # Marks the cache as "populated at least once". Distinguishes
    # "startup, no DB read yet" (never apply override, YAML wins) from
    # "table empty, confirmed by a successful read" (same effect, but
    # next refresh doesn't panic on zero rows).
    populated: bool = False


class RuntimeOverrideManager:
    """Cached lookup of per-strategy runtime overrides.

    Refreshed every ``refresh_interval_sec`` from the DB. Refresh runs as
    an asyncio background task started via ``start()`` — the registry
    calls this once at engine boot (after the DB pool is wired). If
    ``start()`` is never called (tests), the manager still works but
    never picks up DB changes, which is the correct behaviour for
    isolated unit tests.
    """

    def __init__(
        self,
        db_pool: Any = None,
        refresh_interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC,
    ) -> None:
        self._db_pool = db_pool
        self._refresh_interval_sec = refresh_interval_sec
        self._state = _CacheState()
        # ``threading.Lock`` (not asyncio) — _evaluate_one() is sync and
        # reads the cache without awaiting. The refresh task acquires
        # the same lock briefly on swap-in. Swap is a dict reassignment
        # so the critical section is microseconds.
        self._lock = threading.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        self._stopped = False

    # ─── Public read API (sync, called from evaluate hot path) ──────────

    def get_runtime_override(self, strategy_id: str) -> Optional[RuntimeOverride]:
        """Return the cached override for ``strategy_id`` or None."""
        with self._lock:
            return self._state.overrides.get(strategy_id)

    def get_effective_mode(self, strategy_id: str, yaml_mode: str) -> str:
        """Return the mode the engine should actually use.

        Override mode wins when present and non-null; otherwise YAML mode.
        Unknown override modes fall through to YAML (defensive — a typo in
        the DB shouldn't break an evaluation).
        """
        ov = self.get_runtime_override(strategy_id)
        if ov is None or ov.mode is None:
            return yaml_mode
        if ov.mode not in ("LIVE", "GHOST", "DISABLED"):
            _log.warning(
                "runtime_override.invalid_mode_ignored",
                strategy_id=strategy_id,
                override_mode=ov.mode,
                yaml_mode=yaml_mode,
            )
            return yaml_mode
        return ov.mode

    def get_effective_params(
        self,
        strategy_id: str,
        yaml_params: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Return the merged gate_params dict.

        YAML is the base; DB override keys shallow-merge on top. Keys
        only in YAML survive; keys only in DB are added; keys in both
        take the DB value. A null ``params`` in the DB row means "no
        param override" — YAML passes through unchanged.
        """
        base = dict(yaml_params or {})
        ov = self.get_runtime_override(strategy_id)
        if ov is None or ov.params is None:
            return base
        merged = base
        for key, value in ov.params.items():
            merged[key] = value
        return merged

    # ─── Refresh path ───────────────────────────────────────────────────

    async def refresh_once(self) -> bool:
        """Refresh the cache from the DB. Returns True on success.

        Safe to call concurrently — ``_apply_rows`` takes the lock for
        the swap, so overlapping refreshes produce the "last write wins"
        snapshot with no partial state visible to readers.
        """
        if self._db_pool is None:
            # No DB wiring — stay on whatever cache we have (empty by
            # default). This is the correct path for unit tests and for
            # composition paths that don't wire persistence.
            return False

        try:
            async with self._db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT strategy_id, mode, params, updated_at,
                           updated_by, updated_reason
                    FROM strategy_runtime_overrides
                    """,
                )
        except Exception as exc:
            # Table missing (migration not yet applied) is the most
            # common error on first deploy — log once at WARNING so it's
            # visible but don't spam every refresh cycle.
            _log.warning(
                "runtime_override.refresh_error",
                error=str(exc)[:300],
            )
            return False

        new_overrides: Dict[str, RuntimeOverride] = {}
        for row in rows:
            try:
                raw_params = row["params"]
                if raw_params is None:
                    params = None
                elif isinstance(raw_params, (dict, list)):
                    # asyncpg jsonb decoder may already return dict
                    params = dict(raw_params) if isinstance(raw_params, dict) else None
                elif isinstance(raw_params, (str, bytes)):
                    decoded = json.loads(raw_params)
                    params = dict(decoded) if isinstance(decoded, dict) else None
                else:
                    params = None
            except Exception as exc:
                _log.warning(
                    "runtime_override.bad_params_row",
                    strategy_id=row["strategy_id"],
                    error=str(exc)[:200],
                )
                params = None

            new_overrides[row["strategy_id"]] = RuntimeOverride(
                strategy_id=row["strategy_id"],
                mode=row["mode"],
                params=params,
            )

        self._apply_rows(new_overrides)
        return True

    def _apply_rows(self, new_overrides: Dict[str, RuntimeOverride]) -> None:
        """Swap in a freshly-read snapshot and log a delta summary."""
        with self._lock:
            old = self._state.overrides
            self._state = _CacheState(
                overrides=new_overrides,
                loaded_at=time.monotonic(),
                populated=True,
            )

        added = [sid for sid in new_overrides if sid not in old]
        removed = [sid for sid in old if sid not in new_overrides]
        mutated = [
            sid for sid, ov in new_overrides.items()
            if sid in old and (
                old[sid].mode != ov.mode or old[sid].params != ov.params
            )
        ]
        if added or removed or mutated:
            _log.info(
                "runtime_override.cache_refreshed",
                added=added,
                removed=removed,
                mutated=mutated,
                total=len(new_overrides),
            )
        else:
            _log.debug(
                "runtime_override.cache_refreshed_no_delta",
                total=len(new_overrides),
            )

    # ─── Background refresh loop ────────────────────────────────────────

    async def start(self) -> None:
        """Kick off the periodic refresh task. Idempotent."""
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        # First refresh is awaited inline so the engine boots with a
        # populated cache — prevents a 30s "YAML-only" window after
        # restart during which a staged override would be invisible.
        ok = await self.refresh_once()
        _log.info(
            "runtime_override.manager_started",
            first_refresh_ok=ok,
            total_overrides=len(self._state.overrides),
            refresh_interval_sec=self._refresh_interval_sec,
        )
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="runtime_override_refresh"
        )

    async def stop(self) -> None:
        """Cancel the refresh task. Idempotent."""
        self._stopped = True
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except (asyncio.CancelledError, Exception):
                pass
            self._refresh_task = None

    async def _refresh_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self._refresh_interval_sec)
            except asyncio.CancelledError:
                return
            if self._stopped:
                return
            try:
                await self.refresh_once()
            except Exception as exc:
                # Final belt-and-braces — a truly pathological refresh
                # error must not kill the loop. refresh_once already logs
                # ordinary DB errors; this catches programming errors
                # (e.g. malformed asyncpg pool at teardown).
                _log.error(
                    "runtime_override.refresh_loop_error",
                    error=str(exc)[:300],
                )


# ─── Module-level singleton wiring ──────────────────────────────────────

_singleton: Optional[RuntimeOverrideManager] = None


def get_runtime_override_manager(
    db_pool: Any = None,
    refresh_interval_sec: float = DEFAULT_REFRESH_INTERVAL_SEC,
) -> RuntimeOverrideManager:
    """Return the process-wide manager, creating it on first call.

    If called with a non-None ``db_pool`` after the singleton already
    exists without a pool (or with a different pool), the manager's
    pool is swapped in-place. This lets the engine boot the registry
    before the DB pool is wired (tests / staged boot) and upgrade
    later.
    """
    global _singleton
    if _singleton is None:
        _singleton = RuntimeOverrideManager(
            db_pool=db_pool,
            refresh_interval_sec=refresh_interval_sec,
        )
    elif db_pool is not None and _singleton._db_pool is not db_pool:
        _singleton._db_pool = db_pool
    return _singleton


def reset_singleton_for_tests() -> None:
    """Test-only: clear the module-level singleton.

    Called by unit tests that construct fresh managers or assert on
    clean-state behaviour. No production call site should use this.
    """
    global _singleton
    _singleton = None


def apply_runtime_overrides(strategy_id: str, yaml_mode: str, yaml_params: Optional[Dict[str, Any]]) -> tuple[str, Dict[str, Any]]:
    """Thin convenience wrapper for registry.py.

    Keeps the registry diff minimal — one function call that returns
    the effective (mode, params) tuple. All DB / cache logic stays in
    this module; registry never imports ``RuntimeOverrideManager``
    directly, so future refactors to the cache backend (e.g. Redis) are
    local to this file.
    """
    mgr = get_runtime_override_manager()
    return (
        mgr.get_effective_mode(strategy_id, yaml_mode),
        mgr.get_effective_params(strategy_id, yaml_params),
    )
