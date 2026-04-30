// /desk — operator alert detectors (VHC bypass, M2M stop-loss).
//
// VHC bypass:
//   The v9 engine surfaces a server flag the FE prefers when present.
//   We DO NOT compute a client-side fallback — the engine's true bypass
//   gate involves oracle agreement (Chainlink + Tiingo deltas concur
//   with LGB direction) plus several yaml-driven preconditions that
//   the FE doesn't have visibility into. A client-side approximation
//   would generate false-positive "trading at 2× Kelly" banners on
//   windows the engine actually skipped — the worst possible UX during
//   live trading. Banner stays dormant until the engine emits the flag.
//
// M2M stop-loss:
//   Same fail-closed posture — only fires on a strict-true server flag.
//   No client guess.
//
// All bool checks use strict equality on `true` so that string "false",
// "0", and other truthy-but-not-true values don't fire false positives.

function strictBool(v) {
  return v === true;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * Detect VHC bypass active state from a 5m snapshot block.
 *
 * @returns {{ active: boolean, edge: number | null }}
 */
export function detectVhcActive(fiveMin) {
  const ensemble = fiveMin?.ensemble_config || {};
  const active = strictBool(ensemble.vhc_bypass_active)
    || strictBool(ensemble.vhc_active);

  const pc = num(fiveMin?.probability_classifier
    ?? fiveMin?.probabilityClassifier
    ?? fiveMin?.p_classifier);
  const edge = pc == null ? null : Math.abs(pc - 0.5);

  return { active, edge };
}

/**
 * Detect M2M stop-loss state from a 5m snapshot block. Returns
 * { active, unrealizedPnl }. Reads the optional position_monitor /
 * position blocks; both absent → { active: false, ... }.
 */
export function detectM2MStopLoss(fiveMin) {
  const monitor = fiveMin?.position_monitor ?? fiveMin?.position ?? null;
  if (!monitor) return { active: false, unrealizedPnl: null };
  const active = strictBool(monitor.stop_loss_triggered)
    || strictBool(monitor.m2m_stop_active)
    || monitor.detector_type === 'mark_to_market';
  return {
    active,
    unrealizedPnl: num(monitor.unrealized_pnl ?? monitor.upnl),
  };
}
