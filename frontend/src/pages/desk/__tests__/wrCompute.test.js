// Confirms that the /desk page uses the canonical computeWr helper
// correctly to colour its last-N-windows footer row.

import { describe, it, expect } from 'vitest';
import { computeWr } from '../../../lib/wr.js';

describe('computeWr — last-20 table footer math', () => {
  it('ignores pending rows in the denominator', () => {
    const rows = [
      { outcome: 'WIN' },
      { outcome: 'LOSS' },
      { outcome: null },       // pending, excluded
      { outcome: 'WIN' },
    ];
    const { n, wr, pending } = computeWr(rows);
    expect(n).toBe(3);
    expect(pending).toBe(1);
    expect(wr).toBeCloseTo(2 / 3, 5);
  });

  it('handles `won` boolean shape', () => {
    const rows = [{ won: true }, { won: false }, { won: true }];
    const { n, wr } = computeWr(rows);
    expect(n).toBe(3);
    expect(wr).toBeCloseTo(2 / 3, 5);
  });

  it('returns wr=null for zero-settled', () => {
    expect(computeWr([]).wr).toBeNull();
    expect(computeWr([{ outcome: null }]).wr).toBeNull();
  });
});
