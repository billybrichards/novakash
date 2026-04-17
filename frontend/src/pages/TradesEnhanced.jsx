import React, { useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import { T, wrTone } from '../theme/tokens.js';

// Shorten strategy id for pill display: v4_down_only → v4/down_only, v10_15m → v10/15m.
const prettyStrategy = s => s.replace(/^v(\d+)_/, 'v$1/');
const OUTCOMES = [
  { label: 'all', value: null },
  { label: 'wins', value: 'WIN' },
  { label: 'losses', value: 'LOSS' },
  { label: 'open', value: 'OPEN' },
];
const RANGES = [
  { label: '24h', value: 1 },
  { label: '7d', value: 7 },
  { label: '30d', value: 30 },
];
// `filled` is the default — hides pre-#211 orphan rows (NULL fill_price AND
// NULL entry_price) that carry a stake/outcome but never actually filled on
// the CLOB. `all` includes them.
const FILL_MODES = [
  { label: 'filled', value: true },
  { label: 'all', value: false },
];

const Stat = ({ lbl, val, sub, tone }) => {
  const color = tone === 'good' ? T.profit : tone === 'bad' ? T.loss : undefined;
  return (
    <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
      <div style={{ fontSize: 10, letterSpacing: '0.12em', color: T.label, textTransform: 'uppercase' }}>{lbl}</div>
      <div style={{ fontSize: 20, marginTop: 6, color }}>{val}</div>
      {sub ? <div style={{ fontSize: 11, color: T.label2, marginTop: 3 }}>{sub}</div> : null}
    </div>
  );
};

const fmtUSD = n => {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return `${v < 0 ? '-' : '+'}$${Math.abs(v).toFixed(2)}`;
};

export default function TradesEnhanced() {
  const api = useApi();
  const [strategy, setStrategy] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [rangeDays, setRangeDays] = useState(7);
  const [onlyFilled, setOnlyFilled] = useState(true);

  const { data, error: err } = useApiLoader(
    (signal) => {
      const params = new URLSearchParams({ limit: '500' });
      if (strategy) params.set('strategy', strategy);
      if (outcome) params.set('outcome', outcome);
      params.set('since_days', String(rangeDays));
      params.set('only_filled', String(onlyFilled));
      return api.get(`/api/trades?${params.toString()}`, { signal });
    },
    [strategy, outcome, rangeDays, onlyFilled]
  );

  const rows = Array.isArray(data) ? data : [];

  // Derive strategy pills from loaded rows so 15m strategies appear automatically.
  // Preserve the currently-selected value even if it's absent from the current page.
  const strategyOptions = useMemo(() => {
    const set = new Set(rows.map(r => r.strategy || r.strategy_id).filter(Boolean));
    if (strategy) set.add(strategy);
    const opts = [{ label: 'all', value: null }];
    for (const s of Array.from(set).sort()) {
      opts.push({ label: prettyStrategy(s), value: s });
    }
    return opts;
  }, [rows, strategy]);

  // Hub /api/trades row fields: pnl_usd, stake_usd, entry_price, fill_price,
  // market_slug, direction, created_at, order_id, clob_order_id, regime,
  // conviction, dedup_key, skip_reason, exit_price, polymarket_confirmed_*.
  // Helpers pick the real field with a fallback to older aliases.
  const pnlOf = r => Number(r.pnl_usd ?? r.pnl);
  const sizeOf = r => Number(r.stake_usd ?? r.size);
  const edgeOf = r => Number(r.edge ?? r.avg_edge);

  const kpi = useMemo(() => {
    const settled = rows.filter(r => r.outcome === 'WIN' || r.outcome === 'LOSS');
    const wins = settled.filter(r => r.outcome === 'WIN').length;
    const net = settled.reduce((s, r) => {
      const v = pnlOf(r);
      return Number.isFinite(v) ? s + v : s;
    }, 0);
    const edges = settled.map(edgeOf).filter(Number.isFinite);
    const avgEdge = edges.length ? edges.reduce((a, b) => a + b, 0) / edges.length : null;
    return {
      n: rows.length,
      wr: settled.length ? wins / settled.length : null,
      net,
      avgEdge,
    };
  }, [rows]);

  return (
    <div>
      <PageHeader
        tag="TRADES · /trades"
        title="Trades"
        subtitle="Every fill with dedup key, regime, conviction, skip reason, and CLOB oid."
        right={<div style={{ fontSize: 11, color: T.label2 }}>{rows.length} rows</div>}
      />

      <div style={{ display: 'flex', gap: 18, marginBottom: 12, flexWrap: 'wrap' }}>
        <FilterPills label="strategy" options={strategyOptions} value={strategy} onChange={setStrategy} />
        <FilterPills label="outcome" options={OUTCOMES} value={outcome} onChange={setOutcome} />
        <FilterPills label="range" options={RANGES} value={rangeDays} onChange={setRangeDays} />
        <FilterPills label="fills" options={FILL_MODES} value={onlyFilled} onChange={setOnlyFilled} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
        <Stat lbl="Trades" val={kpi.n} sub={`${rangeDays}d window`} />
        <Stat lbl="Win rate" val={kpi.wr == null ? '—' : `${(kpi.wr * 100).toFixed(1)}%`} tone={wrTone(kpi.wr)} />
        <Stat lbl="Net PnL" val={fmtUSD(kpi.net)} tone={kpi.net >= 0 ? 'good' : 'bad'} />
        <Stat lbl="Avg edge" val={kpi.avgEdge == null ? '—' : `${(kpi.avgEdge * 100).toFixed(1)}¢`} />
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        <DataTable
          emptyText="No trades match these filters."
          columns={[
            { key: 'ts', label: 'time', render: r => <span style={{ color: T.label2 }}>{(r.created_at || r.ts || '').toString().slice(11, 19)}</span> },
            { key: 'strategy', label: 'strategy' },
            { key: 'regime', label: 'regime', render: r => r.regime || '—' },
            { key: 'conviction', label: 'conv', render: r => r.conviction || '—' },
            { key: 'market', label: 'market', render: r => <span style={{ color: T.label2 }}>{r.market_slug || r.market || r.question || '—'}</span> },
            { key: 'outcome_side', label: 'side', render: r => r.direction || r.side || r.outcome_side || '—' },
            { key: 'size', label: 'size', num: true, render: r => {
              const v = sizeOf(r);
              return Number.isFinite(v) ? `$${v.toFixed(2)}` : '—';
            }},
            { key: 'fill_price', label: 'fill', num: true, render: r => {
              // Prefer actual fill_price over entry_price (intent). The old
              // order swallowed real fills whenever entry_price was NULL even
              // though fill_price was present — those were the "—" rows.
              const v = Number(r.fill_price ?? r.entry_price);
              return Number.isFinite(v) ? v.toFixed(3) : '—';
            }},
            { key: 'exit_price', label: 'exit', num: true, render: r => {
              const v = Number(r.exit_price ?? r.polymarket_confirmed_fill_price);
              return Number.isFinite(v) ? v.toFixed(3) : '—';
            }},
            { key: 'pnl', label: 'pnl', num: true, render: r => {
              const v = pnlOf(r);
              if (!Number.isFinite(v)) return <span style={{ color: T.label }}>—</span>;
              return <span style={{ color: v >= 0 ? T.profit : T.loss }}>{fmtUSD(v)}</span>;
            }},
            { key: 'outcome', label: 'out', render: r => r.outcome === 'WIN' ? <span style={{ color: T.profit, fontSize: 10 }}>WIN</span>
              : r.outcome === 'LOSS' ? <span style={{ color: T.loss, fontSize: 10 }}>LOSS</span>
              : <span style={{ color: T.label }}>open</span> },
            { key: 'clob_oid', label: 'CLOB oid', render: r => {
              const oid = r.order_id || r.clob_oid;
              return oid ? <code style={{ fontSize: 10, color: T.label2 }}>{oid.toString().slice(0, 6)}…{oid.toString().slice(-4)}</code> : '—';
            }},
            { key: 'dedup_key', label: 'dedup', render: r => r.dedup_key ? <code style={{ fontSize: 10, color: T.label2 }}>{r.dedup_key}</code> : '—' },
            { key: 'skip_reason', label: 'skip', render: r => r.skip_reason ? <span style={{ color: T.warn, fontSize: 10 }}>{r.skip_reason}</span> : '—' },
          ]}
          rows={rows.map((r, i) => ({ ...r, _key: r.id ?? i }))}
        />
      </div>
    </div>
  );
}
