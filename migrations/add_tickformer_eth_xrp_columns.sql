-- Migration: add ETH/XRP per-asset TickFormer probability columns to signal_evaluations
-- Date: 2026-06-02
-- PR: fix/eth-xrp-tickformer-column-pipeline
-- RDS note: #833
--
-- WHAT THIS DOES:
--   Adds 8 nullable NUMERIC columns to signal_evaluations for the per-asset
--   TickFormer probability heads emitted by the classifier-gpu sister PR
--   (feat/v4-eth-xrp-emit) into /v4/snapshot.timescales.5m.
--
--   Columns follow the naming convention probability_tickformer_v{N}_{asset}
--   to avoid polluting the BTC-only probability_tickformer_v{N} columns that
--   cross-asset BTC strategies (asset: ANY) still read.
--
-- 8 NEW COLUMNS:
--   probability_tickformer_v16_eth  NUMERIC(10,8)   ETH TickFormer v16 P(UP)
--   probability_tickformer_v17_eth  NUMERIC(10,8)   ETH TickFormer v17 P(UP)
--   probability_tickformer_v18_eth  NUMERIC(10,8)   ETH TickFormer v18 P(UP)
--   probability_tickformer_v20_eth  NUMERIC(10,8)   ETH TickFormer v20 P(UP)
--   probability_tickformer_v16_xrp  NUMERIC(10,8)   XRP TickFormer v16 P(UP)
--   probability_tickformer_v17_xrp  NUMERIC(10,8)   XRP TickFormer v17 P(UP)
--   probability_tickformer_v18_xrp  NUMERIC(10,8)   XRP TickFormer v18 P(UP)
--   probability_tickformer_v20_xrp  NUMERIC(10,8)   XRP TickFormer v20 P(UP)
--
-- DEPENDENCY:
--   Classifier-gpu must emit these fields on /v4/snapshot.timescales.5m for
--   non-None values to flow. Until the sister PR is deployed the columns stay
--   NULL — the ETH/XRP strategies emit tickformer_vN_{eth,xrp}_not_available
--   SKIP (forward-compatible).
--
--   Engine writers (DBClient.update_signal_evaluations_tickformer_eth_xrp and
--   PgSignalRepo.update_signal_evaluations_tickformer_eth_xrp) are wired by
--   the companion engine PR.
--
-- IDEMPOTENT: safe to re-run (ADD COLUMN IF NOT EXISTS).
--
-- DOWN-MIGRATION (manual, if needed):
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v16_eth;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v17_eth;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v18_eth;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v20_eth;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v16_xrp;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v17_xrp;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v18_xrp;
--   ALTER TABLE signal_evaluations DROP COLUMN IF EXISTS probability_tickformer_v20_xrp;

-- ── ETH columns ──────────────────────────────────────────────────────────────

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v16_eth NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v16_eth IS
    'TickFormer v16 P(UP) for ETH 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v17_eth NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v17_eth IS
    'TickFormer v17 P(UP) for ETH 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v18_eth NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v18_eth IS
    'TickFormer v18 P(UP) for ETH 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v20_eth NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v20_eth IS
    'TickFormer v20 P(UP) for ETH 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

-- ── XRP columns ──────────────────────────────────────────────────────────────

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v16_xrp NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v16_xrp IS
    'TickFormer v16 P(UP) for XRP 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v17_xrp NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v17_xrp IS
    'TickFormer v17 P(UP) for XRP 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v18_xrp NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v18_xrp IS
    'TickFormer v18 P(UP) for XRP 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

ALTER TABLE signal_evaluations
    ADD COLUMN IF NOT EXISTS probability_tickformer_v20_xrp NUMERIC(10, 8);

COMMENT ON COLUMN signal_evaluations.probability_tickformer_v20_xrp IS
    'TickFormer v20 P(UP) for XRP 5m windows. '
    'Emitted by classifier-gpu feat/v4-eth-xrp-emit on /v4/snapshot.timescales.5m. '
    'NULL until classifier-gpu PR deployed. '
    'Added by fix/eth-xrp-tickformer-column-pipeline (2026-06-02, RDS note #833).';

-- ── Verify ────────────────────────────────────────────────────────────────────
SELECT
    column_name,
    data_type,
    numeric_precision,
    numeric_scale,
    is_nullable
FROM information_schema.columns
WHERE table_name = 'signal_evaluations'
  AND column_name LIKE 'probability_tickformer_%'
ORDER BY column_name;
