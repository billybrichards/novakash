import React from 'react';

/**
 * StatusBadge — Colored dot + label indicator.
 *
 * Props:
 *   status: 'ok' | 'error' | 'warning' | 'online' | 'offline'
 *   label: string
 */
export default function StatusBadge({ status, label }) {
  const statusConfig = {
    ok: { color: 'var(--profit)', dot: '●' },
    error: { color: 'var(--loss)', dot: '●' },
    warning: { color: 'var(--warning)', dot: '●' },
    online: { color: 'var(--profit)', dot: '●' },
    offline: { color: 'var(--loss)', dot: '●' },
  };

  const { color, dot } = statusConfig[status] || statusConfig.error;

  return (
    <div className="flex items-center gap-2">
      <span style={{ color }}>{dot}</span>
      <span style={{ color }} className="text-xs font-semibold">
        {label}
      </span>
    </div>
  );
}
