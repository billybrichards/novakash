import React from 'react';

/**
 * ContinuousFeed — Single feed health row showing name, frequency, latency, value, and status.
 */
export default function ContinuousFeed({ name, hz, latency, val, change, status }) {
  const dotColor =
    status === 'err' ? '#ef4444' :
    status === 'warn' ? '#f59e0b' :
    '#10b981';

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '6px 0',
      borderBottom: '1px solid rgba(30,41,59,1)',
      fontSize: 12,
      fontFamily: 'monospace',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div style={{
          width: 6, height: 6, borderRadius: '50%',
          background: dotColor,
          animation: status === 'err' ? 'pulse 1s infinite' : (status === 'ok' ? 'pulse 2s infinite' : 'none'),
        }} />
        <span style={{ color: 'rgba(203,213,225,1)', width: 96, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{name}</span>
        <span style={{ color: 'rgba(71,85,105,1)', fontSize: 9 }}>{hz}</span>
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <span style={{ color: 'rgba(100,116,139,1)', fontSize: 10, width: 40, textAlign: 'right' }}>{latency}ms</span>
        <span style={{ color: 'rgba(226,232,240,1)', width: 64, textAlign: 'right' }}>{val}</span>
        <span style={{
          color: change >= 0 ? '#4ade80' : '#f87171',
          width: 40, textAlign: 'right', fontSize: 10,
        }}>
          {change > 0 ? '+' : ''}{change}%
        </span>
      </div>
    </div>
  );
}
