// /desk — manual-trade polling hook.
//
// Polls GET /api/desk/manual-trades?recent=20 every 3s.
// On each poll, diffes status against last-known state and fires toast
// callbacks for transitions (executing, filled, failed).
//
// Returns:
//   rows            — raw rows from the server (most-recent first)
//   latestPending   — first row that is still pending/executing, or null
//   latestForWindow(epoch) — first row matching the given window_epoch, or null
//   reload          — manual re-fetch trigger
//
// Toast callbacks are passed in by the caller (ManualTradeBar) to avoid
// coupling this hook to the Toast context directly (keeps it testable).

import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { detectTransitions, PENDING_STATUSES } from '../lib/manualTradeStatus.js';

export function useManualTrades({ onTransition, pollMs = 3_000 } = {}) {
  const api = useApi();
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Persist last-known status per trade_id across polls.
  const prevStatusesRef = useRef(new Map());
  const acRef = useRef(null);

  const reload = useCallback(async (signal) => {
    try {
      const r = await api.get('/api/desk/manual-trades?recent=20', signal ? { signal } : undefined);
      const body = r?.data ?? r;
      const incoming = Array.isArray(body?.rows) ? body.rows : [];

      // Detect transitions against last snapshot.
      const { transitions, nextStatuses } = detectTransitions(
        prevStatusesRef.current,
        incoming,
      );
      prevStatusesRef.current = nextStatuses;

      // Fire transition callbacks.
      if (onTransition && transitions.length > 0) {
        for (const t of transitions) {
          onTransition(t);
        }
      }

      setRows(incoming);
      setError(null);
    } catch (e) {
      if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
      setError(e.message || 'manual-trades fetch failed');
    } finally {
      setLoading(false);
    }
  }, [api, onTransition]);

  useEffect(() => {
    if (acRef.current) acRef.current.abort();
    const ac = new AbortController();
    acRef.current = ac;
    reload(ac.signal);
    const h = setInterval(() => reload(ac.signal), pollMs);
    return () => {
      clearInterval(h);
      ac.abort();
    };
  }, [reload, pollMs]);

  const latestPending = rows.find(r => PENDING_STATUSES.has(r.status)) ?? null;

  const latestForWindow = useCallback((epoch) => {
    if (epoch == null) return null;
    return rows.find(r => r.window_epoch === epoch) ?? null;
  }, [rows]);

  const manualReload = useCallback(() => {
    if (acRef.current) acRef.current.abort();
    const ac = new AbortController();
    acRef.current = ac;
    reload(ac.signal);
  }, [reload]);

  return { rows, latestPending, latestForWindow, reload: manualReload, loading, error };
}
