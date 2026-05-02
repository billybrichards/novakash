// /desk Phase 2 — CLOB order book hook.
//
// Two-step fetch (primary path, when HUB_ALLOW_CLOB_FETCH is set):
//   1. GET /api/windows/condition?window_epoch=... — resolves window →
//      { condition_id, yes_token_id, no_token_id }.
//   2. GET /api/clob/book?condition_id=...&token_yes=...&token_no=... —
//      live 5-level book + metrics.
//
// Fallback path (when HUB_ALLOW_CLOB_FETCH is NOT set, default AWS hub):
//   On 503 `feature_flag_off` from either /api/windows/condition or
//   /api/clob/book, the hook falls back to:
//   GET /api/desk/clob-book?window_epoch=...&asset=BTC
//   which reads window_snapshots CLOB columns written by the engine.
//   This is 1-deep (not 5-level) but is sufficient for ManualTradeBar price
//   display and ClobLadder inside-quote rendering.
//
// The condition lookup is cached in-memory per-window_epoch so the expensive
// Gamma round-trip happens once per 5-minute window. The book call polls
// on `pollMs` cadence (default 10s to match server cache TTL of 8s).
//
// Return shape:
//   { book, unavailable, error, bookSource }
//   bookSource: 'clob' | 'snapshots' | null

import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

const conditionMemo = new Map(); // window_epoch → { condition_id, yes_token_id, no_token_id }

export function useClobBook({ windowEpoch, asset = 'BTC', pollMs = 10_000 }) {
  const api = useApi();
  const [book, setBook] = useState(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState(null);
  const [bookSource, setBookSource] = useState(null); // 'clob' | 'snapshots'
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
        // 503 feature_flag_off → signal to caller to use snapshot fallback.
        // 404 = market not yet listed (brief at window-roll) → retry next tick.
        const status = e?.response?.status;
        const reason = e?.response?.data?.detail?.reason ?? e?.response?.data?.reason;
        if (status === 503 && reason === 'feature_flag_off') {
          return 'feature_flag_off';
        } else if (status === 503) {
          return 'feature_flag_off'; // treat any 503 from condition as flag-off
        } else if (status === 404) {
          // transient, let caller keep polling
        } else {
          setError(e?.message || 'condition fetch failed');
        }
        return null;
      }
    };

    const loadFromSnapshots = async () => {
      try {
        const r = await api.get(
          `/api/desk/clob-book?window_epoch=${windowEpoch}&asset=${encodeURIComponent(asset)}`,
        );
        if (cancelled) return;
        const body = r?.data ?? r;
        // Normalise to /api/clob/book shape so callers work identically.
        // window_snapshots gives us yes.asks[0].price and yes_ask (flat).
        // We add flat aliases for ManualTradeBar's fallback read pattern.
        const normalised = {
          ...body,
          yes_ask: body?.yes?.asks?.[0]?.price ?? null,
          no_ask: body?.no?.asks?.[0]?.price ?? null,
        };
        setBook(normalised);
        setUnavailable(false);
        setError(null);
        setBookSource('snapshots');
      } catch (e) {
        if (cancelled || e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
        // Snapshot endpoint is always 200 on missing data — only real errors land here.
        setError(e?.message || 'snapshot book fetch failed');
      }
    };

    const loadBook = async () => {
      const cond = await resolveCondition();
      if (cancelled) return;

      // condition resolution returned feature_flag_off → fall back to snapshots.
      if (cond === 'feature_flag_off') {
        await loadFromSnapshots();
        return;
      }

      if (!cond) return;

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
        setBookSource('clob');
      } catch (e) {
        if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
        const status = e?.response?.status;
        const reason = e?.response?.data?.detail?.reason ?? e?.response?.data?.reason;
        if (status === 503 && reason === 'feature_flag_off') {
          // Hub flag not set — fall back to window_snapshots.
          await loadFromSnapshots();
        } else if (status === 503) {
          // Other upstream failures (timeout, upstream error) — try snapshots too.
          await loadFromSnapshots();
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

  return { book, unavailable, error, bookSource };
}
