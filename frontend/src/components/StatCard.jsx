import React from 'react';

/**
 * StatCard — Label, value, change indicator.
 *
 * Props:
 *   label: string
 *   value: string
 *   change: string | null
 *   trend: 'up' | 'down' | 'neutral'
 */
export default function StatCard({ label, value, change, trend }) {
  const trendColor = {
    up: 'var(--profit)',
    down: 'var(--loss)',
    neutral: 'var(--text-secondary)',
  }[trend];

  return (
    <div className="card p-6 fade-in">
      <div style={{ color: 'var(--text-secondary)' }} className="text-xs font-medium mb-2 uppercase tracking-wider">
        {label}
      </div>
      <div className="text-2xl font-bold mb-2">{value}</div>
      {change && (
        <div style={{ color: trendColor }} className="text-xs font-semibold">
          {trend === 'up' && '↑'} {trend === 'down' && '↓'} {change}
        </div>
      )}
    </div>
  );
}
