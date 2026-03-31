import React from 'react';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';

/**
 * EquityCurve — Area chart showing cumulative P&L over time.
 */
export default function EquityCurve({ data }) {
  if (!data || data.length === 0) {
    return <div style={{ color: 'var(--text-secondary)' }}>No P&L data yet</div>;
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={data} margin={{ top: 5, right: 30, left: 0, bottom: 5 }}>
        <CartesianGrid stroke="var(--border)" strokeDasharray="0" />
        <XAxis
          dataKey="timestamp"
          stroke="var(--text-secondary)"
          style={{ fontSize: '12px' }}
          tick={{ angle: -45, textAnchor: 'end', height: 80 }}
        />
        <YAxis stroke="var(--text-secondary)" style={{ fontSize: '12px' }} />
        <Tooltip
          contentStyle={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
          }}
          formatter={v => `$${v.toFixed(2)}`}
        />
        <Area
          type="monotone"
          dataKey="cumulative_pnl"
          fill="var(--profit)"
          fillOpacity={0.1}
          stroke="var(--profit)"
          strokeWidth={2}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}
