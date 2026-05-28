# TickFormer Ghost Rollout Protocol

**Status:** authoritative as of 2026-05-28 (PR #619 FIX 4)
**Owner:** Billy (manual gate per `feedback_no_auto_promote.md`)
**Scope:** `tickformer_v16_pure`, `tickformer_v17_sniper`,
`tickformer_v18_t180`, and any future `tickformer_*` strategy that
declares `gate_params.mutex_group: tickformer`.

This document is the single source of truth for promoting a
tickformer strategy from SHADOW → GHOST → LIVE. It supersedes the
"Rollout Plan" stanza in each strategy's YAML header (those remain
informational but defer to this doc on conflicts).

## Three-stage promotion model

The engine + DB carry **two** independent kill switches per strategy:

1. `strategy_configs.mode` (DB row) — `SHADOW` / `GHOST` / `LIVE` /
   `DISABLED`. Authoritative for the engine's decision-write pipeline.
2. `gate_params.shadow_only` (int) — `1` forces SKIP-with-record
   even when mode is LIVE. The "go-live" gate at the hook level.

Both layers must be flipped for a real fire to emit. Promotion
between stages flips exactly **one** layer at a time so a single
rollback is always possible.

| Stage  | `mode`   | `shadow_only` | `max_position_usd` | Decision write? | Real fire? |
|--------|----------|---------------|--------------------|-----------------|------------|
| SHADOW | `SHADOW` | `1`           | `5`                | yes (would_trade) | no       |
| GHOST  | `GHOST`  | `0`           | `5`                | yes (TRADE)     | no (mode blocks execution) |
| LIVE   | `LIVE`   | `0`           | `5` (initial)      | yes (TRADE)     | yes      |

Stage transitions ALWAYS flip the field that differs in the table
above — never two at once.

### SHADOW → GHOST

**Requirement:** ≥ 50 shadow fires (combined UP+DN) with WR ≥ 90% in
both directions independently. Use the monitoring queries below; do
NOT eyeball.

Operation: flip `gate_params.shadow_only` from `1` to `0` via DB
runtime override.

```sql
INSERT INTO strategy_runtime_overrides (strategy_id, gate_params, updated_at)
VALUES ('tickformer_v18_t180', '{"shadow_only": 0}'::jsonb, NOW())
ON CONFLICT (strategy_id) DO UPDATE
  SET gate_params = strategy_runtime_overrides.gate_params || EXCLUDED.gate_params,
      updated_at  = EXCLUDED.updated_at;
```

In GHOST, `mode=GHOST` still blocks execution (the engine's
`execute_trade_uc` ignores GHOST decisions) — but the
`strategy_decisions` row now records `action=TRADE` instead of
`SKIP(shadow_only_no_trade)`, which lets Strategy Lab compute a
"would have fired" WR against real surface telemetry.

### GHOST → LIVE

**Requirement:** ≥ 100 GHOST fires sustaining ≥ 90% WR per
direction; ≥ 50 trading hours of GHOST exposure; no live divergence
> 3pp between GHOST measured WR and paper WR.

Operation: flip `mode` from `GHOST` to `LIVE` via the strategy_configs
migration (NOT runtime override — mode is the persistent kill
switch).

```sql
UPDATE strategy_configs
   SET mode = 'LIVE',
       updated_at = NOW()
 WHERE strategy_id = 'tickformer_v18_t180'
   AND version = '1.0.0';
```

Initial `max_position_usd` stays at `5` for at least 48h after LIVE.
Scale up only when 48h live WR matches GHOST WR within 3pp.

## Rollback procedure

Two independent rollbacks, deployable separately.

### Soft rollback — mode back to SHADOW

```sql
-- migrations/rollback_tickformer_v17_v18_strategies.sql (mode-only)
UPDATE strategy_configs
   SET mode = 'SHADOW',
       updated_at = NOW()
 WHERE strategy_id IN (
     'tickformer_v16_pure',
     'tickformer_v17_sniper',
     'tickformer_v18_t180'
 );
```

Use when WR drops but the strategy + its surface data look healthy.
Keeps the row + history; just stops fires.

### Hard rollback — DELETE the strategy

See `migrations/rollback_tickformer_v17_v18_strategies.sql` (PR #619
FIX 5). Removes the strategy_configs rows; the engine's YAML loader
re-seeds them at next boot in SHADOW mode UNLESS the YAML files are
also removed in the same release. Use only when the strategy is
being retired or replaced.

## Monitoring queries

The queries below are stable across all `tickformer_*` strategies
because they key off `metadata->>'mutex_group' = 'tickformer'`. Run
in `psql` against the engine's primary RDS (NOT the local PG
snapshot — see `feedback_local_db_means_local.md`).

### Daily WR rollup

```sql
SELECT
    strategy_id,
    DATE_TRUNC('day', created_at) AS day,
    COUNT(*) FILTER (WHERE action = 'TRADE') AS fires,
    COUNT(*) FILTER (
        WHERE action = 'TRADE'
          AND (metadata->>'outcome_win')::bool IS TRUE
    ) AS wins,
    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE action = 'TRADE'
              AND (metadata->>'outcome_win')::bool IS TRUE
        ) / NULLIF(COUNT(*) FILTER (WHERE action = 'TRADE'), 0),
        2
    ) AS wr_pct
FROM strategy_decisions
WHERE strategy_id IN (
    'tickformer_v16_pure',
    'tickformer_v17_sniper',
    'tickformer_v18_t180'
)
  AND created_at > NOW() - INTERVAL '14 days'
GROUP BY 1, 2
ORDER BY 1, 2 DESC;
```

### Per-tier breakdown

```sql
SELECT
    strategy_id,
    COALESCE(metadata->>'tier', 'default') AS tier,
    direction,
    COUNT(*) AS n,
    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE (metadata->>'outcome_win')::bool IS TRUE
        ) / NULLIF(COUNT(*), 0),
        2
    ) AS wr_pct
FROM strategy_decisions
WHERE strategy_id IN (
    'tickformer_v16_pure',
    'tickformer_v17_sniper',
    'tickformer_v18_t180'
)
  AND action = 'TRADE'
  AND created_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;
```

### Paper-vs-shadow divergence alert

Use when the strategy is in SHADOW. Fires if shadow-decision WR
(would_trade=true) diverges from the paper-WR estimate from the
original val sweep by more than 5pp over the last 24h.

```sql
WITH shadow AS (
    SELECT
        strategy_id,
        COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true') AS would_n,
        ROUND(
            100.0 * COUNT(*) FILTER (
                WHERE metadata->>'would_trade' = 'true'
                  AND (metadata->>'outcome_win')::bool IS TRUE
            ) / NULLIF(COUNT(*) FILTER (WHERE metadata->>'would_trade' = 'true'), 0),
            2
        ) AS shadow_wr_pct
    FROM strategy_decisions
    WHERE strategy_id IN (
        'tickformer_v16_pure',
        'tickformer_v17_sniper',
        'tickformer_v18_t180'
    )
      AND created_at > NOW() - INTERVAL '24 hours'
    GROUP BY 1
)
SELECT
    s.strategy_id,
    s.would_n,
    s.shadow_wr_pct,
    paper.paper_wr_pct,
    s.shadow_wr_pct - paper.paper_wr_pct AS divergence_pp
FROM shadow s
JOIN (
    VALUES
        ('tickformer_v16_pure',  94.0),
        ('tickformer_v17_sniper', 96.0),
        ('tickformer_v18_t180',  92.0)
) AS paper(strategy_id, paper_wr_pct) ON paper.strategy_id = s.strategy_id
WHERE ABS(s.shadow_wr_pct - paper.paper_wr_pct) > 5.0;
```

### Mutex-group loss attribution

Counts losses by winner — useful when promoting a sibling triggers
SKIP-storms on the others.

```sql
SELECT
    strategy_id AS loser,
    metadata->>'mutex_group_winner' AS winner,
    COUNT(*) AS n
FROM strategy_decisions
WHERE skip_reason = 'mutex_group_lost'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY 1, 2
ORDER BY 3 DESC;
```

## Recommended materialised views

For dashboard latency, persist the rollup queries above as
materialised views refreshed every 5 minutes:

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_tickformer_daily_wr AS
-- body matches the "Daily WR rollup" query above
SELECT ...;

CREATE UNIQUE INDEX IF NOT EXISTS mv_tickformer_daily_wr_pk
    ON mv_tickformer_daily_wr (strategy_id, day);

-- 5-min refresh in a pg_cron job:
-- SELECT cron.schedule('refresh-tickformer-wr', '*/5 * * * *',
--   $$REFRESH MATERIALIZED VIEW CONCURRENTLY mv_tickformer_daily_wr$$);
```

`CONCURRENTLY` requires the unique index above and avoids blocking
the read side during refresh.

## Decision-log contract

Every tickformer decision (TRADE / SKIP / mutex_group_lost) carries
the following metadata keys (PR #619 FIX 1 contract):

- `probability_tickformer_v1{6,7,8}` (float)
- `tickformer_trade_signal` (str | null) — `UP` / `DOWN` / `HOLD`
- `eval_offset` (int) — seconds into the 5m window
- `eval_offset_remaining` (int) — seconds left in the 5m window
- `up_threshold`, `down_threshold` (float) — effective values used
- `eval_offset_remaining_min`, `eval_offset_remaining_max` (int)
- `mutex_group` (str) — `tickformer` when opted in
- `tier` (str) — present only when gate_params.tier is set
- `shadow_only` (bool) — at-decision-time value
- `would_trade`, `would_direction` (bool, str) — present on
  shadow-SKIP and mutex-loss SKIP rows
- `mutex_group_winner`, `mutex_group_winner_confidence_score`,
  `mutex_group_loser_confidence_score` — present on mutex-loss rows

This contract is enforced by the test suite
(`test_tickformer_strategies.py::test_v16_shadow_record_metadata_has_required_keys`).
