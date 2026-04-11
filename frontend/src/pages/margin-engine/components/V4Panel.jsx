import React from 'react';
import { T, SHORT_TERM } from './constants.js';

/**
 * V4Panel — surfaces the /v4/snapshot fusion decision surface.
 *
 * Shows exactly what the margin engine sees: the Qwen-generated macro bias
 * with per-timescale divergence, the direction gate that blocks LONG/SHORT
 * entries, the consensus health across price sources, and per-timescale
 * recommended_actions with the actual gate-stack reason
 * (macro_gate_skip_up, regime_choppy_skip, quantile_fee_wall_skip, ...).
 *
 * This is the paper-mode monitoring surface. If a human looks at this and
 * the engine logs at the same time, the engine's behavior should be
 * traceable to a single field on this panel.
 */

const BIAS_COLOR = {
  BULL: T.green,
  BEAR: T.red,
  NEUTRAL: T.amber,
};

const GATE_COLOR = {
  ALLOW_ALL: T.green,
  SKIP_UP: T.red,    // blocks LONG entries
  SKIP_DOWN: T.red,  // blocks SHORT entries
};

const REGIME_COLOR = {
  TRENDING_UP: T.green,
  TRENDING_DOWN: T.red,
  MEAN_REVERTING: T.cyan,
  CHOPPY: T.amber,
  NO_EDGE: T.textDim,
};

const CONVICTION_COLOR = {
  EXTREME: T.green,
  HIGH: T.green,
  MEDIUM: T.cyan,
  LOW: T.amber,
  NONE: T.textDim,
};

function Chip({ color, bg, border, label, value, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 8, fontWeight: 800, padding: '2px 6px', borderRadius: 3,
        background: bg, color, border: `1px solid ${border}`,
        fontFamily: T.mono, letterSpacing: '0.04em', textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      {label && <span style={{ opacity: 0.7 }}>{label}</span>}
      <span>{value}</span>
    </span>
  );
}

function BiasChip({ bias, confidence, label, title }) {
  const color = BIAS_COLOR[bias] || T.textMuted;
  return (
    <Chip
      color={color}
      bg={`${color}26`}
      border={`${color}55`}
      label={label}
      value={`${bias || '—'}${confidence != null ? ` ${confidence}` : ''}`}
      title={title}
    />
  );
}

function GateChip({ gate, title }) {
  const color = GATE_COLOR[gate] || T.textMuted;
  return (
    <Chip
      color={color}
      bg={`${color}26`}
      border={`${color}55`}
      label="GATE"
      value={gate || '—'}
      title={title}
    />
  );
}

function ConsensusChip({ consensus }) {
  if (!consensus) return null;
  const safe = consensus.safe_to_trade;
  const div = consensus.max_divergence_bps;
  const sources = consensus.sources || {};
  const live = Object.values(sources).filter(s => s?.available).length;
  const total = Object.keys(sources).length;
  const color = safe ? T.green : T.red;
  return (
    <Chip
      color={color}
      bg={`${color}26`}
      border={`${color}55`}
      label="CONS"
      value={`${live}/${total} ${div != null ? `· ${div.toFixed(1)}bp` : ''}`}
      title={`${safe ? 'Safe to trade' : 'NOT safe'} — max divergence ${div?.toFixed(2) ?? '?'} bps · ${live} of ${total} sources live`}
    />
  );
}

function TimescaleCard({ ts, data }) {
  if (!data || data.status !== 'ok') {
    return (
      <div style={{
        background: 'rgba(15,23,42,0.5)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6, padding: 10,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontSize: 11, fontWeight: 800, color: T.white }}>{ts}</span>
          <span style={{ fontSize: 8, color: T.amber, fontFamily: T.mono }}>
            {data?.status?.toUpperCase() || 'NO_DATA'}
          </span>
        </div>
        <div style={{ fontSize: 9, color: T.textDim }}>
          {data?.status === 'no_model' && 'No model loaded for this timeframe'}
          {data?.status === 'cold_start' && 'Warming up price buffer'}
          {data?.status === 'stale' && 'Inputs older than max_age_s'}
          {!data && 'Awaiting first poll'}
        </div>
      </div>
    );
  }

  const p = data.probability_up;
  const move = data.expected_move_bps;
  const regime = data.regime;
  const action = data.recommended_action || {};
  const side = action.side;
  const conviction = action.conviction || 'NONE';
  const reason = action.reason || '—';
  const collateralPct = action.collateral_pct;

  // Direction inferred from p_up
  const directional = p > 0.55 ? 'LONG' : p < 0.45 ? 'SHORT' : 'FLAT';
  const probColor = p > 0.55 ? T.green : p < 0.45 ? T.red : T.amber;
  const moveColor = move > 0 ? T.green : move < 0 ? T.red : T.textMuted;
  const regimeColor = REGIME_COLOR[regime] || T.textMuted;
  const convictionColor = CONVICTION_COLOR[conviction] || T.textMuted;

  // The recommended_action.side is null when skipping; show the engine's verdict
  const verdictText = side ? `${side} ${(collateralPct * 100).toFixed(2)}%` : 'SKIP';
  const verdictColor = side === 'LONG' ? T.green : side === 'SHORT' ? T.red : T.textMuted;

  return (
    <div style={{
      background: 'rgba(15,23,42,0.5)',
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 6, padding: 10,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
        <span style={{ fontSize: 11, fontWeight: 800, color: T.white }}>{ts}</span>
        <span style={{
          fontSize: 7, fontWeight: 800, padding: '1px 5px', borderRadius: 3, letterSpacing: '0.05em',
          background: `${verdictColor}26`,
          color: verdictColor,
          border: `1px solid ${verdictColor}55`,
        }}>{verdictText}</span>
      </div>

      {/* Probability + expected move */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
        <div>
          <div style={{ fontSize: 7, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em' }}>P_UP</div>
          <div style={{ fontSize: 15, fontWeight: 900, fontFamily: T.mono, color: probColor }}>
            {p != null ? p.toFixed(3) : '—'}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ fontSize: 7, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em' }}>MOVE</div>
          <div style={{ fontSize: 13, fontWeight: 800, fontFamily: T.mono, color: moveColor }}>
            {move != null ? `${move >= 0 ? '+' : ''}${move.toFixed(1)}bp` : '—'}
          </div>
        </div>
      </div>

      {/* Regime + conviction */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 6, flexWrap: 'wrap' }}>
        <Chip
          color={regimeColor}
          bg={`${regimeColor}26`}
          border={`${regimeColor}55`}
          value={regime || '—'}
          title="v3 regime classification"
        />
        <Chip
          color={convictionColor}
          bg={`${convictionColor}26`}
          border={`${convictionColor}55`}
          value={conviction}
          title={`Conviction tier — score ${(action.conviction_score ?? 0).toFixed(2)}`}
        />
      </div>

      {/* Reason — the gate stack's actual verdict */}
      <div title={reason} style={{
        fontSize: 8, fontFamily: T.mono, color: T.textMuted,
        padding: '4px 6px', borderRadius: 3,
        background: 'rgba(0,0,0,0.25)',
        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
      }}>
        {reason}
      </div>
    </div>
  );
}

export default function V4Panel({ snapshot }) {
  const macro = snapshot?.macro;
  const consensus = snapshot?.consensus;
  const timescales = snapshot?.timescales || {};
  const ts = snapshot?.ts;
  const lastPrice = snapshot?.last_price;
  const strategy = snapshot?.strategy || 'fee_aware_15m';
  const events = snapshot?.events_upcoming || [];
  const macroAgeS = macro?.age_s;
  const macroStale = macroAgeS != null && macroAgeS > 180;

  const hasData = Object.values(timescales).some(v => v != null && (v.status === 'ok' || v.status === 'stale'));

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      overflow: 'hidden',
    }}>
      {/* Header — title + macro/gate/consensus chips */}
      <div style={{
        padding: '10px 14px',
        borderBottom: `1px solid ${T.cardBorder}`,
        background: T.headerBg,
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        gap: 12, flexWrap: 'wrap',
      }}>
        <div>
          <span style={{ fontSize: 11, fontWeight: 700, color: T.text }}>V4 FUSION DECISION SURFACE</span>
          <span style={{ fontSize: 9, color: T.textMuted, marginLeft: 8 }}>
            strategy: <span style={{ color: T.cyan, fontFamily: T.mono }}>{strategy}</span>
            {lastPrice != null && (
              <> · last <span style={{ color: T.text, fontFamily: T.mono }}>${lastPrice.toFixed(2)}</span></>
            )}
            {ts != null && (
              <> · age <span style={{ color: macroStale ? T.amber : T.textDim, fontFamily: T.mono }}>
                {macroAgeS != null ? `${Math.round(macroAgeS)}s` : '—'}
              </span></>
            )}
          </span>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {macro && (
            <BiasChip
              bias={macro.bias}
              confidence={macro.confidence}
              label="MACRO"
              title={macro.reasoning}
            />
          )}
          {macro && <GateChip gate={macro.direction_gate} title={`Threshold mod ${macro.threshold_modifier} · size mod ${macro.size_modifier}`} />}
          <ConsensusChip consensus={consensus} />
        </div>
      </div>

      {/* Macro reasoning text — single-line truncation, full on hover */}
      {macro?.reasoning && (
        <div
          title={macro.reasoning}
          style={{
            padding: '6px 14px',
            borderBottom: `1px solid ${T.cardBorder}`,
            background: 'rgba(168,85,247,0.05)',
            fontSize: 9, color: T.text, fontStyle: 'italic',
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}
        >
          <span style={{ color: T.purple, fontWeight: 700, marginRight: 6 }}>QWEN ✦</span>
          {macro.reasoning}
        </div>
      )}

      {/* Per-timescale macro chips */}
      {macro?.timescale_map && (
        <div style={{
          padding: '8px 14px',
          borderBottom: `1px solid ${T.cardBorder}`,
          display: 'flex', gap: 6, flexWrap: 'wrap',
        }}>
          <span style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em', alignSelf: 'center' }}>PER-HORIZON:</span>
          {SHORT_TERM.map(ts => {
            const tm = macro.timescale_map[ts];
            if (!tm) return null;
            return (
              <BiasChip
                key={ts}
                bias={tm.bias}
                confidence={tm.confidence}
                label={ts}
                title={tm.reasoning}
              />
            );
          })}
        </div>
      )}

      {/* Upcoming high-impact events */}
      {events.length > 0 && (
        <div style={{
          padding: '6px 14px',
          borderBottom: `1px solid ${T.cardBorder}`,
          background: 'rgba(245,158,11,0.05)',
          fontSize: 9, color: T.amber, fontFamily: T.mono,
        }}>
          ⚠ {events.slice(0, 3).map(e =>
            `${e.event_name || 'event'} (${e.impact || '?'}) in ${e.in_minutes ?? '?'}min`
          ).join(' · ')}
        </div>
      )}

      {/* Per-timescale grid */}
      <div style={{ padding: 12 }}>
        {!hasData ? (
          <div style={{ textAlign: 'center', padding: '16px 0' }}>
            <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4 }}>Waiting for v4 fusion snapshot...</div>
            <div style={{ fontSize: 8, color: T.textDim }}>Calls /api/v4/snapshot every 4s</div>
          </div>
        ) : (
          <div style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: 8,
          }}>
            {SHORT_TERM.map(t => (
              <TimescaleCard key={t} ts={t} data={timescales[t]} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
