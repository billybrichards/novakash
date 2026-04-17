import React, { useEffect, useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import EquityCurve from '../components/EquityCurve.jsx';
import { T } from '../theme/tokens.js';

const TIMEFRAMES = [
  { label: 'all', value: null },
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
];

function Stat({ lbl, val, sub, tone }) {
  const color = tone === 'good' ? T.profit : tone === 'bad' ? T.loss : tone === 'warn' ? T.warn : undefined;
  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 2, padding: 14 }}>
      <div style={{ fontSize: 10, letterSpacing: '0.12em', color: T.label, textTransform: 'uppercase' }}>{lbl}</div>
      <div style={{ fontSize: 20, marginTop: 6, color }}>{val}</div>
      {sub ? <div style={{ fontSize: 11, color: T.label2, marginTop: 3 }}>{sub}</div> : null}
    </div>
  );
}

function StatusChip({ label, ok, warn }) {
  const color = ok ? T.profit : warn ? T.warn : T.loss;
  return (
    <span style={{
      fontSize: 10,
      padding: '2px 8px',
      borderRadius: 2,
      border: `1px solid ${color}55`,
      color,
      marginRight: 8,
    }}>{label}</span>
  );
}

const fmtUSD = n => (n == null ? '—' : `${n < 0 ? '-' : '+'}$${Math.abs(Number(n)).toFixed(2)}`);
const fmtPct = n => (n == null ? '—' : `${(Number(n) * 100).toFixed(1)}%`);

export default function UnifiedDashboard() {
  const api = useApi();
  const [tf, setTf] = useState(null);

  const dashLoader = useApiLoader(
    (s) => api.get(`/api/dashboard${tf ? `?timeframe=${tf}` : ''}`, { signal: s }),
    [tf]
  );
  const statsLoader = useApiLoader((s) => api.get('/api/trades/stats', { signal: s }));
  const sysLoader = useApiLoader((s) => api.get('/api/system/status', { signal: s }));

  useEffect(() => {
    const t = setInterval(() => {
      dashLoader.reload();
      statsLoader.reload();
      sysLoader.reload();
    }, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dash = dashLoader.data;
  const stats = statsLoader.data;
  const sys = sysLoader.data;
  const err = dashLoader.error || statsLoader.error || sysLoader.error;

  // Backend may ignore the timeframe param. Client-filter open_positions by
  // timeframe as a fallback so at least the positions table is accurate.
  const rawPositions = dash?.open_positions || [];
  const positions = useMemo(() => {
    if (!tf) return rawPositions;
    return rawPositions.filter(p => p.timeframe === tf);
  }, [rawPositions, tf]);
  // Backend honored the filter iff every position already matches (or has no tf tag at all).
  const backendHonoredTf = tf == null || rawPositions.every(p => !p.timeframe || p.timeframe === tf);
  const equity = dash?.equity_curve || [];

  return (
    <div>
      <PageHeader
        tag="DASHBOARD · /"
        title="Dashboard"
        subtitle="Unified operator view · replaces Paper · Playwright · Execution HQ · Live Trading"
        right={
          <div style={{ fontSize: 11, color: T.label2 }}>
            {/* /api/system/status returns { status, data:{status, bankroll}, updated_at }.
                Derive chips from that real shape; fall back to muted unknown when missing. */}
            {sys ? (() => {
              const hubOk = sys.status === 'online';
              const engineStatus = sys?.data?.status;
              const engineOk = engineStatus === 'active';
              const engineLabel = engineStatus
                ? `ENGINE · ${engineStatus.toUpperCase()}`
                : 'ENGINE · UNKNOWN';
              return (
                <>
                  <StatusChip label={`HUB · ${hubOk ? 'OK' : 'DOWN'}`} ok={hubOk} warn={!hubOk} />
                  <StatusChip label={engineLabel} ok={engineOk} warn={!engineOk} />
                  {sys?.data?.bankroll != null ? (
                    <StatusChip label={`BANKROLL · $${Number(sys.data.bankroll).toFixed(2)}`} ok />
                  ) : null}
                  {sys?.updated_at ? (
                    <span style={{ color: T.label, marginLeft: 8 }}>
                      updated {(sys.updated_at || '').toString().slice(11, 19)}
                    </span>
                  ) : null}
                </>
              );
            })() : (
              <StatusChip label="STATUS · LOADING" warn />
            )}
          </div>
        }
      />

      <div style={{ display: 'flex', gap: 18, marginBottom: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <FilterPills label="timeframe" options={TIMEFRAMES} value={tf} onChange={setTf} />
        {tf && !backendHonoredTf ? (
          <span style={{ fontSize: 10, color: T.label }}>
            (backend ignored filter · positions filtered client-side · KPIs are all-timeframe)
          </span>
        ) : null}
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
        <Stat lbl="Net today" val={fmtUSD(dash?.pnl_today)} sub={dash?.trades_today ? `${dash.trades_today} trades` : null} tone={dash?.pnl_today == null ? undefined : (dash.pnl_today >= 0 ? 'good' : 'bad')} />
        <Stat lbl="Win rate · 7d" val={fmtPct(stats?.win_rate_7d)} sub={stats?.trades_7d ? `${stats.win_count_7d}/${stats.trades_7d}` : null} tone="good" />
        <Stat lbl="Open exposure" val={dash?.open_exposure != null ? `$${dash.open_exposure.toFixed(0)}` : '—'} sub={dash?.exposure_pct != null ? fmtPct(dash.exposure_pct) + ' of bankroll' : null} />
        <Stat lbl="Drawdown" val={fmtPct(dash?.drawdown)} sub="kill · 45%" tone={dash?.drawdown == null ? undefined : (dash.drawdown < -0.2 ? 'warn' : undefined)} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12, marginBottom: 14 }}>
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <div style={{ fontSize: 13, marginBottom: 8 }}>Equity · 30d</div>
          {equity.length > 0
            ? <EquityCurve data={equity} />
            : <EmptyState message="No equity curve yet." hint="First closed trade will populate." />}
        </div>

        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <div style={{ fontSize: 13, marginBottom: 8 }}>Active alerts</div>
          {(dash?.alerts?.length ?? 0) === 0
            ? <EmptyState message="No active alerts." />
            : dash.alerts.map((a, i) => (
                <div key={i} style={{ fontSize: 11, color: a.level === 'warn' ? T.warn : T.label2, padding: '3px 0' }}>
                  {a.message}
                </div>
              ))}
        </div>
      </div>

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        <div style={{ fontSize: 13, marginBottom: 8 }}>Open positions · {positions.length}</div>
        <DataTable
          emptyText="No open positions."
          columns={[
            { key: 'market', label: 'market' },
            { key: 'outcome', label: 'side' },
            { key: 'strategy', label: 'strategy' },
            { key: 'size', label: 'size', num: true, render: r => `$${Number(r.size ?? 0).toFixed(0)}` },
            { key: 'avg', label: 'avg', num: true, render: r => Number(r.avg_price ?? r.avg ?? 0).toFixed(2) },
            { key: 'mark', label: 'mark', num: true, render: r => Number(r.mark_price ?? r.mark ?? 0).toFixed(2) },
            { key: 'upnl', label: 'uPnL', num: true, render: r => {
              const v = r.unrealized_pnl ?? r.upnl ?? 0;
              return <span style={{ color: v >= 0 ? T.profit : T.loss }}>{fmtUSD(v)}</span>;
            }},
          ]}
          rows={positions.map((p, i) => ({ ...p, _key: p.id ?? i }))}
        />
      </div>
    </div>
  );
}
