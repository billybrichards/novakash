/**
 * V2Surface — dedicated /data/v2 dashboard for the Sequoia v5.2 probability
 * scorer.
 *
 * v2 is the calibrated-probability layer: a LightGBM scorer on top of the
 * v1 TimesFM features + session/VPIN/volatility features, with temperature
 * scaling to return a well-calibrated P(UP) at a specific window close.
 *
 * What this page shows:
 *   - Asset selector (BTC/ETH/SOL/XRP)
 *   - Timescale tabs (5m/15m)
 *   - Large P(UP) gauge with raw-vs-calibrated split
 *   - TimesFM-derived metrics (predicted_close, confidence, spread)
 *   - Quantile fan (p10..p90) from the nested timesfm block
 *   - Model version chip (training commit SHA prefix)
 *   - Feature freshness summary (ms ages from the feature cache)
 *   - Prediction history strip (last 20 snapshots, in-memory)
 *
 * Data source: /api/v2/probability and /api/v2/probability/15m
 *   → hub/api/margin.py → TIMESFM_URL /v2/probability
 *
 * NOTE: the timesfm-repo v2 scorer supports 5m and 15m windows. The
 * "/v2/probability/1h" variant does NOT exist in the current deploy, so
 * this page deliberately ships with only 5m and 15m tabs. Adding a 1h
 * horizon would require extending the separate timesfm repo.
 *
 * Follow-up (deferred): push-mode feature drift table — that work would
 * require extending v2_routes.py in the timesfm repo to emit the feature
 * cache deltas over a wire contract. Scoped out of this PR per FE-05.
 *
 * Refresh cadence: 4s.
 */

import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useApi } from '../../hooks/useApi.js';

// ─── Theme (copied verbatim from V4Surface.jsx) ─────────────────────────
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

const TIMESCALES = [
  { key: '5m', label: '5m', endpoint: '/v2/probability', defaultSeconds: 60 },
  { key: '15m', label: '15m', endpoint: '/v2/probability/15m', defaultSeconds: 300 },
];

// ─── Primitives ─────────────────────────────────────────────────────────

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

function SectionHeader({ title, subtitle, badge, badgeColor = T.cyan }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'baseline',
      justifyContent: 'space-between',
      marginBottom: 10,
      padding: '0 2px',
    }}>
      <div>
        <span style={{
          fontSize: 11,
          fontWeight: 800,
          color: T.white,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
        }}>{title}</span>
        {subtitle && (
          <span style={{
            fontSize: 9,
            color: T.textMuted,
            marginLeft: 8,
            fontFamily: T.mono,
          }}>{subtitle}</span>
        )}
      </div>
      {badge && (
        <Chip
          color={badgeColor}
          bg={`${badgeColor}1a`}
          border={`${badgeColor}55`}
          value={badge}
        />
      )}
    </div>
  );
}

function Metric({ label, value, color = T.text, sub }) {
  return (
    <div style={{
      background: 'rgba(15,23,42,0.6)',
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 4,
      padding: '6px 10px',
    }}>
      <div style={{
        fontSize: 8,
        color: T.textMuted,
        fontWeight: 700,
        letterSpacing: '0.08em',
        marginBottom: 2,
      }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 900, fontFamily: T.mono, color }}>{value}</div>
      {sub && <div style={{ fontSize: 8, color: T.textDim, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

// ─── P(UP) gauge ────────────────────────────────────────────────────────
//
// A large dial showing 0.0 to 1.0 calibrated P(UP). Green above 0.55,
// red below 0.45, amber in the neutral zone.

function ProbabilityGauge({ pCalibrated, pRaw }) {
  // Colour transitions on the calibrated value
  const color =
    pCalibrated == null
      ? T.textMuted
      : pCalibrated > 0.55
      ? T.green
      : pCalibrated < 0.45
      ? T.red
      : T.amber;

  // Arc geometry — 180° sweep
  const R = 80;
  const CX = 100;
  const CY = 100;

  const toXY = (p) => {
    // Map [0,1] to angle [180°, 0°] (left to right across the top arc)
    const angle = Math.PI * (1 - p);
    return [
      CX + R * Math.cos(angle),
      CY - R * Math.sin(angle),
    ];
  };

  const [x1, y1] = toXY(0);
  const [x2, y2] = toXY(1);

  let needleLine = null;
  if (pCalibrated != null) {
    const [nx, ny] = toXY(pCalibrated);
    needleLine = `M${CX},${CY} L${nx.toFixed(1)},${ny.toFixed(1)}`;
  }

  // Raw probability as a ghost dot
  let rawDot = null;
  if (pRaw != null && pRaw !== pCalibrated) {
    const [rx, ry] = toXY(pRaw);
    rawDot = { x: rx, y: ry };
  }

  // Background arc
  const backgroundArc = `M${x1},${y1} A${R},${R} 0 0 1 ${x2},${y2}`;

  // Build coloured segments: [0..0.45] red, [0.45..0.55] amber, [0.55..1] green
  const segment = (from, to, strokeColor) => {
    const [sx, sy] = toXY(from);
    const [ex, ey] = toXY(to);
    // large-arc flag is 0 for <180° slices of this dial
    return (
      <path
        d={`M${sx},${sy} A${R},${R} 0 0 1 ${ex},${ey}`}
        fill="none"
        stroke={strokeColor}
        strokeWidth={10}
        strokeLinecap="round"
      />
    );
  };

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
    }}>
      <SectionHeader
        title="P(UP) Probability"
        subtitle="calibrated via temperature scaling"
      />
      <svg viewBox="0 0 200 130" style={{ width: '100%', maxWidth: 360, display: 'block' }}>
        {/* Background arc */}
        <path
          d={backgroundArc}
          fill="none"
          stroke="rgba(30,41,59,0.8)"
          strokeWidth={12}
          strokeLinecap="round"
        />
        {/* Coloured zones */}
        {segment(0, 0.45, 'rgba(239,68,68,0.65)')}
        {segment(0.45, 0.55, 'rgba(245,158,11,0.7)')}
        {segment(0.55, 1, 'rgba(16,185,129,0.65)')}

        {/* Needle */}
        {needleLine && (
          <path
            d={needleLine}
            stroke={color}
            strokeWidth={3}
            strokeLinecap="round"
          />
        )}
        {/* Center dot */}
        <circle cx={CX} cy={CY} r={5} fill={color} />

        {/* Raw ghost dot */}
        {rawDot && (
          <circle
            cx={rawDot.x}
            cy={rawDot.y}
            r={4}
            fill="none"
            stroke={T.textMuted}
            strokeWidth={1.5}
            strokeDasharray="1.5 1.5"
          />
        )}

        {/* Scale ticks */}
        {[0, 0.25, 0.5, 0.75, 1].map((p) => {
          const [tx, ty] = toXY(p);
          return (
            <text
              key={p}
              x={tx}
              y={ty + 14}
              fontSize="6.5"
              fontFamily="monospace"
              fill="rgba(100,116,139,1)"
              textAnchor="middle"
            >
              {p.toFixed(2)}
            </text>
          );
        })}
      </svg>
      <div style={{
        fontSize: 28,
        fontWeight: 900,
        fontFamily: T.mono,
        color,
        marginTop: 4,
      }}>
        {pCalibrated != null ? pCalibrated.toFixed(3) : '—'}
      </div>
      {pRaw != null && pRaw !== pCalibrated && (
        <div style={{
          fontSize: 9,
          color: T.textMuted,
          fontFamily: T.mono,
          marginTop: 2,
        }}>
          raw {pRaw.toFixed(3)} → cal {pCalibrated?.toFixed(3)}
        </div>
      )}
    </div>
  );
}

// ─── Quantile fan ───────────────────────────────────────────────────────

function QuantileFan({ quantiles }) {
  if (!quantiles) return null;
  const { p10, p25, p50, p75, p90 } = quantiles;
  // v2 nested quantiles are ARRAYS of horizon values; use the final element
  const finalize = (arr) => (Array.isArray(arr) ? arr[arr.length - 1] : arr);
  const p10v = finalize(p10);
  const p25v = finalize(p25);
  const p50v = finalize(p50);
  const p75v = finalize(p75);
  const p90v = finalize(p90);
  if (p10v == null || p90v == null || p50v == null) return null;

  const range = p90v - p10v;
  if (range === 0) return null;

  const iqrStart = ((p25v - p10v) / range) * 100;
  const iqrEnd = ((p75v - p10v) / range) * 100;
  const medianPos = ((p50v - p10v) / range) * 100;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Quantile Fan"
        subtitle="p10 / p25 / p50 / p75 / p90 at horizon end"
      />
      <div style={{
        position: 'relative',
        height: 28,
        background: 'rgba(15,23,42,0.6)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 4,
        overflow: 'visible',
        marginTop: 8,
      }}>
        {/* IQR band */}
        <div style={{
          position: 'absolute',
          left: `${iqrStart}%`,
          width: `${iqrEnd - iqrStart}%`,
          top: 0,
          bottom: 0,
          background: 'rgba(6,182,212,0.38)',
        }} />
        {/* Median dot */}
        <div style={{
          position: 'absolute',
          left: `calc(${medianPos}% - 4px)`,
          top: -3,
          width: 8,
          height: 34,
          background: T.cyan,
          borderRadius: 2,
          boxShadow: '0 0 8px rgba(6,182,212,0.6)',
        }} />
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(5, 1fr)',
        marginTop: 6,
        fontSize: 9,
        color: T.textDim,
        fontFamily: T.mono,
      }}>
        <span>p10 {p10v != null ? p10v.toFixed(0) : '—'}</span>
        <span style={{ textAlign: 'center' }}>p25 {p25v != null ? p25v.toFixed(0) : '—'}</span>
        <span style={{ textAlign: 'center', color: T.cyan, fontWeight: 800 }}>p50 {p50v != null ? p50v.toFixed(0) : '—'}</span>
        <span style={{ textAlign: 'center' }}>p75 {p75v != null ? p75v.toFixed(0) : '—'}</span>
        <span style={{ textAlign: 'right' }}>p90 {p90v != null ? p90v.toFixed(0) : '—'}</span>
      </div>
    </div>
  );
}

// ─── Prediction history strip ──────────────────────────────────────────

function HistoryStrip({ history }) {
  if (!history || history.length === 0) return null;
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Recent Predictions"
        subtitle={`last ${history.length} · in-memory`}
      />
      <div style={{
        display: 'flex',
        alignItems: 'end',
        gap: 3,
        height: 48,
      }}>
        {history.map((h, i) => {
          const p = h?.probability_up;
          if (p == null) {
            return <div key={i} style={{ flex: 1, height: 4, background: T.textDim, borderRadius: 1 }} />;
          }
          // Bar height encodes distance from 0.5; colour encodes direction
          const pct = Math.max(6, Math.abs(p - 0.5) * 200);
          const c = p > 0.55 ? T.green : p < 0.45 ? T.red : T.amber;
          return (
            <div
              key={i}
              title={`P(UP)=${p.toFixed(3)}`}
              style={{
                flex: 1,
                height: `${pct}%`,
                minHeight: 3,
                background: c,
                borderRadius: 1,
                opacity: 0.4 + (i / history.length) * 0.6,
              }}
            />
          );
        })}
      </div>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        marginTop: 6,
        fontSize: 8,
        color: T.textDim,
        fontFamily: T.mono,
      }}>
        <span>older</span>
        <span>latest</span>
      </div>
    </div>
  );
}

// ─── Feature freshness ─────────────────────────────────────────────────

function FeatureFreshness({ freshness }) {
  if (!freshness || Object.keys(freshness).length === 0) return null;
  const entries = Object.entries(freshness);
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Feature Freshness"
        subtitle={`${entries.length} features · ms age`}
      />
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))',
        gap: 4,
      }}>
        {entries.map(([k, v]) => {
          const ms = typeof v === 'number' ? v : null;
          const stale = ms == null ? true : ms > 30000;
          const c = stale ? T.amber : T.green;
          const label =
            ms == null ? '—' : ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
          return (
            <div
              key={k}
              style={{
                background: `${c}0d`,
                border: `1px solid ${c}33`,
                borderLeft: `3px solid ${c}`,
                borderRadius: 3,
                padding: '4px 8px',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                fontFamily: T.mono,
                fontSize: 9,
              }}
            >
              <span style={{ color: T.textMuted, textTransform: 'uppercase' }}>{k}</span>
              <span style={{ color: c, fontWeight: 800 }}>{label}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Raw JSON peek ─────────────────────────────────────────────────────

function RawJsonPeek({ data }) {
  const [open, setOpen] = useState(false);
  if (!data) return null;
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
        <span>Raw /v2/probability JSON</span>
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
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ─── Page ──────────────────────────────────────────────────────────────

export default function V2Surface() {
  const api = useApi();
  const [probability, setProbability] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [asset, setAsset] = useState('BTC');
  const [timescale, setTimescale] = useState('5m');
  const [history, setHistory] = useState([]);
  const lastFetch = useRef(0);

  const tsConfig = TIMESCALES.find((t) => t.key === timescale) || TIMESCALES[0];

  const fetchProbability = async () => {
    try {
      const res = await api(
        'GET',
        `${tsConfig.endpoint}?asset=${asset}&seconds_to_close=${tsConfig.defaultSeconds}`,
      );
      const payload = res?.data || res;
      setProbability(payload);
      setError(null);
      // Push into in-memory history (max 20)
      setHistory((prev) => {
        const next = [...prev, payload];
        return next.slice(-20);
      });
    } catch (err) {
      setError(err.message || `Failed to fetch ${tsConfig.endpoint}`);
    } finally {
      setLoading(false);
      lastFetch.current = Date.now();
    }
  };

  // Reset history when asset or timescale changes
  useEffect(() => {
    setHistory([]);
  }, [asset, timescale]);

  useEffect(() => { fetchProbability(); }, [api, asset, timescale]);

  useEffect(() => {
    const interval = setInterval(fetchProbability, 4000);
    return () => clearInterval(interval);
  }, [api, asset, timescale]);

  const pUp = probability?.probability_up;
  const pRaw = probability?.probability_raw;
  const modelVersion = probability?.model_version;
  const secondsToClose = probability?.seconds_to_close;
  const deltaBucket = probability?.delta_bucket;
  const timesfm = probability?.timesfm;
  const timestamp = probability?.timestamp;
  const freshness = probability?.feature_freshness_ms;

  // Shorten the model version string for the chip (commit SHA prefix + name)
  const shortModel = (() => {
    if (!modelVersion) return null;
    // Format: "15a4e3e@v2/btc/btc_5m/..."
    const at = modelVersion.indexOf('@');
    if (at > 0) {
      const sha = modelVersion.slice(0, at);
      return sha;
    }
    return modelVersion.slice(0, 12);
  })();

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
              flexWrap: 'wrap',
            }}>
              V2 Data Surface
              <Chip
                color={T.purple}
                bg="rgba(168,85,247,0.15)"
                border="rgba(168,85,247,0.3)"
                value="Sequoia v5.2"
              />
              <Chip
                color={T.cyan}
                bg="rgba(6,182,212,0.12)"
                border="rgba(6,182,212,0.3)"
                value={`${tsConfig.endpoint} · ${asset}`}
              />
              {shortModel && (
                <Chip
                  color={T.amber}
                  bg="rgba(245,158,11,0.1)"
                  border="rgba(245,158,11,0.3)"
                  label="sha"
                  value={shortModel}
                  title={modelVersion}
                />
              )}
            </h1>
            <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 880, lineHeight: 1.5 }}>
              Calibrated P(UP) probability from the Sequoia v5.2 LightGBM
              scorer. Raw probability is the model's direct output; calibrated
              is after temperature scaling. The timesfm sub-block carries the
              underlying point forecast, confidence and quantiles that the
              scorer sees as features. Refreshes every 4 seconds.
            </p>
          </div>

          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            {/* Timescale tabs */}
            <div style={{
              display: 'flex',
              gap: 0,
              borderRadius: 4,
              border: `1px solid ${T.cardBorder}`,
              overflow: 'hidden',
            }}>
              {TIMESCALES.map((t) => {
                const active = t.key === timescale;
                return (
                  <button
                    key={t.key}
                    onClick={() => setTimescale(t.key)}
                    style={{
                      padding: '6px 14px',
                      fontSize: 10,
                      fontWeight: 700,
                      fontFamily: T.mono,
                      background: active ? 'rgba(168,85,247,0.18)' : 'transparent',
                      color: active ? T.purple : T.textMuted,
                      border: 'none',
                      borderRight: `1px solid ${T.cardBorder}`,
                      cursor: 'pointer',
                      letterSpacing: '0.05em',
                    }}
                  >
                    {t.label}
                  </button>
                );
              })}
            </div>

            {/* Asset selector */}
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
          {timestamp != null && (
            <span>
              <span style={{ color: T.textDim }}>scored</span>{' '}
              <span style={{ color: T.text }}>{new Date(timestamp * 1000).toISOString().slice(11, 19)}Z</span>
            </span>
          )}
          {secondsToClose != null && (
            <span>
              <span style={{ color: T.textDim }}>to close</span>{' '}
              <span style={{ color: T.text }}>{secondsToClose}s</span>
            </span>
          )}
          {deltaBucket != null && (
            <span>
              <span style={{ color: T.textDim }}>delta bucket</span>{' '}
              <span style={{ color: T.text }}>T-{deltaBucket}</span>
            </span>
          )}
          {timesfm?.direction && (
            <span>
              <span style={{ color: T.textDim }}>timesfm dir</span>{' '}
              <span style={{ color: timesfm.direction === 'UP' ? T.green : T.red }}>
                {timesfm.direction}
              </span>
            </span>
          )}
          {timesfm?.confidence != null && (
            <span>
              <span style={{ color: T.textDim }}>timesfm conf</span>{' '}
              <span style={{ color: T.text }}>{timesfm.confidence.toFixed(3)}</span>
            </span>
          )}
        </div>
      </div>

      {/* Error surface */}
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

      {loading && !probability && (
        <div style={{
          padding: 40,
          textAlign: 'center',
          color: T.textMuted,
          fontSize: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
        }}>
          Waiting for first {tsConfig.endpoint} response...
        </div>
      )}

      {probability && (
        <>
          {/* Top row: gauge + timesfm metrics grid */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
            gap: 14,
            marginBottom: 14,
          }}>
            <ProbabilityGauge pCalibrated={pUp} pRaw={pRaw} />

            <div style={{
              background: T.card,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 8,
              padding: 14,
            }}>
              <SectionHeader
                title="TimesFM Core"
                subtitle="point forecast features"
                badge={timesfm?.direction}
                badgeColor={timesfm?.direction === 'UP' ? T.green : timesfm?.direction === 'DOWN' ? T.red : T.textMuted}
              />
              <div style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(2, 1fr)',
                gap: 8,
              }}>
                <Metric
                  label="PREDICTED CLOSE"
                  value={timesfm?.predicted_close != null ? `$${timesfm.predicted_close.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}
                  color={T.cyan}
                />
                <Metric
                  label="TIMESFM CONF"
                  value={timesfm?.confidence != null ? timesfm.confidence.toFixed(3) : '—'}
                  color={T.text}
                />
                <Metric
                  label="SPREAD"
                  value={timesfm?.spread != null ? `$${timesfm.spread.toFixed(2)}` : '—'}
                  color={T.amber}
                />
                <Metric
                  label="P(DOWN)"
                  value={probability?.probability_down != null ? probability.probability_down.toFixed(3) : '—'}
                  color={T.red}
                />
              </div>

              {/* Raw vs calibrated split */}
              {pRaw != null && pUp != null && (
                <div style={{
                  marginTop: 10,
                  padding: '8px 10px',
                  background: 'rgba(168,85,247,0.06)',
                  border: '1px solid rgba(168,85,247,0.22)',
                  borderRadius: 4,
                  fontSize: 10,
                  color: T.text,
                  fontFamily: T.mono,
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 12,
                }}>
                  <span>
                    <span style={{ color: T.textDim }}>raw</span>{' '}
                    <span style={{ fontWeight: 800 }}>{pRaw.toFixed(3)}</span>
                  </span>
                  <span style={{ color: T.textDim }}>→</span>
                  <span>
                    <span style={{ color: T.textDim }}>calibrated</span>{' '}
                    <span style={{ color: T.purple, fontWeight: 800 }}>{pUp.toFixed(3)}</span>
                  </span>
                  <span style={{ color: T.textDim, fontSize: 9 }}>
                    Δ {(pUp - pRaw).toFixed(3)}
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Quantile fan */}
          <div style={{ marginBottom: 14 }}>
            <QuantileFan quantiles={timesfm?.quantiles} />
          </div>

          {/* History + freshness */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
            gap: 14,
            marginBottom: 14,
          }}>
            <HistoryStrip history={history} />
            <FeatureFreshness freshness={freshness} />
          </div>

          {/* Info footer */}
          <div style={{
            padding: '10px 14px',
            marginBottom: 14,
            background: 'rgba(6,182,212,0.06)',
            border: '1px solid rgba(6,182,212,0.2)',
            borderRadius: 6,
            fontSize: 10,
            color: T.text,
            lineHeight: 1.5,
          }}>
            <span style={{ color: T.cyan, fontWeight: 800, marginRight: 6 }}>ℹ</span>
            v2 is the Sequoia v5.2 LightGBM scorer layered on top of v1's
            TimesFM features plus session-aware, VPIN-dynamics and
            volatility-normalised feature blocks. For the full 9-timescale
            composite signal see{' '}
            <Link to="/data/v3" style={{ color: T.cyan, textDecoration: 'none' }}>/data/v3</Link>,
            and for the fused decision surface see{' '}
            <Link to="/data/v4" style={{ color: T.cyan, textDecoration: 'none' }}>/data/v4</Link>.
            <span style={{ color: T.textDim }}>
              {' '}· Push-mode feature drift table deferred (requires
              timesfm repo changes).
            </span>
          </div>

          {/* Raw JSON peek */}
          <RawJsonPeek data={probability} />
        </>
      )}
    </div>
  );
}
