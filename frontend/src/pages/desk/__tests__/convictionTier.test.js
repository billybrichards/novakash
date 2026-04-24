import { describe, it, expect } from 'vitest';
import {
  convictionTier,
  isClassifierHighConviction,
  probDisagreement,
  isSourceConflict,
  directionFromProbUp,
} from '../lib/conviction.js';

describe('convictionTier', () => {
  it('returns NONE for null / NaN', () => {
    expect(convictionTier(null)).toBe('NONE');
    expect(convictionTier(undefined)).toBe('NONE');
    expect(convictionTier(Number.NaN)).toBe('NONE');
  });

  it('buckets edge correctly on the boundaries', () => {
    // edge = |p - 0.5|
    expect(convictionTier(0.50)).toBe('NONE');          // edge 0
    expect(convictionTier(0.549)).toBe('NONE');         // edge 0.049
    expect(convictionTier(0.55)).toBe('LOW');           // edge 0.05
    expect(convictionTier(0.62)).toBe('MEDIUM');        // edge 0.12
    expect(convictionTier(0.71)).toBe('HIGH');          // edge 0.21 (avoid FP drift at 0.20)
    expect(convictionTier(0.76)).toBe('VERY_HIGH');     // edge 0.26 (avoid FP drift at 0.25)
    expect(convictionTier(1.0)).toBe('VERY_HIGH');
  });

  it('is symmetric around 0.5', () => {
    expect(convictionTier(0.60)).toBe(convictionTier(0.40));
    expect(convictionTier(0.75)).toBe(convictionTier(0.25));
  });
});

describe('isClassifierHighConviction (VHC)', () => {
  it('is false for null', () => {
    expect(isClassifierHighConviction(null)).toBe(false);
    expect(isClassifierHighConviction(undefined)).toBe(false);
  });
  it('fires at |pc - 0.5| >= 0.25', () => {
    expect(isClassifierHighConviction(0.5)).toBe(false);
    expect(isClassifierHighConviction(0.74)).toBe(false);
    expect(isClassifierHighConviction(0.75)).toBe(true);
    expect(isClassifierHighConviction(0.25)).toBe(true);
    expect(isClassifierHighConviction(0.20)).toBe(true);
  });
});

describe('probDisagreement / isSourceConflict', () => {
  it('returns null when either input is null', () => {
    expect(probDisagreement(null, 0.5)).toBeNull();
    expect(probDisagreement(0.5, null)).toBeNull();
    expect(isSourceConflict(null, 0.5)).toBe(false);
  });
  it('flags conflict only above 0.25 (strict)', () => {
    expect(isSourceConflict(0.5, 0.5)).toBe(false);
    expect(isSourceConflict(0.5, 0.75)).toBe(false); // exactly 0.25 — not strict
    expect(isSourceConflict(0.5, 0.76)).toBe(true);
    expect(isSourceConflict(0.8, 0.3)).toBe(true);
  });
});

describe('directionFromProbUp', () => {
  it('returns UP / DOWN around 0.5', () => {
    expect(directionFromProbUp(0.51)).toBe('UP');
    expect(directionFromProbUp(0.49)).toBe('DOWN');
    expect(directionFromProbUp(0.5)).toBeNull();
    expect(directionFromProbUp(null)).toBeNull();
  });
});
