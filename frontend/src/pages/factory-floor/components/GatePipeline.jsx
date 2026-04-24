import React from 'react';

/**
 * GatePipeline -- Visualizes the v8 champion's 9-gate pipeline as a
 * horizontal row of dots. Green = pass, red = fail. The first failing
 * gate gets a label and a subtle highlight.
 */

const GATE_LABELS = [
  'AGR',   // agreement
  'VPIN',  // vpin
  'DLT',   // delta
  'CG',    // cg_veto
  'MAC',   // macro
  'DIV',   // divergence
  'FLR',   // floor
  'CAP',   // cap
  'CNF',   // confidence
];

const GATE_FULL_NAMES = [
  'Source Agreement',
  'VPIN Threshold',
  'Delta Minimum',
  'CG Veto Check',
  'Macro Filter',
  'Divergence',
  'Price Floor',
  'Price Cap',
  'Confidence',
];

export default function GatePipeline({ gates = [] }) {
  // Pad or trim to 9 gates
  const normalized = GATE_LABELS.map((label, i) => {
    const gate = gates[i];
    return {
      label,
      fullName: GATE_FULL_NAMES[i],
      passed: gate?.passed ?? null,
    };
  });

  const firstFail = normalized.findIndex(g => g.passed === false);
  const allPassed = normalized.every(g => g.passed === true);
  const hasData = gates.length > 0;

  return (
    <div>
      {/* Gate dots row */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 0,
        position: 'relative',
      }}>
        {normalized.map((g, i) => {
          const isBlocker = i === firstFail;
          const color = g.passed === null
            ? '#334155'
            : g.passed
              ? '#00ff88'
              : '#ff3344';

          return (
            <React.Fragment key={i}>
              {/* Connector line between dots */}
              {i > 0 && (
                <div style={{
                  width: 12,
                  height: 1,
                  background: g.passed === null
                    ? '#1e293b'
                    : g.passed
                      ? 'rgba(0, 255, 136, 0.3)'
                      : 'rgba(255, 51, 68, 0.2)',
                  flexShrink: 0,
                }} />
              )}

              {/* Gate dot */}
              <div
                title={`${g.fullName}: ${g.passed === null ? 'N/A' : g.passed ? 'PASS' : 'FAIL'}`}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 3,
                  position: 'relative',
                }}
              >
                <div style={{
                  width: isBlocker ? 12 : 10,
                  height: isBlocker ? 12 : 10,
                  borderRadius: '50%',
                  background: color,
                  boxShadow: g.passed === null
                    ? 'none'
                    : `0 0 6px ${color}66`,
                  border: isBlocker ? '1px solid #ff3344' : 'none',
                  animation: isBlocker ? 'ffPulse 1.5s ease-in-out infinite' : 'none',
                  flexShrink: 0,
                }} />
                <span style={{
                  fontSize: 7,
                  color: isBlocker ? '#ff3344' : '#475569',
                  letterSpacing: '0.04em',
                  fontFamily: "'JetBrains Mono', monospace",
                  fontWeight: isBlocker ? 700 : 400,
                }}>
                  {g.label}
                </span>
              </div>
            </React.Fragment>
          );
        })}
      </div>

      {/* Summary line */}
      <div style={{
        marginTop: 6,
        fontSize: 9,
        fontFamily: "'JetBrains Mono', monospace",
        color: !hasData
          ? '#475569'
          : allPassed
            ? '#00ff88'
            : '#ff3344',
      }}>
        {!hasData
          ? 'No gate data'
          : allPassed
            ? 'ALL GATES PASS'
            : `BLOCKED at ${normalized[firstFail]?.fullName || 'unknown'}`
        }
      </div>
    </div>
  );
}
