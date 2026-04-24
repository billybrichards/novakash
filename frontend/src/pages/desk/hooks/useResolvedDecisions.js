// /desk — resolved per-strategy decisions for the last-N-windows table.
//
// Pulls GET /api/v58/strategy-decisions?strategy_id=X&timeframe=5m&resolved=true&limit=N
// for each tracked strategy, then re-buckets by window_ts so the table
// can look up "what did v6_sniper call for window_epoch=XYZ".
//
// There is no separate /strategy-decisions-resolved path — the same
// v58 endpoint serves resolved-enriched rows via the ?resolved=true
// query flag (see hub/api/v58_monitor.py L3790-L3870).
//
// There is also no `actual_direction` field on the response; derive it
// from (direction, outcome) via `deriveActualDirection` below.
//
// Rows with sot_reconciliation_state='engine_optimistic' + outcome='LOSS'
// are accounting-only losses (wallet untouched per hub note #64) — flag
// them via `isAccountingOnlyLoss` so the UI can annotate rather than hide.
//
// Uses Promise.allSettled so a single strategy endpoint failing does
// not blank the whole table.

import { useCallback, useEffect, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

export function useResolvedDecisions({
  strategyIds,
  timeframe = '5m',
  limit = 50,
  pollMs = 30_000,
} = {}) {
  const api = useApi();
  const [byStrategy, setByStrategy] = useState({}); // { strategyId: [rows] }
  const [errors, setErrors] = useState({});
  const [loading, setLoading] = useState(true);

  const key = (strategyIds || []).join(',');

  const load = useCallback(async () => {
    if (!strategyIds?.length) {
      setByStrategy({});
      setLoading(false);
      return;
    }
    const results = await Promise.allSettled(
      strategyIds.map(sid =>
        api
          .get(
            `/api/v58/strategy-decisions?strategy_id=${encodeURIComponent(sid)}&timeframe=${encodeURIComponent(timeframe)}&resolved=true&limit=${limit}`,
          )
          .then(r => ({ sid, body: r?.data ?? r })),
      ),
    );
    const next = {};
    const errs = {};
    for (const r of results) {
      if (r.status === 'fulfilled') {
        const { sid, body } = r.value;
        const rows = Array.isArray(body?.rows)
          ? body.rows
          : Array.isArray(body?.decisions)
            ? body.decisions
            : Array.isArray(body)
              ? body
              : [];
        next[sid] = rows;
      } else {
        // Match shape — allSettled rejected reason.
        const sid = strategyIds[results.indexOf(r)] ?? '?';
        errs[sid] = r.reason?.message || String(r.reason || 'error');
      }
    }
    setByStrategy(next);
    setErrors(errs);
    setLoading(false);
  }, [api, key, timeframe, limit]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
    const h = setInterval(load, pollMs);
    return () => clearInterval(h);
  }, [load, pollMs]);

  return { byStrategy, errors, loading, reload: load };
}

// Index a flat list of decision rows by window_epoch for O(1) lookup.
// A row's window_ts is either a string (ISO) or a unix seconds number;
// we normalize to unix-seconds integers.
export function indexByWindowEpoch(rows) {
  if (!Array.isArray(rows)) return new Map();
  const out = new Map();
  for (const r of rows) {
    const epoch = toEpoch(r.window_ts ?? r.windowEpoch ?? r.window_epoch);
    if (epoch == null) continue;
    // Prefer LIVE over GHOST when duplicates exist within a strategy.
    const prev = out.get(epoch);
    if (!prev || (r.mode === 'live' && prev.mode !== 'live')) {
      out.set(epoch, r);
    }
  }
  return out;
}

// Derive the realised market direction from (decision direction, outcome).
// The v58 endpoint does NOT return actual_direction — only `direction`
// (what the strategy called) + `outcome` ('WIN' | 'LOSS' | null). So:
//   WIN  → actual == direction
//   LOSS → actual is the opposite
//   null → unresolved (return null)
export function deriveActualDirection(direction, outcome) {
  if (outcome == null) return null;
  if (!direction) return null;
  if (outcome === 'WIN') return direction;
  return direction === 'UP' ? 'DOWN' : 'UP';
}

// Accounting-only loss flag: wallet untouched per hub note #64.
export function isAccountingOnlyLoss(row) {
  return (
    row?.sot_reconciliation_state === 'engine_optimistic' &&
    row?.outcome === 'LOSS'
  );
}

export function toEpoch(v) {
  if (v == null) return null;
  if (typeof v === 'number' && Number.isFinite(v)) {
    // Heuristic: ms vs s.
    return v > 1e12 ? Math.floor(v / 1000) : Math.floor(v);
  }
  if (typeof v === 'string') {
    const n = Date.parse(v);
    if (Number.isFinite(n)) return Math.floor(n / 1000);
  }
  return null;
}
