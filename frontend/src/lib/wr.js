// Single source of truth for win-rate computation across FE pages.
// Keeps SignalExplorer + Strategies + any future callers in sync.
//
// A row counts as SETTLED when either `outcome ∈ {WIN, LOSS}` OR
// `won ∈ {true, false}`. Pending/null rows are excluded from the
// denominator so WR isn't diluted during active trading hours.

export function computeWr(rows) {
  if (!Array.isArray(rows)) return { n: 0, pending: 0, wr: null };
  const isSettled = r => r.outcome === 'WIN' || r.outcome === 'LOSS' || r.won === true || r.won === false;
  const isWin = r => r.outcome === 'WIN' || r.won === true;
  const settled = rows.filter(isSettled);
  const wins = settled.filter(isWin).length;
  return {
    n: settled.length,
    pending: rows.length - settled.length,
    wr: settled.length > 0 ? wins / settled.length : null,
  };
}
