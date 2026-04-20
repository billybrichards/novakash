import React, { useMemo } from 'react';
import { T } from '../../../theme/tokens.js';
import DataTable from '../../../components/shared/DataTable.jsx';
import SotBadge from '../sotBadge.jsx';
import { fmtUsd, summarizeHistory } from '../reconcile.js';

/**
 * Redemption history tab (spec #189 §5.4).
 *
 * 24h default, 7d toggle. Columns: time · market · side · cost · payout ·
 * P&L · transport · initiator · tx.  When `/api/wallet/history` is missing,
 * we fall back to /api/trades filtered by outcome ∈ {WIN, LOSS} — transport
 * and initiator columns render as "—" because the trades table does not
 * carry them yet (migration deferred to M2).
 */
export default function HistoryTab({ rows, rank, days, setDays, missing }) {
  const sorted = useMemo(() => {
    if (!Array.isArray(rows)) return [];
    return [...rows].sort((a, b) => (b.ts || 0) - (a.ts || 0));
  }, [rows]);
  const stats = useMemo(() => summarizeHistory(sorted), [sorted]);
  const netColor = stats.net >= 0 ? T.profit : T.loss;

  return (
    <div>
      <div style={panelStyle}>
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          marginBottom: 10, flexWrap: 'wrap', gap: 8,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ fontSize: 13 }}>Redemption history · last {days}d</div>
            <SotBadge rank={rank} compact />
          </div>
          <div style={{ fontSize: 11, color: T.label2 }}>
            {stats.n} events ·{' '}
            <span style={{ color: T.profit }}>{stats.wins}W</span> /{' '}
            <span style={{ color: T.loss }}>{stats.losses}L</span> ·{' '}
            net <span style={{ color: netColor, fontWeight: 500 }}>{fmtUsd(stats.net, { signed: true })}</span>
          </div>
        </div>

        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12,
          fontSize: 11, color: T.label2,
        }}>
          <span>Window:</span>
          {[1, 7].map(d => (
            <button
              key={d}
              type="button"
              aria-pressed={days === d}
              onClick={() => setDays(d)}
              style={{
                fontSize: 10,
                padding: '2px 10px',
                borderRadius: 10,
                border: `1px solid ${days === d ? T.purple : T.borderStrong}`,
                background: days === d ? 'rgba(168,85,247,0.15)' : 'transparent',
                color: days === d ? '#fff' : T.label2,
                cursor: 'pointer',
                fontFamily: T.font,
              }}
            >{d}d</button>
          ))}
          {missing?.length ? (
            <span style={{ color: T.label, marginLeft: 8 }}>
              Fallback in use: {missing.join(', ')} missing.
            </span>
          ) : null}
        </div>

        <DataTable
          emptyText={rank === 'db-only'
            ? `No settled trades in local DB in the last ${days}d.`
            : `No redemptions in the last ${days}d.`}
          columns={[
            { key: 'ts', label: 'time', render: r => <TimeCell r={r} /> },
            { key: 'market', label: 'market', render: r => (
              <span style={{ color: T.label2, fontSize: 10.5 }}>{r.market}</span>
            ) },
            { key: 'side', label: 'side', render: r => <span style={{
              color: r.side === 'YES' || r.side === 'Up' ? T.cyan
                : r.side === 'NO' || r.side === 'Down' ? T.purple
                : T.label,
              fontSize: 10,
            }}>{r.side}</span> },
            { key: 'cost_usd', label: 'cost', num: true, render: r => <span style={{ color: T.label2 }}>{fmtUsd(r.cost_usd)}</span> },
            { key: 'payout_usd', label: 'payout', num: true, render: r => fmtUsd(r.payout_usd) },
            { key: 'pnl_usd', label: 'pnl', num: true, render: r => <PnlCell v={r.pnl_usd} /> },
            { key: 'transport', label: 'transport', render: r => <TransportBadge v={r.transport} /> },
            { key: 'initiator', label: 'initiator', render: r => <InitiatorBadge v={r.initiator} /> },
            { key: 'tx', label: 'tx', render: r => r.tx ? (
              <a
                href={`https://polygonscan.com/tx/${r.tx}`}
                target="_blank"
                rel="noreferrer"
                style={{ color: T.cyan, fontSize: 10, textDecoration: 'none' }}
                title={r.tx}
              >{short(r.tx)}</a>
            ) : <span style={{ color: T.label }}>—</span> },
          ]}
          rows={sorted.map(r => ({ ...r, _key: r.key }))}
        />
      </div>
    </div>
  );
}

function TimeCell({ r }) {
  if (!r.redeemed_at_utc) return <span style={{ color: T.label }}>—</span>;
  const d = new Date(r.redeemed_at_utc);
  return (
    <span style={{ color: T.label2, fontSize: 10.5, fontVariantNumeric: 'tabular-nums' }}>
      {d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })}
    </span>
  );
}

function PnlCell({ v }) {
  if (v == null) return <span style={{ color: T.label }}>—</span>;
  const color = v >= 0 ? T.profit : T.loss;
  return <span style={{ color, fontWeight: 500 }}>{fmtUsd(v, { signed: true })}</span>;
}

function TransportBadge({ v }) {
  if (!v) return <span style={{ color: T.label }}>—</span>;
  const color = v === 'onchain_matic' ? T.cyan : v === 'relayer' ? T.label2 : T.label;
  return <span style={{ color, fontSize: 10 }}>{v}</span>;
}

function InitiatorBadge({ v }) {
  if (!v) return <span style={{ color: T.label }}>—</span>;
  const color = v.startsWith('engine') ? T.profit
    : v.startsWith('manual') ? T.cyan
    : T.label2;
  return <span style={{ color, fontSize: 10 }}>{v}</span>;
}

function short(tx) {
  const s = String(tx || '');
  if (s.length <= 12) return s;
  return `${s.slice(0, 6)}…${s.slice(-4)}`;
}

const panelStyle = {
  background: T.card,
  border: `1px solid ${T.border}`,
  padding: 14,
  borderRadius: 2,
  marginBottom: 14,
};
