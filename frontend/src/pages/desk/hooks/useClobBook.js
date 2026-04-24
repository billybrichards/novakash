// /desk Phase 2 — CLOB order book hook.
//
// Two-step fetch:
//   1. GET /api/windows/condition?window_epoch=... — resolves window →
//      { condition_id, yes_token_id, no_token_id }.
//   2. GET /api/clob/book?condition_id=...&token_yes=...&token_no=... —
//      live 5-level book + metrics.
//
// Both endpoints are gated server-side by HUB_ALLOW_CLOB_FETCH. When the
// flag is off (default AWS hub) they return 503 `{error: "clob_unavailable"}`
// — the hook surfaces `unavailable: true` and callers hide their UI.
//
// The condition lookup is cached in-memory per-window_epoch so the expensive
// Gamma round-trip happens once per 5-minute window. The book call polls
// on `pollMs` cadence (default 10s to match server cache TTL of 8s).

import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

const conditionMemo = new Map(); // window_epoch → { condition_id, yes_token_id, no_token_id }

export function useClobBook({ windowEpoch, asset = 'BTC', pollMs = 10_000 }) {
  const api = useApi();
  const [book, setBook] = useState(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState(null);
  const acRef = useRef(null);

  useEffect(() => {
    if (!windowEpoch) return;
    let cancelled = false;

    const resolveCondition = async () => {
      const cached = conditionMemo.get(windowEpoch);
      if (cached) return cached;
      try {
        const r = await api.get(
          `/api/windows/condition?window_epoch=${windowEpoch}&asset=${encodeURIComponent(asset)}`,
        );
        const body = r?.data ?? r;
        if (body?.condition_id && body?.yes_token_id && body?.no_token_id) {
          conditionMemo.set(windowEpoch, body);
          return body;
        }
        return null;
      } catch (e) {
        // 503 = feature flag off → unavailable. 404 = market not yet listed
        // (briefly at window-roll) → retry next tick. Other errors pass up.
        const status = e?.response?.status;
        if (status === 503) {
          setUnavailable(true);
        } else if (status === 404) {
          // transient, let caller keep polling
        } else {
          setError(e?.message || 'condition fetch failed');
        }
        return null;
      }
    };

    const loadBook = async () => {
      const cond = await resolveCondition();
      if (cancelled || !cond) return;
      if (acRef.current) acRef.current.abort();
      const ac = new AbortController();
      acRef.current = ac;
      try {
        const qs = new URLSearchParams({
          condition_id: cond.condition_id,
          token_yes: cond.yes_token_id,
          token_no: cond.no_token_id,
        }).toString();
        const r = await api.get(`/api/clob/book?${qs}`, { signal: ac.signal });
        if (cancelled || ac.signal.aborted) return;
        const body = r?.data ?? r;
        setBook(body);
        setUnavailable(false);
        setError(null);
      } catch (e) {
        if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
        const status = e?.response?.status;
        if (status === 503) {
          setUnavailable(true);
          setBook(null);
        } else {
          setError(e?.message || 'book fetch failed');
        }
      }
    };

    loadBook();
    const h = setInterval(loadBook, pollMs);
    return () => {
      cancelled = true;
      clearInterval(h);
      if (acRef.current) acRef.current.abort();
    };
  }, [api, windowEpoch, asset, pollMs]);

  return { book, unavailable, error };
}
