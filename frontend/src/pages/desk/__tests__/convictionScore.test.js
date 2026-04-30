import { describe, it, expect } from 'vitest';
import { tierForScore, TIER_BREAKS } from '../lib/convictionScore.js';

describe('tierForScore', () => {
  it('returns NONE for null', () => {
    expect(tierForScore(null).label).toBe('NONE');
  });

  it('returns NONE for NaN', () => {
    expect(tierForScore(NaN).label).toBe('NONE');
  });

  it('returns NONE for 0', () => {
    expect(tierForScore(0).label).toBe('NONE');
  });

  it('returns NONE below LOW threshold', () => {
    expect(tierForScore(0.24).label).toBe('NONE');
  });

  it('returns LOW at boundary', () => {
    expect(tierForScore(0.25).label).toBe('LOW');
  });

  it('returns MEDIUM at boundary', () => {
    expect(tierForScore(0.50).label).toBe('MEDIUM');
  });

  it('returns HIGH at boundary', () => {
    expect(tierForScore(0.70).label).toBe('HIGH');
  });

  it('returns VERY HIGH at boundary', () => {
    expect(tierForScore(0.85).label).toBe('VERY HIGH');
  });

  it('returns VERY HIGH at 1.0', () => {
    expect(tierForScore(1.0).label).toBe('VERY HIGH');
  });

  it('returns NONE for negative scores', () => {
    expect(tierForScore(-0.5).label).toBe('NONE');
  });

  it('TIER_BREAKS has 5 entries', () => {
    expect(TIER_BREAKS).toHaveLength(5);
  });
});
