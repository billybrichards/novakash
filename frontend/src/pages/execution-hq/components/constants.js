// Theme tokens matching the existing codebase dark theme
export const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: 'rgba(30, 41, 59, 1)',
  headerBorder: 'rgba(51, 65, 85, 1)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  cyanDim: 'rgba(6, 182, 212, 0.2)',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  white: '#fff',
};

// Gate names for the audit matrix — v9.0 pipeline
export const GATES = [
  'gate_agreement',
  'gate_vpin',
  'gate_delta',
  'gate_cg_veto',
  'gate_cap',
];

// Legacy gates kept for backwards compat with pre-v9 windows
export const LEGACY_GATES = [
  'gate_vpin',
  'gate_delta',
  'gate_cg_veto',
  'gate_macro',
  'gate_divergence',
  'gate_floor',
  'gate_cap',
  'gate_confidence',
];

// Evaluation checkpoint offsets (seconds before window close)
export const CHECKPOINTS = [240, 230, 220, 210, 200, 190, 180, 170, 160, 150, 140, 130, 120, 110, 100, 90, 80, 70, 60];

// v9.0 two-tier dynamic entry caps
// EARLY_CASCADE: T-240..T-130, VPIN >= 0.65 -> $0.55
// GOLDEN:        T-130..T-60,  VPIN >= 0.45 -> $0.65
export const getEntryCap = (t, vpin) => {
  if (t > 130) return 0.55;  // Early zone
  if (t > 60) return 0.65;   // Golden zone
  return 0.73;                // Post-eval (shouldn't trade here)
};

// Pi bonus: cap + 3.14 cents when CLOB is within pi% of cap
export const PI_BONUS_CENTS = 0.0314;
export const getCapWithPi = (cap) => Math.round((cap + PI_BONUS_CENTS) * 100) / 100;

// Window status helpers
export const windowStatusColor = (w) => {
  if (w.trade_placed && w.poly_outcome === 'WIN') return T.green;
  if (w.trade_placed && w.poly_outcome === 'LOSS') return T.red;
  if (!w.trade_placed && w.shadow_would_win) return T.amber;
  if (w.trade_placed) return T.cyan;
  return T.textDim;
};

export const windowStatusLabel = (w) => {
  if (w.trade_placed && w.poly_outcome === 'WIN') return 'WIN';
  if (w.trade_placed && w.poly_outcome === 'LOSS') return 'LOSS';
  if (!w.trade_placed && w.shadow_would_win) return 'MISSED';
  if (w.trade_placed) return 'OPEN';
  return 'SKIP';
};
