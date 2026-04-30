-- View: v_signal_comparison
-- Purpose: easy comparison of all signal sources per resolved window.
--          Use to compare WR / ECE / agreement across v9 / v10 / v12 / blend
--          / classifier without writing complex JOINs each time.
-- Per hub note #285 (signal comparison framework).
-- Safe to run multiple times (CREATE OR REPLACE VIEW).

CREATE OR REPLACE VIEW v_signal_comparison AS
SELECT
  ws.window_ts,
  ws.asset,
  ws.timeframe,
  ws.eval_offset,
  ws.regime,
  ws.outcome,
  -- raw signals
  ws.ensemble_p_lgb        AS p_v9,
  ws.ensemble_p_classifier AS p_classifier,
  ws.probability_lgb_v12   AS p_v12,
  -- existing blend
  ws.ensemble_p_up         AS p_blend,
  -- derived directions
  CASE WHEN ws.ensemble_p_lgb > 0.5 THEN 'UP'
       WHEN ws.ensemble_p_lgb < 0.5 THEN 'DOWN' END AS dir_v9,
  CASE WHEN ws.probability_lgb_v12 > 0.5 THEN 'UP'
       WHEN ws.probability_lgb_v12 < 0.5 THEN 'DOWN' END AS dir_v12,
  CASE WHEN ws.ensemble_p_up > 0.5 THEN 'UP'
       WHEN ws.ensemble_p_up < 0.5 THEN 'DOWN' END AS dir_blend,
  -- correctness flags
  (CASE WHEN ws.ensemble_p_lgb > 0.5 THEN 'UP' WHEN ws.ensemble_p_lgb < 0.5 THEN 'DOWN' END = ws.outcome) AS v9_correct,
  (CASE WHEN ws.probability_lgb_v12 > 0.5 THEN 'UP' WHEN ws.probability_lgb_v12 < 0.5 THEN 'DOWN' END = ws.outcome) AS v12_correct,
  (CASE WHEN ws.ensemble_p_up > 0.5 THEN 'UP' WHEN ws.ensemble_p_up < 0.5 THEN 'DOWN' END = ws.outcome) AS blend_correct,
  -- distances (confidence relative to 0.5)
  abs(ws.ensemble_p_lgb - 0.5)        AS dist_v9,
  abs(ws.probability_lgb_v12 - 0.5)   AS dist_v12,
  abs(ws.ensemble_p_up - 0.5)         AS dist_blend,
  -- agreement flags
  (sign(ws.ensemble_p_lgb - 0.5) = sign(ws.probability_lgb_v12 - 0.5)) AS v9_v12_agree
FROM window_snapshots ws
WHERE ws.outcome IN ('UP','DOWN');

COMMENT ON VIEW v_signal_comparison IS
  'Signal source comparison helper. Per hub note #285 / framework. Joins resolved windows with all probabilities + correctness flags + distances.';
