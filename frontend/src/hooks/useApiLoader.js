// frontend/src/hooks/useApiLoader.js
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi } from './useApi.js';

/**
 * Unified fetch hook for Tier-1+ pages.
 *
 * Usage:
 *   const { data, error, loading, reload } = useApiLoader(
 *     (signal) => api.get(`/api/trades?limit=500`, { signal }),
 *     [limit]
 *   );
 *
 * - Aborts previous request on new fetch + unmount.
 * - Filters AbortError / ERR_CANCELED from reported errors.
 * - Unwraps axios envelope (r.data ?? r) and normalizes to array
 *   when response is {rows:[...]}, {trades:[...]}, or already an array.
 *   Returns raw object for non-array shapes.
 *
 * @param {(signal: AbortSignal, api: object) => Promise<any>} fetcher
 * @param {Array<string|number|boolean|null|undefined>} [deps=[]]
 *        IMPORTANT: deps MUST be primitives or memoized values. Passing a
 *        fresh object/array/function literal each render causes the callback
 *        to recreate every render, triggering an infinite abort+refetch loop.
 *        For object-shaped state, stabilize via `useMemo` or serialize.
 */
export function useApiLoader(fetcher, deps = []) {
  const api = useApi();
  const acRef = useRef(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (acRef.current) acRef.current.abort();
    const ac = new AbortController();
    acRef.current = ac;
    setLoading(true);
    setError(null);
    try {
      const r = await fetcher(ac.signal, api);
      if (ac.signal.aborted) return;
      const raw = r?.data ?? r;
      if (Array.isArray(raw)) {
        setData(raw);
      } else if (raw && typeof raw === 'object') {
        // Hub returns arrays under one of these envelope keys depending on route:
        //   /api/audit-tasks   → { rows: [...] }
        //   /api/trades        → { trades: [...], total }
        //   /api/v58/strategy-decisions → { decisions: [...] }
        //   /api/pnl/*         → { items: [...] }
        if (Array.isArray(raw.rows)) setData(raw.rows);
        else if (Array.isArray(raw.trades)) setData(raw.trades);
        else if (Array.isArray(raw.decisions)) setData(raw.decisions);
        else if (Array.isArray(raw.items)) setData(raw.items);
        else setData(raw);
      } else {
        setData(raw);
      }
    } catch (e) {
      if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
      setError(e.message || 'load failed');
    } finally {
      if (acRef.current === ac) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, ...deps]);

  useEffect(() => {
    load();
    return () => { if (acRef.current) acRef.current.abort(); };
  }, [load]);

  return { data, error, loading, reload: load };
}
