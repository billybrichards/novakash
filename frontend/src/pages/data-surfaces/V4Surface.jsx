/**
 * V4Surface — dedicated /data/v4 dashboard for the fusion surface.
 *
 * This is the full-page evolution of V4Panel (which lives on /margin and
 * embeds alongside positions and signal stacks). Here the v4 snapshot
 * gets the whole viewport so every sub-surface is first-class:
 *
 *   - consensus strip            → 6 sources, age_ms, available, max_divergence
 *   - macro card                 → Qwen bias + reasoning + per-timescale map
 *   - events timeline            → upcoming high/extreme macro events
 *   - per-timescale grid         → 5m/15m/1h/4h cards with verdict / p_up /
 *                                   quantile fan / regime / cascade / gate reason
 *   - raw JSON peek              → collapsed by default, for diagnostics
 *
 * Data source: /api/v4/snapshot (hub/api/margin.py:125 → TIMESFM_URL /v4/snapshot)
 *
 * Refresh cadence: 4s (matches the V4Panel on /margin to avoid doubling load
 * on the cross-region fetcher).
 */

import { useEffect, useRef, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import V4Panel from '../margin-engine/components/V4Panel.jsx';
import { DEFAULT_ASSET, DEFAULT_TIMESCALES, DEFAULT_STRATEGY } from '../margin-engine/components/constants.js';

// ─── Theme ────────────────────────────────────────────────────────────────
const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: 'rgba(30, 41, 59, 1)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  blue: '#3b82f6',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const REGIME_COLOR = {
  TRENDING_UP: T.green,
  TRENDING_DOWN: T.red,
  MEAN_REVERTING: T.cyan,
  CHOPPY: T.amber,
  NO_EDGE: T.textDim,
};

const BIAS_COLOR = {
  BULL: T.green,
  BEAR: T.red,
  NEUTRAL: T.amber,
};

const GATE_COLOR = {
  ALLOW_ALL: T.green,
  SKIP_UP: T.red,
  SKIP_DOWN: T.red,
};

const IMPACT_COLOR = {
  EXTREME: T.red,
  HIGH: T.amber,
  MEDIUM: T.cyan,
  LOW: T.textMuted,
};

const SHORT_TERM = ['5m', '15m', '1h', '4h'];

// ─── Small primitives ────────────────────────────────────────────────────

function Chip({ color, bg, border, label, value, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 9, fontWeight: 800, padding: '3px 8px', borderRadius: 3,
        background: bg, color, border: `1px solid ${border}`,
        fontFamily: T.mono, letterSpacing: '0.04em', textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      {label && <span style={{ opacity: 0.65 }}>{label}</span>}
      <span>{value}</span>
    </span>
  );
}





// ─── Raw JSON peek ───────────────────────────────────────────────────────

function RawJsonPeek({ snapshot }) {
  const [open, setOpen] = useState(false);
  if (!snapshot) return null;
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      marginTop: 14,
      overflow: 'hidden',
    }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          width: '100%',
          background: 'transparent',
          border: 'none',
          color: T.text,
          padding: '10px 14px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          cursor: 'pointer',
          fontSize: 10,
          fontWeight: 800,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
        }}
      >
        <span>Raw Snapshot JSON</span>
        <span style={{ color: T.textDim, fontFamily: T.mono }}>{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <pre style={{
          margin: 0,
          padding: '10px 14px',
          background: 'rgba(0,0,0,0.35)',
          borderTop: `1px solid ${T.cardBorder}`,
          color: T.textMuted,
          fontFamily: T.mono,
          fontSize: 9,
          lineHeight: 1.4,
          overflowX: 'auto',
          maxHeight: 420,
          overflowY: 'auto',
        }}>
          {JSON.stringify(snapshot, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────

export default function V4Surface() {
  const api = useApi();
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [asset, setAsset] = useState(DEFAULT_ASSET);
  const lastFetch = useRef(0);

  const fetchSnapshot = async () => {
    try {
      const res = await api(
        'GET',
        `/v4/snapshot?asset=${asset}&timescales=${DEFAULT_TIMESCALES}&strategy=${DEFAULT_STRATEGY}`,
      );
      setSnapshot(res?.data || res);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch /v4/snapshot');
    } finally {
      setLoading(false);
      lastFetch.current = Date.now();
    }
  };

  useEffect(() => { fetchSnapshot(); }, [api, asset]);

  useEffect(() => {
    const interval = setInterval(fetchSnapshot, 4000);
    return () => clearInterval(interval);
  }, [api, asset]);

  const macro = snapshot?.macro;
  const consensus = snapshot?.consensus;
  const events = snapshot?.events_upcoming || [];
  const timescales = snapshot?.timescales || {};
  const lastPrice = snapshot?.last_price;
  const snapshotTs = snapshot?.ts;
  const strategy = snapshot?.strategy || 'fee_aware_15m';

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: 12 }}>
          <div>
            <h1 style={{
              fontSize: 16,
              fontWeight: 800,
              color: T.white,
              margin: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}>
              V4 Data Surface
              <Chip
                color={T.purple}
                bg="rgba(168,85,247,0.15)"
                border="rgba(168,85,247,0.3)"
                value="fusion"
              />
              <Chip color={T.amber} bg="rgba(245,158,11,0.12)" border="rgba(245,158,11,0.3)" value="Hyperliquid Perps" title="Primary decision surface for the Hyperliquid perpetual futures margin engine" />
              <Chip
                color={T.cyan}
                bg="rgba(6,182,212,0.12)"
                border="rgba(6,182,212,0.3)"
                value={`/v4/snapshot · ${strategy}`}
              />
            </h1>
            <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 880, lineHeight: 1.5 }}>
              Live fusion of all v4 inputs: 6-source consensus, Qwen macro bias,
              upcoming macro events, and per-timescale model output with quantile
              fan, regime, cascade, and gate-stack reason. Refreshes every 4 seconds.
            </p>
          </div>

          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {['BTC', 'ETH', 'SOL', 'XRP'].map((a) => (
              <button
                key={a}
                onClick={() => setAsset(a)}
                style={{
                  padding: '6px 12px',
                  borderRadius: 4,
                  fontSize: 10,
                  fontWeight: 700,
                  fontFamily: T.mono,
                  background: asset === a ? 'rgba(6,182,212,0.15)' : 'transparent',
                  color: asset === a ? T.cyan : T.textMuted,
                  border: `1px solid ${asset === a ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
                  cursor: 'pointer',
                  letterSpacing: '0.05em',
                }}
              >
                {a}
              </button>
            ))}
          </div>
        </div>

        {/* Sub-header stats */}
        <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: 10, color: T.textMuted, fontFamily: T.mono, flexWrap: 'wrap' }}>
          {lastPrice != null && (
            <span>
              <span style={{ color: T.textDim }}>last</span>{' '}
              <span style={{ color: T.text, fontWeight: 700 }}>${lastPrice.toLocaleString()}</span>
            </span>
          )}
          {snapshotTs != null && (
            <span>
              <span style={{ color: T.textDim }}>snapshot</span>{' '}
              <span style={{ color: T.text }}>{new Date(snapshotTs * 1000).toISOString().slice(11, 19)}Z</span>
            </span>
          )}
          {macro?.age_s != null && (
            <span>
              <span style={{ color: T.textDim }}>macro age</span>{' '}
              <span style={{ color: macro.age_s > 180 ? T.amber : T.text }}>{Math.round(macro.age_s)}s</span>
            </span>
          )}
        </div>
      </div>

      {error && (
        <div style={{
          padding: '10px 14px',
          marginBottom: 14,
          borderRadius: 6,
          background: 'rgba(239,68,68,0.1)',
          border: '1px solid rgba(239,68,68,0.2)',
          fontSize: 11,
          color: T.red,
        }}>
          {error}
        </div>
      )}

      {loading && !snapshot && (
        <div style={{
          padding: 40,
          textAlign: 'center',
          color: T.textMuted,
          fontSize: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
        }}>
          Waiting for first /v4/snapshot response...
        </div>
      )}

      {snapshot && (
        <>
          {/* Use V4Panel component - the shared decision surface */}
          <V4Panel snapshot={snapshot} />

          {/* Additional full-page specific content can go here if needed */}
          <RawJsonPeek snapshot={snapshot} />
        </>
      )}
    </div>
  );
}
