import React, { useState, useMemo } from 'react';
import { Activity, AlertTriangle } from 'lucide-react';
import Panel from './Panel.jsx';
import { T } from './constants.js';

/**
 * GateHeartbeat — UI-01.
 *
 * Live view of the V10.6 8-gate pipeline state for the most recent
 * signal_evaluations rows. Operator uses this to see which gate is
 * blocking the engine right now and what the blocking reason looks
 * like in aggregate.
 *
 * Data source: /api/v58/execution-hq → `gate_heartbeat` array (last 50
 * signal_evaluations rows newest-first). Each entry has:
 *   - evaluated_at, window_ts, eval_offset, decision, v2_probability_up
 *   - gate_failed: canonical name of the gate that blocked (or null)
 *   - gate_results: per-gate {true|false|null} for the 8-gate pipeline
 *
 * Pipeline order (matches engine/strategies/five_min_vpin.py ~line 695):
 *   G0 eval_offset_bounds   (DS-01, V10.6 EvalOffsetBoundsGate)
 *   G1 source_agreement
 *   G2 delta_magnitude
 *   G3 taker_flow
 *   G4 cg_confirmation
 *   G5 dune_confidence
 *   G6 spread_gate
 *   G7 dynamic_cap
 *
 * Render layout:
 *   - Current window strip: 8 chips + large TRADE/SKIP pill for gate_heartbeat[0]
 *   - Recent decisions rail: horizontal scrollable, 8-pixel mini-strips for last 20 rows
 *   - Aggregate stats: trade/skip counts + per-gate_failed breakdown across last 50
 */

// Canonical 8-gate pipeline order (G0..G7).
const PIPELINE = [
  { key: 'eval_offset_bounds', g: 'G0', short: 'EvalOffset', full: 'Eval Offset Bounds (V10.6 DS-01)' },
  { key: 'source_agreement',   g: 'G1', short: 'SrcAgree',   full: 'Source Agreement (CL+TI)' },
  { key: 'delta_magnitude',    g: 'G2', short: 'Delta',      full: 'Delta Magnitude' },
  { key: 'taker_flow',         g: 'G3', short: 'Taker',      full: 'Taker Flow (CG)' },
  { key: 'cg_confirmation',    g: 'G4', short: 'CGConfirm',  full: 'CG Confirmation' },
  { key: 'dune_confidence',    g: 'G5', short: 'DUNE',       full: 'Dune Confidence' },
  { key: 'spread_gate',        g: 'G6', short: 'Spread',     full: 'Spread Gate' },
  { key: 'dynamic_cap',        g: 'G7', short: 'DynCap',     full: 'Dynamic Cap' },
];

// Pretty display names for aggregate "gate_failed" bucket labels.
const GATE_DISPLAY = Object.fromEntries(PIPELINE.map(p => [p.key, p.short]));

function statusColor(status) {
  if (status === true)  return { color: T.green,   bg: 'rgba(16,185,129,0.15)',  border: 'rgba(16,185,129,0.4)' };
  if (status === false) return { color: T.red,     bg: 'rgba(239,68,68,0.18)',   border: 'rgba(239,68,68,0.5)'  };
  return                 { color: T.textDim, bg: 'rgba(30,41,59,0.5)',   border: T.cardBorder          };
}

function decisionColor(decision) {
  if (decision === 'TRADE') return { color: T.green,   bg: 'rgba(16,185,129,0.18)', border: 'rgba(16,185,129,0.5)' };
  if (decision === 'SKIP')  return { color: T.amber,   bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.4)' };
  return                     { color: T.textDim, bg: 'rgba(30,41,59,0.5)',     border: T.cardBorder           };
}

function fmtWindowTs(ts) {
  if (!ts) return '--';
  try {
    const d = new Date(ts * 1000);
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    return `${hh}:${mm}Z`;
  } catch {
    return String(ts);
  }
}

function fmtEvaluatedAt(iso) {
  if (!iso) return '--';
  try {
    const d = new Date(iso);
    return d.toISOString().substring(11, 19);
  } catch {
    return '--';
  }
}

function fmtProb(p) {
  if (p == null) return '--';
  return (Number(p) * 100).toFixed(1) + '%';
}

function GateChip({ gate, status }) {
  const c = statusColor(status);
  const mark = status === true ? '\u2713' : status === false ? '\u2717' : '--';
  return (
    <div
      title={`${gate.full} — ${status === true ? 'PASS' : status === false ? 'FAIL' : 'not evaluated'}`}
      style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center',
        justifyContent: 'center', gap: 2, padding: '6px 8px',
        background: c.bg, border: `1px solid ${c.border}`, borderRadius: 3,
        minWidth: 72, flex: '0 1 auto',
      }}
    >
      <span style={{
        fontSize: 8, color: T.textMuted, fontFamily: 'monospace',
        letterSpacing: '0.05em', textTransform: 'uppercase',
      }}>{gate.g}</span>
      <span style={{
        fontSize: 11, fontWeight: 700, color: c.color,
        fontFamily: "'JetBrains Mono', monospace", letterSpacing: '0.02em',
      }}>{gate.short}</span>
      <span style={{ fontSize: 12, color: c.color, fontWeight: 700 }}>{mark}</span>
    </div>
  );
}

function DecisionPill({ decision }) {
  const c = decisionColor(decision);
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', alignItems: 'center',
      justifyContent: 'center', padding: '8px 18px',
      background: c.bg, border: `2px solid ${c.border}`, borderRadius: 4,
      minWidth: 110, marginLeft: 'auto',
    }}>
      <span style={{
        fontSize: 8, color: T.textMuted, fontFamily: 'monospace',
        letterSpacing: '0.1em', textTransform: 'uppercase',
      }}>Decision</span>
      <span style={{
        fontSize: 18, fontWeight: 800, color: c.color,
        fontFamily: "'JetBrains Mono', monospace", letterSpacing: '0.05em',
      }}>{decision || '--'}</span>
    </div>
  );
}

function MiniStrip({ entry, selected, onClick }) {
  const cells = PIPELINE.map(p => {
    const s = entry.gate_results?.[p.key];
    const color = s === true ? T.green : s === false ? T.red : T.textDim;
    return { color, key: p.key };
  });
  const dcol = decisionColor(entry.decision);
  const title = [
    `window=${fmtWindowTs(entry.window_ts)}  offset=T-${entry.eval_offset ?? '--'}`,
    `evaluated_at=${fmtEvaluatedAt(entry.evaluated_at)}`,
    `decision=${entry.decision}`,
    entry.gate_failed ? `blocked_by=${entry.gate_failed}` : 'all_passed',
    `v2_p_up=${fmtProb(entry.v2_probability_up)}`,
  ].join('\n');
  return (
    <button
      onClick={onClick}
      title={title}
      style={{
        display: 'flex', flexDirection: 'column', alignItems: 'stretch',
        gap: 2, padding: 4, cursor: 'pointer',
        background: selected ? 'rgba(6,182,212,0.12)' : 'rgba(15,23,42,0.6)',
        border: `1px solid ${selected ? T.cyan : T.cardBorder}`,
        borderRadius: 3, flexShrink: 0, minWidth: 90,
      }}
    >
      <div style={{ display: 'flex', gap: 2, height: 8 }}>
        {cells.map(c => (
          <div key={c.key} style={{ flex: 1, background: c.color, opacity: c.color === T.textDim ? 0.35 : 0.9, borderRadius: 1 }} />
        ))}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 8, fontFamily: 'monospace' }}>
        <span style={{ color: T.textDim }}>{fmtWindowTs(entry.window_ts)}</span>
        <span style={{ color: T.textDim }}>T-{entry.eval_offset ?? '--'}</span>
      </div>
      <div style={{
        fontSize: 8, fontWeight: 700, color: dcol.color, textAlign: 'center',
        fontFamily: 'monospace', letterSpacing: '0.05em',
      }}>{entry.decision || '--'}</div>
    </button>
  );
}

function AggregateStats({ entries }) {
  const stats = useMemo(() => {
    const s = { total: entries.length, trades: 0, skips: 0, byGate: {} };
    for (const e of entries) {
      if (e.decision === 'TRADE') s.trades += 1;
      else if (e.decision === 'SKIP') s.skips += 1;
      if (e.gate_failed) {
        s.byGate[e.gate_failed] = (s.byGate[e.gate_failed] || 0) + 1;
      }
    }
    return s;
  }, [entries]);

  const sortedGates = Object.entries(stats.byGate).sort((a, b) => b[1] - a[1]);
  const total = Math.max(stats.total, 1);

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
      fontSize: 10, fontFamily: 'monospace',
    }}>
      <span style={{ color: T.textMuted }}>
        Last <span style={{ color: T.text }}>{stats.total}</span> evals —
        <span style={{ color: T.green, marginLeft: 6 }}>{stats.trades} TRADE</span>
        <span style={{ color: T.textDim, margin: '0 4px' }}>/</span>
        <span style={{ color: T.amber }}>{stats.skips} SKIP</span>
      </span>
      {sortedGates.length > 0 && (
        <span style={{ color: T.textMuted, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.08em' }}>Blocked by:</span>
          {sortedGates.map(([name, count]) => {
            const pct = Math.round((count / total) * 100);
            const hot = pct >= 50;
            return (
              <span
                key={name}
                title={`${count} of ${stats.total} evaluations blocked by ${name}`}
                style={{
                  padding: '2px 6px',
                  background: hot ? 'rgba(239,68,68,0.15)' : 'rgba(245,158,11,0.1)',
                  border: `1px solid ${hot ? 'rgba(239,68,68,0.4)' : 'rgba(245,158,11,0.3)'}`,
                  borderRadius: 3, color: hot ? T.red : T.amber,
                  display: 'inline-flex', alignItems: 'center', gap: 4,
                }}
              >
                {hot && <AlertTriangle size={9} />}
                <span style={{ fontWeight: 700 }}>{GATE_DISPLAY[name] || name}</span>
                <span style={{ color: T.textMuted }}>×{count}</span>
                <span style={{ color: T.textDim }}>({pct}%)</span>
              </span>
            );
          })}
        </span>
      )}
    </div>
  );
}

export default function GateHeartbeat({ gateHeartbeat }) {
  const entries = Array.isArray(gateHeartbeat) ? gateHeartbeat : [];
  const [pinnedIdx, setPinnedIdx] = useState(null);

  // What shows in the main strip: pinned row, or newest entry
  const mainIdx = pinnedIdx != null && pinnedIdx < entries.length ? pinnedIdx : 0;
  const main = entries[mainIdx];
  const railEntries = entries.slice(0, 20);

  if (entries.length === 0) {
    return (
      <Panel title="V10.6 Gate Heartbeat" icon={Activity} style={{ flexShrink: 0, marginBottom: 8 }}>
        <div style={{
          padding: 16, textAlign: 'center', fontSize: 11, fontFamily: 'monospace',
          color: T.textMuted,
        }}>
          No signal_evaluations rows yet. Engine must run at least one window evaluation.
        </div>
      </Panel>
    );
  }

  return (
    <Panel
      title="V10.6 Gate Heartbeat — 8-Gate Pipeline"
      icon={Activity}
      style={{ flexShrink: 0, marginBottom: 8 }}
      headerRight={
        <span style={{ fontSize: 9, color: T.textMuted, fontFamily: 'monospace' }}>
          window <span style={{ color: T.cyan }}>{fmtWindowTs(main.window_ts)}</span>
          <span style={{ color: T.textDim }}> · </span>
          offset <span style={{ color: T.cyan }}>T-{main.eval_offset ?? '--'}</span>
          <span style={{ color: T.textDim }}> · </span>
          evaluated <span style={{ color: T.text }}>{fmtEvaluatedAt(main.evaluated_at)}</span>
          {pinnedIdx != null && (
            <>
              <span style={{ color: T.textDim }}> · </span>
              <button
                onClick={() => setPinnedIdx(null)}
                style={{
                  background: 'none', border: `1px solid ${T.cardBorder}`, color: T.cyan,
                  fontSize: 9, fontFamily: 'monospace', cursor: 'pointer', padding: '1px 6px',
                  borderRadius: 2, marginLeft: 4,
                }}
              >UNPIN</button>
            </>
          )}
        </span>
      }
    >
      {/* Current window strip */}
      <div style={{
        display: 'flex', alignItems: 'stretch', gap: 6, flexWrap: 'wrap',
        paddingBottom: 8, borderBottom: `1px solid ${T.cardBorder}`,
      }}>
        {PIPELINE.map(gate => (
          <GateChip key={gate.key} gate={gate} status={main.gate_results?.[gate.key]} />
        ))}
        <DecisionPill decision={main.decision} />
      </div>

      {/* Reason line */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 2px', fontSize: 10, fontFamily: 'monospace',
      }}>
        <div style={{ color: T.textMuted }}>
          {main.gate_failed ? (
            <>
              <span style={{ color: T.textMuted }}>blocked by </span>
              <span style={{ color: T.red, fontWeight: 700 }}>
                {GATE_DISPLAY[main.gate_failed] || main.gate_failed}
              </span>
              {main.gate_failed_raw && main.gate_failed_raw !== main.gate_failed && (
                <span style={{ color: T.textDim }}> ({main.gate_failed_raw})</span>
              )}
            </>
          ) : (
            <span style={{ color: T.green }}>all 8 gates passed</span>
          )}
        </div>
        <div style={{ color: T.textMuted }}>
          v2 p_up = <span style={{ color: T.text }}>{fmtProb(main.v2_probability_up)}</span>
        </div>
      </div>

      {/* Recent decisions rail */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 4,
        overflowX: 'auto', padding: '6px 0',
        borderTop: `1px solid ${T.cardBorder}`, borderBottom: `1px solid ${T.cardBorder}`,
      }}>
        <span style={{
          fontSize: 9, color: T.textMuted, fontFamily: 'monospace',
          letterSpacing: '0.08em', textTransform: 'uppercase', marginRight: 4, flexShrink: 0,
        }}>Recent:</span>
        {railEntries.map((e, i) => (
          <MiniStrip
            key={`${e.window_ts}-${e.eval_offset}-${i}`}
            entry={e}
            selected={i === mainIdx}
            onClick={() => setPinnedIdx(i === pinnedIdx ? null : i)}
          />
        ))}
      </div>

      {/* Aggregate stats */}
      <div style={{ paddingTop: 8 }}>
        <AggregateStats entries={entries} />
      </div>
    </Panel>
  );
}
