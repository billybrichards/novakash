import React, { useMemo, useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import { useWebSocket } from '../hooks/useWebSocket.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import Loading from '../components/shared/Loading.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import { T, wrColor } from '../theme/tokens.js';
import { computeWr as wrOf } from '../lib/wr.js';

const TIMEFRAMES = [
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
];
const CONVICTIONS = [
  { label: 'any', value: null },
  { label: 'STRONG', value: 'STRONG' },
  { label: 'MODERATE', value: 'MODERATE' },
  { label: 'WEAK', value: 'WEAK' },
];

export default function SignalExplorer() {
  const api = useApi();
  const [tf, setTf] = useState('5m');
  const [conv, setConv] = useState(null);
  const [liveRows, setLiveRows] = useState([]);

  const { data, error: err, loading } = useApiLoader(
    (signal) => {
      const params = new URLSearchParams({ limit: '1000', timeframe: tf });
      if (conv) params.set('conviction', conv);
      return api.get(`/api/v58/strategy-decisions?${params.toString()}`, { signal });
    },
    [tf, conv]
  );

  // Seed local state from API snapshot; reset on filter change
  useEffect(() => {
    setLiveRows(Array.isArray(data) ? data : []);
  }, [data]);

  // WebSocket: receive real-time signal events and prepend to heatmap data
  const { isConnected, data: wsMsg } = useWebSocket('/ws/feed');
  useEffect(() => {
    if (wsMsg?.type === 'signal' && wsMsg.payload) {
      setLiveRows(prev => [wsMsg.payload, ...prev]);
    }
  }, [wsMsg]);

  const rows = liveRows;

  const matrix = useMemo(() => {
    const by = {};
    for (const r of rows) {
      const s = r.strategy_id || r.strategy || 'unknown';
      const rg = r.regime || 'unknown';
      by[s] ??= {};
      by[s][rg] ??= [];
      by[s][rg].push(r);
    }
    const strategies = Object.keys(by).sort();
    const regimes = Array.from(new Set(rows.map(r => r.regime || 'unknown'))).sort();
    const cells = {};
    for (const s of strategies) {
      cells[s] = {};
      for (const rg of regimes) {
        cells[s][rg] = wrOf(by[s]?.[rg] ?? []);
      }
      cells[s].__total = wrOf(Object.values(by[s] ?? {}).flat());
    }
    return { strategies, regimes, cells };
  }, [rows]);

  return (
    <div>
      <PageHeader
        tag="SIGNALS · /signals"
        title="Signal Explorer"
        subtitle="Strategy × regime × conviction win-rate slicer over /api/v58/strategy-decisions."
        right={<div style={{ fontSize: 11, color: T.label2, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: isConnected ? T.profit : T.label,
              display: 'inline-block',
            }} />
            {isConnected ? 'LIVE' : 'poll'}
          </span>
          {rows.length} decisions · {matrix.strategies.length} strategies · {matrix.regimes.length} regimes
        </div>}
      />

      <div style={{ display: 'flex', gap: 18, marginBottom: 12, flexWrap: 'wrap' }}>
        <FilterPills label="timeframe" options={TIMEFRAMES} value={tf} onChange={setTf} />
        <FilterPills label="conviction" options={CONVICTIONS} value={conv} onChange={setConv} />
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 14 }}>
        <div style={{ fontSize: 13, marginBottom: 10 }}>Win-rate matrix · strategy × regime</div>
        {matrix.strategies.length === 0 ? (
          loading ? <Loading /> : <EmptyState message="No decisions match these filters." />
        ) : (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
                <th style={{ textAlign: 'left', padding: '6px 10px' }}>STRATEGY</th>
                {matrix.regimes.map(rg => <th key={rg} style={{ textAlign: 'right', padding: '6px 10px' }}>{rg.toUpperCase()}</th>)}
                <th style={{ textAlign: 'right', padding: '6px 10px' }}>TOTAL</th>
              </tr>
            </thead>
            <tbody>
              {matrix.strategies.map(s => (
                <tr key={s} style={{ borderTop: `1px solid ${T.border}` }}>
                  <td style={{ padding: '6px 10px' }}>{s}</td>
                  {matrix.regimes.map(rg => {
                    const c = matrix.cells[s][rg];
                    if (!c || c.n === 0) return <td key={rg} style={{ textAlign: 'right', padding: '6px 10px', color: T.label }}>—</td>;
                    const color = wrColor(c.wr);
                    const pendingTag = c.pending ? <span style={{ color: T.label, fontSize: 10 }}> · {c.pending}p</span> : null;
                    return <td key={rg} style={{ textAlign: 'right', padding: '6px 10px', color, fontVariantNumeric: 'tabular-nums' }}>
                      {(c.wr * 100).toFixed(1)}% <span style={{ color: T.label, fontSize: 10 }}>({c.n})</span>{pendingTag}
                    </td>;
                  })}
                  <td style={{ textAlign: 'right', padding: '6px 10px', color: wrColor(matrix.cells[s].__total.wr), fontVariantNumeric: 'tabular-nums' }}>
                    {matrix.cells[s].__total.wr == null ? '—' : `${(matrix.cells[s].__total.wr * 100).toFixed(1)}%`}
                    <span style={{ color: T.label, fontSize: 10 }}> ({matrix.cells[s].__total.n})</span>
                    {matrix.cells[s].__total.pending ? <span style={{ color: T.label, fontSize: 10 }}> · {matrix.cells[s].__total.pending}p</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        <div style={{ fontSize: 13, marginBottom: 8 }}>Recent decisions · {Math.min(rows.length, 500)} of {rows.length}</div>
        <DataTable
          emptyText="No decisions match these filters."
          columns={[
            { key: 'ts', label: 'time', render: r => <span style={{ color: T.label2 }}>{(r.decided_at || r.ts || '').toString().slice(11, 19)}</span> },
            { key: 'strategy_id', label: 'strategy' },
            { key: 'regime', label: 'regime' },
            { key: 'conviction', label: 'conviction' },
            { key: 'distance', label: 'distance ($)', num: true, render: r => r.distance_usd != null ? Number(r.distance_usd).toFixed(0) : '—' },
            { key: 'min_dist', label: 'min_dist', num: true, render: r => r.min_distance != null ? Number(r.min_distance).toFixed(2) : '—' },
            { key: 'outcome', label: 'outcome', render: r => {
              const v = r.outcome || (r.won === true ? 'WIN' : r.won === false ? 'LOSS' : null);
              if (!v) return <span style={{ color: T.label }}>pending</span>;
              return <span style={{ color: v === 'WIN' ? T.profit : T.loss, fontSize: 10 }}>{v}</span>;
            }},
            { key: 'skip_reason', label: 'skip', render: r => r.skip_reason ? <span style={{ color: T.warn, fontSize: 10 }}>{r.skip_reason}</span> : '' },
          ]}
          rows={rows.slice(0, 500).map((r, i) => ({ ...r, _key: r.id ?? i }))}
        />
      </div>
    </div>
  );
}
