import React from 'react';

/**
 * Panel — Reusable dark container with title bar, icon, optional status indicator.
 *
 * Props:
 *   title      — Panel header text
 *   icon       — Lucide icon component
 *   status     — 'ok' | 'warn' | 'err'
 *   headerRight — React node to render on the right side of the header
 *   className  — Additional CSS classes
 *   style      — Additional inline styles (for flex sizing)
 *   children   — Panel content
 */
export default function Panel({ title, children, icon: Icon, className = '', status = 'ok', headerRight, style }) {
  const borderColor =
    status === 'warn' ? 'rgba(245,158,11,0.5)' :
    status === 'err' ? 'rgba(239,68,68,0.5)' :
    'rgba(51,65,85,1)';

  const headerBg =
    status === 'warn' ? 'rgba(245,158,11,0.1)' :
    status === 'err' ? 'rgba(239,68,68,0.1)' :
    'rgba(30,41,59,1)';

  const headerColor =
    status === 'warn' ? 'rgba(245,158,11,1)' :
    status === 'err' ? 'rgba(239,68,68,1)' :
    'rgba(148,163,184,1)';

  return (
    <div
      className={className}
      style={{
        background: 'rgba(15,23,42,0.8)',
        border: `1px solid ${borderColor}`,
        borderRadius: 2,
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        backdropFilter: 'blur(12px)',
        ...style,
      }}
    >
      {title && (
        <div
          style={{
            fontSize: 10,
            textTransform: 'uppercase',
            fontFamily: 'monospace',
            letterSpacing: '0.1em',
            padding: '6px 12px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: `1px solid ${borderColor}`,
            background: headerBg,
            color: headerColor,
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {Icon && <Icon size={12} />}
            {title}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            {headerRight}
            {status !== 'ok' && (
              <span style={{ animation: 'pulse 2s infinite' }}>[{status.toUpperCase()}]</span>
            )}
          </div>
        </div>
      )}
      <div style={{ flex: 1, padding: 12, overflow: 'hidden', display: 'flex', flexDirection: 'column', position: 'relative' }}>
        {children}
      </div>
    </div>
  );
}
