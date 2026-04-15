"""PostgreSQL Redeem Attempts Repository.

Implements :class:`engine.domain.ports.RedeemAttemptsRepository` by
writing to the ``redeem_attempts`` table.

Used by the Builder Relayer ``Redeemer`` to track per-condition_id
redeem outcomes and back off on repeatedly-failing positions so they
don't keep burning relayer quota on every 15-min sweep.

Migration: migrations/add_redeem_attempts_table.sql
PR: D (redeem attempts tracking).
"""

from __future__ import annotations

from typing import Optional

import asyncpg
import structlog

from domain.ports import RedeemAttemptsRepository

log = structlog.get_logger(__name__)


class PgRedeemAttemptsRepository(RedeemAttemptsRepository):
    """asyncpg-backed redeem-attempts repository.

    Never raises out of its methods — logs and returns a safe default
    instead. The Redeemer uses ``recent_failures`` on a hot code path
    (every sweep, every candidate position) so it must not crash the
    sweep on a transient DB hiccup.
    """

    def __init__(
        self,
        pool: Optional[asyncpg.Pool] = None,
        db_client: Optional[object] = None,
    ) -> None:
        self._pool = pool
        self._db_client = db_client  # lazy pool extraction (matches sibling repos)

    def _get_pool(self) -> Optional[asyncpg.Pool]:
        if self._pool:
            return self._pool
        if self._db_client:
            return getattr(self._db_client, "_pool", None)
        return None

    async def record(
        self,
        condition_id: str,
        outcome: str,
        tx_hash: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Insert one attempt row. Fire-and-forget semantics."""
        pool = self._get_pool()
        if not pool or not condition_id:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO redeem_attempts (
                        condition_id, outcome, tx_hash, error
                    ) VALUES ($1, $2, $3, $4)
                    """,
                    condition_id,
                    outcome,
                    tx_hash,
                    error[:500] if error else None,
                )
        except Exception as exc:
            log.warning(
                "pg_redeem_attempts.record_failed",
                condition_id=condition_id[:20],
                outcome=outcome,
                error=str(exc)[:120],
            )

    async def recent_failures(
        self,
        condition_id: str,
        hours: int = 24,
    ) -> int:
        """Count FAILED attempts within the trailing window.

        Excludes ``COOLDOWN`` (those are our own back-off, not a real
        failure) and ``SUCCESS`` rows.
        """
        pool = self._get_pool()
        if not pool or not condition_id:
            return 0
        try:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS n
                    FROM redeem_attempts
                    WHERE condition_id = $1
                      AND outcome = 'FAILED'
                      AND attempted_at >= NOW() - ($2::int || ' hours')::interval
                    """,
                    condition_id,
                    int(hours),
                )
                return int(row["n"]) if row else 0
        except Exception as exc:
            log.warning(
                "pg_redeem_attempts.recent_failures_failed",
                condition_id=condition_id[:20],
                error=str(exc)[:120],
            )
            return 0
