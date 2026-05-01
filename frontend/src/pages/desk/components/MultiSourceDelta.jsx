// /desk — multi-source price delta panel (Chainlink / Binance / Tiingo).
//
// Replaces the single "Window-relative Chainlink" box. Renders three rows,
// one per source, each showing:
//
//   • current $ price
//   • Δ$ vs window-open Chainlink target
//   • Δbps vs target
//   • last-60s sparkline (mini SVG, no Recharts)
//   • freshness — seconds since the most recent tick
//
// The row with the largest |Δbps| against the others is highlighted in red
// to mirror the CONSENSUS UNSAFE banner — one less place the operator has
// to triangulate when sources disagree.
//
// Data: /api/ticks/{chainlink,binance,tiingo}?asset=BTC&limit=60. Hub
// endpoints added with this commit (hub/api/ticks.py); cold tables degrade
// to {rows: []}.

import React, { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';

const SOURCES = [
  { key: 'chainlink', label: 'Chainlink', accent: T.cyan,
    note: 'oracle · resolution source-of-truth' },
  { key: 'binance',   label: 'Binance',   accent: '#f59e0b',
    note: 'aggTrade · primary BTC price' },
  { key: 'tiingo',    label: 'Tiingo',    accent: '#84cc16',
    note: 'multi-exchange top-of-book' },
];

const STALE_AFTER_S = 30;

export default function MultiSourceDelta({ asset = 'BTC', targetPrice, pollMs = 5_000 }) {
  const sources = useTickSources(asset, pollMs);

  const enriched = useMemo(() => {
    return sources.map(s => {
      const last = s.ticks.length ? s.ticks[s.ticks.length - 1] : null;
      const lastPrice = last ? Number(last.price) : null;
      const lastTs = last ? Number(last.ts_unix) : null;
      const target = Number(targetPrice);
      const deltaUsd = (Number.isFinite(lastPrice) && Number.isFinite(target))
        ? lastPrice - target : null;
      const deltaBps = (Number.isFinite(lastPrice) && Number.isFinite(target) && target > 0)
        ? ((lastPrice - target) / target) * 10_000 : null;
      const ageS = lastTs != null
        ? Math.max(0, Math.floor(Date.now() / 1000) - lastTs)
        : null;
      return { ...s, lastPrice, deltaUsd, deltaBps, ageS };
    });
  }, [sources, targetPrice]);

  const maxDivergenceKey = useMemo(() => pickMaxDivergence(enriched), [enriched]);

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <span style={{ letterSpacing: '0.1em' }}>MULTI-SOURCE BTC PRICE · vs target @ open</span>
        <span style={{ color: T.label2, fontSize: 9 }}>
          target {Number.isFinite(targetPrice) ? `$${fmtUsd(targetPrice)}` : '—'}
        </span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {enriched.map(s => (
          <SourceRow
            key={s.key}
            source={s}
            isMaxDivergent={s.key === maxDivergenceKey}
          />
        ))}
      </div>
    </div>
  );
}

function SourceRow({ source, isMaxDivergent }) {
  const {
    label, accent, note, ticks, lastPrice, deltaUsd, deltaBps, ageS, error,
  } = source;
  const stale = ageS != null && ageS > STALE_AFTER_S;
  const empty = !ticks || ticks.length === 0;

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '90px 110px 90px 70px 1fr 60px',
        alignItems: 'center',
        gap: 8,
        padding: '6px 8px',
        border: `1px solid ${isMaxDivergent ? T.loss : T.border}`,
        background: isMaxDivergent ? 'rgba(239,68,68,0.06)' : 'transparent',
        fontFamily: T.font,
        fontSize: 11,
      }}
    >
      <div>
        <div style={{ color: accent, fontWeight: 600 }}>{label}</div>
        <div style={{ color: T.label2, fontSize: 9 }}>{note}</div>
      </div>

      <div style={{ fontVariantNumeric: 'tabular-nums' }}>
        {Number.isFinite(lastPrice) ? `$${fmtUsd(lastPrice)}` : (empty ? '—' : '…')}
      </div>

      <div style={{
        fontVariantNumeric: 'tabular-nums',
        color: deltaUsd == null ? T.label : (deltaUsd >= 0 ? T.profit : T.loss),
      }}>
        {deltaUsd != null
          ? `${deltaUsd >= 0 ? '+' : ''}${fmtUsd(deltaUsd)}`
          : '—'}
      </div>

      <div style={{
        fontVariantNumeric: 'tabular-nums',
        color: deltaBps == null ? T.label : (deltaBps >= 0 ? T.profit : T.loss),
      }}>
        {deltaBps != null
          ? `${deltaBps >= 0 ? '+' : ''}${deltaBps.toFixed(1)}bps`
          : '—'}
      </div>

      <Spark ticks={ticks} accent={accent} />

      <div style={{
        fontSize: 9,
        color: error ? T.loss : (stale ? '#facc15' : T.label2),
        textAlign: 'right',
      }}>
        {error
          ? 'err'
          : ageS != null
            ? `${ageS}s${stale ? ' STALE' : ''}`
            : '—'}
      </div>
    </div>
  );
}

function Spark({ ticks, accent }) {
  const W = 200, H = 32;
  const prices = (ticks || []).map(r => Number(r.price)).filter(Number.isFinite);
  if (prices.length < 2) {
    return <svg width={W} height={H} />;
  }
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const span = Math.max(1e-9, max - min);
  const pts = prices.map((p, i) => {
    const x = (i / (prices.length - 1)) * W;
    const y = H - ((p - min) / span) * H;
    return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} style={{ display: 'block' }}>
      <path d={pts} fill="none" stroke={accent} strokeWidth={1.25} />
    </svg>
  );
}

// Hook: fan out a fetch per source, return their parallel state.
function useTickSources(asset, pollMs) {
  const api = useApi();
  const [state, setState] = useState(() =>
    SOURCES.map(s => ({ ...s, ticks: [], error: null })),
  );

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const results = await Promise.all(SOURCES.map(async (s) => {
        try {
          const r = await api.get(
            `/api/ticks/${s.key}?asset=${encodeURIComponent(asset)}&limit=60`,
          );
          const body = r?.data ?? r;
          const rows = Array.isArray(body?.rows) ? body.rows
            : Array.isArray(body) ? body : [];
          return { ...s, ticks: rows, error: null };
        } catch (e) {
          return { ...s, ticks: [], error: e?.message || 'error' };
        }
      }));
      if (!cancelled) setState(results);
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, asset, pollMs]);

  return state;
}

function pickMaxDivergence(rows) {
  // The most-divergent source = furthest from the median delta across all
  // sources reporting a delta. Returns the source key (or null).
  const withBps = rows.filter(r => Number.isFinite(r.deltaBps));
  if (withBps.length < 2) return null;
  const sorted = [...withBps].map(r => r.deltaBps).sort((a, b) => a - b);
  const median = sorted.length % 2
    ? sorted[(sorted.length - 1) / 2]
    : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;

  let worstKey = null;
  let worstDist = -Infinity;
  for (const r of withBps) {
    const dist = Math.abs(r.deltaBps - median);
    if (dist > worstDist) {
      worstDist = dist;
      worstKey = r.key;
    }
  }
  // Only highlight if the divergence is meaningful (>10 bps from median).
  if (worstDist < 10) return null;
  return worstKey;
}

const panelStyle = {
  border: `1px solid ${T.border}`,
  padding: 10,
  background: T.card,
  marginBottom: 12,
};

const headerStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  fontSize: 10,
  color: T.label,
  marginBottom: 8,
};

function fmtUsd(v) {
  if (!Number.isFinite(v)) return '—';
  return v.toLocaleString('en-US', { maximumFractionDigits: 2 });
}
