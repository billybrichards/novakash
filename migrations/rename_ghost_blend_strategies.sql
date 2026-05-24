-- Migration: rename GHOST blend strategies for naming clarity
-- Date: 2026-05-24
-- Purpose: Rename strategy_configs rows that were misleadingly named
--          "raw_lgb" / "tight" when they're actually BLENDS (50/50 of the
--          LGB head + the TimesFM HF classifier via app/v2_scorer.py:
--          blend_ensemble). The new `_blend` suffix makes that explicit so
--          future readers can immediately see whether a strategy reads a
--          blended probability column or a PURE LGB+iso one.
--
-- Background — the "blend bug" discovery (RDS notes #631, #632):
--   - app/v2_scorer.py's blend_ensemble was discovered to cap effective
--     probabilities at ~0.92 because the TimesFM HF classifier saturates
--     at ~0.84. Every `probability_lgb_v9_*` column that flows through this
--     path is a BLEND, NOT a raw model output.
--   - Walk-forward CV (e.g. v9.5 ETH PURE) shows ~10x more fires at the
--     same 90% WR target when the blend is bypassed, hence the new
--     `_pure_lgb` strategies (this PR ships v9_5_eth_pure_lgb).
--   - The existing strategies don't change behaviour — only their
--     strategy_id is renamed for clarity.
--
-- SAFE: this migration ONLY renames GHOST strategy_configs rows. LIVE
-- strategies are NEVER touched. Verified via direct RDS query on 2026-05-24
-- (see /tmp/rename_candidates_db_verify.txt — output snapshot at PR time):
--   v9_5_eth_raw_lgb|1.0.0|GHOST
--   v9_5_xrp_raw_lgb|1.0.0|GHOST
--   v9_5_xrp_tight|1.0.0|GHOST
--   v9_3_btc_raw_lgb|1.0.0|GHOST
--   v9_3_btc_tight|1.0.0|GHOST
--
-- Renames:
--   v9_5_eth_raw_lgb -> v9_5_eth_blend
--   v9_5_xrp_raw_lgb -> v9_5_xrp_blend
--   v9_5_xrp_tight   -> v9_5_xrp_tight_blend
--   v9_3_btc_raw_lgb -> v9_3_btc_blend
--   v9_3_btc_tight   -> v9_3_btc_tight_blend
--
-- NOT renamed in this PR (per "do not rename if LIVE" rule):
--   v9_2_v12_combo    -- LIVE
--   v9_2_raw_lgb      -- LIVE per parquet snapshot
--   v9_2_eth_raw_lgb  -- v9.4 model emits into v9.2 column (column reuse;
--                       cautious skip — separate follow-up PR)
--   v9_2_eth_down_late -- same v9.2 column concern; deferred
--   v9_2_iso_expand    -- deferred (deep cross-references via cell_pause
--                        / rolling_wr / comparison_shadow)
--   v9_2_v12_AND_ghost -- deferred (same)
--   v12_lgb_combo      -- deferred (heavy coupling to cell_bucketing /
--                        cell_pause / comparison_shadow / rolling_wr)
--
-- IMPORTANT — DEFAULTS GHOST. Per feedback_no_auto_promote.md, Billy
-- promotes every strategy by hand. This rename does NOT change mode. The
-- canonical engine seeder (StrategyRegistry.seed_registry_to_db) will
-- re-UPSERT from the renamed YAML files on next boot, so the old rows
-- would not be re-written. We DELETE the old GHOST rows after copying
-- their mode forward to the new strategy_id (idempotent via the WHERE
-- clauses).
--
-- Idempotent — running this twice is a no-op:
--   - Step 1 INSERTs only if the new strategy_id is missing
--     (NOT EXISTS guard).
--   - Step 2 DELETEs the old strategy_id only if it exists
--     (DELETE...WHERE always safe).

-- ── Step 1: copy each old strategy_configs row forward to the new id ──────
-- We preserve mode, asset, timescale, version, config_yaml, gates_json,
-- sizing_json verbatim. The hooks_file is patched to the renamed module name.
-- timestamps are refreshed to NOW() so the dashboard shows the rename event.

WITH renames AS (
    SELECT * FROM (VALUES
        ('v9_5_eth_raw_lgb',  'v9_5_eth_blend',        'v9_5_eth_blend.py'),
        ('v9_5_xrp_raw_lgb',  'v9_5_xrp_blend',        'v9_5_xrp_blend.py'),
        ('v9_5_xrp_tight',    'v9_5_xrp_tight_blend',  'v9_5_xrp_tight_blend.py'),
        ('v9_3_btc_raw_lgb',  'v9_3_btc_blend',        'v9_3_btc_blend.py'),
        ('v9_3_btc_tight',    'v9_3_btc_tight_blend',  'v9_3_btc_tight_blend.py')
    ) AS r(old_id, new_id, new_hooks_file)
)
INSERT INTO strategy_configs (
    strategy_id, version, mode, asset, timescale,
    config_yaml, gates_json, sizing_json, hooks_file,
    created_at, updated_at
)
SELECT
    r.new_id,
    sc.version,
    sc.mode,            -- preserve mode (must be GHOST per verification)
    sc.asset,
    sc.timescale,
    sc.config_yaml,     -- engine seeder will overwrite from renamed YAML at boot
    sc.gates_json,
    sc.sizing_json,
    r.new_hooks_file,
    NOW(),
    NOW()
FROM strategy_configs sc
JOIN renames r ON sc.strategy_id = r.old_id
WHERE sc.mode = 'GHOST'  -- belt-and-braces: never copy a LIVE row
  AND NOT EXISTS (
      SELECT 1 FROM strategy_configs sc2
      WHERE sc2.strategy_id = r.new_id AND sc2.version = sc.version
  );

-- ── Step 2: delete the old GHOST rows (only if their new counterpart now
--           exists — guards against a half-applied migration). ──────────────
DELETE FROM strategy_configs sc_old
USING (VALUES
    ('v9_5_eth_raw_lgb',  'v9_5_eth_blend'),
    ('v9_5_xrp_raw_lgb',  'v9_5_xrp_blend'),
    ('v9_5_xrp_tight',    'v9_5_xrp_tight_blend'),
    ('v9_3_btc_raw_lgb',  'v9_3_btc_blend'),
    ('v9_3_btc_tight',    'v9_3_btc_tight_blend')
) AS r(old_id, new_id)
WHERE sc_old.strategy_id = r.old_id
  AND sc_old.mode = 'GHOST'  -- never DELETE a LIVE row
  AND EXISTS (
      SELECT 1 FROM strategy_configs sc_new
      WHERE sc_new.strategy_id = r.new_id AND sc_new.version = sc_old.version
  );

-- ── Verify ─────────────────────────────────────────────────────────────────
SELECT strategy_id, version, mode, asset, timescale, hooks_file
FROM strategy_configs
WHERE strategy_id IN (
    'v9_5_eth_raw_lgb',  'v9_5_eth_blend',
    'v9_5_xrp_raw_lgb',  'v9_5_xrp_blend',
    'v9_5_xrp_tight',    'v9_5_xrp_tight_blend',
    'v9_3_btc_raw_lgb',  'v9_3_btc_blend',
    'v9_3_btc_tight',    'v9_3_btc_tight_blend'
)
ORDER BY strategy_id, version;
