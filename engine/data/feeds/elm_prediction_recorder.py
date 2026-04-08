"""
ELM v3 Prediction Recorder — saves ML model predictions for all assets.

Polls the ELM v3 model every 30s for BTC, ETH, SOL, XRP across multiple
delta buckets (T-60, T-120, T-180). Writes to ticks_elm_predictions table.

This is READ-ONLY from trading perspective — it never places orders.
Purpose: build training dataset for future model improvements.

Runs as a background task in the orchestrator.
"""

import asyncio
import os
import time
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
DELTAS = [60, 90, 120, 180]  # seconds to close
POLL_INTERVAL = 30  # seconds between full sweeps


class ELMPredictionRecorder:
    """Records ELM v3 predictions for all assets and delta buckets."""

    def __init__(self, elm_client, db_pool, shutdown_event: asyncio.Event):
        self._client = elm_client
        self._pool = db_pool
        self._shutdown = shutdown_event
        self._log = log.bind(component="elm_recorder")
        self._table_ensured = False

    async def _ensure_table(self):
        """Create the predictions table if it doesn't exist."""
        if self._table_ensured or not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS ticks_elm_predictions (
                        id BIGSERIAL PRIMARY KEY,
                        ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        asset TEXT NOT NULL,
                        seconds_to_close INT NOT NULL,
                        model_version TEXT,
                        probability_up DOUBLE PRECISION,
                        probability_raw DOUBLE PRECISION,
                        delta_bucket INT,
                        feature_age_ms JSONB
                    )
                """)
                # Index for efficient querying
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_elm_pred_asset_ts
                    ON ticks_elm_predictions (asset, ts DESC)
                """)
            self._table_ensured = True
            self._log.info("elm_recorder.table_ensured")
        except Exception as exc:
            self._log.warning("elm_recorder.table_create_error", error=str(exc)[:100])

    async def run(self):
        """Main loop: poll ELM for all assets/deltas every 30s."""
        self._log.info("elm_recorder.started", assets=ASSETS, deltas=DELTAS,
                        interval=POLL_INTERVAL)
        await self._ensure_table()

        while not self._shutdown.is_set():
            try:
                await self._record_sweep()
            except Exception as exc:
                self._log.warning("elm_recorder.sweep_error", error=str(exc)[:100])

            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=POLL_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass

        self._log.info("elm_recorder.stopped")

    async def _record_sweep(self):
        """Query ELM for all asset/delta combos and write to DB."""
        if not self._client or not self._pool:
            return

        rows = []
        for asset in ASSETS:
            for delta in DELTAS:
                try:
                    result = await self._client.get_probability(
                        asset=asset,
                        seconds_to_close=delta,
                        model="oak",
                    )
                    if result and "probability_up" in result:
                        rows.append((
                            asset,
                            delta,
                            result.get("model_version", ""),
                            float(result["probability_up"]),
                            float(result.get("probability_raw", 0)),
                            result.get("delta_bucket"),
                            str(result.get("feature_freshness_ms", {})),
                        ))
                except Exception as exc:
                    self._log.debug("elm_recorder.query_error",
                                    asset=asset, delta=delta, error=str(exc)[:50])

        if not rows:
            return

        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO ticks_elm_predictions
                       (asset, seconds_to_close, model_version, probability_up,
                        probability_raw, delta_bucket, feature_age_ms)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)""",
                    rows,
                )
            self._log.info("elm_recorder.wrote", count=len(rows))
        except Exception as exc:
            self._log.warning("elm_recorder.write_error", error=str(exc)[:100])
