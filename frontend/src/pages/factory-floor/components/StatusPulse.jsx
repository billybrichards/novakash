import React from 'react';

/**
 * StatusPulse -- Animated status indicator dot with glow.
 *
 * Colors:
 *   'ok'      -> green (#00ff88)
 *   'warn'    -> amber (#ffaa00)
 *   'error'   -> red   (#ff3344)
 *   'offline' -> dim gray
 *   or pass a raw hex string
 */

const STATUS_COLORS = {
  ok: '#00ff88',
  warn: '#ffaa00',
  error: '#ff3344',
  offline: '#334155',
};

export default function StatusPulse({ status = 'ok', size = 8, pulse = true, label }) {
  const color = STATUS_COLORS[status] || status;
  const shouldPulse = pulse && status !== 'offline';

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span
        style={{
          display: 'inline-block',
          width: size,
          height: size,
          borderRadius: '50%',
          background: color,
          boxShadow: `0 0 ${size}px ${color}88, 0 0 ${size * 2}px ${color}33`,
          animation: shouldPulse ? 'ffPulse 2s ease-in-out infinite' : 'none',
          flexShrink: 0,
        }}
      />
      {label && (
        <span style={{
          fontSize: 9,
          fontFamily: "'JetBrains Mono', 'IBM Plex Mono', monospace",
          color: '#94a3b8',
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
        }}>
          {label}
        </span>
      )}
    </span>
  );
}
