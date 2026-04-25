// /desk — v4 snapshot hook (ensemble + classifier + regime + VPIN).
//
// GET /api/v4/snapshot?asset=BTC on a 2s cadence. The shape is described
// in hub note #226 — per-timescale entries contain `probability_up`
// (ensemble), `probability_classifier` (new), `probability_lgb`, plus
// VPIN, regime, funding, taker_buy_sell.
//
// `probability_classifier` may be null during warmup or when the
// classifier box is down — consumers must render `—` in that case rather
// than fabricating a value.

import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';

export function useSnapshot(asset = 'BTC', intervalMs = 2_000) {
  const api = useApi();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const acRef = useRef(null);

  useEffect(() => {
    let cancelled = false;

    const fetchOnce = async () => {
      if (acRef.current) acRef.current.abort();
      const ac = new AbortController();
      acRef.current = ac;
      try {
        const r = await api.get(
          `/api/v4/snapshot?asset=${encodeURIComponent(asset)}`,
          { signal: ac.signal },
        );
        if (cancelled || ac.signal.aborted) return;
        setData(r?.data ?? r);
        setError(null);
      } catch (e) {
        if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
        if (!cancelled) setError(e.message || 'snapshot fetch failed');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchOnce();
    const h = setInterval(fetchOnce, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(h);
      if (acRef.current) acRef.current.abort();
    };
  }, [api, asset, intervalMs]);

  // Pull out the 5m block — the primary surface /desk operates on.
  const fiveMin = extractTimescale(data, '5m');

  return { data, fiveMin, loading, error };
}

function extractTimescale(snap, tf) {
  if (!snap) return null;
  // Known shapes across hub/ML-box versions:
  //  - { timescales: { '5m': {...} } }
  //  - { '5m': {...}, '15m': {...} }  (older)
  //  - { timescale: '5m', probability_up: ... }  (single-timescale)
  if (snap.timescales && snap.timescales[tf]) return snap.timescales[tf];
  if (snap[tf] && typeof snap[tf] === 'object') return snap[tf];
  if (snap.timescale === tf) return snap;
  return null;
}

// Convenience accessor: pick `probability_up`, `probability_classifier`,
// `probability_lgb` from a timescale block, coercing to finite numbers
// or null. Hub + ML-box use both snake and camel case at different
// layers so we accept both.
export function pickProbs(tsBlock) {
  if (!tsBlock) return { pu: null, pc: null, pl: null };
  const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : null);
  return {
    pu: num(tsBlock.probability_up ?? tsBlock.probabilityUp ?? tsBlock.p_up),
    pc: num(
      tsBlock.probability_classifier
        ?? tsBlock.probabilityClassifier
        ?? tsBlock.p_classifier,
    ),
    pl: num(
      tsBlock.probability_lgb
        ?? tsBlock.probabilityLgb
        ?? tsBlock.p_lgb,
    ),
  };
}
