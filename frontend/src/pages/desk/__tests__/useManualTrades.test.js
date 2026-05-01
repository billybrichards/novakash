// Tests for the pure status-transition detection logic in
// lib/manualTradeStatus.js. No React, no hooks — pure logic only.

import { describe, it, expect } from 'vitest';
import {
  detectTransitions,
  failedReason,
  FAILED_STATUSES,
  PENDING_STATUSES,
} from '../lib/manualTradeStatus.js';

// ─── failedReason ────────────────────────────────────────────────────────────

describe('failedReason', () => {
  it('maps failed_no_token correctly', () => {
    expect(failedReason('failed_no_token')).toMatch(/token/i);
  });
  it('maps failed_risk_gate correctly', () => {
    expect(failedReason('failed_risk_gate')).toMatch(/risk/i);
  });
  it('returns fallback for unknown status', () => {
    expect(failedReason('failed_other')).toBe('unknown failure');
  });
});

// ─── detectTransitions — filled ──────────────────────────────────────────────

describe('detectTransitions — filled transition', () => {
  it('pending_live → open fires FILLED once', () => {
    const prev = new Map([['t1', 'pending_live']]);
    const rows = [{ trade_id: 't1', status: 'open', fill_price: 0.55, fill_size: 9.09 }];
    const { transitions, nextStatuses } = detectTransitions(prev, rows);

    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('filled');
    expect(transitions[0].trade_id).toBe('t1');
    expect(transitions[0].fill_price).toBe(0.55);
    expect(transitions[0].fill_size).toBe(9.09);
    expect(nextStatuses.get('t1')).toBe('open');
  });

  it('executing → open fires FILLED once', () => {
    const prev = new Map([['t2', 'executing']]);
    const rows = [{ trade_id: 't2', status: 'open', fill_price: 0.62, fill_size: 8.06 }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('filled');
  });

  it('does NOT double-fire on idempotent polls (open → open)', () => {
    const prev = new Map([['t3', 'open']]);
    const rows = [{ trade_id: 't3', status: 'open', fill_price: 0.55, fill_size: 9.09 }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(0);
  });

  it('does NOT fire on first-sight (undefined → open)', () => {
    const prev = new Map(); // no previous record
    const rows = [{ trade_id: 't4', status: 'open', fill_price: 0.55, fill_size: 9.09 }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(0);
  });
});

// ─── detectTransitions — failed ──────────────────────────────────────────────

describe('detectTransitions — failed transitions', () => {
  it('pending_live → failed_no_token fires FAILED with correct reason', () => {
    const prev = new Map([['t5', 'pending_live']]);
    const rows = [{ trade_id: 't5', status: 'failed_no_token' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('failed');
    expect(transitions[0].reason).toMatch(/token/i);
  });

  it('pending_live → failed_risk_gate fires FAILED with risk reason', () => {
    const prev = new Map([['t6', 'pending_live']]);
    const rows = [{ trade_id: 't6', status: 'failed_risk_gate' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('failed');
    expect(transitions[0].reason).toMatch(/risk/i);
  });

  it('executing → failed_risk_gate also fires FAILED', () => {
    const prev = new Map([['t7', 'executing']]);
    const rows = [{ trade_id: 't7', status: 'failed_risk_gate' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('failed');
  });

  it('does NOT double-fire: failed_no_token → failed_no_token is idempotent', () => {
    const prev = new Map([['t8', 'failed_no_token']]);
    const rows = [{ trade_id: 't8', status: 'failed_no_token' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(0);
  });
});

// ─── detectTransitions — executing ───────────────────────────────────────────

describe('detectTransitions — executing transition', () => {
  it('pending_live → executing fires EXECUTING', () => {
    const prev = new Map([['t9', 'pending_live']]);
    const rows = [{ trade_id: 't9', status: 'executing' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(1);
    expect(transitions[0].kind).toBe('executing');
  });
});

// ─── detectTransitions — paper mode ──────────────────────────────────────────

describe('detectTransitions — paper mode', () => {
  it('paper → open does NOT fire (paper mode silent)', () => {
    const prev = new Map([['t10', 'paper']]);
    const rows = [{ trade_id: 't10', status: 'open' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(0);
  });

  it('pending_live → paper does NOT fire', () => {
    const prev = new Map([['t11', 'pending_live']]);
    const rows = [{ trade_id: 't11', status: 'paper' }];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(0);
  });
});

// ─── detectTransitions — nextStatuses tracking ───────────────────────────────

describe('detectTransitions — nextStatuses tracking', () => {
  it('nextStatuses reflects all current rows', () => {
    const prev = new Map([['a', 'pending_live'], ['b', 'open']]);
    const rows = [
      { trade_id: 'a', status: 'executing' },
      { trade_id: 'b', status: 'open' },
    ];
    const { nextStatuses } = detectTransitions(prev, rows);
    expect(nextStatuses.get('a')).toBe('executing');
    expect(nextStatuses.get('b')).toBe('open');
  });

  it('transitions are multi-row: handles two trades at once', () => {
    const prev = new Map([['x', 'pending_live'], ['y', 'pending_live']]);
    const rows = [
      { trade_id: 'x', status: 'open', fill_price: 0.5, fill_size: 10 },
      { trade_id: 'y', status: 'failed_risk_gate' },
    ];
    const { transitions } = detectTransitions(prev, rows);
    expect(transitions).toHaveLength(2);
    const kinds = transitions.map(t => t.kind);
    expect(kinds).toContain('filled');
    expect(kinds).toContain('failed');
  });
});

// ─── PENDING_STATUSES / FAILED_STATUSES sets ─────────────────────────────────

describe('status sets', () => {
  it('PENDING_STATUSES contains expected values', () => {
    expect(PENDING_STATUSES.has('pending_live')).toBe(true);
    expect(PENDING_STATUSES.has('executing')).toBe(true);
    expect(PENDING_STATUSES.has('open')).toBe(false);
  });
  it('FAILED_STATUSES contains expected values', () => {
    expect(FAILED_STATUSES.has('failed_no_token')).toBe(true);
    expect(FAILED_STATUSES.has('failed_risk_gate')).toBe(true);
    expect(FAILED_STATUSES.has('open')).toBe(false);
  });
});
