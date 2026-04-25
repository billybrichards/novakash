import { describe, it, expect } from 'vitest';
import {
  computeImpliedDivergence,
  isModelDisagreement,
  isImpliedMarketDivergence,
  pickDivergenceBanner,
} from '../lib/divergence.js';

describe('computeImpliedDivergence', () => {
  it('returns null when either input is null', () => {
    expect(computeImpliedDivergence(null, 0.5)).toBeNull();
    expect(computeImpliedDivergence(0.5, null)).toBeNull();
    expect(computeImpliedDivergence(null, null)).toBeNull();
  });

  it('returns model − implied', () => {
    expect(computeImpliedDivergence(0.54, 0.62)).toBeCloseTo(0.08, 6);
    expect(computeImpliedDivergence(0.70, 0.50)).toBeCloseTo(-0.20, 6);
  });

  it('rejects NaN / Infinity', () => {
    expect(computeImpliedDivergence(Number.NaN, 0.5)).toBeNull();
    expect(computeImpliedDivergence(Infinity, 0.5)).toBeNull();
  });
});

describe('isModelDisagreement', () => {
  it('trusts server flag exactly — never infers from pc/pl', () => {
    expect(isModelDisagreement({ ensemble_config: { disagreement_detected: true } })).toBe(true);
    expect(isModelDisagreement({ ensemble_config: { disagreement_detected: false } })).toBe(false);
    expect(isModelDisagreement({})).toBe(false);
    expect(isModelDisagreement(null)).toBe(false);
  });
});

describe('isImpliedMarketDivergence', () => {
  it('fires above 0.10 threshold', () => {
    expect(isImpliedMarketDivergence(0.50, 0.61)).toBe(true);
    expect(isImpliedMarketDivergence(0.50, 0.60)).toBe(false); // exactly at threshold — spec says "> 0.10"
    expect(isImpliedMarketDivergence(0.50, 0.39)).toBe(true);
  });

  it('false on missing data', () => {
    expect(isImpliedMarketDivergence(null, 0.5)).toBe(false);
    expect(isImpliedMarketDivergence(0.5, null)).toBe(false);
  });
});

describe('pickDivergenceBanner', () => {
  it('prefers implied divergence when both fire', () => {
    const banner = pickDivergenceBanner({
      fiveMin: { ensemble_config: { disagreement_detected: true, disagreement_magnitude: 0.3 } },
      pu: 0.70, pc: 0.80, pl: 0.40,
      impliedPUp: 0.54,
    });
    expect(banner.kind).toBe('implied');
    expect(banner.diff).toBeCloseTo(0.16, 6);
    expect(banner.direction).toBe('model_bullish');
  });

  it('falls back to ensemble when only ensemble fires', () => {
    const banner = pickDivergenceBanner({
      fiveMin: { ensemble_config: { disagreement_detected: true, disagreement_magnitude: 0.30 } },
      pu: 0.60, pc: 0.80, pl: 0.40,
      impliedPUp: 0.58, // within 0.10 of pu → no implied divergence
    });
    expect(banner.kind).toBe('ensemble');
    expect(banner.magnitude).toBeCloseTo(0.30, 6);
  });

  it('null when all quiet', () => {
    const banner = pickDivergenceBanner({
      fiveMin: { ensemble_config: { disagreement_detected: false } },
      pu: 0.60, pc: 0.62, pl: 0.58,
      impliedPUp: 0.59,
    });
    expect(banner).toBeNull();
  });

  it('null when impliedPUp missing AND no ensemble disagreement', () => {
    const banner = pickDivergenceBanner({
      fiveMin: { ensemble_config: { disagreement_detected: false } },
      pu: 0.60, pc: 0.62, pl: 0.58,
      impliedPUp: null,
    });
    expect(banner).toBeNull();
  });
});
