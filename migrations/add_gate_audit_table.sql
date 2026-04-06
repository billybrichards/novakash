-- Migration: v8.0 gate_audit table
-- Date: 2026-04-06
-- Purpose: Per-window gate pass/fail audit trail for signal analysis

CREATE TABLE IF NOT EXISTS gate_audit (
    id              BIGSERIAL PRIMARY KEY,
    window_ts       BIGINT NOT NULL,
    asset           VARCHAR(10) NOT NULL,
    timeframe       VARCHAR(5) NOT NULL DEFAULT '5m',
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    engine_version  VARCHAR(10) NOT NULL DEFAULT 'v8.0',

    -- Delta source used for this evaluation
    delta_source    VARCHAR(20),    -- 'tiingo' | 'chainlink' | 'binance'

    -- Price inputs
    open_price      FLOAT,
    tiingo_open     FLOAT,
    tiingo_close    FLOAT,
    delta_tiingo    FLOAT,
    delta_binance   FLOAT,
    delta_chainlink FLOAT,
    delta_pct       FLOAT,          -- primary delta used for decision

    -- VPIN
    vpin            FLOAT,
    regime          VARCHAR(15),    -- 'CASCADE' | 'TRANSITION' | 'NORMAL' | 'CALM'

    -- Gate results (TRUE = passed, FALSE = failed, NULL = not evaluated)
    gate_vpin       BOOLEAN,
    gate_delta      BOOLEAN,
    gate_cg         BOOLEAN,
    gate_floor      BOOLEAN,
    gate_cap        BOOLEAN,

    -- Overall result
    gate_passed     BOOLEAN,        -- TRUE if all required gates passed
    gate_failed     VARCHAR(20),    -- name of first failed gate (NULL if all passed)
    gates_passed_list TEXT,         -- CSV: 'vpin,delta,cg,floor,cap'
    decision        VARCHAR(10),    -- 'TRADE' | 'SKIP'
    skip_reason     TEXT,

    CONSTRAINT gate_audit_window_uq UNIQUE (window_ts, asset, timeframe)
);

CREATE INDEX IF NOT EXISTS gate_audit_ts_idx ON gate_audit (window_ts DESC);
CREATE INDEX IF NOT EXISTS gate_audit_asset_idx ON gate_audit (asset, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS gate_audit_decision_idx ON gate_audit (decision, evaluated_at DESC);
