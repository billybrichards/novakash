"""PostgreSQL shadow decision repo — implements ``ShadowDecisionRepository``.

Persists every (LIVE + GHOST) StrategyDecision for a window so the
post-resolve shadow report can reconstruct per-strategy outcomes.

Schema: ``shadow_decisions`` table (see Alembic migration
``XXX_create_shadow_decisions.py``).
"""
from __future__ import annotations

import json
from typing import Optional

import asyncpg
import structlog

from domain.ports import ShadowDecisionRepository
from domain.value_objects import StrategyDecision, WindowKey

log = structlog.get_logger(__name__)


class PgShadowDecisionRepository(ShadowDecisionRepository):
    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_client: Optional[object] = None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            return getattr(self._db_client, "_pool", None)
        return None

    async def save(
        self,
        window_key: WindowKey,
        decisions: list[StrategyDecision],
    ) -> None:
        pool = self._get_pool()
        if not pool or not decisions:
            return
        window_id = window_key.key
        timeframe = window_key.timeframe
        try:
            async with pool.acquire() as conn:
                for d in decisions:
                    mode = "GHOST"
                    stake_usdc = None
                    if d.metadata:
                        mode = str(d.metadata.get("mode", "GHOST"))
                        if "stake_usdc" in d.metadata:
                            stake_usdc = d.metadata["stake_usdc"]
                    await conn.execute(
                        """
                        INSERT INTO shadow_decisions (
                            window_id, timeframe, strategy_id, strategy_version,
                            mode, action, direction, confidence, confidence_score,
                            entry_reason, skip_reason, gate_results, metadata
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13
                        )
                        ON CONFLICT (window_id, strategy_id) DO UPDATE SET
                            strategy_version = EXCLUDED.strategy_version,
                            mode = EXCLUDED.mode,
                            action = EXCLUDED.action,
                            direction = EXCLUDED.direction,
                            confidence = EXCLUDED.confidence,
                            confidence_score = EXCLUDED.confidence_score,
                            entry_reason = EXCLUDED.entry_reason,
                            skip_reason = EXCLUDED.skip_reason,
                            gate_results = EXCLUDED.gate_results,
                            metadata = EXCLUDED.metadata,
                            evaluated_at = NOW()
                        """,
                        window_id,
                        timeframe,
                        d.strategy_id,
                        d.strategy_version,
                        mode,
                        d.action,
                        d.direction,
                        d.confidence,
                        d.confidence_score,
                        d.entry_reason,
                        d.skip_reason,
                        json.dumps(
                            d.metadata.get("gate_results", []) if d.metadata else []
                        ),
                        json.dumps(d.metadata or {}),
                    )
        except Exception as exc:
            log.warning(
                "pg_shadow_decision_repo.save_failed",
                error=str(exc)[:200],
                window_id=window_id,
            )

    async def find_by_window(
        self,
        window_key: WindowKey,
    ) -> list[StrategyDecision]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT strategy_id, strategy_version, mode, action, direction,
                           confidence, confidence_score, entry_reason, skip_reason,
                           gate_results, metadata
                    FROM shadow_decisions
                    WHERE window_id = $1
                    ORDER BY strategy_id
                    """,
                    window_key.key,
                )
                return [self._row_to_decision(r) for r in rows]
        except Exception as exc:
            log.warning(
                "pg_shadow_decision_repo.find_by_window_failed",
                error=str(exc)[:200],
                window_id=window_key.key,
            )
            return []

    async def find_by_strategy(
        self,
        strategy_id: str,
        since_unix: int,
        limit: int = 1000,
    ) -> list[StrategyDecision]:
        pool = self._get_pool()
        if not pool:
            return []
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT strategy_id, strategy_version, mode, action, direction,
                           confidence, confidence_score, entry_reason, skip_reason,
                           gate_results, metadata
                    FROM shadow_decisions
                    WHERE strategy_id = $1
                      AND evaluated_at >= to_timestamp($2)
                    ORDER BY evaluated_at DESC
                    LIMIT $3
                    """,
                    strategy_id,
                    since_unix,
                    limit,
                )
                return [self._row_to_decision(r) for r in rows]
        except Exception as exc:
            log.warning(
                "pg_shadow_decision_repo.find_by_strategy_failed",
                error=str(exc)[:200],
            )
            return []

    @staticmethod
    def _row_to_decision(r) -> StrategyDecision:
        meta_raw = r["metadata"]
        gates_raw = r["gate_results"]
        try:
            meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        except Exception:
            meta = {}
        try:
            gates = json.loads(gates_raw) if isinstance(gates_raw, str) else (gates_raw or [])
        except Exception:
            gates = []
        # Store mode + gate_results back into metadata so callers have it.
        meta = dict(meta or {})
        meta["mode"] = r["mode"]
        meta["gate_results"] = gates
        return StrategyDecision(
            action=r["action"],
            direction=r["direction"],
            confidence=r["confidence"],
            confidence_score=r["confidence_score"],
            entry_cap=None,
            collateral_pct=None,
            strategy_id=r["strategy_id"],
            strategy_version=r["strategy_version"],
            entry_reason=r["entry_reason"] or "",
            skip_reason=r["skip_reason"],
            metadata=meta,
        )
