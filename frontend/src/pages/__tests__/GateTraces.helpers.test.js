import { describe, it, expect } from 'vitest';
import {
  formatSkipReason,
  humanSkipReason,
  formatTopSkipReasonsTooltip,
} from '../GateTraces.jsx';

describe('formatSkipReason', () => {
  it('converts snake_case to sentence case', () => {
    expect(formatSkipReason('conviction_below_threshold')).toBe(
      'Conviction below threshold'
    );
  });

  it('uppercases known acronyms: vpin', () => {
    expect(formatSkipReason('vpin_too_low')).toBe('VPIN too low');
  });

  it('uppercases known acronyms: btc', () => {
    expect(formatSkipReason('btc_price_stale')).toBe('BTC price stale');
  });

  it('uppercases known acronyms: pnl', () => {
    expect(formatSkipReason('daily_pnl_limit_hit')).toBe('Daily PnL limit hit');
  });

  it('returns "—" for null', () => {
    expect(formatSkipReason(null)).toBe('—');
  });

  it('returns "—" for undefined', () => {
    expect(formatSkipReason(undefined)).toBe('—');
  });

  it('returns "—" for empty string', () => {
    expect(formatSkipReason('')).toBe('—');
  });

  it('handles single-word reason', () => {
    expect(formatSkipReason('timeout')).toBe('Timeout');
  });
});

describe('humanSkipReason', () => {
  it('prefixes the formatted reason with "Skipped:"', () => {
    expect(humanSkipReason('vpin_too_low')).toBe('Skipped: VPIN too low');
  });

  it('returns fallback when no reason provided', () => {
    expect(humanSkipReason(null)).toBe('No skip reason recorded');
    expect(humanSkipReason(undefined)).toBe('No skip reason recorded');
    expect(humanSkipReason('')).toBe('No skip reason recorded');
  });
});

describe('formatTopSkipReasonsTooltip', () => {
  it('returns empty string for empty array', () => {
    expect(formatTopSkipReasonsTooltip([], 5)).toBe('');
  });

  it('returns empty string for null/undefined', () => {
    expect(formatTopSkipReasonsTooltip(null, 5)).toBe('');
    expect(formatTopSkipReasonsTooltip(undefined, 5)).toBe('');
  });

  it('formats a single reason with rank, count, and percentage', () => {
    const result = formatTopSkipReasonsTooltip(
      [{ reason: 'vpin_too_low', n: 8 }],
      10
    );
    expect(result).toContain('Top skip reasons:');
    expect(result).toContain('1. VPIN too low');
    expect(result).toContain('8×');
    expect(result).toContain('(80%)');
  });

  it('ranks multiple reasons in order', () => {
    const result = formatTopSkipReasonsTooltip(
      [
        { reason: 'vpin_too_low', n: 5 },
        { reason: 'conviction_below_threshold', n: 3 },
      ],
      8
    );
    expect(result).toContain('1. VPIN too low');
    expect(result).toContain('2. Conviction below threshold');
  });

  it('omits percentage when skippedCount is 0', () => {
    const result = formatTopSkipReasonsTooltip(
      [{ reason: 'timeout', n: 2 }],
      0
    );
    expect(result).toContain('2×');
    expect(result).not.toContain('%');
  });

  it('leads with a blank line before the header', () => {
    const result = formatTopSkipReasonsTooltip(
      [{ reason: 'timeout', n: 1 }],
      1
    );
    expect(result.startsWith('\n')).toBe(true);
  });
});
