import { describe, it, expect } from 'vitest';
import {
  dedupeByWindow,
  verdictFor,
  summarise,
} from '../ComparativeWRCard.jsx';

describe('verdictFor', () => {
  it('returns WIN when p>0.5 and actual UP', () => {
    expect(verdictFor(0.7, 'UP')).toBe('WIN');
  });
  it('returns LOSS when p>0.5 and actual DOWN', () => {
    expect(verdictFor(0.8, 'DOWN')).toBe('LOSS');
  });
  it('returns WIN when p<0.5 and actual DOWN', () => {
    expect(verdictFor(0.2, 'DOWN')).toBe('WIN');
  });
  it('returns LOSS when p<0.5 and actual UP', () => {
    expect(verdictFor(0.2, 'UP')).toBe('LOSS');
  });
  it('abstains on null probability', () => {
    expect(verdictFor(null, 'UP')).toBeNull();
  });
  it('abstains on p=0.5 exactly', () => {
    expect(verdictFor(0.5, 'UP')).toBeNull();
  });
  it('abstains on unresolved actual', () => {
    expect(verdictFor(0.8, null)).toBeNull();
    expect(verdictFor(0.8, 'UNKNOWN')).toBeNull();
  });
});

describe('dedupeByWindow', () => {
  it('keeps last sample per window_ts', () => {
    const samples = [
      { window_ts: 100, probability_up: 0.3 },
      { window_ts: 100, probability_up: 0.5 },
      { window_ts: 200, probability_up: 0.7 },
      { window_ts: 100, probability_up: 0.9 },
    ];
    const map = dedupeByWindow(samples);
    expect(map.size).toBe(2);
    expect(map.get(100).probability_up).toBe(0.9);
    expect(map.get(200).probability_up).toBe(0.7);
  });
  it('skips samples without window_ts', () => {
    const samples = [
      { window_ts: null, probability_up: 0.3 },
      { window_ts: 100, probability_up: 0.5 },
    ];
    expect(dedupeByWindow(samples).size).toBe(1);
  });
});

describe('summarise', () => {
  it('counts WIN/LOSS and computes WR', () => {
    const s = summarise(['WIN', 'LOSS', 'WIN', null, 'WIN']);
    expect(s.n).toBe(4);
    expect(s.wins).toBe(3);
    expect(s.losses).toBe(1);
    expect(s.wr).toBe(0.75);
  });
  it('null WR when nothing settled', () => {
    expect(summarise([null, null]).wr).toBeNull();
  });
});
