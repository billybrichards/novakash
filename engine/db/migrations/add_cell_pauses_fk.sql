-- engine/db/migrations/add_cell_pauses_fk.sql
-- F2 followup (PR #494 Opus review): add FK from cell_pauses.strategy_id to
-- the canonical strategies table.
--
-- INVESTIGATION RESULT (2026-05-06):
-- The `strategies_registry` table named in the review comment does NOT exist.
-- The canonical strategies table is `strategy_configs`, defined in
-- hub/db/migrations/versions/20260417_03_strategy_configs.sql.
--
-- `strategy_configs` uses a composite PRIMARY KEY (strategy_id, version) —
-- there is NO single-column unique constraint on strategy_id alone, because
-- the same strategy_id can have multiple rows at different versions.
--
-- A conventional FOREIGN KEY constraint from cell_pauses.strategy_id →
-- strategy_configs.strategy_id is therefore not directly expressible in
-- standard SQL (FKs must reference a unique / primary key).
--
-- OPTIONS CONSIDERED:
--   A) Add a UNIQUE INDEX on strategy_configs(strategy_id) — this would
--      enforce "one active version per strategy", which contradicts the
--      versioning design (bumping YAML version inserts a new row, not an
--      update).  REJECTED.
--   B) Add a CHECK constraint via a trigger that verifies strategy_id
--      EXISTS IN strategy_configs at INSERT time.  Correct semantics but
--      adds trigger maintenance overhead and is uncommon in this codebase.
--   C) Document the constraint as "data-only, validated by app code".
--      The engine's StrategyRegistry (seed_registry_to_db) is the sole
--      writer and only inserts known strategy_ids — the app invariant holds.
--      ADOPTED — see note below.
--
-- DECISION: constraint is enforced by application code only.  The engine
-- StrategyRegistry.seed_registry_to_db() upserts every loaded strategy into
-- strategy_configs before any cell_pauses row can be written (RollingWRMonitor
-- only fires after the engine has booted and evaluated at least `min_trades`
-- live trades, by which time the registry has already been seeded).  If a
-- strategy is renamed and re-deployed, historical cell_pauses rows for the
-- old strategy_id become orphaned — this is acceptable and consistent with
-- the existing v8_v12_strong_agree rename precedent.
--
-- We DO add a comment on the column to make the intended reference explicit
-- for schema readers, and we add an index to support JOIN-based cleanup queries.

-- Comment on column (PostgreSQL extension — safe to apply on existing table).
COMMENT ON COLUMN cell_pauses.strategy_id IS
    'Logical strategy identifier. References strategy_configs.strategy_id '
    '(no FK enforced — composite PK prevents direct constraint; validated '
    'by engine StrategyRegistry boot sequence).';

-- Index to support orphan-cleanup queries: find cell_pauses rows whose
-- strategy_id no longer appears in strategy_configs.
CREATE INDEX IF NOT EXISTS idx_cell_pauses_strategy_id
    ON cell_pauses (strategy_id);

-- If the table ever gains a single-column PK on strategy_id (unlikely),
-- uncomment to add the FK:
--
-- ALTER TABLE cell_pauses
-- ADD CONSTRAINT fk_cell_pauses_strategy_id
-- FOREIGN KEY (strategy_id)
-- REFERENCES strategy_configs(strategy_id) ON DELETE CASCADE;

-- Reverse migration:
-- DROP INDEX IF EXISTS idx_cell_pauses_strategy_id;
-- COMMENT ON COLUMN cell_pauses.strategy_id IS NULL;
