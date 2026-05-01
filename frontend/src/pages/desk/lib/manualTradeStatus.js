// lib/manualTradeStatus.js
//
// Pure logic for detecting manual-trade status transitions across polls.
// Extracted so it can be unit-tested independently of the React hook.
//
// Status vocabulary (from Track A spec):
//   pending_live  — submitted, waiting for engine to pick up
//   executing     — engine is placing the CLOB order
//   open          — filled (position live)
//   failed_no_token   — resolution: no matching token found
//   failed_risk_gate  — risk gate rejected the trade
//   paper         — paper mode execution (no real order)
//
// Transition rules:
//   pending_live → executing   → "EXECUTING" toast (pending/amber)
//   pending_live → open        → "FILLED" toast    (success/green)
//   executing    → open        → "FILLED" toast    (success/green)
//   pending_live → failed_*    → "FAILED" toast    (error/red)
//   executing    → failed_*    → "FAILED" toast    (error/red)
//   paper        → (any)       → no toast (paper mode, silent)
//
// Each transition fires ONCE — the caller must persist `prevStatuses`
// between polls and pass it back in.

export const FAILED_STATUSES = new Set(['failed_no_token', 'failed_risk_gate']);
export const PENDING_STATUSES = new Set(['pending_live', 'executing']);

/**
 * Map failed status → human-readable reason.
 */
export function failedReason(status) {
  if (status === 'failed_no_token') return 'token resolution failed — no matching market token';
  if (status === 'failed_risk_gate') return 'rejected by risk gate';
  return 'unknown failure';
}

/**
 * Detect transitions between two snapshots of the trade list.
 *
 * @param {Map<string, string>} prevStatuses  — trade_id → old status
 * @param {Array<{trade_id, status, fill_price, fill_size}>} newRows
 * @returns {{ transitions: Array<Transition>, nextStatuses: Map<string,string> }}
 *
 * Transition shape:
 *   { trade_id, from, to, kind, fill_price?, fill_size? }
 *   kind: 'executing' | 'filled' | 'failed'
 */
export function detectTransitions(prevStatuses, newRows) {
  const nextStatuses = new Map();
  const transitions = [];

  for (const row of newRows) {
    const { trade_id, status, fill_price, fill_size } = row;
    nextStatuses.set(trade_id, status);

    const prev = prevStatuses.get(trade_id);

    // New trade — no previous record. Only fire if already terminal/interesting.
    if (prev === undefined) {
      // Don't fire on first-sight unless it's already in a final state
      // (the poll that shows the newly created trade shows pending_live —
      //  the POST 200 handler already fires the "queued" toast).
      continue;
    }

    if (prev === status) continue; // idempotent — no transition

    // Skip paper mode entirely.
    if (status === 'paper' || prev === 'paper') continue;

    const from = prev;
    const to = status;

    if (PENDING_STATUSES.has(from) && to === 'executing') {
      transitions.push({ trade_id, from, to, kind: 'executing' });
    } else if (PENDING_STATUSES.has(from) && to === 'open') {
      transitions.push({ trade_id, from, to, kind: 'filled', fill_price, fill_size });
    } else if (PENDING_STATUSES.has(from) && FAILED_STATUSES.has(to)) {
      transitions.push({ trade_id, from, to, kind: 'failed', reason: failedReason(to) });
    }
    // executing → open (engine placed + filled in one cycle)
    else if (from === 'executing' && to === 'open') {
      transitions.push({ trade_id, from, to, kind: 'filled', fill_price, fill_size });
    } else if (from === 'executing' && FAILED_STATUSES.has(to)) {
      transitions.push({ trade_id, from, to, kind: 'failed', reason: failedReason(to) });
    }
  }

  return { transitions, nextStatuses };
}
