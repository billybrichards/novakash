import React from 'react';
import StatusPulse from './StatusPulse.jsx';

/**
 * MachineCard -- A subsystem "machine" in the factory floor.
 *
 * Each card represents one component of the engine pipeline.
 * Shows: title, status indicator, key-value metrics, optional children.
 */

export default function MachineCard({
  title,
  status = 'ok',      // 'ok' | 'warn' | 'error' | 'offline'
  metrics = [],        // [{ label, value, color?, mono? }]
  children,
  compact = false,
  highlight = false,
  style: extraStyle = {},
}) {
  return (
    <div
      style={{
        background: highlight
          ? 'rgba(0, 204, 255, 0.03)'
          : 'rgba(255, 255, 255, 0.012)',
        border: `1px solid ${highlight ? 'rgba(0, 204, 255, 0.15)' : '#1e293b'}`,
        borderRadius: 6,
        padding: compact ? '8px 10px' : '10px 12px',
        fontFamily: "'JetBrains Mono', 'IBM Plex Mono', monospace",
        position: 'relative',
        overflow: 'hidden',
        ...extraStyle,
      }}
    >
      {/* Header row: title + status dot */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: metrics.length > 0 || children ? (compact ? 6 : 8) : 0,
      }}>
        <span style={{
          fontSize: 9,
          fontWeight: 700,
          letterSpacing: '0.1em',
          color: '#94a3b8',
          textTransform: 'uppercase',
        }}>
          {title}
        </span>
        <StatusPulse status={status} size={6} />
      </div>

      {/* Key-value metrics */}
      {metrics.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: compact ? 3 : 4 }}>
          {metrics.map((m, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'baseline',
                justifyContent: 'space-between',
                gap: 8,
              }}
            >
              <span style={{
                fontSize: 9,
                color: '#475569',
                flexShrink: 0,
                letterSpacing: '0.02em',
              }}>
                {m.label}
              </span>
              <span style={{
                fontSize: m.large ? 14 : 10,
                fontWeight: m.large ? 700 : 500,
                color: m.color || '#00ccff',
                fontFamily: m.mono !== false
                  ? "'JetBrains Mono', 'IBM Plex Mono', monospace"
                  : 'inherit',
                textAlign: 'right',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}>
                {m.value ?? '\u2014'}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Custom children */}
      {children}
    </div>
  );
}
