import { describe, it, expect } from 'vitest';
import {
  readPath,
  renderValue,
  stableKey,
  modalKey,
  buildRow,
  MISSING,
  KNOBS,
} from '../knobs.js';

describe('readPath', () => {
  it('returns value at a nested dotted path', () => {
    expect(readPath({ a: { b: { c: 42 } } }, 'a.b.c')).toBe(42);
  });

  it('returns MISSING when any segment is absent', () => {
    expect(readPath({ a: { b: {} } }, 'a.b.c')).toBe(MISSING);
    expect(readPath({}, 'a')).toBe(MISSING);
  });

  it('distinguishes absent from explicit false / null / 0', () => {
    expect(readPath({ gp: { flag: false } }, 'gp.flag')).toBe(false);
    expect(readPath({ gp: { flag: null } }, 'gp.flag')).toBe(null);
    expect(readPath({ gp: { n: 0 } }, 'gp.n')).toBe(0);
    // absent is different
    expect(readPath({ gp: {} }, 'gp.flag')).toBe(MISSING);
  });

  it('handles null input gracefully', () => {
    expect(readPath(null, 'a')).toBe(MISSING);
    expect(readPath(undefined, 'a')).toBe(MISSING);
  });
});

describe('renderValue', () => {
  it('renders MISSING as em-dash', () => {
    expect(renderValue(MISSING)).toBe('—');
  });

  it('renders null literal', () => {
    expect(renderValue(null)).toBe('null');
  });

  it('renders booleans as text', () => {
    expect(renderValue(true)).toBe('true');
    expect(renderValue(false)).toBe('false');
  });

  it('renders arrays with bracket syntax', () => {
    expect(renderValue([1, 2, 3])).toBe('[1, 2, 3]');
    expect(renderValue([])).toBe('[]');
    expect(renderValue(['calm', 'vol'])).toBe('[calm, vol]');
  });

  it('renders numbers preserving 0', () => {
    expect(renderValue(0)).toBe('0');
    expect(renderValue(0.0)).toBe('0');
    expect(renderValue(0.025)).toBe('0.025');
  });

  it('renders objects as JSON', () => {
    expect(renderValue({ a: 1 })).toBe('{"a":1}');
  });
});

describe('stableKey', () => {
  it('distinguishes MISSING from null from false from 0', () => {
    const keys = new Set([
      stableKey(MISSING),
      stableKey(null),
      stableKey(false),
      stableKey(0),
    ]);
    expect(keys.size).toBe(4);
  });

  it('is stable for equal arrays regardless of identity', () => {
    expect(stableKey([1, 2])).toBe(stableKey([1, 2]));
  });

  it('is stable for equal primitives', () => {
    expect(stableKey('x')).toBe(stableKey('x'));
    expect(stableKey(0.025)).toBe(stableKey(0.025));
  });
});

describe('modalKey', () => {
  it('picks the majority value', () => {
    const vals = [45, 45, 45, 60];
    const mode = modalKey(vals);
    expect(mode).toBe(stableKey(45));
  });

  it('returns null when every value is distinct (no modal group)', () => {
    const vals = [1, 2, 3, 4];
    expect(modalKey(vals)).toBe(null);
  });

  it('handles tied values by first-seen order', () => {
    // Two 'a' and two 'b' — 'a' seen first, should win.
    const vals = ['a', 'b', 'a', 'b'];
    expect(modalKey(vals)).toBe(stableKey('a'));
  });

  it('modes on MISSING when most values are absent', () => {
    const vals = [MISSING, MISSING, MISSING, true];
    expect(modalKey(vals)).toBe(stableKey(MISSING));
  });

  it('returns null for empty input', () => {
    expect(modalKey([])).toBe(null);
  });

  it('treats 0.0 (present) differently from MISSING', () => {
    // ensemble_disagreement_threshold=0.0 is ENABLED-with-zero, not absent.
    const vals = [0.0, 0.0, MISSING, 0.2];
    const mode = modalKey(vals);
    // 0.0 appears twice, wins modal.
    expect(mode).toBe(stableKey(0));
    // And absent is NOT the modal.
    expect(mode).not.toBe(stableKey(MISSING));
  });
});

describe('buildRow', () => {
  const knob = { id: 'health_gate', path: 'gate_params.health_gate' };
  const strategies = [
    { id: 'v4_fusion',   yaml: { gate_params: { health_gate: 'degraded' } } },
    { id: 'v5_ensemble', yaml: { gate_params: { health_gate: 'degraded' } } },
    { id: 'v5_fresh',    yaml: { gate_params: { health_gate: 'unsafe' } } },
    { id: 'v6_sniper',   yaml: { gate_params: { health_gate: 'degraded' } } },
  ];

  it('flags the modal cells and highlights the outlier', () => {
    const cells = buildRow(knob, strategies);
    expect(cells.map(c => c.value)).toEqual(['degraded', 'degraded', 'unsafe', 'degraded']);
    expect(cells.map(c => c.isModal)).toEqual([true, true, false, true]);
  });

  it('marks missing keys and still computes modal across present ones', () => {
    const s2 = [
      { id: 'a', yaml: { gate_params: { x: 1 } } },
      { id: 'b', yaml: { gate_params: {} } },
      { id: 'c', yaml: { gate_params: { x: 1 } } },
    ];
    const cells = buildRow({ id: 'x', path: 'gate_params.x' }, s2);
    expect(cells[0].isMissing).toBe(false);
    expect(cells[1].isMissing).toBe(true);
    expect(cells[2].isMissing).toBe(false);
    expect(cells[0].isModal).toBe(true);
    expect(cells[2].isModal).toBe(true);
  });

  it('supports custom get() for synthesised values', () => {
    const synth = {
      id: 'utc',
      get: (y) => {
        const gp = y.gate_params || {};
        if ('blocked_utc_hours' in gp) return gp.blocked_utc_hours;
        if ('block_utc_hours' in gp) return gp.block_utc_hours;
        return MISSING;
      },
    };
    const s3 = [
      { id: 'a', yaml: { gate_params: { block_utc_hours: [7, 8, 9] } } },
      { id: 'b', yaml: { gate_params: { blocked_utc_hours: [] } } },
      { id: 'c', yaml: { gate_params: {} } },
    ];
    const cells = buildRow(synth, s3);
    expect(cells[0].value).toEqual([7, 8, 9]);
    expect(cells[1].value).toEqual([]);
    expect(cells[2].isMissing).toBe(true);
  });
});

describe('KNOBS curated list', () => {
  it('has unique ids', () => {
    const ids = KNOBS.map(k => k.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('every entry has label + desc', () => {
    for (const k of KNOBS) {
      expect(typeof k.label).toBe('string');
      expect(k.label.length).toBeGreaterThan(0);
      expect(typeof k.desc).toBe('string');
      expect(k.desc.length).toBeGreaterThan(0);
    }
  });

  it('every entry has path or get (but not neither)', () => {
    for (const k of KNOBS) {
      expect(Boolean(k.path) || typeof k.get === 'function').toBe(true);
    }
  });
});
