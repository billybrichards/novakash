/**
 * V3Surface — dedicated /data/v3 dashboard for the composite signal surface.
 *
 * v3 is the 9-timescale composite: at each of 5m/15m/1h/4h/24h/48h/72h/1w/2w
 * the scorer produces a composite ∈ [-1, +1] from 7 sub-signals
 * (elm, cascade, taker, oi, funding, vpin, momentum) plus a cascade FSM
 * state and a regime label. This page exposes all of it at once:
 *
 *   - Composite heatmap (9 timescales × 1 row)
 *   - 7-signal grid per timescale (compact bars)
 *   - Cascade FSM chips (strength, tau1, tau2, exhaustion_t, signal)
 *   - Regime strip (current regime across timescales)
 *   - Direction agreement bar (derived from composites)
 *   - Model lineage chip
 *
 * Data source: /api/v3/snapshot (hub/api/margin.py → TIMESFM_URL /v3/snapshot)
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
  orange: '#f97316',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const TIMESCALES = ['5m', '15m', '1h', '4h', '24h', '48h', '72h', '1w', '2w'];

const SIGNAL_KEYS = ['elm', 'cascade', 'taker', 'oi', 'funding', 'vpin', 'momentum'];

const SIGNAL_COLORS = {
  elm: T.purple,
  cascade: T.red,
  taker: T.cyan,
  oi: T.blue,
  funding: T.amber,
  vpin: T.green,
  momentum: T.orange,
};

const REGIME_COLOR = {
  TRENDING_UP: T.green,
  TRENDING_DOWN: T.red,
  MEAN_REVERTING: T.cyan,
  CHOPPY: T.amber,
  NO_EDGE: T.textDim,
};

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

// ─── Heatmap cell ──────────────────────────────────────────────────────
//
// Colour scale for composite ∈ [-1, +1]:
//   -1..-0.3 red
//   -0.3..0.3 amber / grey
//   0.3..1 green
// Intensity = |composite|. NO_EDGE and null → deep grey.

function compositeColor(c) {
  if (c == null) return T.textDim;
  const abs = Math.min(1, Math.abs(c));
  const alpha = 0.15 + abs * 0.7;
  if (c >= 0.3) return `rgba(16, 185, 129, ${alpha})`;
  if (c <= -0.3) return `rgba(239, 68, 68, ${alpha})`;
  return `rgba(245, 158, 11, ${Math.max(0.2, abs * 0.5)})`;
}

function HeatmapRow({ timescales }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Composite Heatmap"
        subtitle="9 timescales × composite_v3 ∈ [-1, +1]"
      />
      <div style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${TIMESCALES.length}, 1fr)`,
        gap: 4,
      }}>
        {TIMESCALES.map((ts) => {
          const data = timescales[ts];
          const c = data?.composite;
          const bg = compositeColor(c);
          const text =
            c == null
              ? '—'
              : c >= 0
              ? `+${c.toFixed(2)}`
              : c.toFixed(2);
          const labelColor =
            c == null ? T.textDim : Math.abs(c) > 0.5 ? T.white : T.text;
          return (
            <div
              key={ts}
              title={c != null ? `${ts}: composite ${c.toFixed(3)}` : `${ts}: no data`}
              style={{
                background: bg,
                border: `1px solid ${T.cardBorder}`,
                borderRadius: 4,
                padding: '10px 6px',
                textAlign: 'center',
                minHeight: 58,
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
              }}
            >
              <div style={{
                fontSize: 10,
                fontWeight: 800,
                color: T.white,
                fontFamily: T.mono,
                letterSpacing: '0.05em',
                opacity: 0.85,
              }}>{ts}</div>
              <div style={{
                fontSize: 13,
                fontWeight: 900,
                color: labelColor,
                fontFamily: T.mono,
                marginTop: 4,
              }}>{text}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Regime strip ──────────────────────────────────────────────────────

function RegimeStrip({ timescales }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Regime"
        subtitle="derived from composite sign + magnitude"
      />
      <div style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${TIMESCALES.length}, 1fr)`,
        gap: 4,
      }}>
        {TIMESCALES.map((ts) => {
          const data = timescales[ts];
          const c = data?.composite;
          // Derive a regime label client-side from composite magnitude:
          // We don't have a dedicated regime field in v3/snapshot, so this
          // is a rough projection matching how v4 labels them.
          let regime = 'NO_EDGE';
          if (c != null) {
            if (c >= 0.3) regime = 'TRENDING_UP';
            else if (c <= -0.3) regime = 'TRENDING_DOWN';
            else if (Math.abs(c) > 0.1) regime = 'MEAN_REVERTING';
            else regime = 'CHOPPY';
          }
          const color = REGIME_COLOR[regime] || T.textDim;
          const shortLabel =
            regime === 'TRENDING_UP'
              ? 'UP'
              : regime === 'TRENDING_DOWN'
              ? 'DN'
              : regime === 'MEAN_REVERTING'
              ? 'MR'
              : regime === 'CHOPPY'
              ? 'CH'
              : '—';
          return (
            <div
              key={ts}
              title={`${ts}: ${regime}`}
              style={{
                background: `${color}18`,
                border: `1px solid ${color}55`,
                borderLeft: `3px solid ${color}`,
                borderRadius: 4,
                padding: '6px 4px',
                textAlign: 'center',
              }}
            >
              <div style={{
                fontSize: 8,
                color: T.textMuted,
                fontFamily: T.mono,
                fontWeight: 700,
              }}>{ts}</div>
              <div style={{
                fontSize: 11,
                fontWeight: 800,
                color,
                fontFamily: T.mono,
                marginTop: 2,
                letterSpacing: '0.05em',
              }}>{shortLabel}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Direction agreement bar ───────────────────────────────────────────

function AlignmentBar({ timescales }) {
  const composites = TIMESCALES.map((ts) => timescales[ts]?.composite).filter(
    (c) => c != null,
  );
  const n = composites.length;
  const long = composites.filter((c) => c > 0.1).length;
  const short = composites.filter((c) => c < -0.1).length;
  const flat = n - long - short;

  const agreement = n === 0 ? 0 : Math.max(long, short) / n;

  const agreementColor =
    agreement >= 0.8 ? T.green : agreement >= 0.5 ? T.amber : T.red;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Direction Agreement"
        subtitle={`${n} timescales · agreement ${(agreement * 100).toFixed(0)}%`}
        badge={
          long > short && agreement >= 0.5
            ? 'BULL'
            : short > long && agreement >= 0.5
            ? 'BEAR'
            : 'MIXED'
        }
        badgeColor={agreementColor}
      />
      <div style={{
        display: 'flex',
        height: 24,
        borderRadius: 4,
        overflow: 'hidden',
        border: `1px solid ${T.cardBorder}`,
        background: 'rgba(15,23,42,0.6)',
      }}>
        {n > 0 && (
          <>
            <div
              title={`${short} SHORT`}
              style={{
                flex: short,
                background: 'rgba(239,68,68,0.55)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 10,
                color: T.white,
                fontWeight: 800,
                fontFamily: T.mono,
              }}
            >
              {short > 0 ? short : ''}
            </div>
            <div
              title={`${flat} FLAT`}
              style={{
                flex: flat,
                background: 'rgba(245,158,11,0.4)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 10,
                color: T.white,
                fontWeight: 800,
                fontFamily: T.mono,
              }}
            >
              {flat > 0 ? flat : ''}
            </div>
            <div
              title={`${long} LONG`}
              style={{
                flex: long,
                background: 'rgba(16,185,129,0.55)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 10,
                color: T.white,
                fontWeight: 800,
                fontFamily: T.mono,
              }}
            >
              {long > 0 ? long : ''}
            </div>
          </>
        )}
        {n === 0 && (
          <div style={{
            flex: 1,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 10,
            color: T.textMuted,
            fontFamily: T.mono,
          }}>No data</div>
        )}
      </div>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        marginTop: 6,
        fontSize: 9,
        color: T.textDim,
        fontFamily: T.mono,
      }}>
        <span>SHORT ({short})</span>
        <span>FLAT ({flat})</span>
        <span>LONG ({long})</span>
      </div>
    </div>
  );
}

// ─── Per-timescale signal grid ─────────────────────────────────────────

function SignalBar({ name, value, color }) {
  const v = typeof value === 'number' ? value : null;
  const pct = v != null ? Math.abs(v) * 100 : 0;
  const direction = (v ?? 0) >= 0 ? 'right' : 'left';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 2 }}>
      <div style={{
        width: 50,
        fontSize: 7,
        color: T.textMuted,
        fontWeight: 700,
        textTransform: 'uppercase',
        fontFamily: T.mono,
      }}>{name}</div>
      <div style={{
        flex: 1,
        height: 8,
        background: 'rgba(255,255,255,0.03)',
        borderRadius: 2,
        position: 'relative',
        overflow: 'hidden',
      }}>
        <div style={{
          position: 'absolute',
          left: '50%',
          top: 0,
          bottom: 0,
          width: 1,
          background: 'rgba(255,255,255,0.08)',
        }} />
        <div style={{
          position: 'absolute',
          [direction === 'right' ? 'left' : 'right']: '50%',
          top: 0,
          bottom: 0,
          width: `${Math.min(pct, 100) / 2}%`,
          background: color,
          borderRadius: 1,
          opacity: 0.7,
        }} />
      </div>
      <div style={{
        width: 32,
        fontSize: 8,
        fontFamily: T.mono,
        color: v == null ? T.textDim : v >= 0 ? T.green : T.red,
        textAlign: 'right',
        fontWeight: 700,
      }}>
        {v != null ? v.toFixed(2) : '—'}
      </div>
    </div>
  );
}

function TimescaleCard({ ts, data }) {
  if (!data) {
    return (
      <div style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        padding: 12,
      }}>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 8,
        }}>
          <span style={{ fontSize: 11, fontWeight: 800, color: T.white }}>{ts}</span>
          <Chip
            color={T.textDim}
            bg="rgba(71,85,105,0.1)"
            border="rgba(71,85,105,0.25)"
            value="NO DATA"
          />
        </div>
        <div style={{ fontSize: 9, color: T.textDim }}>
          No score received yet.
        </div>
      </div>
    );
  }

  const c = data.composite;
  const signals = data.signals || {};
  const cascade = data.cascade || {};
  const color = c == null ? T.textMuted : c >= 0.3 ? T.green : c <= -0.3 ? T.red : T.amber;
  const dir = c == null ? '—' : c >= 0.1 ? 'LONG' : c <= -0.1 ? 'SHORT' : 'FLAT';

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 12,
    }}>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: 6,
      }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: T.white }}>{ts}</span>
        <Chip
          color={color}
          bg={`${color}18`}
          border={`${color}55`}
          value={dir}
        />
      </div>
      <div style={{
        fontSize: 20,
        fontWeight: 900,
        fontFamily: T.mono,
        color,
        marginBottom: 6,
      }}>
        {c != null ? (c >= 0 ? `+${c.toFixed(3)}` : c.toFixed(3)) : '—'}
      </div>

      {/* Signal bars */}
      <div style={{ marginBottom: 6 }}>
        {SIGNAL_KEYS.map((k) => (
          <SignalBar
            key={k}
            name={k}
            value={signals[k]}
            color={SIGNAL_COLORS[k]}
          />
        ))}
      </div>

      {/* Cascade FSM chips */}
      {(cascade.strength != null ||
        cascade.tau1 != null ||
        cascade.tau2 != null ||
        cascade.exhaustion_t != null ||
        cascade.signal != null) && (
        <div style={{
          display: 'flex',
          gap: 3,
          flexWrap: 'wrap',
          marginTop: 6,
          paddingTop: 6,
          borderTop: `1px solid ${T.cardBorder}`,
        }}>
          {cascade.strength != null && (
            <Chip
              color={T.red}
              bg="rgba(239,68,68,0.08)"
              border="rgba(239,68,68,0.2)"
              label="STR"
              value={cascade.strength.toFixed(2)}
            />
          )}
          {cascade.tau1 != null && (
            <Chip
              color={T.amber}
              bg="rgba(245,158,11,0.08)"
              border="rgba(245,158,11,0.2)"
              label="τ1"
              value={cascade.tau1.toFixed(1)}
            />
          )}
          {cascade.tau2 != null && (
            <Chip
              color={T.amber}
              bg="rgba(245,158,11,0.08)"
              border="rgba(245,158,11,0.2)"
              label="τ2"
              value={cascade.tau2.toFixed(1)}
            />
          )}
          {cascade.exhaustion_t != null && (
            <Chip
              color={T.cyan}
              bg="rgba(6,182,212,0.08)"
              border="rgba(6,182,212,0.2)"
              label="EXH"
              value={`${Math.round(cascade.exhaustion_t)}s`}
            />
          )}
          {cascade.signal != null && (
            <Chip
              color={cascade.signal > 0 ? T.green : cascade.signal < 0 ? T.red : T.textMuted}
              bg="rgba(15,23,42,0.4)"
              border={T.cardBorder}
              label="SIG"
              value={cascade.signal.toFixed(2)}
            />
          )}
        </div>
      )}
    </div>
  );
}

// ─── Raw JSON peek ─────────────────────────────────────────────────────

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
        <span>Raw /v3/snapshot JSON</span>
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

// ─── Page ──────────────────────────────────────────────────────────────

export default function V3Surface() {
  const api = useApi();
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [asset, setAsset] = useState('BTC');
  const lastFetch = useRef(0);

  const fetchSnapshot = async () => {
    try {
      const res = await api('GET', `/v3/snapshot?asset=${asset}`);
      setSnapshot(res?.data || res);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch /v3/snapshot');
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

  const timescales = snapshot?.timescales || {};
  const model = snapshot?.model;
  const ts = snapshot?.ts;
  const modelName = model?.model_family || model?.model_version;
  const modelSha = model?.git_sha;

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
              V3 Data Surface
              <Chip
                color={T.purple}
                bg="rgba(168,85,247,0.15)"
                border="rgba(168,85,247,0.3)"
                value="composite"
              />
              <Chip
                color={T.cyan}
                bg="rgba(6,182,212,0.12)"
                border="rgba(6,182,212,0.3)"
                value={`/v3/snapshot · ${asset}`}
              />
              {modelName && (
                <Chip
                  color={T.amber}
                  bg="rgba(245,158,11,0.1)"
                  border="rgba(245,158,11,0.3)"
                  label="model"
                  value={modelName}
                  title={`git_sha: ${modelSha || 'unknown'}`}
                />
              )}
            </h1>
            <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 880, lineHeight: 1.5 }}>
              Multi-scale composite signal: 9 timescales × 7 sub-signals
              (ELM, cascade, taker-volume, open-interest, funding, VPIN,
              momentum) plus cascade FSM state. Weighted sum clamped to
              [-1, +1]. Refreshes every 4 seconds.
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
          {ts != null && (
            <span>
              <span style={{ color: T.textDim }}>snapshot</span>{' '}
              <span style={{ color: T.text }}>{new Date(ts * 1000).toISOString().slice(11, 19)}Z</span>
            </span>
          )}
          {modelSha && (
            <span>
              <span style={{ color: T.textDim }}>sha</span>{' '}
              <span style={{ color: T.text }}>{modelSha}</span>
            </span>
          )}
          <span>
            <span style={{ color: T.textDim }}>timescales live</span>{' '}
            <span style={{ color: T.text }}>
              {Object.values(timescales).filter((v) => v != null).length} / {TIMESCALES.length}
            </span>
          </span>
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
          Waiting for first /v3/snapshot response...
        </div>
      )}

      {snapshot && (
        <>
          {/* Heatmap row */}
          <div style={{ marginBottom: 14 }}>
            <HeatmapRow timescales={timescales} />
          </div>

          {/* Regime + alignment */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(420px, 1fr))',
            gap: 14,
            marginBottom: 14,
          }}>
            <RegimeStrip timescales={timescales} />
            <AlignmentBar timescales={timescales} />
          </div>

          {/* Per-timescale cards — full grid */}
          <div style={{ marginBottom: 14 }}>
            <SectionHeader
              title="Per-Timescale Signals"
              subtitle={`${SIGNAL_KEYS.join(' · ')} + cascade FSM`}
            />
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
              gap: 10,
            }}>
              {TIMESCALES.map((ts) => (
                <TimescaleCard key={ts} ts={ts} data={timescales[ts]} />
              ))}
            </div>
          </div>

          {/* Info footer */}
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
            v3 composite aggregates 7 signals across 9 timescales, each
            with its own cadence (1s-5m). The ELM signal is a direct
            linear remap of v2's calibrated P(UP): see{' '}
            <Link to="/data/v2" style={{ color: T.cyan, textDecoration: 'none' }}>/data/v2</Link>.
            For the fused-with-macro decision surface see{' '}
            <Link to="/data/v4" style={{ color: T.cyan, textDecoration: 'none' }}>/data/v4</Link>.
            The regime labels here are client-side projections from
            composite magnitude; v4 publishes authoritative regimes.
          </div>

          {/* Raw JSON peek */}
          <RawJsonPeek snapshot={snapshot} />
        </>
      )}
    </div>
  );
}
