// /desk Phase 2 — divergence math.
//
// Two distinct divergence signals feed the banner:
//
//   1. Model disagreement — ensemble_config.disagreement_detected from
//      /v4/snapshot. Server computes this with its own threshold (the
//      spec warns us to NOT naively do `|pc - pl| > 0.25` — the server
//      threshold is regime-aware).
//
//   2. Implied-vs-model — |implied_p_up - p_up| > 0.10, where
//      implied_p_up comes from the CLOB YES mid.
//
// Both can fire independently; the banner picks the most actionable.

const IMPLIED_VS_MODEL_THRESHOLD = 0.10;

export function computeImpliedDivergence(impliedPUp, modelPUp) {
  if (impliedPUp == null || modelPUp == null) return null;
  const a = Number(impliedPUp);
  const b = Number(modelPUp);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return null;
  return b - a; // positive → model more bullish than market
}

export function isModelDisagreement(fiveMin) {
  // Strictly trust server — never second-guess with |pc - pl|.
  return Boolean(fiveMin?.ensemble_config?.disagreement_detected);
}

export function isImpliedMarketDivergence(impliedPUp, modelPUp) {
  const diff = computeImpliedDivergence(impliedPUp, modelPUp);
  if (diff == null) return false;
  return Math.abs(diff) > IMPLIED_VS_MODEL_THRESHOLD;
}

/**
 * Pick which banner to show (or null if all quiet).
 * Returns { kind: 'implied' | 'ensemble', ... }.
 */
export function pickDivergenceBanner({
  fiveMin,
  pu, pc, pl,
  impliedPUp,
}) {
  // Implied gap wins — it's a more actionable, tradeable divergence
  // (market vs model) than two-models-within-the-ensemble disagreeing.
  if (isImpliedMarketDivergence(impliedPUp, pu)) {
    const diff = computeImpliedDivergence(impliedPUp, pu);
    return {
      kind: 'implied',
      impliedPUp,
      modelPUp: pu,
      diff,
      direction: diff > 0 ? 'model_bullish' : 'model_bearish',
    };
  }

  if (isModelDisagreement(fiveMin)) {
    const magnitude =
      Number(fiveMin?.ensemble_config?.disagreement_magnitude) || null;
    return {
      kind: 'ensemble',
      pc,
      pl,
      magnitude,
    };
  }

  return null;
}

export const _TEST_ONLY = { IMPLIED_VS_MODEL_THRESHOLD };
