import React from 'react';
import { GATES, CHECKPOINTS } from './constants.js';

/**
 * GateAuditMatrix — 19-checkpoint × 8-gate heatmap table showing which gates
 * passed/failed at each evaluation offset for the current window.
 *
 * Props:
 *   currentT     — Current countdown (highlights the active column)
 *   gateData     — Optional: {[checkpoint]: {[gate]: 'pass'|'fail'}} from API
 *                  Falls back to deterministic mock when not provided.
 *   windowData   — Window snapshot with gates_passed / gate_failed strings
 */
export default function GateAuditMatrix({ currentT, gateData, windowData }) {

  const getGateStatus = (gate, t) => {
    // If real gate data provided, use it
    if (gateData && gateData[t] && gateData[t][gate] !== undefined) {
      return gateData[t][gate];
    }
    // If we have window-level gate info, apply it
    if (windowData) {
      const passed = windowData.gates_passed || '';
      const failed = windowData.gate_failed || '';
      if (failed && gate.includes(failed.replace('gate_', ''))) return 'fail';
      if (passed && passed.includes(gate.replace('gate_', ''))) return 'pass';
    }
    // Deterministic mock fallback
    return (t * (GATES.indexOf(gate) + 1)) % 7 !== 0 ? 'pass' : 'fail';
  };

  return (
    <div style={{ flex: 1, overflowX: 'auto', overflowY: 'auto' }}>
      <table style={{ width: '100%', textAlign: 'left', borderCollapse: 'collapse', fontSize: 9, fontFamily: 'monospace' }}>
        <thead>
          <tr style={{ borderBottom: '1px solid rgba(51,65,85,1)' }}>
            <th style={{
              padding: 4, position: 'sticky', left: 0, top: 0,
              background: '#0f172a', zIndex: 20, width: 96,
              borderBottom: '1px solid rgba(51,65,85,1)',
            }}>GATE / TIME</th>
            {CHECKPOINTS.map(t => (
              <th key={t} style={{
                padding: 4, textAlign: 'center', position: 'sticky', top: 0,
                borderBottom: '1px solid rgba(51,65,85,1)',
                borderLeft: '1px solid rgba(30,41,59,1)',
                zIndex: 10,
                background: t === currentT ? 'rgba(6,182,212,0.15)' : '#0f172a',
                color: t === currentT ? '#06b6d4' : 'rgba(100,116,139,1)',
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
                color: 'rgba(203,213,225,1)',
              }}>
                {gate.replace('gate_', '')}
              </td>
              {CHECKPOINTS.map(t => {
                const isPast = t > currentT;
                const isCurrent = t === currentT;
                const status = getGateStatus(gate, t);
                const passes = status === 'pass';

                let bgColor = 'rgba(30,41,59,0.2)';
                let dotColor = 'rgba(51,65,85,1)';

                if (isPast) {
                  bgColor = passes ? 'rgba(16,185,129,0.1)' : 'rgba(239,68,68,0.1)';
                  dotColor = passes ? '#10b981' : '#ef4444';
                } else if (isCurrent) {
                  bgColor = 'rgba(6,182,212,0.2)';
                  dotColor = '#06b6d4';
                }

                return (
                  <td key={t} style={{
                    padding: 4, textAlign: 'center',
                    borderLeft: '1px solid rgba(30,41,59,0.5)',
                    background: bgColor,
                    animation: isCurrent ? 'pulse 2s infinite' : 'none',
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
    </div>
  );
}
