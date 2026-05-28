-- Migration: add_exit_monitor_shadow_table.sql
-- Shadow exit-detection layer for TickFormer probability-based u-turn detection.
--
-- LOG-ONLY — no exits are executed. This table banks "would-have-exited"
-- markers paired with live CLOB context so the CTF mergePositions execution
-- path has months of training/eval data when it lands.
--
-- Design: EXIT_MONITOR_DESIGN.md + EXIT_MONITOR_FEASIBILITY.md in timesfm
-- magic-model/. Feature flag: EXIT_MONITOR_SHADOW_ENABLED env var.
--
-- Idempotent: safe to run multiple times.

CREATE TABLE IF NOT EXISTS exit_monitor_shadow (
    id                          BIGSERIAL PRIMARY KEY,
    created_at                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Trade reference (loose FK — novakash convention, no hard constraint)
    decision_id                 BIGINT          NOT NULL,  -- strategy_decisions.id
    asset                       TEXT            NOT NULL,  -- 'BTC', 'ETH', 'SOL', 'XRP'
    window_ts                   BIGINT          NOT NULL,
    strategy_id                 TEXT            NOT NULL,  -- e.g. 'tickformer_v18_t180'
    side                        TEXT            NOT NULL,  -- 'UP' or 'DN' — trade holding direction

    -- Entry snapshot
    entry_p                     NUMERIC(10,6),             -- TickFormer prob at trade entry
    entry_eval_offset           INT,                       -- seconds-to-close at entry

    -- Trigger fields
    trigger_eval_offset         INT             NOT NULL,  -- seconds-to-close when shadow exit fired
    trigger_threshold           NUMERIC(4,3)    NOT NULL,  -- 0.500 | 0.550 | 0.600 | 0.650
    p_against_at_trigger        NUMERIC(10,6)   NOT NULL,  -- TickFormer p for the WRONG side
    p_for_at_trigger            NUMERIC(10,6)   NOT NULL,  -- TickFormer p for the held side
    tickformer_model            TEXT            NOT NULL,  -- 'v18' or 'v20'

    -- CLOB snapshot at trigger (NULL when sidecar feed unavailable — see TODO in CLOBSnapshotReader)
    clob_best_bid_held          NUMERIC(10,6),             -- hypothetical exit price
    clob_best_ask_held          NUMERIC(10,6),
    clob_best_bid_against       NUMERIC(10,6),
    clob_best_ask_against       NUMERIC(10,6),
    clob_book_depth_usd         NUMERIC(14,2),             -- resting liquidity on held bid

    -- Outcome fields — NULL at trigger, backfilled when window resolves
    realized_outcome            TEXT,                      -- 'WIN' | 'LOSS' | 'PUSH'
    realized_pnl_held_to_close  NUMERIC(10,6),             -- $1 win / $0 loss minus entry cost
    realized_pnl_shadow_exit    NUMERIC(10,6),             -- would-have-realized at trigger CLOB bid
    ev_delta                    NUMERIC(10,6) GENERATED ALWAYS AS
                                    (realized_pnl_shadow_exit - realized_pnl_held_to_close) STORED
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS ix_exit_monitor_shadow_asset_window
    ON exit_monitor_shadow (asset, window_ts);

CREATE INDEX IF NOT EXISTS ix_exit_monitor_shadow_decision_id
    ON exit_monitor_shadow (decision_id);

CREATE INDEX IF NOT EXISTS ix_exit_monitor_shadow_created_at
    ON exit_monitor_shadow (created_at DESC);

CREATE INDEX IF NOT EXISTS ix_exit_monitor_shadow_threshold_outcome
    ON exit_monitor_shadow (trigger_threshold, realized_outcome);

COMMENT ON TABLE  exit_monitor_shadow IS 'Shadow exit-detection log for TickFormer u-turn signals. Detection only — no exits executed. Backfill realized_outcome + pnl fields on window close for EV analysis.';
COMMENT ON COLUMN exit_monitor_shadow.decision_id        IS 'Loose FK to strategy_decisions.id (novakash no-hard-FK convention).';
COMMENT ON COLUMN exit_monitor_shadow.trigger_threshold  IS 'p_against threshold that fired: 0.500 | 0.550 | 0.600 | 0.650. All thresholds logged per tick so re-tuning needs no re-run.';
COMMENT ON COLUMN exit_monitor_shadow.ev_delta           IS 'shadow_exit_pnl - held_to_close_pnl — positive means shadow exit would have saved money. GENERATED ALWAYS stored.';
COMMENT ON COLUMN exit_monitor_shadow.clob_best_bid_held IS 'Live CLOB bid for held side at trigger. NULL when CLOB sidecar unavailable (see CLOBSnapshotReader TODO).';
