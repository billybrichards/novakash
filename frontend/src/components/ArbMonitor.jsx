import React, { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';

/**
 * ArbMonitor — Combined YES+NO price with $1.00 threshold line.
 */
export default function ArbMonitor() {
  const api = useApi();
  const [data, setData] = useState([]);

  useEffect(() => {
    const fetchArbs = async () => {
      try {
        const res = await api.get('/signals/arb', { params: { limit: 50 } });
        const arbs = (res.data.signals || []).map(s => ({
          timestamp: new Date(s.timestamp).toLocaleTimeString(),
          combined_price: parseFloat(s.combined_price),
        }));
        setData(arbs);
      } catch (err) {
        console.error('Failed to fetch arbs:', err);
      }
    };

    fetchArbs();
    const interval = setInterval(fetchArbs, 10000);
    return () => clearInterval(interval);
  }, [api]);

  return (
    <div>
      {data.length === 0 ? (
        <div style={{ color: 'var(--text-secondary)' }}>No arb data yet</div>
      ) : (
        <ResponsiveContainer width="100%" height={250}>
          <LineChart data={data} margin={{ top: 5, right: 30, left: 0, bottom: 5 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="0" />
            <XAxis
              dataKey="timestamp"
              stroke="var(--text-secondary)"
              style={{ fontSize: '12px' }}
              tick={{ fontSize: 10 }}
            />
            <YAxis stroke="var(--text-secondary)" domain={[0.9, 1.0]} style={{ fontSize: '12px' }} />
            <Tooltip
              contentStyle={{
                background: 'var(--card)',
                border: '1px solid var(--border)',
                borderRadius: '8px',
              }}
              formatter={v => `$${v.toFixed(4)}`}
            />
            {/* $1.00 threshold */}
            <ReferenceLine
              y={1.0}
              stroke="var(--text-secondary)"
              strokeDasharray="5 5"
              label={{ value: '$1.00', fill: 'var(--text-secondary)', fontSize: 10 }}
            />
            {/* Arb zone (< $0.985) */}
            <ReferenceLine
              y={0.985}
              stroke="var(--profit)"
              strokeDasharray="5 5"
              label={{ value: '$0.985 (arb zone)', fill: 'var(--profit)', fontSize: 10 }}
            />
            <Line
              type="monotone"
              dataKey="combined_price"
              stroke="var(--accent-cyan)"
              dot={false}
              isAnimationActive={false}
              strokeWidth={2}
              name="Combined Price (YES+NO)"
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
