-- 2026-04-26 (PR 01) — window_snapshots: v3/v4 surface columns missed by PR #216.
--
-- Three columns referenced by every surface-write upsert
-- (engine/persistence/db_client.py::update_window_surface_fields and
-- engine/adapters/persistence/pg_signal_repo.py::update_window_surface_fields)
-- were defined in the new pg_window_repo bootstrap (CA-04, never wired into
-- runtime.py) but never landed in the legacy DBClient.ensure_window_tables
-- list that actually runs at engine startup. Result: every window cycle
-- emits `db.update_window_surface_fields_failed` for all 4 assets, and the
-- v4 surface fields are dropped on the floor (analytics cannot reconstruct
-- conviction-score, consensus divergence, macro size modifier WR by regime).
--
-- Trading is unaffected — the upsert is fire-and-forget — but log noise is
-- material and the columns are referenced in downstream SQL the moment
-- they exist (shadow_analysis.py / strategy WR breakdowns).
--
-- Mapping to producers (engine/strategies/five_min_vpin.py::_window_surface_fields):
--   strategy_conviction_score  ← surface.v4_conviction_score
--   consensus_divergence_bps   ← surface.v4_consensus_max_divergence_bps
--   macro_size_modifier        ← surface.v4_macro_size_modifier
--
-- Additive + idempotent. All columns nullable — rows written before this
-- migration stay valid. Mirrors the pattern used in
-- 20260419_02_window_snapshots_ensemble_cols.sql.

ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS strategy_conviction_score DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS consensus_divergence_bps  DOUBLE PRECISION;
ALTER TABLE window_snapshots
    ADD COLUMN IF NOT EXISTS macro_size_modifier       DOUBLE PRECISION;

COMMENT ON COLUMN window_snapshots.strategy_conviction_score IS
    'v4 conviction score (numeric backing for strategy_conviction label STRONG/MODERATE/WEAK). Sourced from FullDataSurface.v4_conviction_score at eval time. Null when v4_snapshot absent.';
COMMENT ON COLUMN window_snapshots.consensus_divergence_bps  IS
    'v4 consensus max-divergence (basis points) across forecast components at eval time. Sourced from v4_snapshot.consensus.max_divergence_bps. Higher = more disagreement among models.';
COMMENT ON COLUMN window_snapshots.macro_size_modifier       IS
    'v4 macro size modifier in [0.0, 1.0+]. Sourced from v4_snapshot.macro.size_modifier. Multiplied into bet sizing when macro bias agrees with strategy direction.';
