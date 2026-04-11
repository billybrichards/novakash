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

// ─── Consensus strip ─────────────────────────────────────────────────────

function ConsensusStrip({ consensus }) {
  if (!consensus) {
    return (
      <div style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        padding: 14,
        color: T.textMuted,
        fontSize: 10,
      }}>No consensus data.</div>
    );
  }

  const safe = consensus.safe_to_trade === true;
  const maxDiv = consensus.max_divergence_bps;
  const meanDiv = consensus.mean_divergence_bps;
  const agree = consensus.source_agreement_score;
  const tolerance = consensus.tolerance_bps;
  const sources = consensus.sources || {};
  const sourceEntries = Object.entries(sources);

  const liveCount = sourceEntries.filter(([, s]) => s?.available).length;
  const totalCount = sourceEntries.length;

  const safeColor = safe ? T.green : T.red;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Consensus"
        subtitle={`${liveCount}/${totalCount} sources live · tolerance ${tolerance?.toFixed?.(1) ?? '?'} bp`}
        badge={safe ? 'SAFE' : 'NOT SAFE'}
        badgeColor={safeColor}
      />

      {/* Summary row */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
        gap: 8,
        marginBottom: 12,
      }}>
        <Metric label="MAX DIV" value={maxDiv != null ? `${maxDiv.toFixed(2)}bp` : '—'} color={maxDiv > (tolerance ?? 15) ? T.red : T.green} />
        <Metric label="MEAN DIV" value={meanDiv != null ? `${meanDiv.toFixed(2)}bp` : '—'} color={T.cyan} />
        <Metric label="AGREEMENT" value={agree != null ? agree.toFixed(2) : '—'} color={agree >= 0.9 ? T.green : agree >= 0.7 ? T.amber : T.red} />
        <Metric label="REFERENCE" value={consensus.reference_price != null ? `$${consensus.reference_price.toLocaleString()}` : '—'} color={T.text} />
      </div>

      {/* Per-source chips */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
        gap: 6,
      }}>
        {sourceEntries.map(([key, src]) => {
          const available = src?.available;
          const price = src?.price;
          const ageMs = src?.age_ms;
          const fresh = available && ageMs != null && ageMs < 120000;
          const color = available ? (fresh ? T.green : T.amber) : T.red;
          const ageLabel = ageMs == null ? '—' : ageMs < 1000 ? `${ageMs}ms` : ageMs < 60000 ? `${(ageMs / 1000).toFixed(1)}s` : `${Math.round(ageMs / 1000)}s`;
          return (
            <div
              key={key}
              style={{
                background: `${color}0d`,
                border: `1px solid ${color}44`,
                borderLeft: `3px solid ${color}`,
                borderRadius: 4,
                padding: '6px 10px',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                gap: 8,
              }}
              title={available ? `age ${ageLabel}` : 'unavailable'}
            >
              <span style={{
                fontSize: 10,
                fontWeight: 700,
                color: T.text,
                fontFamily: T.mono,
                textTransform: 'uppercase',
                letterSpacing: '0.05em',
              }}>{key}</span>
              <div style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                <span style={{ fontSize: 11, color, fontFamily: T.mono, fontWeight: 800 }}>
                  {price != null ? `$${price.toLocaleString()}` : '—'}
                </span>
                <span style={{ fontSize: 8, color: T.textDim, fontFamily: T.mono }}>
                  {ageLabel}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {consensus.safe_to_trade_reason && (
        <div style={{
          marginTop: 10,
          padding: '6px 10px',
          background: safe ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)',
          border: `1px solid ${safeColor}33`,
          borderRadius: 4,
          fontSize: 9,
          color: safeColor,
          fontFamily: T.mono,
        }}>
          {consensus.safe_to_trade_reason}
        </div>
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

// ─── Macro card ──────────────────────────────────────────────────────────

function MacroCard({ macro }) {
  if (!macro) {
    return (
      <div style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        padding: 14,
        color: T.textMuted,
        fontSize: 10,
      }}>No macro data.</div>
    );
  }

  const biasColor = BIAS_COLOR[macro.bias] || T.textMuted;
  const gateColor = GATE_COLOR[macro.direction_gate] || T.textMuted;
  const ageS = macro.age_s;
  const stale = ageS != null && ageS > 180;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Macro · Qwen"
        subtitle={`age ${ageS != null ? `${Math.round(ageS)}s` : '—'}${stale ? ' · STALE' : ''}`}
        badge={macro.bias}
        badgeColor={biasColor}
      />

      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
        gap: 8,
        marginBottom: 12,
      }}>
        <Metric label="CONFIDENCE" value={macro.confidence != null ? `${macro.confidence}` : '—'} color={biasColor} />
        <Metric label="DIRECTION GATE" value={macro.direction_gate || '—'} color={gateColor} />
        <Metric label="SIZE MOD" value={macro.size_modifier != null ? `${macro.size_modifier.toFixed(2)}x` : '—'} color={T.cyan} />
        <Metric label="THRESH MOD" value={macro.threshold_modifier != null ? `${macro.threshold_modifier.toFixed(2)}x` : '—'} color={T.cyan} />
      </div>

      {macro.reasoning && (
        <div style={{
          padding: '8px 10px',
          background: 'rgba(168,85,247,0.08)',
          border: '1px solid rgba(168,85,247,0.22)',
          borderRadius: 4,
          fontSize: 10,
          color: T.text,
          fontStyle: 'italic',
          lineHeight: 1.4,
        }}>
          <span style={{ color: T.purple, fontWeight: 800, marginRight: 6 }}>✦</span>
          {macro.reasoning}
        </div>
      )}

      {/* Per-timescale macro map */}
      {macro.timescale_map && (
        <div style={{ marginTop: 12 }}>
          <div style={{
            fontSize: 8,
            color: T.textMuted,
            fontWeight: 800,
            letterSpacing: '0.08em',
            marginBottom: 6,
          }}>
            PER-HORIZON BIAS
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            {SHORT_TERM.map((ts) => {
              const tm = macro.timescale_map[ts];
              if (!tm) return null;
              const c = BIAS_COLOR[tm.bias] || T.textMuted;
              return (
                <Chip
                  key={ts}
                  color={c}
                  bg={`${c}1a`}
                  border={`${c}55`}
                  label={ts}
                  value={`${tm.bias || '—'}${tm.confidence != null ? ` ${tm.confidence}` : ''}`}
                  title={tm.reasoning || ''}
                />
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Events timeline ─────────────────────────────────────────────────────

function EventsTimeline({ events }) {
  if (!events || events.length === 0) return null;
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
      marginBottom: 14,
    }}>
      <SectionHeader
        title="Upcoming Macro Events"
        subtitle={`${events.length} in next window`}
      />
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {events.slice(0, 10).map((e, i) => {
          const c = IMPACT_COLOR[e.impact] || T.textMuted;
          return (
            <div
              key={i}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                padding: '6px 10px',
                background: `${c}0d`,
                border: `1px solid ${c}33`,
                borderLeft: `3px solid ${c}`,
                borderRadius: 4,
              }}
            >
              <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                <Chip color={c} bg={`${c}22`} border={`${c}55`} value={e.impact || '?'} />
                <span style={{ fontSize: 11, color: T.text, fontWeight: 600 }}>
                  {e.event_name || '—'}
                </span>
              </div>
              <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>
                in {e.in_minutes != null ? `${Math.round(e.in_minutes)}min` : '—'}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Per-timescale cards ─────────────────────────────────────────────────

function QuantileFan({ quantiles }) {
  // Tiny visualization: render a horizontal bar showing p10..p90 spread
  // (relative) with p50 as a dot. Bar width encodes IQR; dot position
  // encodes p50 position within p10..p90.
  if (!quantiles) return null;
  const { p10, p25, p50, p75, p90 } = quantiles;
  if (p10 == null || p90 == null || p50 == null) return null;

  const range = p90 - p10;
  if (range === 0) return null;

  const iqrStart = ((p25 - p10) / range) * 100;
  const iqrEnd = ((p75 - p10) / range) * 100;
  const medianPos = ((p50 - p10) / range) * 100;

  return (
    <div style={{ width: '100%', marginTop: 4 }}>
      <div style={{
        position: 'relative',
        height: 6,
        background: 'rgba(15,23,42,0.6)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 3,
        overflow: 'visible',
      }}>
        {/* IQR band */}
        <div style={{
          position: 'absolute',
          left: `${iqrStart}%`,
          width: `${iqrEnd - iqrStart}%`,
          top: 0,
          bottom: 0,
          background: 'rgba(6,182,212,0.4)',
        }} />
        {/* Median dot */}
        <div style={{
          position: 'absolute',
          left: `calc(${medianPos}% - 3px)`,
          top: -1,
          width: 6,
          height: 8,
          background: T.cyan,
          borderRadius: 1,
        }} />
      </div>
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        marginTop: 2,
        fontSize: 7,
        color: T.textDim,
        fontFamily: T.mono,
      }}>
        <span>{typeof p10 === 'number' ? p10.toFixed(0) : '—'}</span>
        <span>{typeof p50 === 'number' ? p50.toFixed(0) : '—'}</span>
        <span>{typeof p90 === 'number' ? p90.toFixed(0) : '—'}</span>
      </div>
    </div>
  );
}

function TimescaleCard({ ts, data }) {
  if (!data || data.status !== 'ok') {
    return (
      <div style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        padding: 14,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 800, color: T.white }}>{ts}</span>
          <Chip
            color={T.amber}
            bg="rgba(245,158,11,0.15)"
            border="rgba(245,158,11,0.3)"
            value={data?.status?.toUpperCase() || 'NO_DATA'}
          />
        </div>
        <div style={{ fontSize: 10, color: T.textMuted, lineHeight: 1.4 }}>
          {data?.status === 'no_model' && 'No model loaded for this timeframe.'}
          {data?.status === 'cold_start' && 'Warming up price buffer.'}
          {data?.status === 'stale' && 'Inputs older than max_age_s.'}
          {!data?.status && 'Awaiting first poll.'}
        </div>
      </div>
    );
  }

  const p = data.probability_up;
  const pRaw = data.probability_raw;
  const move = data.expected_move_bps;
  const vol = data.vol_forecast_bps;
  const regime = data.regime;
  const cascade = data.cascade;
  const quantiles = data.quantiles_at_close;
  const action = data.recommended_action || {};
  const side = action.side;
  const conviction = action.conviction;
  const reason = action.reason;

  const probColor = p > 0.55 ? T.green : p < 0.45 ? T.red : T.amber;
  const moveColor = move > 0 ? T.green : move < 0 ? T.red : T.textMuted;
  const regimeColor = REGIME_COLOR[regime] || T.textMuted;
  const verdictColor = side === 'LONG' ? T.green : side === 'SHORT' ? T.red : T.textMuted;
  const verdictText = side ? `${side}` : 'SKIP';

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 800, color: T.white }}>{ts}</span>
        <Chip
          color={verdictColor}
          bg={`${verdictColor}22`}
          border={`${verdictColor}55`}
          value={verdictText}
        />
      </div>

      {/* P_UP + MOVE grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 6,
        marginBottom: 10,
      }}>
        <div>
          <div style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em' }}>P_UP</div>
          <div style={{ fontSize: 18, fontWeight: 900, fontFamily: T.mono, color: probColor }}>
            {p != null ? p.toFixed(3) : '—'}
          </div>
          {pRaw != null && pRaw !== p && (
            <div style={{ fontSize: 7, color: T.textDim, fontFamily: T.mono }}>
              raw {pRaw.toFixed(3)}
            </div>
          )}
        </div>
        <div>
          <div style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em' }}>MOVE</div>
          <div style={{ fontSize: 16, fontWeight: 800, fontFamily: T.mono, color: moveColor }}>
            {move != null ? `${move >= 0 ? '+' : ''}${move.toFixed(1)}bp` : '—'}
          </div>
          {vol != null && (
            <div style={{ fontSize: 7, color: T.textDim, fontFamily: T.mono }}>
              vol {vol.toFixed(1)}bp
            </div>
          )}
        </div>
      </div>

      {/* Quantile fan */}
      {quantiles && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em', marginBottom: 2 }}>QUANTILES @ CLOSE</div>
          <QuantileFan quantiles={quantiles} />
        </div>
      )}

      {/* Regime + cascade + conviction chips */}
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 10 }}>
        {regime && (
          <Chip
            color={regimeColor}
            bg={`${regimeColor}1a`}
            border={`${regimeColor}55`}
            value={regime}
          />
        )}
        {conviction && (
          <Chip
            color={T.cyan}
            bg="rgba(6,182,212,0.1)"
            border="rgba(6,182,212,0.3)"
            label="CONV"
            value={conviction}
          />
        )}
        {cascade?.signal != null && (
          <Chip
            color={cascade.signal > 0 ? T.green : cascade.signal < 0 ? T.red : T.textMuted}
            bg="rgba(15,23,42,0.4)"
            border={T.cardBorder}
            label="CASC"
            value={cascade.signal.toFixed(2)}
            title={`exhaustion_t ${cascade.exhaustion_t?.toFixed?.(0) ?? '—'}s`}
          />
        )}
      </div>

      {/* Gate stack reason */}
      {reason && (
        <div
          title={reason}
          style={{
            fontSize: 9,
            fontFamily: T.mono,
            color: T.textMuted,
            padding: '5px 8px',
            borderRadius: 3,
            background: 'rgba(0,0,0,0.25)',
            border: `1px solid ${T.cardBorder}`,
            lineHeight: 1.4,
          }}
        >
          {reason}
        </div>
      )}
    </div>
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
  const [asset, setAsset] = useState('BTC');
  const lastFetch = useRef(0);

  const fetchSnapshot = async () => {
    try {
      const res = await api(
        'GET',
        `/v4/snapshot?asset=${asset}&timescales=5m,15m,1h,4h&strategy=fee_aware_15m`,
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
          {/* Top row: consensus + macro side by side */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(380px, 1fr))',
            gap: 14,
            marginBottom: 14,
          }}>
            <ConsensusStrip consensus={consensus} />
            <MacroCard macro={macro} />
          </div>

          {/* Events timeline (only if present) */}
          <EventsTimeline events={events} />

          {/* Per-timescale grid */}
          <div style={{ marginBottom: 14 }}>
            <SectionHeader
              title="Per-Timescale"
              subtitle="5m · 15m · 1h · 4h"
            />
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
              gap: 10,
            }}>
              {SHORT_TERM.map((ts) => (
                <TimescaleCard key={ts} ts={ts} data={timescales[ts]} />
              ))}
            </div>
          </div>

          {/* Raw JSON peek (diagnostics) */}
          <RawJsonPeek snapshot={snapshot} />
        </>
      )}
    </div>
  );
}
