import React, { useEffect, useRef, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from '../../theme/tokens.js';

// Endpoint catalog. `proxied: false` entries are rendered as grey "not
// proxied" pills — see PR body for the hub-proxy gap list. Do NOT hit the
// ML box directly from the browser (CORS + network boundary).
const ENDPOINTS = [
  { id: 'health',      label: '/health',       url: '/api/health',                        proxied: false, poll: 10000 },
  { id: 'v2_health',   label: '/v2/health',    url: '/api/v2/health',                     proxied: true,  poll: 30000 },
  { id: 'v3_health',   label: '/v3/health',    url: '/api/v3/health',                     proxied: true,  poll: 30000 },
  { id: 'v4_health',   label: '/v4/health',    url: '/api/v4/health',                     proxied: false, poll: 10000 },
  { id: 'v4_snapshot', label: '/v4/snapshot',  url: '/api/v4/snapshot?asset=BTC&timescales=5m', proxied: true,  poll: 5000, latencyHigh: 150, latencyCrit: 750 },
  { id: 'v4_regime',   label: '/v4/regime',    url: '/api/v4/regime?asset=BTC',           proxied: false, poll: 10000 },
  { id: 'v4_macro',    label: '/v4/macro',     url: '/api/v4/macro',                      proxied: true,  poll: 15000 },
  { id: 'v4_consensus',label: '/v4/consensus', url: '/api/v4/consensus?asset=BTC',        proxied: false, poll: 10000 },
  { id: 'hub',         label: 'hub',           url: '/api/system/status',                 proxied: true,  poll: 10000 },
];

// Pill latency thresholds. Can be overridden per endpoint (see v4_snapshot).
function latencyColor(ms, high = 100, crit = 500) {
  if (ms == null) return T.label;
  if (ms <= high) return '#10b981';
  if (ms <= crit) return '#f59e0b';
  return '#ef4444';
}

/** One pill. Polls independently so slow endpoints don't starve fast ones. */
function HealthPill({ ep, api }) {
  const [state, setState] = useState({
    status: 'loading', // loading | ok | err | skip
    latency: null,
    code: null,
    ts: 0,
  });
  const acRef = useRef(null);
  const backoffRef = useRef(1); // 1 → 2 → 4 → 8, cap 8

  useEffect(() => {
    if (!ep.proxied) {
      setState({ status: 'skip', latency: null, code: null, ts: Date.now() });
      return () => {};
    }
    let cancelled = false;

    const run = async () => {
      if (document.visibilityState !== 'visible') {
        // Pause when tab hidden — rescheduled on visibility change below.
        return;
      }
      if (acRef.current) acRef.current.abort();
      const ac = new AbortController();
      acRef.current = ac;
      const t0 = performance.now();
      try {
        await api.get(ep.url, { signal: ac.signal });
        if (cancelled) return;
        const dt = performance.now() - t0;
        backoffRef.current = 1;
        setState({ status: 'ok', latency: dt, code: 200, ts: Date.now() });
      } catch (e) {
        if (e?.name === 'CanceledError' || e?.code === 'ERR_CANCELED') return;
        if (cancelled) return;
        backoffRef.current = Math.min(backoffRef.current * 2, 8);
        setState({
          status: 'err',
          latency: null,
          code: e?.response?.status ?? 0,
          ts: Date.now(),
        });
      }
    };

    run();
    let timer = setInterval(run, ep.poll * backoffRef.current);
    const onVis = () => {
      if (document.visibilityState === 'visible') run();
    };
    document.addEventListener('visibilitychange', onVis);

    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVis);
      if (acRef.current) acRef.current.abort();
    };
  }, [ep, api]);

  const color = state.status === 'skip' ? T.label
    : state.status === 'err' ? '#ef4444'
    : state.status === 'loading' ? T.label
    : latencyColor(state.latency, ep.latencyHigh, ep.latencyCrit);

  const tip = state.status === 'skip'
    ? 'not proxied through hub — see PR for hub-proxy gaps'
    : state.status === 'err'
    ? `HTTP ${state.code || 'net'} — backoff ×${backoffRef.current}`
    : state.status === 'loading'
    ? 'first request…'
    : `${Math.round(state.latency)}ms · ${new Date(state.ts).toISOString().slice(11,19)}Z`;

  return (
    <span
      title={tip}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        padding: '4px 8px',
        border: `1px solid ${T.border}`,
        borderRadius: 3,
        fontFamily: T.font,
        fontSize: 10,
        color: state.status === 'skip' ? T.label : T.text,
        background: 'transparent',
        opacity: state.status === 'skip' ? 0.5 : 1,
      }}
    >
      <span style={{
        width: 6, height: 6, borderRadius: '50%',
        background: color,
        display: 'inline-block',
        flexShrink: 0,
      }} />
      <span>{ep.label}</span>
      <span style={{ color: T.label }}>
        {state.status === 'ok' ? `${Math.round(state.latency)}ms`
          : state.status === 'err' ? `err`
          : state.status === 'skip' ? '—'
          : '…'}
      </span>
    </span>
  );
}

export default function HealthStrip() {
  const api = useApi();
  return (
    <div style={{
      display: 'flex', gap: 6, flexWrap: 'wrap',
      padding: '8px 0 12px',
      borderBottom: `1px solid ${T.border}`,
      marginBottom: 14,
    }}>
      {ENDPOINTS.map((ep) => <HealthPill key={ep.id} ep={ep} api={api} />)}
    </div>
  );
}

export { ENDPOINTS };
