import React, { useMemo, useState } from 'react';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import Loading from '../components/shared/Loading.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import { T } from '../theme/tokens.js';
import {
  ComposedChart, AreaChart, Area, BarChart, Bar,
  Line, XAxis, YAxis, Tooltip, CartesianGrid,
  ResponsiveContainer, Cell, ReferenceArea, ReferenceLine,
} from 'recharts';
import { formatUSD, formatPercent } from '../lib/utils.js';

const RANGES = [
  { label: '30d', value: 30 },
  { label: '90d', value: 90 },
  { label: 'all', value: null },
];

const DAY_MS = 24 * 60 * 60 * 1000;
const SHARPE_WINDOW = 20; // rolling window in trading days

const Stat = ({ lbl, val, sub, tone }) => {
  const color = tone === 'good' ? T.profit : tone === 'bad' ? T.loss : tone === 'warn' ? T.warn : undefined;
  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
      <div style={{ fontSize: 10, letterSpacing: '0.12em', color: T.label, textTransform: 'uppercase' }}>{lbl}</div>
      <div style={{ fontSize: 20, marginTop: 6, fontVariantNumeric: 'tabular-nums', color }}>{val}</div>
      {sub ? <div style={{ fontSize: 11, color: T.label2, marginTop: 3 }}>{sub}</div> : null}
    </div>
  );
};

const pnlTone = (v) => {
  if (v == null) return undefined;
  return v >= 0 ? 'good' : 'bad';
};

const Card = ({ title, children }) => (
  <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 12 }}>
    {title && (
      <div style={{ fontSize: 12, color: T.label2, marginBottom: 10, letterSpacing: '0.08em' }}>{title}</div>
    )}
    {children}
  </div>
);

const TOOLTIP_STYLE = { background: '#0f0f14', border: `1px solid ${T.border}`, fontSize: 11 };

/** Compute rolling annualised Sharpe from an ordered daily returns array. */
function computeRollingSharpe(dailyArr, window = SHARPE_WINDOW) {
  const result = {};
  for (let i = window - 1; i < dailyArr.length; i++) {
    const slice = dailyArr.slice(i - window + 1, i + 1).map(d => d.net_pnl ?? 0);
    const mean = slice.reduce((a, b) => a + b, 0) / window;
    const variance = slice.reduce((s, v) => s + (v - mean) ** 2, 0) / window;
    const std = Math.sqrt(variance);
    if (std > 0) {
      result[dailyArr[i].date] = +(mean / std * Math.sqrt(252)).toFixed(2);
    }
  }
  return result;
}

/** Find the max-drawdown start/end dates from a cumulative P&L series. */
function findMaxDrawdownPeriod(cumulativeArr, dateKey) {
  if (cumulativeArr.length < 2) return { ddStart: null, ddEnd: null };
  let peak = cumulativeArr[0].cumulative_pnl;
  let peakDateVal = cumulativeArr[0][dateKey];
  let maxDD = 0;
  let ddStart = null;
  let ddEnd = null;

  for (const d of cumulativeArr) {
    const val = d.cumulative_pnl;
    if (val > peak) {
      peak = val;
      peakDateVal = d[dateKey];
    }
    const dd = peak === 0 ? 0 : (val - peak) / Math.abs(peak);
    if (dd < maxDD) {
      maxDD = dd;
      ddStart = peakDateVal;
      ddEnd = d[dateKey];
    }
  }
  return { ddStart, ddEnd };
}

export default function PnL() {
  const [rangeDays, setRangeDays] = useState(30);

  const cumulative = useApiLoader(
    (signal, api) => api.get('/api/pnl/cumulative', { signal })
  );
  const daily = useApiLoader(
    (signal, api) => api.get('/api/pnl/daily', { signal })
  );
  const monthly = useApiLoader(
    (signal, api) => api.get('/api/pnl/monthly', { signal })
  );
  const byStrategy = useApiLoader(
    (signal, api) => api.get('/api/pnl/by-strategy', { signal })
  );

  const filteredDaily = useMemo(() => {
    const items = Array.isArray(daily.data) ? daily.data : [];
    if (!rangeDays) return items;
    const cutoff = Date.now() - rangeDays * DAY_MS;
    return items.filter(d => {
      const ts = new Date(d.date).getTime();
      return Number.isFinite(ts) && ts >= cutoff;
    });
  }, [daily.data, rangeDays]);

  const filteredCumulative = useMemo(() => {
    const items = Array.isArray(cumulative.data) ? cumulative.data : [];
    if (!rangeDays) return items;
    const cutoff = Date.now() - rangeDays * DAY_MS;
    return items.filter(d => {
      const ts = new Date(d.date || d.timestamp).getTime();
      return Number.isFinite(ts) && ts >= cutoff;
    });
  }, [cumulative.data, rangeDays]);

  // Merge rolling Sharpe into the cumulative series for dual-axis overlay
  const enrichedCumulative = useMemo(() => {
    if (!filteredCumulative.length) return [];
    const dailyArr = Array.isArray(daily.data) ? daily.data : [];
    const sharpeByDate = computeRollingSharpe(dailyArr);
    const dateKey = filteredCumulative[0].date != null ? 'date' : 'timestamp';
    return filteredCumulative.map(d => ({
      ...d,
      rolling_sharpe: sharpeByDate[d[dateKey]] ?? undefined,
    }));
  }, [filteredCumulative, daily.data]);

  const dateKey = enrichedCumulative.length > 0
    ? (enrichedCumulative[0].date != null ? 'date' : 'timestamp')
    : 'date';

  // Max-drawdown period for ReferenceArea highlight
  const { ddStart, ddEnd } = useMemo(
    () => findMaxDrawdownPeriod(enrichedCumulative, dateKey),
    [enrichedCumulative, dateKey]
  );

  const stats = byStrategy.data && typeof byStrategy.data === 'object' && !Array.isArray(byStrategy.data)
    ? byStrategy.data
    : null;

  // Per-strategy breakdown for comparison chart
  const strategyBreakdown = useMemo(() => {
    if (!stats) return [];
    return [
      { name: 'Sub-$1 Arb', pnl: stats.arb_pnl ?? 0 },
      { name: 'VPIN Cascade', pnl: stats.vpin_pnl ?? 0 },
    ];
  }, [stats]);

  const loading = cumulative.loading && daily.loading && monthly.loading && byStrategy.loading;
  const anyError = cumulative.error || daily.error || monthly.error || byStrategy.error;

  const monthlyColumns = [
    { key: 'month', label: 'Month' },
    { key: 'trade_count', label: 'Trades', num: true },
    {
      key: 'win_rate', label: 'Win Rate', num: true,
      render: r => (
        <span style={{ color: r.win_rate >= 0.55 ? T.profit : T.loss, fontVariantNumeric: 'tabular-nums' }}>
          {formatPercent(r.win_rate)}
        </span>
      ),
    },
    {
      key: 'gross_pnl', label: 'Gross P&L', num: true,
      render: r => <span style={{ fontVariantNumeric: 'tabular-nums' }}>{formatUSD(r.gross_pnl)}</span>,
    },
    {
      key: 'fees_paid', label: 'Fees', num: true,
      render: r => <span style={{ color: T.loss, fontVariantNumeric: 'tabular-nums' }}>{formatUSD(r.fees_paid)}</span>,
    },
    {
      key: 'net_pnl', label: 'Net P&L', num: true,
      render: r => (
        <span style={{ color: r.net_pnl >= 0 ? T.profit : T.loss, fontWeight: 500, fontVariantNumeric: 'tabular-nums' }}>
          {formatUSD(r.net_pnl)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageHeader
        tag="P&L · /pnl"
        title="Profit & Loss"
        subtitle="Equity curve, daily returns, and monthly summaries."
        right={
          <FilterPills label="Range" options={RANGES} value={rangeDays} onChange={setRangeDays} />
        }
      />

      {anyError && (
        <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>
          {cumulative.error || daily.error || monthly.error || byStrategy.error}
        </div>
      )}

      {loading ? (
        <Loading label="Loading P&L data..." />
      ) : (
        <>
          {/* Key Stats */}
          {stats && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 8, marginBottom: 12 }}>
              <Stat lbl="Total P&L" val={formatUSD(stats.total_pnl)} tone={pnlTone(stats.total_pnl)} />
              <Stat lbl="Sharpe" val={stats.sharpe_ratio != null ? stats.sharpe_ratio.toFixed(2) : '\u2014'} />
              <Stat lbl="Max Drawdown" val={formatPercent(stats.max_drawdown)} tone={stats.max_drawdown < -0.1 ? 'bad' : undefined} />
              <Stat lbl="Win Rate" val={formatPercent(stats.win_rate)} />
              <Stat lbl="Arb P&L" val={formatUSD(stats.arb_pnl)} tone={pnlTone(stats.arb_pnl)} />
              <Stat lbl="VPIN P&L" val={formatUSD(stats.vpin_pnl)} tone={pnlTone(stats.vpin_pnl)} />
            </div>
          )}

          {/* Cumulative Equity — with rolling Sharpe overlay + max-drawdown highlight */}
          <Card title="Cumulative Equity">
            {enrichedCumulative.length === 0 ? (
              <EmptyState message="No cumulative P&L data." hint="Check /api/pnl/cumulative endpoint." />
            ) : (
              <>
                <div style={{ fontSize: 10, color: T.label, marginBottom: 6, display: 'flex', gap: 16 }}>
                  <span style={{ color: T.profit }}>━━ Equity</span>
                  <span style={{ color: T.cyan }}>━━ Rolling Sharpe (20d, right axis)</span>
                  {ddStart && <span style={{ color: T.warn }}>░ Max Drawdown Period</span>}
                </div>
                <div style={{ height: 300 }} data-testid="cumulative-chart-wrapper">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={enrichedCumulative} margin={{ top: 5, right: 50, left: 0, bottom: 5 }}>
                      <CartesianGrid stroke={T.grid} strokeDasharray="0" />
                      <XAxis
                        dataKey={dateKey}
                        tick={{ fill: T.label, fontSize: 10 }}
                        tickFormatter={v => v ? String(v).slice(5, 10) : ''}
                      />
                      {/* Left Y-axis: cumulative P&L */}
                      <YAxis
                        yAxisId="pnl"
                        tick={{ fill: T.label, fontSize: 10 }}
                        tickFormatter={v => `$${v}`}
                      />
                      {/* Right Y-axis: rolling Sharpe */}
                      <YAxis
                        yAxisId="sharpe"
                        orientation="right"
                        tick={{ fill: T.cyan, fontSize: 10 }}
                        tickFormatter={v => v.toFixed(1)}
                        label={{ value: 'Sharpe', angle: 90, position: 'insideRight', fill: T.cyan, fontSize: 10 }}
                      />
                      <Tooltip
                        contentStyle={TOOLTIP_STYLE}
                        labelStyle={{ color: T.label2 }}
                        formatter={(v, name) =>
                          name === 'rolling_sharpe'
                            ? [v != null ? v.toFixed(2) : '—', 'Rolling Sharpe']
                            : [formatUSD(v), 'Equity']
                        }
                      />
                      {/* Max-drawdown highlight */}
                      {ddStart && ddEnd && (
                        <ReferenceArea
                          yAxisId="pnl"
                          x1={ddStart}
                          x2={ddEnd}
                          fill={T.loss}
                          fillOpacity={0.08}
                          stroke={T.loss}
                          strokeOpacity={0.3}
                          strokeWidth={1}
                          label={{ value: 'Max DD', fill: T.loss, fontSize: 9, position: 'insideTopLeft' }}
                        />
                      )}
                      <ReferenceLine yAxisId="pnl" y={0} stroke={T.border} strokeWidth={1} />
                      <Area
                        yAxisId="pnl"
                        type="monotone"
                        dataKey="cumulative_pnl"
                        fill={T.profit}
                        fillOpacity={0.08}
                        stroke={T.profit}
                        strokeWidth={1.5}
                        isAnimationActive={false}
                      />
                      <Line
                        yAxisId="sharpe"
                        type="monotone"
                        dataKey="rolling_sharpe"
                        stroke={T.cyan}
                        strokeWidth={1.5}
                        dot={false}
                        connectNulls={false}
                        isAnimationActive={false}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </>
            )}
          </Card>

          {/* Per-Strategy Breakdown */}
          {strategyBreakdown.length > 0 && (
            <Card title="Per-Strategy P&L Breakdown">
              <div style={{ height: 180 }} data-testid="strategy-breakdown-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={strategyBreakdown} margin={{ top: 5, right: 20, left: 0, bottom: 5 }}>
                    <CartesianGrid stroke={T.grid} strokeDasharray="0" />
                    <XAxis dataKey="name" tick={{ fill: T.label, fontSize: 11 }} />
                    <YAxis tick={{ fill: T.label, fontSize: 10 }} tickFormatter={v => `$${v}`} />
                    <Tooltip
                      contentStyle={TOOLTIP_STYLE}
                      labelStyle={{ color: T.label2 }}
                      formatter={v => [formatUSD(v), 'Net P&L']}
                    />
                    <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
                      {strategyBreakdown.map((entry, i) => (
                        <Cell key={i} fill={entry.pnl >= 0 ? T.profit : T.loss} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </Card>
          )}

          {/* Daily P&L */}
          <Card title="Daily P&L">
            {filteredDaily.length === 0 ? (
              <EmptyState message="No daily P&L data." hint="Check /api/pnl/daily endpoint." />
            ) : (
              <div style={{ height: 250 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={filteredDaily}>
                    <CartesianGrid stroke={T.grid} strokeDasharray="0" />
                    <XAxis
                      dataKey="date"
                      tick={{ fill: T.label, fontSize: 10 }}
                      tickFormatter={v => v ? v.slice(5, 10) : ''}
                    />
                    <YAxis tick={{ fill: T.label, fontSize: 10 }} tickFormatter={v => `$${v}`} />
                    <Tooltip
                      contentStyle={TOOLTIP_STYLE}
                      labelStyle={{ color: T.label2 }}
                      formatter={v => formatUSD(v)}
                    />
                    <Bar dataKey="net_pnl" radius={[2, 2, 0, 0]}>
                      {filteredDaily.map((entry, i) => (
                        <Cell key={i} fill={entry.net_pnl >= 0 ? T.profit : T.loss} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>

          {/* Monthly Summary */}
          <Card title="Monthly Summary">
            {Array.isArray(monthly.data) && monthly.data.length > 0 ? (
              <DataTable columns={monthlyColumns} rows={monthly.data} emptyText="No monthly data." />
            ) : monthly.loading ? (
              <Loading label="Loading monthly data..." />
            ) : (
              <EmptyState message="No monthly summary data." hint="Check /api/pnl/monthly endpoint." />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
