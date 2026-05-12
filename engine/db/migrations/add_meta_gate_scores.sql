-- engine/db/migrations/add_meta_gate_scores.sql
-- Audit #963 (2026-05-12): add meta gate score columns to signal_evaluations_v2
-- and window_snapshots.
--
-- The gate models (v2_meta_gate, v9_2_meta_gate, v12_meta_gate) emit
-- probability scores in /v4/snapshot. These columns store them for
-- post-hoc analysis and future retraining.
--
-- Apply via SSM tunnel through the Montreal EC2:
--   aws ssm start-session --target i-05dbd2ca41f8a75ec --region ca-central-1
--   psql -h novakash-pg-prod.cpmisy2asv71.ca-central-1.rds.amazonaws.com \
--        -U postgres -d novakash \
--        -f engine/db/migrations/add_meta_gate_scores.sql

ALTER TABLE signal_evaluations
  ADD COLUMN IF NOT EXISTS probability_v2_meta_gate  double precision,
  ADD COLUMN IF NOT EXISTS probability_v9_2_meta_gate double precision,
  ADD COLUMN IF NOT EXISTS probability_v12_meta_gate double precision;

ALTER TABLE window_snapshots
  ADD COLUMN IF NOT EXISTS probability_v2_meta_gate  double precision,
  ADD COLUMN IF NOT EXISTS probability_v9_2_meta_gate double precision,
  ADD COLUMN IF NOT EXISTS probability_v12_meta_gate double precision;
