// Theme tokens — shared across all Polymarket Monitor sub-components.
// Matches existing dark-theme palette from execution-hq/constants.js and FactoryFloor.jsx.
export const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: '#0f172a',
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
  mono: "'JetBrains Mono', 'IBM Plex Mono', monospace",
  border: 'rgba(51, 65, 85, 0.6)',
};

// Signal display names (SQ-01 / Section 6 of design spec)
export const SIGNAL_NAMES = {
  elm: 'Sequoia v5.2',
  cascade: 'Cascade',
  taker: 'Taker Flow',
  oi: 'Open Interest',
  funding: 'Funding Rate',
  vpin: 'VPIN',
  momentum: 'Momentum',
};

// Gate display names for the 8-gate pipeline (Band 4)
export const GATE_NAMES = {
  eval_offset: 'EvalOffset',
  gate_agreement: 'SrcAgree',
  gate_delta: 'Delta',
  gate_taker: 'Taker',
  gate_cg_veto: 'CGConfirm',
  gate_dune: 'DUNE',
  gate_spread: 'Spread',
  gate_cap: 'DynCap',
};

// Helpers
export function fmt(v, decimals = 2) {
  if (v == null || isNaN(v)) return '\u2014';
  return Number(v).toFixed(decimals);
}

export function utcHHMM(ts) {
  if (!ts) return '\u2014';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(11, 16);
}

export function pct(v) {
  if (v == null || isNaN(v)) return '\u2014';
  return (Number(v) * 100).toFixed(1) + '%';
}
