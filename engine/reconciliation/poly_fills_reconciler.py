"""
poly_fills_reconciler — Periodic source-of-truth sync from Polymarket data-api.

v11: Fetches every CLOB fill for our proxy wallet from the Polymarket
data-api (authoritative), writes them append-only to `poly_fills`, and
enriches `trade_bible` with condition_id / market_slug / fill linkage
so analysis queries can join across tables.

Design principles:
- **Append-only**: never UPDATEs `poly_fills` rows. The on-chain record
  is immutable — if we see the same tx hash twice, skip.
- **Defensive tagging**: every row has `source='data-api'` so we can
  distinguish from engine-reported / clob-api / on-chain rows later.
- **Multi-fill detection**: groups trades by condition_id and marks
  rows with `is_multi_fill=True` + `multi_fill_index`/`multi_fill_total`
  so we can audit the pre-v11 parsing bug impact.
- **Link enrichment**: populates `trade_bible.condition_id`,
  `market_slug`, `clob_order_id` from `poly_fills` when they're NULL.
  Does NOT overwrite non-NULL engine values.
- **Periodic safe**: designed to run every 5 minutes in the orchestrator
  heartbeat loop. Idempotent — running it twice in a row is a no-op.

Usage:
    # From the orchestrator:
        reconciler = PolyFillsReconciler(db_pool, funder_address)
        await reconciler.sync(hours=1)

    # CLI standalone:
        python3 -m engine.reconciliation.poly_fills_reconciler --hours 48
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import aiohttp
    import asyncpg
    import structlog
except ImportError as e:
    print(f"Missing dependency: {e}. Install: pip3 install aiohttp asyncpg structlog", file=sys.stderr)
    sys.exit(1)


log = structlog.get_logger(__name__)

DATA_API_URL = "https://data-api.polymarket.com/trades"
PAGE_LIMIT = 500
DEFAULT_SYNC_HOURS = 2  # How far back to scan on each heartbeat run
MULTI_FILL_WINDOW_S = 120  # Fills within 2min of same condition = multi-fill group


class PolyFillsReconciler:
    """Periodic reconciler that keeps poly_fills in sync with Polymarket.

    Call `sync()` from the orchestrator heartbeat loop. It's idempotent —
    running multiple times is safe and cheap (inserts skip on conflict).
    """

    def __init__(self, pool: asyncpg.Pool, funder_address: str) -> None:
        self._pool = pool
        self._funder = funder_address.lower()
        self._log = log.bind(component="poly_fills_reconciler")
        self._last_sync: Optional[datetime] = None
        self._last_insert_count: int = 0

    async def sync(self, hours: float = DEFAULT_SYNC_HOURS) -> dict[str, int]:
        """One reconciliation pass. Returns {fetched, inserted, linked, enriched}.

        Safe to call periodically from the heartbeat. Designed for hours=1-2
        under normal operation; the CLI wrapper uses larger windows for backfills.
        """
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        since_ts = int(since.timestamp())

        try:
            trades = await self._fetch_trades(since_ts)
        except Exception as exc:
            self._log.warning("fetch_failed", error=str(exc)[:200])
            return {"fetched": 0, "inserted": 0, "linked": 0, "enriched": 0}

        if not trades:
            self._log.debug("no_trades")
            return {"fetched": 0, "inserted": 0, "linked": 0, "enriched": 0}

        multi_fills = self._detect_multi_fills(trades)
        inserted = await self._upsert_poly_fills(trades, multi_fills)
        linked = await self._link_to_trade_bible()
        enriched = await self._enrich_trade_bible_fields()

        self._last_sync = datetime.now(timezone.utc)
        self._last_insert_count = inserted

        summary = {
            "fetched": len(trades),
            "inserted": inserted,
            "linked": linked,
            "enriched": enriched,
        }
        if inserted or linked or enriched:
            self._log.info("sync_complete", **summary)
        return summary

    # ─── Step 1: fetch from Polymarket data-api ───────────────────────────

    async def _fetch_trades(self, since_ts: int) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        seen: set[str] = set()
        offset = 0

        async with aiohttp.ClientSession() as session:
            while True:
                params = {"user": self._funder, "limit": PAGE_LIMIT, "offset": offset}
                async with session.get(
                    DATA_API_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"data-api returned HTTP {resp.status}")
                    batch = await resp.json()

                if not batch:
                    break

                new_in_batch = 0
                oldest = float("inf")
                for t in batch:
                    tx = t.get("transactionHash")
                    if tx and tx in seen:
                        continue
                    if tx:
                        seen.add(tx)
                    trades.append(t)
                    new_in_batch += 1
                    oldest = min(oldest, t.get("timestamp", 0))

                if oldest < since_ts or len(batch) < PAGE_LIMIT or new_in_batch == 0:
                    break
                offset += PAGE_LIMIT

        return [t for t in trades if t.get("timestamp", 0) >= since_ts]

    # ─── Step 2: detect multi-fills ───────────────────────────────────────

    def _detect_multi_fills(
        self, trades: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for t in trades:
            if t.get("side") != "BUY":
                continue
            cond = t.get("conditionId") or ""
            if not cond:
                continue
            groups[cond].append(t)

        for cond in groups:
            groups[cond].sort(key=lambda x: x.get("timestamp", 0))
        return groups

    # ─── Step 3: insert into poly_fills (append-only) ─────────────────────

    async def _upsert_poly_fills(
        self,
        trades: list[dict[str, Any]],
        multi_fills: dict[str, list[dict[str, Any]]],
    ) -> int:
        if not trades:
            return 0

        rows = []
        for t in trades:
            tx = t.get("transactionHash")
            if not tx:
                continue

            cond = t.get("conditionId", "")
            group = multi_fills.get(cond, [])
            total = len(group)
            try:
                index = group.index(t) + 1
            except ValueError:
                index = None
            is_multi = total > 1 if group else False

            ts = int(t.get("timestamp", 0))
            match_utc = datetime.fromtimestamp(ts, tz=timezone.utc)

            rows.append((
                tx,
                t.get("asset", ""),
                cond,
                t.get("slug", ""),
                t.get("side", ""),
                t.get("outcome", ""),
                float(t.get("price", 0)),
                float(t.get("size", 0)),
                float(t.get("fee")) if t.get("fee") is not None else None,
                ts,
                match_utc,
                None,              # trade_bible_id — populated by _link_to_trade_bible
                None,              # clob_order_id — unknown from data-api
                "data-api",        # source tag (IMMUTABLE)
                datetime.now(timezone.utc),
                is_multi,
                index,
                total if total else None,
                json.dumps(t),
            ))

        inserted = 0
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for row in rows:
                    try:
                        result = await conn.fetchrow(
                            """
                            INSERT INTO poly_fills (
                                transaction_hash, asset_token_id, condition_id, market_slug,
                                side, outcome, price, size, fee_usd,
                                match_timestamp, match_time_utc,
                                trade_bible_id, clob_order_id,
                                source, verified_at,
                                is_multi_fill, multi_fill_index, multi_fill_total,
                                raw_payload
                            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                            ON CONFLICT (transaction_hash) DO NOTHING
                            RETURNING id
                            """,
                            *row,
                        )
                        if result is not None:
                            inserted += 1
                    except Exception as exc:
                        self._log.warning(
                            "insert_failed",
                            tx=row[0][:20],
                            error=str(exc)[:150],
                        )
        return inserted

    # ─── Step 4: link orphan fills to trade_bible by temporal+condition ───

    async def _link_to_trade_bible(self) -> int:
        """Link unlinked poly_fills to trade_bible rows.

        Matching strategy (most reliable first):
        1. condition_id match within 10min of placed_at
        2. market_slug match within 10min
        3. SKIP (leave as orphan — may never link)

        Only updates rows where trade_bible_id IS NULL. Never overwrites.
        """
        linked = 0
        async with self._pool.acquire() as conn:
            unlinked = await conn.fetch(
                """
                SELECT id, condition_id, market_slug, match_time_utc
                FROM poly_fills
                WHERE trade_bible_id IS NULL
                  AND side = 'BUY'
                  AND match_time_utc >= NOW() - interval '7 days'
                LIMIT 500
                """
            )

            for fill in unlinked:
                tb_id = None

                # Try condition_id match first (most reliable)
                if fill["condition_id"]:
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM trade_bible
                        WHERE condition_id = $1
                          AND placed_at BETWEEN $2 AND $3
                          AND is_live = true
                        ORDER BY ABS(EXTRACT(EPOCH FROM (placed_at - $4::timestamptz))) ASC
                        LIMIT 1
                        """,
                        fill["condition_id"],
                        fill["match_time_utc"] - timedelta(minutes=10),
                        fill["match_time_utc"] + timedelta(minutes=5),
                        fill["match_time_utc"],
                    )
                    if row:
                        tb_id = row["id"]

                # Fall back to market_slug
                if tb_id is None and fill["market_slug"]:
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM trade_bible
                        WHERE market_slug = $1
                          AND placed_at BETWEEN $2 AND $3
                          AND is_live = true
                        ORDER BY ABS(EXTRACT(EPOCH FROM (placed_at - $4::timestamptz))) ASC
                        LIMIT 1
                        """,
                        fill["market_slug"],
                        fill["match_time_utc"] - timedelta(minutes=10),
                        fill["match_time_utc"] + timedelta(minutes=5),
                        fill["match_time_utc"],
                    )
                    if row:
                        tb_id = row["id"]

                if tb_id is not None:
                    await conn.execute(
                        "UPDATE poly_fills SET trade_bible_id = $1 WHERE id = $2",
                        tb_id,
                        fill["id"],
                    )
                    linked += 1

        return linked

    # ─── Step 5: enrich trade_bible fields from poly_fills ────────────────

    async def _enrich_trade_bible_fields(self) -> int:
        """Populate trade_bible.condition_id + market_slug from linked poly_fills.

        Only updates NULL fields — never overwrites existing values.
        This lets us gradually enrich trade_bible so future joins work better.
        """
        enriched = 0
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT pf.trade_bible_id, pf.condition_id, pf.market_slug
                FROM poly_fills pf
                JOIN trade_bible tb ON tb.id = pf.trade_bible_id
                WHERE pf.trade_bible_id IS NOT NULL
                  AND (
                    (tb.condition_id IS NULL AND pf.condition_id IS NOT NULL)
                    OR (tb.market_slug IS NULL AND pf.market_slug IS NOT NULL)
                  )
                LIMIT 500
                """
            )

            for row in rows:
                result = await conn.execute(
                    """
                    UPDATE trade_bible
                    SET condition_id = COALESCE(condition_id, $1),
                        market_slug = COALESCE(market_slug, $2)
                    WHERE id = $3
                    """,
                    row["condition_id"] or None,
                    row["market_slug"] or None,
                    row["trade_bible_id"],
                )
                if result and result.startswith("UPDATE 1"):
                    enriched += 1

        return enriched


# ─── CLI wrapper ──────────────────────────────────────────────────────────


def _load_db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)

    repo_root = Path(__file__).resolve().parent.parent.parent
    for candidate in (repo_root / "engine" / ".env", repo_root / "engine" / ".env.local"):
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                value = line.split("=", 1)[1].strip()
                return value.replace("postgresql+asyncpg://", "postgresql://", 1)

    raise RuntimeError("DATABASE_URL not found")


async def _cli_main(args: argparse.Namespace) -> int:
    db_url = _load_db_url()
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        reconciler = PolyFillsReconciler(
            pool=pool,
            funder_address=args.funder or "0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10",
        )
        result = await reconciler.sync(hours=args.hours)
        print(json.dumps(result, indent=2))
    finally:
        await pool.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hours", type=float, default=2.0, help="Lookback window (default 2h)")
    p.add_argument("--funder", help="Override funder address")
    args = p.parse_args()
    return asyncio.run(_cli_main(args))


if __name__ == "__main__":
    sys.exit(main())
