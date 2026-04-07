import React from 'react';
import { GATES, CHECKPOINTS, T } from './constants.js';

/**
 * GateAuditMatrix — v9.0 gate pipeline heatmap.
 *
 * Shows 19 checkpoints (T-240 to T-60) x 5 gates (Agreement, VPIN, Delta, CG Veto, Cap).
 * Uses real signal_evaluations data when available via v9GateData prop.
 *
 * Props:
 *   currentT     — Current countdown (highlights the active column)
 *   gateData     — Optional: {[checkpoint]: {[gate]: 'pass'|'fail'}} from API
 *   windowData   — Window snapshot with gates_passed / gate_failed strings
 *   v9GateData   — v9 signal_evaluations keyed by window_ts -> offset -> gate results
 */

// Human-readable gate labels for v9.0 pipeline
const GATE_LABELS = {
  'gate_agreement': 'SRC AGREE',
  'gate_vpin': 'VPIN',
  'gate_delta': 'DELTA',
  'gate_cg_veto': 'CG VETO',
  'gate_cap': 'CAP',
};

export default function GateAuditMatrix({ currentT, gateData, windowData, v9GateData }) {

  const getGateStatus = (gate, t) => {
    // Priority 1: Real v9 gate data from signal_evaluations
    if (v9GateData) {
      // v9GateData is keyed by window_ts -> offset -> gate results
      // Find the most recent window's data
      const windowKeys = Object.keys(v9GateData).sort((a, b) => b - a);
      if (windowKeys.length > 0) {
        const latestWindow = v9GateData[windowKeys[0]];
        if (latestWindow && latestWindow[t] && latestWindow[t][gate] !== undefined) {
          return latestWindow[t][gate];
        }
      }
    }
    // Priority 2: Direct gate data prop
    if (gateData && gateData[t] && gateData[t][gate] !== undefined) {
      return gateData[t][gate];
    }
    // Priority 3: Window-level gate info
    if (windowData) {
      const passed = windowData.gates_passed || '';
      const failed = windowData.gate_failed || '';
      if (failed && gate.includes(failed.replace('gate_', ''))) return 'fail';
      if (passed && passed.includes(gate.replace('gate_', ''))) return 'pass';
    }
    // Priority 4: Deterministic mock fallback (for display before real data loads)
    return (t * (GATES.indexOf(gate) + 1)) % 7 !== 0 ? 'pass' : 'fail';
  };

  return (
    <div style={{ flex: 1, overflowX: 'auto', overflowY: 'auto' }}>
      {/* v9.0 pipeline label */}
      <div style={{
        fontSize: 9, fontFamily: 'monospace', color: T.purple, padding: '4px 8px',
        borderBottom: `1px solid ${T.cardBorder}`, display: 'flex', alignItems: 'center', gap: 8,
        marginBottom: 4,
      }}>
        <span style={{ fontWeight: 700 }}>v9.0 PIPELINE:</span>
        <span style={{ color: T.textMuted }}>Agreement</span>
        <span style={{ color: T.textDim }}>{'\u2192'}</span>
        <span style={{ color: T.textMuted }}>VPIN Tier</span>
        <span style={{ color: T.textDim }}>{'\u2192'}</span>
        <span style={{ color: T.textMuted }}>CG Veto</span>
        <span style={{ color: T.textDim }}>{'\u2192'}</span>
        <span style={{ color: T.textMuted }}>Cap</span>
        <span style={{ color: T.textDim }}>{'\u2192'}</span>
        <span style={{ color: T.textMuted }}>FAK</span>
      </div>
      <table style={{ width: '100%', textAlign: 'left', borderCollapse: 'collapse', fontSize: 9, fontFamily: "'JetBrains Mono', monospace" }}>
        <thead>
          <tr style={{ borderBottom: '1px solid rgba(51,65,85,1)' }}>
            <th style={{
              padding: 4, position: 'sticky', left: 0, top: 0,
              background: '#0f172a', zIndex: 20, width: 80,
              borderBottom: '1px solid rgba(51,65,85,1)',
            }}>GATE / T</th>
            {CHECKPOINTS.map(t => (
              <th key={t} style={{
                padding: 4, textAlign: 'center', position: 'sticky', top: 0,
                borderBottom: '1px solid rgba(51,65,85,1)',
                borderLeft: '1px solid rgba(30,41,59,1)',
                zIndex: 10,
                background: t === currentT ? 'rgba(168,85,247,0.15)' : '#0f172a',
                color: t === currentT ? T.purple : 'rgba(100,116,139,1)',
              }}>{t}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {GATES.map((gate) => (
            <tr key={gate} style={{ borderBottom: '1px solid rgba(30,41,59,0.5)' }}>
              <td style={{
                padding: 4, position: 'sticky', left: 0,
                background: '#0f172a', zIndex: 10,
                borderRight: '1px solid rgba(30,41,59,1)',
                color: gate === 'gate_agreement' ? T.purple : 'rgba(203,213,225,1)',
                fontWeight: gate === 'gate_agreement' ? 700 : 400,
              }}>
                {GATE_LABELS[gate] || gate.replace('gate_', '')}
              </td>
              {CHECKPOINTS.map(t => {
                const isPast = t > currentT;
                const isCurrent = t === currentT;
                const status = getGateStatus(gate, t);
                const passes = status === 'pass';

                // Tier-aware background: early zone vs golden zone
                const isEarlyZone = t > 130;
                let bgColor = 'rgba(30,41,59,0.2)';
                let dotColor = 'rgba(51,65,85,1)';

                if (isPast) {
                  bgColor = passes ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)';
                  dotColor = passes ? '#10b981' : '#ef4444';
                } else if (isCurrent) {
                  bgColor = 'rgba(168,85,247,0.2)';
                  dotColor = T.purple;
                }

                return (
                  <td key={t} style={{
                    padding: 4, textAlign: 'center',
                    borderLeft: '1px solid rgba(30,41,59,0.5)',
                    background: bgColor,
                    animation: isCurrent ? 'pulse 2s infinite' : 'none',
                    // Subtle border at the tier boundary (T-130)
                    borderRight: t === 130 ? `2px solid rgba(168,85,247,0.3)` : undefined,
                  }}>
                    <div style={{
                      margin: '0 auto', width: 6, height: 6, borderRadius: '50%',
                      background: dotColor,
                    }} />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {/* Tier labels beneath the table */}
      <div style={{ display: 'flex', fontSize: 8, fontFamily: 'monospace', color: T.textDim, marginTop: 4, paddingLeft: 84 }}>
        <div style={{ flex: 1, textAlign: 'center', borderRight: `1px solid rgba(168,85,247,0.2)`, paddingRight: 4 }}>
          EARLY ($0.55 cap, VPIN {'\u2265'} 0.65)
        </div>
        <div style={{ flex: 1, textAlign: 'center', paddingLeft: 4 }}>
          GOLDEN ($0.65 cap, VPIN {'\u2265'} 0.45)
        </div>
      </div>
    </div>
  );
}
