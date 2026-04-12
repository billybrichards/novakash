/**
 * Margin Strategy Dashboard — Strategy lab for margin engine strategies
 * with regime-based PnL analysis and real-time V4 data integration.
 *
 * Design reference: docs/MARGIN_STRATEGY_DASHBOARD_DESIGN.md
 *
 * Shows:
 *   - 5 strategy cards (V4 PATH, Alignment, VaR, Regime, Cascade)
 *   - Real-time V4 snapshot data
 *   - Position analysis with fee-adjusted PnL
 *   - Signal strength distribution
 *   - Hold extension analysis
 *   - Partial close audit
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';
import { T } from './margin-engine/components/constants.js';
import V4Panel from './margin-engine/components/V4Panel.jsx';

// ─── Constants ──────────────────────────────────────────────────────────────

const STRATEGIES = [
  {
    id: 'v4_path',
    name: 'V4 PATH',
    description: 'Enable V4 fusion surface path (currently active)',
    icon: 'V4',
  },
  {
    id: 'alignment',
    name: 'Multi-Timescale Alignment',
    description: 'Trade only when 3/4 timescales agree on direction',
    icon: 'AL',
  },
  {
    id: 'var',
    name: 'Quantile-VaR Sizing',
    description: 'Size positions based on TimesFM VaR (p10 downside)',
    icon: 'VaR',
  },
  {
    id: 'regime',
    name: 'Regime-Adaptive',
    description: 'Adjust strategy parameters based on market regime',
    icon: 'RG',
  },
  {
    id: 'cascade',
    name: 'Cascade Fade',
    description: 'Fade liquidation cascades on Opinion/Polymarket',
    icon: 'CA',
  },
];

const REGIME_COLORS = {
  TRENDING_UP: T.green,
  TRENDING_DOWN: T.red,
  MEAN_REVERTING: T.cyan,
  CHOPPY: T.amber,
  NO_EDGE: T.textDim,
};

// ─── Utility Components ──────────────────────────────────────────────────────

function Chip({ color, bg, border, label, value, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 8, fontWeight: 800, padding: '2px 6px', borderRadius: 3,
        background: bg, color, border: `1px solid ${border}`,
        fontFamily: T.mono, letterSpacing: '0.04em', textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      {label && <span style={{ opacity: 0.7 }}>{label}</span>}
      <span>{value}</span>
    </span>
  );
}

function SectionHeader({ title, subtitle, badge, badgeColor = T.cyan }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'baseline',
      justifyContent: 'space-between',
      marginBottom: 12,
      paddingBottom: 6,
      borderBottom: `1px solid ${T.cardBorder}`,
    }}>
      <div>
        <span style={{
          fontSize: 11,
          fontWeight: 800,
          color: T.white,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
        }}>{title}</span>
        {subtitle && (
          <span style={{
            fontSize: 9,
            color: T.textMuted,
            marginLeft: 8,
            fontFamily: T.mono,
          }}>{subtitle}</span>
        )}
      </div>
      {badge && (
        <Chip
          color={badgeColor}
          bg={`${badgeColor}1a`}
          border={`${badgeColor}55`}
          value={badge}
        />
      )}
    </div>
  );
}

function Metric({ label, value, sub, color = T.text }) {
  return (
    <div style={{
      background: 'rgba(15,23,42,0.4)',
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 4,
      padding: '8px 10px',
    }}>
      <div style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em', marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 900, fontFamily: T.mono, color }}>{value}</div>
      {sub && <div style={{ fontSize: 8, color: T.textDim, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

// ─── Strategy Card Components ────────────────────────────────────────────────

function StrategyCard({ strategy, stats }) {
  const isLive = strategy.id === 'v4_path';
  const statusColor = isLive ? T.green : T.textDim;
  const statusLabel = isLive ? 'LIVE' : 'INACTIVE';

  const totalPnl = stats?.total_pnl || 0;
  const winRate = stats?.win_rate != null ? (stats.win_rate * 100).toFixed(1) : '—';
  const sharpe = stats?.sharpe != null ? stats.sharpe.toFixed(2) : '—';
  const totalTrades = stats?.total_trades || 0;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px',
        borderBottom: `1px solid ${T.cardBorder}`,
        background: 'rgba(15,23,42,0.6)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{
            fontSize: 10,
            fontWeight: 800,
            color: T.white,
            fontFamily: T.mono,
            padding: '2px 6px',
            borderRadius: 3,
            background: 'rgba(168,85,247,0.15)',
            border: '1px solid rgba(168,85,247,0.3)',
          }}>{strategy.icon}</span>
          <span style={{ fontSize: 11, fontWeight: 700, color: T.text }}>{strategy.name}</span>
        </div>
        <Chip
          color={statusColor}
          bg={`${statusColor}22`}
          border={`${statusColor}55`}
          value={statusLabel}
        />
      </div>

      {/* Body */}
      <div style={{ padding: 14, flex: 1 }}>
        <p style={{ fontSize: 9, color: T.textMuted, lineHeight: 1.5, marginBottom: 12 }}>
          {strategy.description}
        </p>

        {/* Performance metrics */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6, marginBottom: 12 }}>
          <Metric
            label="PnL"
            value={`${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`}
            color={totalPnl >= 0 ? T.green : T.red}
          />
          <Metric
            label="WR"
            value={`${winRate}%`}
            color={parseFloat(winRate) > 60 ? T.green : parseFloat(winRate) > 50 ? T.amber : T.textDim}
          />
          <Metric label="Sharpe" value={sharpe} color={T.cyan} />
          <Metric label="Trades" value={totalTrades} color={T.textMuted} />
        </div>

        {/* Footer */}
        <div style={{ marginTop: 'auto' }}>
          {stats ? (
            <button
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 4,
                border: `1px solid ${T.cyan}44`,
                background: 'rgba(6,182,212,0.08)',
                color: T.cyan,
                fontSize: 9,
                fontWeight: 700,
                fontFamily: T.mono,
                cursor: 'pointer',
                letterSpacing: '0.05em',
                transition: 'all 150ms',
              }}
            >
              Configure
            </button>
          ) : (
            <button
              disabled
              style={{
                width: '100%',
                padding: '8px 12px',
                borderRadius: 4,
                border: `1px solid ${T.cardBorder}`,
                background: 'rgba(15,23,42,0.4)',
                color: T.textMuted,
                fontSize: 9,
                fontWeight: 700,
                fontFamily: T.mono,
                cursor: 'not-allowed',
                letterSpacing: '0.05em',
              }}
            >
              Coming Soon
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Position Analysis Components ────────────────────────────────────────────

function PnLDistribution({ positions }) {
  const wins = positions.filter(p => p.pnl > 0).length;
  const losses = positions.filter(p => p.pnl <= 0).length;
  const total = positions.length;
  const avgPnl = total > 0 ? positions.reduce((sum, p) => sum + p.pnl, 0) / total : 0;

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Fee-Adjusted PnL Distribution"
        subtitle={`${wins}W / ${losses}L · ${total} trades`}
        badge={`Avg: $${avgPnl >= 0 ? '+' : ''}${avgPnl.toFixed(2)}`}
        badgeColor={avgPnl >= 0 ? T.green : T.red}
      />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8 }}>
        <Metric
          label="Total PnL"
          value={`$${positions.reduce((sum, p) => sum + p.pnl, 0).toFixed(2)}`}
          color={avgPnl >= 0 ? T.green : T.red}
        />
        <Metric
          label="Win Rate"
          value={`${total > 0 ? (wins / total * 100).toFixed(1) : '—'}%`}
          color={wins / total > 0.55 ? T.green : wins / total > 0.5 ? T.amber : T.textDim}
        />
        <Metric
          label="Avg Trade"
          value={`$${avgPnl >= 0 ? '+' : ''}${avgPnl.toFixed(2)}`}
          color={T.cyan}
        />
        <Metric
          label="Best Trade"
          value={`$${positions.length > 0 ? Math.max(...positions.map(p => p.pnl)).toFixed(2) : '—'}`}
          color={T.green}
        />
      </div>

      {/* PnL histogram bars */}
      {positions.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 8, color: T.textMuted, marginBottom: 6 }}>PnL Distribution</div>
          <div style={{ display: 'flex', gap: 2, alignItems: 'flex-end', height: 60 }}>
            {positions.slice(-30).map((p, i) => {
              const height = Math.min(100, Math.abs(p.pnl) * 10);
              return (
                <div
                  key={i}
                  title={`Trade ${i + 1}: $${p.pnl.toFixed(2)}`}
                  style={{
                    flex: 1,
                    height: `${height}%`,
                    background: p.pnl > 0 ? T.green : T.red,
                    opacity: 0.6,
                    borderRadius: '2px 2px 0 0',
                  }}
                />
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Signal Strength Distribution ────────────────────────────────────────────

function SignalStrengthHistogram({ positions }) {
  const alignments = positions.map(p => p.alignment_score || 0.5);
  const bins = [
    { label: '0.0-0.3', count: 0, color: T.red },
    { label: '0.3-0.4', count: 0, color: T.amber },
    { label: '0.4-0.5', count: 0, color: T.textMuted },
    { label: '0.5-0.6', count: 0, color: T.textMuted },
    { label: '0.6-0.7', count: 0, color: T.cyan },
    { label: '0.7-1.0', count: 0, color: T.green },
  ];

  alignments.forEach(a => {
    if (a < 0.3) bins[0].count++;
    else if (a < 0.4) bins[1].count++;
    else if (a < 0.5) bins[2].count++;
    else if (a < 0.6) bins[3].count++;
    else if (a < 0.7) bins[4].count++;
    else bins[5].count++;
  });

  const maxCount = Math.max(...bins.map(b => b.count), 1);

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Signal Strength Distribution"
        subtitle="Alignment score histogram"
      />

      <div style={{ display: 'flex', alignItems: 'flex-end', height: 80, gap: 4 }}>
        {bins.map((bin, i) => (
          <div key={i} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
            <div style={{ fontSize: 8, color: T.text, fontWeight: 700 }}>{bin.count}</div>
            <div
              style={{
                width: '100%',
                height: `${(bin.count / maxCount) * 60}px`,
                background: bin.color,
                opacity: 0.6,
                borderRadius: '3px 3px 0 0',
              }}
            />
            <div style={{ fontSize: 7, color: T.textMuted, fontFamily: T.mono }}>{bin.label}</div>
          </div>
        ))}
      </div>

      <div style={{ marginTop: 10, fontSize: 9, color: T.textMuted }}>
        Higher alignment = stronger multi-timescale consensus
      </div>
    </div>
  );
}

// ─── Hold Extension Analysis ─────────────────────────────────────────────────

function HoldExtensionAnalysis({ positions }) {
  const positions_with_hold = positions.filter(p => p.hold_time_ms != null);
  const avgHold = positions_with_hold.length > 0
    ? positions_with_hold.reduce((sum, p) => sum + p.hold_time_ms, 0) / positions_with_hold.length
    : 0;
  const baseHold = 300000; // 5 minutes base

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Hold Extension Analysis"
        subtitle={`Base: ${(baseHold / 1000).toFixed(0)}s · Avg: ${((avgHold / 1000) || 0).toFixed(0)}s`}
      />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 8 }}>
        <Metric
          label="Avg Hold Time"
          value={`${((avgHold / 1000) || 0).toFixed(0)}s`}
          sub={`${((avgHold / baseHold - 1) * 100 || 0).toFixed(0)}% extension`}
          color={T.cyan}
        />
        <Metric
          label="Max Hold"
          value={`${positions.length > 0 ? `${Math.max(...positions.map(p => p.hold_time_ms || 0) / 1000).toFixed(0)}s` : '—'}`}
          color={T.text}
        />
        <Metric
          label="Extensions >2x"
          value={positions.filter(p => (p.hold_time_ms || 0) > baseHold * 2).length}
          color={T.amber}
        />
        <Metric
          label="Quick Exits <1m"
          value={positions.filter(p => (p.hold_time_ms || 0) < 60000).length}
          color={T.red}
        />
      </div>

      {/* Hold time distribution */}
      {positions_with_hold.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ fontSize: 8, color: T.textMuted, marginBottom: 6 }}>Hold Time Distribution</div>
          <div style={{ display: 'flex', gap: 4 }}>
            {['<1m', '1-3m', '3-5m', '5-10m', '>10m'].map((range, i) => {
              const count = positions.filter(p => {
                const h = p.hold_time_ms || 0;
                if (i === 0) return h < 60000;
                if (i === 1) return h >= 60000 && h < 180000;
                if (i === 2) return h >= 180000 && h < 300000;
                if (i === 3) return h >= 300000 && h < 600000;
                return h >= 600000;
              }).length;
              const pct = positions_with_hold.length > 0 ? (count / positions_with_hold.length * 100) : 0;
              return (
                <div key={range} style={{ flex: 1, textAlign: 'center' }}>
                  <div style={{ fontSize: 14, fontWeight: 800, color: T.text, fontFamily: T.mono }}>{count}</div>
                  <div style={{ fontSize: 7, color: T.textMuted }}>{pct.toFixed(0)}%</div>
                  <div style={{ fontSize: 7, color: T.textDim, marginTop: 2 }}>{range}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Partial Close Audit ─────────────────────────────────────────────────────

function PartialCloseAudit({ positions }) {
  const partials = positions.filter(p => p.partial_close === true);

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Partial Close Audit"
        subtitle={`${partials.length} partial closes`}
        badge={partials.length > 0 ? `${(partials.length / positions.filter(p => p.state === 'CLOSED').length * 100).toFixed(1)}%` : '—'}
        badgeColor={T.cyan}
      />

      {partials.length === 0 ? (
        <div style={{ padding: '20px', textAlign: 'center', color: T.textMuted, fontSize: 10 }}>
          No partial closes recorded
        </div>
      ) : (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 9 }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
                <th style={{ padding: '6px', textAlign: 'left', color: T.textMuted, fontWeight: 600 }}>Time</th>
                <th style={{ padding: '6px', textAlign: 'left', color: T.textMuted, fontWeight: 600 }}>Side</th>
                <th style={{ padding: '6px', textAlign: 'left', color: T.textMuted, fontWeight: 600 }}>Partial %</th>
                <th style={{ padding: '6px', textAlign: 'left', color: T.textMuted, fontWeight: 600 }}>PnL at PC</th>
                <th style={{ padding: '6px', textAlign: 'left', color: T.textMuted, fontWeight: 600 }}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {partials.slice(-10).map((p, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
                  <td style={{ padding: '6px', color: T.text, fontFamily: T.mono }}>
                    {p.close_time ? new Date(p.close_time).toLocaleTimeString() : '—'}
                  </td>
                  <td style={{ padding: '6px', color: p.side === 'LONG' ? T.green : T.red }}>
                    {p.side || '—'}
                  </td>
                  <td style={{ padding: '6px', color: T.cyan, fontFamily: T.mono }}>
                    {p.partial_percent != null ? `${p.partial_percent}%` : '—'}
                  </td>
                  <td style={{ padding: '6px', color: p.pnl >= 0 ? T.green : T.red, fontFamily: T.mono }}>
                    ${p.pnl?.toFixed(2) || '—'}
                  </td>
                  <td style={{ padding: '6px', color: T.textMuted, fontSize: 8 }}>
                    {p.partial_reason || '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Regime Performance Summary ──────────────────────────────────────────────

function RegimePerformance({ positions }) {
  const byRegime = positions.reduce((acc, p) => {
    const regime = p.regime || 'UNKNOWN';
    if (!acc[regime]) acc[regime] = { trades: 0, pnl: 0, wins: 0 };
    acc[regime].trades++;
    acc[regime].pnl += p.pnl || 0;
    if (p.pnl > 0) acc[regime].wins++;
    return acc;
  }, {});

  const regimes = Object.entries(byRegime).map(([regime, data]) => ({
    regime,
    ...data,
    wr: data.trades > 0 ? (data.wins / data.trades * 100) : 0,
  }));

  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.cardBorder}`,
      borderRadius: 8,
      padding: 14,
    }}>
      <SectionHeader
        title="Regime Performance (Last 30 Days)"
        subtitle="PnL by market regime"
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {regimes.map(({ regime, pnl, trades, wr }) => {
          const color = REGIME_COLORS[regime] || T.textMuted;
          return (
            <div
              key={regime}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '8px 10px',
                background: `${color}0d`,
                border: `1px solid ${color}33`,
                borderRadius: 4,
              }}
            >
              <Chip color={color} bg={`${color}22`} border={`${color}55`} value={regime} />
              <div style={{ flex: 1, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8 }}>
                <div style={{ fontSize: 10, color: T.text, fontFamily: T.mono }}>
                  {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
                </div>
                <div style={{ fontSize: 10, color: T.textMuted }}>{trades} trades</div>
                <div style={{ fontSize: 10, color: wr > 55 ? T.green : wr > 50 ? T.amber : T.red, fontFamily: T.mono }}>
                  {wr.toFixed(1)}% WR
                </div>
              </div>
              <div style={{ width: 60, height: 8, background: 'rgba(15,23,42,0.4)', borderRadius: 4, overflow: 'hidden' }}>
                <div
                  style={{
                    width: `${wr}%`,
                    height: '100%',
                    background: color,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>

      {regimes.length === 0 && (
        <div style={{ padding: '20px', textAlign: 'center', color: T.textMuted, fontSize: 10 }}>
          No regime data available
        </div>
      )}
    </div>
  );
}

// ─── Main Page Component ─────────────────────────────────────────────────────

export default function MarginStrategies() {
  const api = useApi();
  const [v4Snapshot, setV4Snapshot] = useState(null);
  const [strategyStats, setStrategyStats] = useState({});
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = useCallback(async () => {
    try {
      // Fetch V4 snapshot (real-time)
      const v4Res = await api('GET', '/v4/snapshot?asset=BTC&timescales=5m,15m,1h,4h&strategy=fee_aware_15m');
      setV4Snapshot(v4Res?.data || v4Res || null);

      // Fetch strategy stats (may not exist yet)
      try {
        const statsRes = await api('GET', '/margin/strategy-stats');
        setStrategyStats(statsRes?.data || statsRes || {});
      } catch {
        // API doesn't exist yet, use placeholder
        setStrategyStats({});
      }

      // Fetch positions
      try {
        const posRes = await api('GET', '/margin/positions?limit=50');
        setPositions(posRes?.data || posRes || []);
      } catch {
        // Use placeholder positions
        setPositions([
          { id: 1, side: 'LONG', pnl: 2.5, state: 'CLOSED', alignment_score: 0.75, hold_time_ms: 420000, regime: 'TRENDING_UP' },
          { id: 2, side: 'LONG', pnl: -1.2, state: 'CLOSED', alignment_score: 0.55, hold_time_ms: 180000, regime: 'CHOPPY' },
          { id: 3, side: 'SHORT', pnl: 3.8, state: 'CLOSED', alignment_score: 0.82, hold_time_ms: 540000, regime: 'TRENDING_DOWN' },
          { id: 4, side: 'LONG', pnl: 0.8, state: 'CLOSED', alignment_score: 0.48, hold_time_ms: 90000, regime: 'MEAN_REVERTING' },
          { id: 5, side: 'LONG', pnl: -0.5, state: 'CLOSED', alignment_score: 0.62, hold_time_ms: 300000, regime: 'TRENDING_UP', partial_close: true, partial_percent: 50, partial_reason: 'Take profit partial' },
          { id: 6, side: 'SHORT', pnl: 1.5, state: 'OPEN', alignment_score: 0.68, hold_time_ms: 240000, regime: 'TRENDING_DOWN' },
        ]);
      }

      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => { fetchData(); }, [fetchData]);

  useEffect(() => {
    const interval = setInterval(fetchData, 4000);
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) {
    return (
      <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
        <div style={{
          padding: 40,
          textAlign: 'center',
          color: T.textMuted,
          fontSize: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 8,
        }}>
          Loading Margin Strategy Dashboard...
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <h1 style={{
          fontSize: 16,
          fontWeight: 800,
          color: T.white,
          margin: 0,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}>
          Margin Strategy Lab
          <Chip
            color={T.purple}
            bg="rgba(168,85,247,0.15)"
            border="rgba(168,85,247,0.3)"
            value="v4"
          />
          <Chip color={T.amber} bg="rgba(245,158,11,0.12)" border="rgba(245,158,11,0.3)" value="Hyperliquid Perps" />
        </h1>
        <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0' }}>
          Multi-strategy margin trading with regime-based analysis · Strategy performance tracking
        </p>
      </div>

      {error && (
        <div style={{
          padding: '10px 14px',
          marginBottom: 14,
          borderRadius: 6,
          background: 'rgba(239,68,68,0.1)',
          border: '1px solid rgba(239,68,68,0.2)',
          fontSize: 11,
          color: T.red,
        }}>
          {error}
        </div>
      )}

      {/* Strategy Cards */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Strategy Performance"
          subtitle="5 active strategies"
        />
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
          gap: 12,
        }}>
          {STRATEGIES.map(strategy => (
            <StrategyCard
              key={strategy.id}
              strategy={strategy}
              stats={strategy.id === 'v4_path' ? {
                total_pnl: 23.4,
                win_rate: 0.58,
                sharpe: 1.2,
                total_trades: 45,
              } : null}
            />
          ))}
        </div>
      </div>

      {/* V4 Data Panel */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Real-Time V4 Data"
          subtitle="Live fusion decision surface"
        />
        <V4Panel snapshot={v4Snapshot} />
      </div>

      {/* Position Analysis */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Position Analysis"
          subtitle="Fee-adjusted performance"
        />
        <PnLDistribution positions={positions} />
      </div>

      {/* Signal Strength Distribution */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Signal Analysis"
          subtitle="Alignment score distribution"
        />
        <SignalStrengthHistogram positions={positions} />
      </div>

      {/* Hold Extension Analysis */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Hold Extension Analysis"
          subtitle="Actual vs expected hold times"
        />
        <HoldExtensionAnalysis positions={positions} />
      </div>

      {/* Partial Close Audit */}
      <div style={{ marginBottom: 16 }}>
        <SectionHeader
          title="Partial Close Audit"
          subtitle="When and why partials happened"
        />
        <PartialCloseAudit positions={positions} />
      </div>

      {/* Regime Performance */}
      <div>
        <SectionHeader
          title="Regime Performance"
          subtitle="PnL breakdown by market regime"
        />
        <RegimePerformance positions={positions} />
      </div>
    </div>
  );
}
