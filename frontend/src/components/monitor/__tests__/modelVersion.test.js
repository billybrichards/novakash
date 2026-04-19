import { describe, it, expect } from 'vitest';
import { parseModelVersion, formatModelDate } from '../modelVersion.js';

describe('parseModelVersion', () => {
  it('parses a valid model_version string', () => {
    const out = parseModelVersion('a547c3d@v2/btc/btc_5m/a547c3d/2026-04-16T04-48-24Z');
    expect(out).not.toBeNull();
    expect(out.hash).toBe('a547c3d');
    expect(out.reg).toBe('2');
    expect(out.asset).toBe('btc');
    expect(out.tf_id).toBe('btc_5m');
    expect(out.hash2).toBe('a547c3d');
    expect(out.date).toBe('2026-04-16T04-48-24Z');
    expect(out.raw).toBe('a547c3d@v2/btc/btc_5m/a547c3d/2026-04-16T04-48-24Z');
  });

  it('returns null for a malformed string', () => {
    expect(parseModelVersion('just-some-garbage')).toBeNull();
    // Missing registry version
    expect(parseModelVersion('a547c3d@/btc/btc_5m/a547c3d/2026-04-16')).toBeNull();
  });

  it('returns null for empty / nullish input', () => {
    expect(parseModelVersion('')).toBeNull();
    expect(parseModelVersion(null)).toBeNull();
    expect(parseModelVersion(undefined)).toBeNull();
    expect(parseModelVersion(12345)).toBeNull();
  });
});

describe('formatModelDate', () => {
  it('formats a spec-shape date', () => {
    expect(formatModelDate('2026-04-16T04-48-24Z')).toBe('2026-04-16 04:48Z');
  });

  it('returns raw on unparseable input', () => {
    expect(formatModelDate('not-a-date')).toBe('not-a-date');
  });

  it('returns em-dash on empty', () => {
    expect(formatModelDate(null)).toBe('—');
    expect(formatModelDate('')).toBe('—');
  });
});
