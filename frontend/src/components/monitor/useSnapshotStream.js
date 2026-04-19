// Single shared /v4/snapshot poller. Mount once at the <Monitor> root and
// pass the latest snapshot + the ring buffer down via context/prop drill.
// Polls every 2s while tab visible; backs off 2→4→8x on consecutive errors
// (cap 30s). Uses a single AbortController so slow requests never pile up.
//
// Why not per-component polling? Every Tier-2 chart + the hero card read the
// same endpoint — polling once and fanning out keeps hub traffic at 30 rpm.

import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { RingBuffer } from './RingBuffer.js';

const POLL_MS = 2000;
const BACKOFF_CAP_MS = 30000;

export function useSnapshotStream({ asset = 'BTC', timescales = '5m', bufferCap = 1800 } = {}) {
  const api = useApi();
  const bufferRef = useRef(null);
  if (bufferRef.current == null) bufferRef.current = new RingBuffer(bufferCap);
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);
  const [lastFetchTs, setLastFetchTs] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let backoff = 1;
    const ac = { current: null };

    const run = async () => {
      if (document.visibilityState !== 'visible') {
        timer = setTimeout(run, POLL_MS);
        return;
      }
      if (ac.current) ac.current.abort();
      const c = new AbortController();
      ac.current = c;
      try {
        const res = await api.get(
          `/v4/snapshot?asset=${asset}&timescales=${timescales}`,
          { signal: c.signal, timeout: 10000 },
        );
        if (cancelled) return;
        const payload = res?.data ?? res;
        setSnapshot(payload);
        setError(null);
        setLastFetchTs(Date.now());
        backoff = 1;

        // Push to ring buffer
        const tf5 = payload?.timescales?.['5m'];
        if (tf5) {
          bufferRef.current.push({
            ts: payload.ts ?? Date.now() / 1000,
            window_ts: tf5.window_ts ?? null,
            probability_up: tf5.probability_up,
            probability_lgb: tf5.probability_lgb,
            probability_classifier: tf5.probability_classifier,
            mode: tf5.ensemble_config?.mode ?? null,
            disagreement_magnitude: tf5.ensemble_config?.disagreement_magnitude ?? null,
            disagreement_detected: tf5.ensemble_config?.disagreement_detected ?? null,
            model_version: tf5.model_version,
          });
        }
      } catch (e) {
        if (e?.name === 'CanceledError' || e?.code === 'ERR_CANCELED') return;
        if (cancelled) return;
        setError(e.message || 'snapshot failed');
        backoff = Math.min(backoff * 2, BACKOFF_CAP_MS / POLL_MS);
      } finally {
        if (!cancelled) timer = setTimeout(run, POLL_MS * backoff);
      }
    };

    run();
    const onVis = () => {
      if (document.visibilityState === 'visible') run();
    };
    document.addEventListener('visibilitychange', onVis);

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      if (ac.current) ac.current.abort();
      document.removeEventListener('visibilitychange', onVis);
    };
  }, [api, asset, timescales]);

  return { snapshot, error, lastFetchTs, buffer: bufferRef.current };
}
