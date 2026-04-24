// /desk — operator picks hook.
//
// Loads GET /api/desk/picks for the last-20 table and exposes an upsert
// function that posts to /api/desk/picks. The FE enforces the T-00:10
// lockout (not the hub) so the operator can change their mind any time
// up until the cutoff.
//
// localStorage is used as a best-effort echo: once the server confirms,
// we cache `desk:lastPick:{windowEpoch}` so the UI still shows the pick
// after a reload even if /api/desk/picks is slow.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

const LS_PREFIX = 'desk:lastPick:';
export const LOCKOUT_SECONDS = 10;

export function usePicks({ limit = 50, pollMs = 30_000 } = {}) {
  const api = useApi();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const acRef = useRef(null);

  const reload = useCallback(async () => {
    if (acRef.current) acRef.current.abort();
    const ac = new AbortController();
    acRef.current = ac;
    try {
      const r = await api.get(`/api/desk/picks?limit=${limit}`, { signal: ac.signal });
      const body = r?.data ?? r;
      if (ac.signal.aborted) return;
      const out = Array.isArray(body?.rows) ? body.rows : [];
      setRows(out);
      setError(null);
    } catch (e) {
      if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
      setError(e.message || 'picks fetch failed');
    } finally {
      setLoading(false);
    }
  }, [api, limit]);

  useEffect(() => {
    reload();
    const h = setInterval(reload, pollMs);
    return () => {
      clearInterval(h);
      if (acRef.current) acRef.current.abort();
    };
  }, [reload, pollMs]);

  const submitPick = useCallback(async ({ windowEpoch, pick, tRemainingS, notes }) => {
    const body = {
      window_epoch: windowEpoch,
      pick,
      t_remaining_s: tRemainingS,
      notes: notes || null,
    };
    const r = await api.post('/api/desk/picks', body);
    // Cache locally so a redeploy mid-window keeps the pick visible.
    try {
      localStorage.setItem(
        `${LS_PREFIX}${windowEpoch}`,
        JSON.stringify({ pick, tRemainingS, ts: Date.now() }),
      );
    } catch { /* storage disabled / quota */ }
    // Optimistically refresh the table.
    reload();
    return r?.data ?? r;
  }, [api, reload]);

  return { rows, loading, error, reload, submitPick };
}

export function readCachedPick(windowEpoch) {
  try {
    const raw = localStorage.getItem(`${LS_PREFIX}${windowEpoch}`);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
