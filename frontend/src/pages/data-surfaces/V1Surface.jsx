/**
 * V1Surface — dedicated /data/v1 dashboard for the legacy TimesFM
 * point forecast.
 *
 * v1 is the original, pre-Sequoia surface: raw TimesFM point forecast
 * with direction, confidence and a full quantile fan (p10..p90). It is
 * BTC only and frozen — no asset parameter, no composite, no regime.
 *
 * This page is the "museum exhibit" view of v1: it renders the live
 * /forecast response alongside a clear "superseded by v2" callout so
 * operators can see at a glance what v1 used to surface before the
 * Sequoia calibration layer landed.
 *
 * Data source: /api/v1/forecast (hub/api/margin.py → TIMESFM_URL /forecast)
 * Refresh cadence: 4s (matches V4Surface to keep the cross-region
 * fetcher load flat).
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

// ─── Primitives (local copies — can't import from V4Surface) ────────────

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

// ─── Forecast line chart (SVG, zero-dep) ───────────────────────────────
//
// We render the full point_forecast array plus the quantile envelope as
// a dependency-free SVG. The project already pulls enough chart code via
// recharts in other pages, but a 60-point forecast is trivially small so
// a hand-rolled SVG keeps this page zero-cost.

function ForecastChart({ point_forecast, quantiles }) {
  if (!point_forecast || point_forecast.length === 0) return null;
  const n = point_forecast.length;
  const p10 = quantiles?.p10 || [];
  const p90 = quantiles?.p90 || [];
  const p25 = quantiles?.p25 || [];
  const p75 = quantiles?.p75 || [];
  const p50 = quantiles?.p50 || point_forecast;

  // Collect all y values to find bounds
  const allValues = [
    ...point_forecast,
    ...p10,
    ...p25,
    ...p75,
    ...p90,
  ].filter((v) => typeof v === 'number' && Number.isFinite(v));
  if (allValues.length === 0) return null;

  const yMin = Math.min(...allValues);
  const yMax = Math.max(...allValues);
  const yRange = yMax - yMin || 1;

  // Use viewBox units so we can pad with CSS
  const W = 100;
  const H = 30;
  const toX = (i) => (i / (n - 1)) * W;
  const toY = (v) => H - ((v - yMin) / yRange) * H;

  const path = (arr) =>
    arr
      .map((v, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(2)},${toY(v).toFixed(2)}`)
      .join(' ');

  // Build the p10-p90 envelope polygon
  const envelope = (lo, hi) => {
    const loPath = lo
      .map((v, i) => `${toX(i).toFixed(2)},${toY(v).toFixed(2)}`)
      .join(' ');
    const hiPath = hi
      .slice()
      .reverse()
      .map((v, i) => `${toX(hi.length - 1 - i).toFixed(2)},${toY(v).toFixed(2)}`)
      .join(' ');
    return `${loPath} ${hiPath}`;
  };

  const hasOuter = p10.length === n && p90.length === n;
  const hasInner = p25.length === n && p75.length === n;

  return (
    <div style={{
      background: 'rgba(15,23,42,0.6)',
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 6,
      padding: 10,
    }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        style={{ width: '100%', height: 200, display: 'block' }}
      >
        {/* Outer envelope p10..p90 */}
        {hasOuter && (
          <polygon
            points={envelope(p10, p90)}
            fill="rgba(6,182,212,0.12)"
            stroke="none"
          />
        )}
        {/* Inner envelope p25..p75 */}
        {hasInner && (
          <polygon
            points={envelope(p25, p75)}
            fill="rgba(6,182,212,0.22)"
            stroke="none"
          />
        )}
        {/* Median / p50 dashed */}
        <path
          d={path(p50)}
          fill="none"
          stroke="rgba(6,182,212,0.6)"
          strokeWidth="0.25"
          strokeDasharray="0.8 0.4"
        />
        {/* Point forecast solid */}
        <path
          d={path(point_forecast)}
          fill="none"
          stroke={T.cyan}
          strokeWidth="0.4"
        />
      </svg>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        marginTop: 6,
        fontSize: 8,
        color: T.textDim,
        fontFamily: T.mono,
      }}>
        <span>t+0</span>
        <span>p10-p90 envelope · p25-p75 band · point forecast</span>
        <span>t+{n - 1}</span>
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
        <span>Raw Forecast JSON</span>
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

export default function V1Surface() {
  const api = useApi();
  const [forecast, setForecast] = useState(null);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [endpointAvailable, setEndpointAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  // v1 is BTC only — asset selector is disabled for non-BTC.
  const [asset, setAsset] = useState('BTC');
  const lastFetch = useRef(0);

  const fetchForecast = async () => {
    if (asset !== 'BTC') {
      // v1 is BTC only — don't try to fetch for other assets
      setForecast(null);
      setLoading(false);
      return;
    }
    try {
      const [fRes, hRes] = await Promise.allSettled([
        api('GET', '/v1/forecast'),
        api('GET', '/v1/health'),
      ]);

      if (fRes.status === 'fulfilled') {
        setForecast(fRes.value?.data || fRes.value);
        setError(null);
        setEndpointAvailable(true);
      } else {
        const err = fRes.reason;
        setError(err?.message || 'Failed to fetch /v1/forecast');
        // If the upstream 502/503s or the endpoint doesn't exist at all
        // we flip the friendly "not available" card.
        const code = err?.response?.status;
        if (code === 404 || code === 502 || code === 503) {
          setEndpointAvailable(false);
        }
      }

      if (hRes.status === 'fulfilled') {
        setHealth(hRes.value?.data || hRes.value);
      }
    } catch (err) {
      setError(err.message || 'Failed to fetch /v1 endpoints');
    } finally {
      setLoading(false);
      lastFetch.current = Date.now();
    }
  };

  useEffect(() => { fetchForecast(); }, [api, asset]);

  useEffect(() => {
    const interval = setInterval(fetchForecast, 4000);
    return () => clearInterval(interval);
  }, [api, asset]);

  const direction = forecast?.direction;
  const confidence = forecast?.confidence;
  const predictedClose = forecast?.predicted_close;
  const spread = forecast?.spread;
  const horizon = forecast?.horizon;
  const inputLength = forecast?.input_length;
  const timestamp = forecast?.timestamp;
  const pointForecast = forecast?.point_forecast;
  const quantiles = forecast?.quantiles;
  const currentPrice = pointForecast?.[0];

  const dirColor = direction === 'UP' ? T.green : direction === 'DOWN' ? T.red : T.textMuted;
  const confColor = confidence > 0.7 ? T.green : confidence > 0.4 ? T.amber : T.red;

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
              V1 Data Surface
              <Chip
                color={T.amber}
                bg="rgba(245,158,11,0.15)"
                border="rgba(245,158,11,0.3)"
                value="legacy"
              />
              <Chip color={T.cyan} bg="rgba(6,182,212,0.12)" border="rgba(6,182,212,0.3)" value="Poly + Perps" title="Legacy point forecast, superseded by v2. BTC only." />
              <Chip
                color={T.cyan}
                bg="rgba(6,182,212,0.12)"
                border="rgba(6,182,212,0.3)"
                value="/forecast · BTC only"
              />
              {health?.status === 'ok' && (
                <Chip
                  color={T.green}
                  bg="rgba(16,185,129,0.12)"
                  border="rgba(16,185,129,0.3)"
                  value="OK"
                />
              )}
            </h1>
            <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 880, lineHeight: 1.5 }}>
              Legacy TimesFM point forecast — raw direction, confidence,
              predicted close at horizon end, and the full quantile fan
              (p10..p90). BTC only, frozen surface; superseded by v2
              calibrated probability and v3 composite signals. Refreshes
              every 4 seconds.
            </p>
          </div>

          {/* Asset selector — only BTC is enabled */}
          <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
            {['BTC', 'ETH', 'SOL', 'XRP'].map((a) => {
              const enabled = a === 'BTC';
              return (
                <button
                  key={a}
                  onClick={() => enabled && setAsset(a)}
                  disabled={!enabled}
                  title={enabled ? '' : 'v1 is BTC only'}
                  style={{
                    padding: '6px 12px',
                    borderRadius: 4,
                    fontSize: 10,
                    fontWeight: 700,
                    fontFamily: T.mono,
                    background: asset === a ? 'rgba(6,182,212,0.15)' : 'transparent',
                    color: !enabled
                      ? T.textDim
                      : asset === a
                      ? T.cyan
                      : T.textMuted,
                    border: `1px solid ${asset === a ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
                    cursor: enabled ? 'pointer' : 'not-allowed',
                    letterSpacing: '0.05em',
                    opacity: enabled ? 1 : 0.4,
                  }}
                >
                  {a}
                </button>
              );
            })}
          </div>
        </div>

        {/* Sub-header stats */}
        <div style={{ display: 'flex', gap: 16, marginTop: 8, fontSize: 10, color: T.textMuted, fontFamily: T.mono, flexWrap: 'wrap' }}>
          {timestamp != null && (
            <span>
              <span style={{ color: T.textDim }}>forecast</span>{' '}
              <span style={{ color: T.text }}>{new Date(timestamp * 1000).toISOString().slice(11, 19)}Z</span>
            </span>
          )}
          {horizon != null && (
            <span>
              <span style={{ color: T.textDim }}>horizon</span>{' '}
              <span style={{ color: T.text }}>{horizon} steps</span>
            </span>
          )}
          {inputLength != null && (
            <span>
              <span style={{ color: T.textDim }}>input</span>{' '}
              <span style={{ color: T.text }}>{inputLength} ticks</span>
            </span>
          )}
          {health?.buffer_size != null && (
            <span>
              <span style={{ color: T.textDim }}>buffer</span>{' '}
              <span style={{ color: T.text }}>{health.buffer_size}</span>
            </span>
          )}
          {health?.uptime_seconds != null && (
            <span>
              <span style={{ color: T.textDim }}>uptime</span>{' '}
              <span style={{ color: T.text }}>{Math.round(health.uptime_seconds)}s</span>
            </span>
          )}
        </div>
      </div>

      {/* Error surface (only when endpoint is reachable but returned an error) */}
      {error && endpointAvailable && (
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

      {/* Non-BTC asset notice */}
      {asset !== 'BTC' && (
        <div style={{
          padding: 20,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
          textAlign: 'center',
          marginBottom: 14,
        }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: T.amber, marginBottom: 6 }}>
            v1 is BTC only
          </div>
          <p style={{ fontSize: 11, color: T.textMuted, margin: '6px 0', lineHeight: 1.5 }}>
            The legacy TimesFM surface only runs against a single BTC price
            feed. For per-asset forecasts use{' '}
            <Link to="/data/v2" style={{ color: T.cyan, textDecoration: 'none' }}>v2</Link>,{' '}
            <Link to="/data/v3" style={{ color: T.cyan, textDecoration: 'none' }}>v3</Link>{' '}
            or{' '}
            <Link to="/data/v4" style={{ color: T.cyan, textDecoration: 'none' }}>v4</Link>.
          </p>
        </div>
      )}

      {/* Endpoint not available card */}
      {!endpointAvailable && asset === 'BTC' && (
        <div style={{
          padding: 24,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderLeft: `4px solid ${T.amber}`,
          borderRadius: 8,
          marginBottom: 14,
        }}>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            marginBottom: 10,
          }}>
            <span style={{ fontSize: 20 }}>⚠</span>
            <span style={{
              fontSize: 13,
              fontWeight: 800,
              color: T.amber,
              letterSpacing: '0.05em',
              textTransform: 'uppercase',
            }}>
              v1 endpoint unavailable in this deploy
            </span>
          </div>
          <p style={{ fontSize: 11, color: T.text, lineHeight: 1.5, margin: '6px 0' }}>
            The legacy <code style={{ color: T.cyan, fontFamily: T.mono }}>/forecast</code>{' '}
            endpoint is not currently reachable through the hub proxy. v1
            has been superseded by the v2 Sequoia calibrated probability
            layer, v3 composite signals and v4 fusion surface.
          </p>
          <div style={{
            display: 'flex',
            gap: 10,
            marginTop: 14,
            flexWrap: 'wrap',
          }}>
            <Link
              to="/data/v2"
              style={{
                padding: '8px 14px',
                borderRadius: 4,
                fontSize: 10,
                fontWeight: 700,
                fontFamily: T.mono,
                background: 'rgba(6,182,212,0.15)',
                color: T.cyan,
                border: '1px solid rgba(6,182,212,0.35)',
                textDecoration: 'none',
                letterSpacing: '0.05em',
              }}
            >
              → Open V2 Probability
            </Link>
            <Link
              to="/data/v3"
              style={{
                padding: '8px 14px',
                borderRadius: 4,
                fontSize: 10,
                fontWeight: 700,
                fontFamily: T.mono,
                background: 'rgba(168,85,247,0.15)',
                color: T.purple,
                border: '1px solid rgba(168,85,247,0.35)',
                textDecoration: 'none',
                letterSpacing: '0.05em',
              }}
            >
              → Open V3 Composite
            </Link>
            <Link
              to="/data/v4"
              style={{
                padding: '8px 14px',
                borderRadius: 4,
                fontSize: 10,
                fontWeight: 700,
                fontFamily: T.mono,
                background: 'rgba(16,185,129,0.15)',
                color: T.green,
                border: '1px solid rgba(16,185,129,0.35)',
                textDecoration: 'none',
                letterSpacing: '0.05em',
              }}
            >
              → Open V4 Fusion
            </Link>
          </div>
        </div>
      )}

      {/* Loading shim */}
      {loading && !forecast && endpointAvailable && asset === 'BTC' && (
        <div style={{
          padding: 40,
          textAlign: 'center',
          color: T.textMuted,
          fontSize: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
        }}>
          Waiting for first /v1/forecast response...
        </div>
      )}

      {/* Live forecast */}
      {forecast && asset === 'BTC' && (
        <>
          {/* Top metrics row */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
            gap: 10,
            marginBottom: 14,
          }}>
            <Metric
              label="DIRECTION"
              value={direction || '—'}
              color={dirColor}
            />
            <Metric
              label="CONFIDENCE"
              value={confidence != null ? confidence.toFixed(3) : '—'}
              color={confColor}
            />
            <Metric
              label="PREDICTED CLOSE"
              value={predictedClose != null ? `$${predictedClose.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}
              color={T.cyan}
            />
            <Metric
              label="CURRENT"
              value={currentPrice != null ? `$${currentPrice.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : '—'}
              color={T.text}
            />
            <Metric
              label="SPREAD (P90-P10)"
              value={spread != null ? `$${spread.toFixed(2)}` : '—'}
              color={T.amber}
            />
          </div>

          {/* Forecast chart */}
          <div style={{ marginBottom: 14 }}>
            <SectionHeader
              title="Point Forecast"
              subtitle={`${pointForecast?.length || 0}-step horizon · p10..p90 envelope`}
              badge={direction}
              badgeColor={dirColor}
            />
            <ForecastChart
              point_forecast={pointForecast}
              quantiles={quantiles}
            />
          </div>

          {/* Info row: explaining v1 vs v2 */}
          <div style={{
            padding: '10px 14px',
            marginBottom: 14,
            background: 'rgba(168,85,247,0.06)',
            border: '1px solid rgba(168,85,247,0.2)',
            borderRadius: 6,
            fontSize: 10,
            color: T.text,
            lineHeight: 1.5,
          }}>
            <span style={{ color: T.purple, fontWeight: 800, marginRight: 6 }}>ℹ</span>
            This is raw TimesFM output — direction is derived from
            predicted_close vs window-open price, confidence is derived
            from spread-to-price ratio. The v2 Sequoia scorer layers a
            LightGBM calibration on top of these features and returns a
            properly calibrated P(UP) probability; see{' '}
            <Link to="/data/v2" style={{ color: T.cyan, textDecoration: 'none' }}>/data/v2</Link>.
          </div>

          {/* Raw JSON peek */}
          <RawJsonPeek data={forecast} />
        </>
      )}
    </div>
  );
}
