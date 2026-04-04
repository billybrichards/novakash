/**
 * TWAPChart.jsx — TWAP-Delta + Gamma token price chart.
 *
 * Left Y-axis: cumulative order-flow delta (buy minus sell volume proxy)
 * Right Y-axis: Gamma token prices (UP/DOWN)
 *
 * Uses Recharts for consistency with the rest of the app.
 */

import React, { useMemo } from 'react';
import {
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  Legend,
} from 'recharts';

const T = {
  profit: '#4ade80',
  loss: '#f87171',
  cyan: '#06b6d4',
  purple: '#a855f7',
  warning: '#f59e0b',
  grid: 'rgba(255,255,255,0.04)',
  border: 'rgba(255,255,255,0.06)',
  text: 'rgba(255,255,255,0.4)',
  card: 'rgba(255,255,255,0.015)',
  mono: "'IBM Plex Mono', monospace",
};

function CustomTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: '#0d0d1a',
      border: `1px solid ${T.border}`,
      borderRadius: 8,
      padding: '10px 14px',
      fontFamily: T.mono,
      fontSize: 11,
    }}>
      <div style={{ color: 'rgba(255,255,255,0.4)', marginBottom: 6, fontSize: 10 }}>{label}</div>
      {payload.map((p, i) => (
        <div key={i} style={{ color: p.color, marginBottom: 3 }}>
          {p.name}: <strong>{typeof p.value === 'number' ? p.value.toFixed(4) : p.value}</strong>
        </div>
      ))}
    </div>
  );
}

export default function TWAPChart({ data = [], height = 320 }) {
  // Determine if delta is trending up or down (for area fill)
  const lastDelta = data.length > 0 ? data[data.length - 1]?.delta : 0;
  const deltaColor = lastDelta >= 0 ? T.profit : T.loss;
  const areaFill = lastDelta >= 0 ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.08)';

  // Zero reference line
  const hasPositive = data.some(d => d.delta > 0);
  const hasNegative = data.some(d => d.delta < 0);

  // Derive agreement: gamma direction vs delta direction
  const lastPoint = data[data.length - 1];
  const gammaDir = lastPoint?.gammaUp > lastPoint?.gammaDown ? 'UP' : 'DOWN';
  const deltaDir = lastDelta >= 0 ? 'UP' : 'DOWN';
  const agreement = gammaDir === deltaDir;

  return (
    <div>
      {/* Agreement indicator */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        marginBottom: 12,
        padding: '8px 12px',
        background: agreement ? 'rgba(74,222,128,0.06)' : 'rgba(248,113,113,0.06)',
        border: `1px solid ${agreement ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.2)'}`,
        borderRadius: 8,
        fontSize: 12,
        fontFamily: T.mono,
      }}>
        <span style={{ fontSize: 14 }}>{agreement ? '✅' : '⚠️'}</span>
        <span style={{ color: agreement ? T.profit : T.loss, fontWeight: 600 }}>
          {agreement ? 'DELTA + GAMMA AGREE' : 'DELTA / GAMMA CONFLICT'}
        </span>
        <span style={{ color: 'rgba(255,255,255,0.3)', marginLeft: 'auto', fontSize: 10 }}>
          Δ {deltaDir} · Gamma {gammaDir}
        </span>
      </div>

      {/* Chart */}
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={data} margin={{ top: 5, right: 60, left: 0, bottom: 5 }}>
          <CartesianGrid stroke={T.grid} strokeDasharray="0" vertical={false} />

          <XAxis
            dataKey="timeLabel"
            stroke={T.text}
            tick={{ fontSize: 10, fontFamily: T.mono }}
            tickLine={false}
            interval="preserveStartEnd"
          />

          {/* Left Y: delta */}
          <YAxis
            yAxisId="delta"
            stroke={T.text}
            tick={{ fontSize: 10, fontFamily: T.mono }}
            tickLine={false}
            tickFormatter={v => v.toFixed(2)}
            label={{
              value: 'Δ Flow',
              angle: -90,
              position: 'insideLeft',
              fill: T.text,
              fontSize: 10,
              fontFamily: T.mono,
            }}
          />

          {/* Right Y: gamma prices */}
          <YAxis
            yAxisId="gamma"
            orientation="right"
            stroke={T.text}
            tick={{ fontSize: 10, fontFamily: T.mono }}
            tickLine={false}
            domain={[0, 1]}
            tickFormatter={v => v.toFixed(2)}
            label={{
              value: 'γ Price',
              angle: 90,
              position: 'insideRight',
              fill: T.text,
              fontSize: 10,
              fontFamily: T.mono,
            }}
          />

          <Tooltip content={<CustomTooltip />} />
          <Legend
            wrapperStyle={{ fontSize: 10, fontFamily: T.mono, color: T.text }}
          />

          {/* Zero reference line */}
          <ReferenceLine
            yAxisId="delta"
            y={0}
            stroke="rgba(255,255,255,0.15)"
            strokeDasharray="4 4"
          />

          {/* TWAP delta area */}
          <Area
            yAxisId="delta"
            type="monotone"
            dataKey="delta"
            name="Δ Delta"
            stroke={deltaColor}
            fill={areaFill}
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />

          {/* TWAP line */}
          <Line
            yAxisId="delta"
            type="monotone"
            dataKey="twap"
            name="TWAP"
            stroke={T.warning}
            strokeWidth={1.5}
            strokeDasharray="4 2"
            dot={false}
            isAnimationActive={false}
          />

          {/* Gamma UP */}
          <Line
            yAxisId="gamma"
            type="monotone"
            dataKey="gammaUp"
            name="γ UP"
            stroke={T.profit}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
            opacity={0.7}
          />

          {/* Gamma DOWN */}
          <Line
            yAxisId="gamma"
            type="monotone"
            dataKey="gammaDown"
            name="γ DOWN"
            stroke={T.loss}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
            opacity={0.7}
          />
        </ComposedChart>
      </ResponsiveContainer>

      {/* Stats row */}
      {lastPoint && (
        <div style={{
          display: 'flex',
          gap: 16,
          marginTop: 8,
          fontSize: 11,
          fontFamily: T.mono,
          color: 'rgba(255,255,255,0.4)',
          flexWrap: 'wrap',
        }}>
          <span>Δ: <strong style={{ color: deltaColor }}>{lastPoint.delta?.toFixed(4)}</strong></span>
          <span>TWAP: <strong style={{ color: T.warning }}>{lastPoint.twap?.toFixed(4)}</strong></span>
          <span>γ UP: <strong style={{ color: T.profit }}>{lastPoint.gammaUp?.toFixed(4)}</strong></span>
          <span>γ DOWN: <strong style={{ color: T.loss }}>{lastPoint.gammaDown?.toFixed(4)}</strong></span>
        </div>
      )}
    </div>
  );
}
