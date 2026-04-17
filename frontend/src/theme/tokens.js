// Single source of truth for color + font tokens used by the new lean shell
// and every Tier-1 page. Legacy pages under /archive/* keep their own inline
// colors — don't touch them.
//
// To reskin, edit this file. Anything that imports from `../theme/tokens.js`
// gets the new palette.

export const T = {
  // Surfaces
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  borderStrong: 'rgba(255,255,255,0.12)',

  // Text
  text: 'rgba(255,255,255,0.9)',
  label: 'rgba(255,255,255,0.3)',
  label2: 'rgba(255,255,255,0.55)',

  // Accent
  purple: '#a855f7',
  cyan: '#06b6d4',

  // Signal
  profit: '#4ade80',
  loss: '#f87171',
  warn: '#f59e0b',

  // Grid / chart
  grid: 'rgba(255,255,255,0.04)',

  // Font
  font: "'IBM Plex Mono', monospace",
};

// WR coloring helper — used by SignalExplorer matrix + any future WR cells.
// Three tiers tuned against observed strategy grades:
//  - >= 0.70 → profit (true alpha)
//  - 0.55-0.70 → warn (marginal / informational)
//  - < 0.55 → loss (no edge or anti-predictive)
//  - null → label (no signal yet)
export function wrColor(wr) {
  if (wr == null) return T.label;
  if (wr >= 0.70) return T.profit;
  if (wr >= 0.55) return T.warn;
  return T.loss;
}

// WR tone helper for `Stat` cards — maps WR to { 'good' | 'warn' | 'bad' | undefined }.
export function wrTone(wr) {
  if (wr == null) return undefined;
  if (wr >= 0.70) return 'good';
  if (wr >= 0.55) return 'warn';
  return 'bad';
}
