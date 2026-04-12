// Theme tokens — shared across all Polymarket Monitor sub-components.
// Supports dark (default) and light modes via getTheme(mode).

const DARK = {
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

const LIGHT = {
  bg: '#f8fafc',
  card: 'rgba(255, 255, 255, 0.95)',
  cardBorder: 'rgba(203, 213, 225, 1)',
  headerBg: '#ffffff',
  text: 'rgba(15, 23, 42, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(148, 163, 184, 1)',
  cyan: '#0891b2',
  cyanDim: 'rgba(8, 145, 178, 0.12)',
  green: '#059669',
  red: '#dc2626',
  amber: '#d97706',
  purple: '#9333ea',
  white: '#0f172a',
  mono: "'JetBrains Mono', 'IBM Plex Mono', monospace",
  border: 'rgba(203, 213, 225, 0.8)',
};

export function getTheme(mode) {
  return mode === 'light' ? LIGHT : DARK;
}

// Backward compat — existing imports of `T` continue to work unchanged.
export const T = DARK;

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
