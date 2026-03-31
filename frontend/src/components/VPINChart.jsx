import React from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';

/**
 * VPINChart — Line chart with threshold lines at 0.55 (informed) and 0.70 (cascade).
 */
export default function VPINChart({ signal }) {
  if (!signal) {
    return <div style={{ color: 'var(--text-secondary)' }}>No VPIN data yet</div>;
  }

  // Mock data for demo (in real app, fetch time series)
  const data = [
    { time: '00:00', vpin: 0.32 },
    { time: '01:00', vpin: 0.38 },
    { time: '02:00', vpin: 0.45 },
    { time: '03:00', vpin: 0.52 },
    { time: '04:00', vpin: 0.58 },
    { time: '05:00', vpin: signal.value },
  ];

  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data} margin={{ top: 5, right: 30, left: 0, bottom: 5 }}>
        <CartesianGrid stroke="var(--border)" strokeDasharray="0" />
        <XAxis dataKey="time" stroke="var(--text-secondary)" style={{ fontSize: '12px' }} />
        <YAxis stroke="var(--text-secondary)" domain={[0, 1]} style={{ fontSize: '12px' }} />
        <Tooltip
          contentStyle={{
            background: 'var(--card)',
            border: '1px solid var(--border)',
            borderRadius: '8px',
          }}
          formatter={v => v.toFixed(4)}
        />
        {/* Informed threshold */}
        <ReferenceLine y={0.55} stroke="var(--warning)" strokeDasharray="5 5" label={{ value: '0.55', fill: 'var(--warning)', fontSize: 10 }} />
        {/* Cascade threshold */}
        <ReferenceLine y={0.70} stroke="var(--loss)" strokeDasharray="5 5" label={{ value: '0.70', fill: 'var(--loss)', fontSize: 10 }} />
        <Line
          type="monotone"
          dataKey="vpin"
          stroke="var(--accent-cyan)"
          dot={false}
          isAnimationActive={false}
          strokeWidth={2}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
