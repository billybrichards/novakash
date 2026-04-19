import { describe, it, expect } from 'vitest';
import {
  computeMetrics,
  AMBER_THRESHOLD,
  RED_THRESHOLD,
} from '../SaturationMeter.jsx';

describe('computeMetrics', () => {
  it('empty buffer → zeros', () => {
    expect(computeMetrics([])).toEqual({
      current: null,
      atEdge: 0,
      n: 0,
      frac: 0,
      streak: 0,
    });
  });

  it('counts edge samples correctly', () => {
    const s = [
      { probability_classifier: 0.005 },
      { probability_classifier: 0.5 },
      { probability_classifier: 0.995 },
      { probability_classifier: 0.7 },
    ];
    const m = computeMetrics(s);
    expect(m.n).toBe(4);
    expect(m.atEdge).toBe(2);
    expect(m.frac).toBe(0.5);
  });

  it('tracks current streak from newest end', () => {
    const s = [
      { probability_classifier: 0.3 }, // interior — before streak
      { probability_classifier: 0.5 }, // interior
      { probability_classifier: 0.995 }, // streak start
      { probability_classifier: 1.0 },
      { probability_classifier: 0.999 },
    ];
    expect(computeMetrics(s).streak).toBe(3);
  });

  it('streak 0 when newest is interior', () => {
    const s = [
      { probability_classifier: 1.0 },
      { probability_classifier: 0.5 }, // newest interior
    ];
    expect(computeMetrics(s).streak).toBe(0);
  });

  it('ignores null samples from denominator', () => {
    const s = [
      { probability_classifier: null },
      { probability_classifier: 0.5 },
      { probability_classifier: 0.999 },
    ];
    const m = computeMetrics(s);
    expect(m.n).toBe(2);
    expect(m.atEdge).toBe(1);
    expect(m.frac).toBe(0.5);
  });

  it('thresholds are consistent', () => {
    expect(RED_THRESHOLD).toBeGreaterThan(AMBER_THRESHOLD);
  });
});
