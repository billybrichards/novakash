/**
 * Signal Comparison Dashboard
 *
 * Track and compare accuracy of all directional prediction signals across
 * timescales and resolution venues (Polymarket vs Hyperliquid).
 *
 * Data Source: GET /api/v58/signal-comparison?period=30d&timescale=15m
 */

import { useEffect, useState } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme Constants ──────────────────────────────────────────────────────
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
  teal: '#14b8a6',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

// Signal display names
const SIGNAL_LABELS = {
  sequoia_v5_2: 'Sequoia v5.2',
  hmm_regime: 'HMM Regime',
  macrov2: 'MacroV2',
  v3_composite: 'V3 Composite',
  v4_consensus: 'V4 Consensus',
  cascade_fsm: 'Cascade FSM',
};

// Regime labels
const REGIME_LABELS = {
  calm_trend: 'calm_trend',
  volatile_trend: 'volatile_trend',
  chop: 'chop',
  risk_off: 'risk_off',
};

// ─── Primitives ───────────────────────────────────────────────────────────

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

// ─── Selector Components ──────────────────────────────────────────────────

function TimescaleSelector({ value, onChange }) {
  const options = ['5m', '15m', '1h', '4h'];
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {options.map((opt) => (
        <button
          key={opt}
          onClick={() => onChange(opt)}
          style={{
            padding: '6px 12px',
            borderRadius: 4,
            fontSize: 10,
            fontWeight: 700,
            fontFamily: T.mono,
            background: value === opt ? 'rgba(6,182,212,0.15)' : 'transparent',
            color: value === opt ? T.cyan : T.textMuted,
            border: `1px solid ${value === opt ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
            cursor: 'pointer',
            letterSpacing: '0.05em',
            transition: 'all 150ms ease-out',
          }}
        >
          {opt}
        </button>
      ))}
    </div>
  );
}

function PeriodSelector({ value, onChange }) {
  const options = [
    { value: '24h', label: '24h' },
    { value: '7d', label: '7d' },
    { value: '30d', label: '30d' },
  ];
  return (
    <div style={{ display: 'flex', gap: 4 }}>
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          style={{
            padding: '6px 12px',
            borderRadius: 4,
            fontSize: 10,
            fontWeight: 700,
            fontFamily: T.mono,
            background: value === opt.value ? 'rgba(168,85,247,0.15)' : 'transparent',
            color: value === opt.value ? T.purple : T.textMuted,
            border: `1px solid ${value === opt.value ? 'rgba(168,85,247,0.3)' : T.cardBorder}`,
            cursor: 'pointer',
            letterSpacing: '0.05em',
            transition: 'all 150ms ease-out',
          }}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

// ─── Accuracy Overview Table ──────────────────────────────────────────────

function AccuracyOverview({ data }) {
  if (!data?.accuracy_overview) return null;

  const signals = Object.entries(data.accuracy_overview).map(([key, val]) => ({
    key,
    ...val,
  }));

  // Calculate deltas (HLP WR - POLY WR)
  const withDelta = signals.map(s => ({
    ...s,
    delta: s.hlp_win_rate != null && s.win_rate != null
      ? s.hlp_win_rate - s.win_rate
      : null,
  }));

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Accuracy Overview"
        subtitle={`Last ${data.period_days} days · ${data.timescale} timescale`}
        badge="POLY vs HLP"
        badgeColor={T.cyan}
      />

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
              <th style={{
                textAlign: 'left', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Signal</th>
              <th style={{
                textAlign: 'right', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>POLY WR</th>
              <th style={{
                textAlign: 'right', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>HLP WR</th>
              <th style={{
                textAlign: 'right', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Δ</th>
              <th style={{
                textAlign: 'right', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>PnL</th>
              <th style={{
                textAlign: 'right', padding: '8px 6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Samples</th>
            </tr>
          </thead>
          <tbody>
            {withDelta.map((signal, idx) => (
              <tr key={signal.key} style={{
                borderBottom: idx < withDelta.length - 1 ? `1px solid ${T.cardBorder}` : 'none',
              }}>
                <td style={{ padding: '10px 6px', fontSize: 10, color: T.white, fontWeight: 600 }}>
                  {SIGNAL_LABELS[signal.key] || signal.key}
                </td>
                <td style={{
                  padding: '10px 6px', textAlign: 'right', fontSize: 10,
                  fontFamily: T.mono, color: signal.win_rate != null
                    ? `${signal.win_rate >= 0.55 ? T.green : T.text}`
                    : T.textDim,
                }}>
                  {signal.win_rate != null ? `${(signal.win_rate * 100).toFixed(0)}%` : '—'}
                </td>
                <td style={{
                  padding: '10px 6px', textAlign: 'right', fontSize: 10,
                  fontFamily: T.mono, color: signal.hlp_win_rate != null
                    ? `${signal.hlp_win_rate >= 0.55 ? T.green : T.text}`
                    : T.textDim,
                }}>
                  {signal.hlp_win_rate != null ? `${(signal.hlp_win_rate * 100).toFixed(0)}%` : '—'}
                </td>
                <td style={{
                  padding: '10px 6px', textAlign: 'right', fontSize: 10,
                  fontFamily: T.mono,
                  color: signal.delta != null
                    ? signal.delta >= 0 ? T.green : T.red
                    : T.textDim,
                }}>
                  {signal.delta != null ? (signal.delta >= 0 ? '+' : '') + (signal.delta * 100).toFixed(1) + '%' : '—'}
                </td>
                <td style={{
                  padding: '10px 6px', textAlign: 'right', fontSize: 10,
                  fontFamily: T.mono,
                  color: signal.pnl != null
                    ? signal.pnl >= 0 ? T.green : T.red
                    : T.textDim,
                }}>
                  {signal.pnl != null ? (signal.pnl >= 0 ? '+' : '') + '$' + signal.pnl.toFixed(1) : '—'}
                </td>
                <td style={{
                  padding: '10px 6px', textAlign: 'right', fontSize: 10,
                  fontFamily: T.mono, color: T.text,
                }}>
                  {signal.samples != null ? signal.samples : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{
        marginTop: 10,
        fontSize: 8,
        color: T.textDim,
        fontFamily: T.mono,
        display: 'flex',
        gap: 12,
      }}>
        <span>POLY = Polymarket resolution</span>
        <span>HLP = Hyperliquid price movement</span>
        <span>Δ = HLP - POLY</span>
      </div>
    </div>
  );
}

// ─── Regime-Specific Accuracy Heatmap ─────────────────────────────────────

function RegimeAccuracy({ data }) {
  if (!data?.regime_specific_accuracy) return null;

  const regimes = Object.keys(REGIME_LABELS);
  const signals = Object.keys(SIGNAL_LABELS);

  // Find max WR for best-in-regime highlighting
  const getMaxWR = (regime) => {
    let max = 0;
    signals.forEach(sig => {
      const wr = data.regime_specific_accuracy[regime]?.[sig];
      if (wr != null && wr > max) max = wr;
    });
    return max;
  };

  const heatmapCell = (value, isBest = false) => {
    if (value == null) return { bg: T.textDim, text: T.textDim };
    const abs = Math.min(1, Math.abs(value));
    const alpha = 0.15 + abs * 0.7;
    let color;
    if (value >= 0.55) color = T.green;
    else if (value >= 0.45) color = T.cyan;
    else color = T.red;
    return {
      bg: `${color}${Math.floor(alpha * 255).toString(16).padStart(2, '0')}`,
      text: isBest ? T.white : color,
      border: isBest ? `2px solid ${T.white}` : `1px solid ${color}55`,
    };
  };

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Regime-Specific Accuracy"
        subtitle="Win rate by regime (higher = better)"
      />

      <div style={{ overflowX: 'auto' }}>
        <div style={{
          display: 'grid',
          gridTemplateColumns: `140px repeat(${regimes.length}, 1fr)`,
          gap: 4,
        }}>
          {/* Header row */}
          <div style={{ padding: '8px 6px', fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
            Signal
          </div>
          {regimes.map((regime) => (
            <div key={regime} style={{
              padding: '8px 6px', textAlign: 'center', fontSize: 9,
              color: T.textMuted, fontFamily: T.mono, textTransform: 'uppercase',
            }}>
              {REGIME_LABELS[regime]}
            </div>
          ))}

          {/* Data rows */}
          {signals.map((signal) => (
            <React.Fragment key={signal}>
              <div style={{
                padding: '8px 6px', fontSize: 9, color: T.white,
                fontWeight: 600, fontFamily: T.mono,
              }}>
                {SIGNAL_LABELS[signal]}
              </div>
              {regimes.map((regime) => {
                const wr = data.regime_specific_accuracy[regime]?.[signal];
                const maxWR = getMaxWR(regime);
                const isBest = wr != null && wr >= maxWR - 0.01 && wr >= 0.5;
                const style = heatmapCell(wr, isBest);
                return (
                  <div key={`${signal}-${regime}`} style={{
                    padding: '8px 6px', textAlign: 'center', fontSize: 10,
                    fontFamily: T.mono, fontWeight: 700,
                    background: style.bg,
                    color: style.text,
                    border: style.border,
                    borderRadius: 3,
                    transition: 'all 150ms ease-out',
                  }}>
                    {wr != null ? (wr * 100).toFixed(0) + '%' : '—'}
                  </div>
                );
              })}
            </React.Fragment>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Correlation Matrix Heatmap ───────────────────────────────────────────

function CorrelationMatrix({ data }) {
  if (!data?.correlation_matrix) return null;

  const signals = Object.keys(SIGNAL_LABELS);

  const corrCell = (value) => {
    if (value == null) return { bg: T.textDim, text: T.textDim, intensity: 0 };
    const abs = Math.abs(value);
    let color;
    let intensity;
    if (abs >= 0.8) { color = T.green; intensity = 0.9; }
    else if (abs >= 0.6) { color = T.cyan; intensity = 0.7; }
    else if (abs >= 0.4) { color = T.amber; intensity = 0.5; }
    else if (abs >= 0.2) { color = T.textMuted; intensity = 0.3; }
    else { color = T.textDim; intensity = 0.1; }
    return {
      bg: `${color}${Math.floor(intensity * 255).toString(16).padStart(2, '0')}`,
      text: Math.abs(value) >= 0.6 ? T.white : color,
      value,
    };
  };

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Signal Correlation Matrix"
        subtitle="Pearson correlation on directional predictions"
      />

      <div style={{ overflowX: 'auto' }}>
        <div style={{
          display: 'grid',
          gridTemplateColumns: `80px repeat(${signals.length}, 1fr)`,
          gap: 4,
        }}>
          {/* Header row */}
          <div style={{ padding: '8px 6px', fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
            Signal
          </div>
          {signals.map((sig) => (
            <div key={sig} style={{
              padding: '8px 6px', textAlign: 'center', fontSize: 8,
              color: T.textMuted, fontFamily: T.mono,
            }}>
              {sig === 'sequoia_v5_2' ? 'Seq' :
               sig === 'hmm_regime' ? 'HMM' :
               sig === 'macrov2' ? 'Macro' :
               sig === 'v3_composite' ? 'V3' :
               sig === 'v4_consensus' ? 'V4' : 'Casc'}
            </div>
          ))}

          {/* Data rows */}
          {signals.map((rowSig) => (
            <React.Fragment key={rowSig}>
              <div style={{
                padding: '8px 6px', fontSize: 8, color: T.textMuted,
                fontFamily: T.mono,
              }}>
                {rowSig === 'sequoia_v5_2' ? 'Seq' :
                 rowSig === 'hmm_regime' ? 'HMM' :
                 rowSig === 'macrov2' ? 'Macro' :
                 rowSig === 'v3_composite' ? 'V3' :
                 rowSig === 'v4_consensus' ? 'V4' : 'Casc'}
              </div>
              {signals.map((colSig) => {
                const corr = data.correlation_matrix[rowSig]?.[colSig];
                const style = corrCell(corr);
                return (
                  <div key={`${rowSig}-${colSig}`} title={`${style.value?.toFixed(2) || 'null'}`} style={{
                    padding: '8px 6px', textAlign: 'center', fontSize: 9,
                    fontFamily: T.mono, fontWeight: 600,
                    background: style.bg,
                    color: style.text,
                    border: `1px solid ${T.cardBorder}`,
                    borderRadius: 3,
                    transition: 'all 150ms ease-out',
                  }}>
                    {style.value != null ? style.value.toFixed(2) : '—'}
                  </div>
                );
              })}
            </React.Fragment>
          ))}
        </div>
      </div>

      <div style={{
        marginTop: 10,
        fontSize: 8,
        color: T.textDim,
        fontFamily: T.mono,
        display: 'flex',
        gap: 12,
      }}>
        <span style={{ color: T.green }}>████ 0.8+</span>
        <span style={{ color: T.cyan }}>███ 0.6-0.8</span>
        <span style={{ color: T.amber }}>██ 0.4-0.6</span>
        <span style={{ color: T.textMuted }}>█ 0.2-0.4</span>
      </div>
    </div>
  );
}

// ─── Signal Timeline ──────────────────────────────────────────────────────

function SignalTimeline({ data }) {
  if (!data?.signal_timeline || data.signal_timeline.length === 0) return null;

  const timeline = data.signal_timeline.slice(0, 50); // Limit to 50 rows

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Signal Timeline"
        subtitle="Last 24 hours · All signals side-by-side"
      />

      <div style={{ overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
              <th style={{
                textAlign: 'left', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Time</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Seq</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>HMM</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Macro</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>V3</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>V4</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Casc</th>
              <th style={{
                textAlign: 'center', padding: '6px', fontSize: 9,
                color: T.textMuted, fontFamily: T.mono, letterSpacing: '0.05em',
                textTransform: 'uppercase',
              }}>Actual</th>
            </tr>
          </thead>
          <tbody>
            {timeline.map((row, idx) => (
              <tr key={idx} style={{
                borderBottom: idx < timeline.length - 1 ? `1px solid ${T.cardBorder}` : 'none',
              }}>
                <td style={{
                  padding: '6px', fontSize: 8, color: T.textMuted,
                  fontFamily: T.mono,
                }}>
                  {new Date(row.timestamp).toLocaleTimeString('en-US', {
                    hour: '2-digit', minute: '2-digit', hour12: false,
                  })}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 9,
                  fontFamily: T.mono, fontWeight: 700,
                  color: row.sequoia_direction === 'UP' ? T.green
                    : row.sequoia_direction === 'DN' ? T.red : T.textDim,
                }}>
                  {row.sequoia_direction || '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 8,
                  fontFamily: T.mono, color: T.text,
                }}>
                  {row.hmm_regime?.slice(0, 4) || '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 9,
                  fontFamily: T.mono, fontWeight: 600,
                  color: row.macrov2_bias === 'LONG' ? T.green
                    : row.macrov2_bias === 'SHORT' ? T.red
                    : row.macrov2_bias === 'NEUTRAL' ? T.textMuted : T.textDim,
                }}>
                  {row.macrov2_bias?.slice(0, 4) || '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 9,
                  fontFamily: T.mono,
                  color: row.v3_composite != null
                    ? row.v3_composite >= 0 ? T.green : T.red
                    : T.textDim,
                }}>
                  {row.v3_composite != null
                    ? (row.v3_composite >= 0 ? '+' : '') + row.v3_composite.toFixed(2)
                    : '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 9,
                  fontFamily: T.mono,
                  color: row.v4_alignment != null
                    ? row.v4_alignment >= 0.5 ? T.green : T.textMuted
                    : T.textDim,
                }}>
                  {row.v4_alignment != null ? row.v4_alignment.toFixed(2) : '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 8,
                  fontFamily: T.mono, color: T.text,
                }}>
                  {row.cascade_state?.slice(0, 4) || '—'}
                </td>
                <td style={{
                  padding: '6px', textAlign: 'center', fontSize: 9,
                  fontFamily: T.mono, fontWeight: 700,
                  color: row.actual_outcome === 'UP' ? T.green
                    : row.actual_outcome === 'DN' ? T.red : T.textDim,
                }}>
                  {row.actual_outcome}
                  {row.all_correct && (
                    <span style={{ color: T.green, marginLeft: 2 }}>✓</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Loading and Error States ─────────────────────────────────────────────

function LoadingSpinner() {
  return (
    <div style={{
      padding: 60, textAlign: 'center',
      background: T.card, border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
    }}>
      <div style={{
        width: 32, height: 32, border: `3px solid ${T.textDim}`,
        borderTopColor: T.cyan, borderRadius: '50%',
        animation: 'spin 1s linear infinite',
        margin: '0 auto 12px',
      }} />
      <div style={{ color: T.textMuted, fontSize: 11, fontFamily: T.mono }}>
        Fetching signal comparison data...
      </div>
      <style>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}

function ErrorMessage({ message }) {
  return (
    <div style={{
      padding: '16px 20px',
      background: 'rgba(239,68,68,0.1)',
      border: '1px solid rgba(239,68,68,0.2)',
      borderRadius: 8,
      color: T.red,
      fontSize: 11,
      fontFamily: T.mono,
    }}>
      <strong>Error:</strong> {message}
    </div>
  );
}

// ─── Main Page Component ──────────────────────────────────────────────────

export default function SignalComparison() {
  const api = useApi();
  const [timescale, setTimescale] = useState('15m');
  const [period, setPeriod] = useState('30d');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await api('GET', `/v58/signal-comparison?period=${period}&timescale=${timescale}`);
      setData(res?.data || res);
    } catch (err) {
      setError(err.message || 'Failed to fetch signal comparison data');
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();
  }, [api, timescale, period]);

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <div style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          flexWrap: 'wrap',
          gap: 12,
        }}>
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
              Signal Comparison
              <Chip
                color={T.cyan}
                bg="rgba(6,182,212,0.15)"
                border="rgba(6,182,212,0.3)"
                value="v58"
              />
              <Chip
                color={T.purple}
                bg="rgba(168,85,247,0.15)"
                border="rgba(168,85,247,0.3)"
                value="POLY vs HLP"
              />
            </h1>
            <p style={{
              fontSize: 10,
              color: T.textMuted,
              margin: '4px 0 0',
              maxWidth: 880,
              lineHeight: 1.5,
            }}>
              Track and compare accuracy of all directional prediction signals
              across timescales and resolution venues. Compare Polymarket oracle
              resolution vs Hyperliquid actual price movement.
            </p>
          </div>

          {/* Selectors */}
          <div style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
            <div>
              <div style={{
                fontSize: 8, color: T.textDim, marginBottom: 4,
                fontFamily: T.mono, textTransform: 'uppercase',
              }}>
                Timescale
              </div>
              <TimescaleSelector value={timescale} onChange={setTimescale} />
            </div>
            <div>
              <div style={{
                fontSize: 8, color: T.textDim, marginBottom: 4,
                fontFamily: T.mono, textTransform: 'uppercase',
              }}>
                Period
              </div>
              <PeriodSelector value={period} onChange={setPeriod} />
            </div>
          </div>
        </div>

        {/* Sub-header stats */}
        {data && (
          <div style={{
            display: 'flex', gap: 16, marginTop: 8,
            fontSize: 10, color: T.textMuted, fontFamily: T.mono, flexWrap: 'wrap',
          }}>
            <span>
              <span style={{ color: T.textDim }}>period</span>{' '}
              <span style={{ color: T.text }}>{data.period_days} days</span>
            </span>
            <span>
              <span style={{ color: T.textDim }}>timescale</span>{' '}
              <span style={{ color: T.text }}>{data.timescale || timescale}</span>
            </span>
            <span>
              <span style={{ color: T.textDim }}>signals</span>{' '}
              <span style={{ color: T.text }}>
                {data.accuracy_overview ? Object.keys(data.accuracy_overview).length : 0}
              </span>
            </span>
          </div>
        )}
      </div>

      {/* Error */}
      {error && <ErrorMessage message={error} />}

      {/* Loading */}
      {loading && !data && <LoadingSpinner />}

      {/* Content */}
      {!loading && data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {/* Section 1: Accuracy Overview */}
          <AccuracyOverview data={data} />

          {/* Section 2: Regime-Specific Accuracy */}
          <RegimeAccuracy data={data} />

          {/* Section 3: Correlation Matrix */}
          <CorrelationMatrix data={data} />

          {/* Section 4: Signal Timeline */}
          <SignalTimeline data={data} />

          {/* Info footer */}
          <div style={{
            padding: '10px 14px',
            background: 'rgba(6,182,212,0.06)',
            border: '1px solid rgba(6,182,212,0.2)',
            borderRadius: 6,
            fontSize: 10,
            color: T.text,
            lineHeight: 1.5,
          }}>
            <span style={{ color: T.cyan, fontWeight: 800, marginRight: 6 }}>ℹ</span>
            <strong>Dual Resolution Tracking:</strong> POLY (Polymarket oracle) and HLP
            (Hyperliquid price) may differ due to oracle lag, basis spread, and
            different window boundaries. Signals with high HLP WR are preferred
            for margin engine trading.
          </div>
        </div>
      )}
    </div>
  );
}
