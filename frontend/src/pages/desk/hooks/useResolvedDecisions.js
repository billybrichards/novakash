// /desk — resolved per-strategy decisions for the last-N-windows table.
//
// Pulls GET /api/v58/strategy-decisions-resolved?strategy_id=X&limit=N
// for each tracked strategy, then re-buckets by window_ts so the table
// can look up "what did v6_sniper call for window_epoch=XYZ".
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
            `/api/v58/strategy-decisions-resolved?strategy_id=${encodeURIComponent(sid)}&timeframe=${timeframe}&limit=${limit}`,
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
