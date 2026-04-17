import React, { useState, useCallback, useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import Loading from '../components/shared/Loading.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import { T } from '../theme/tokens.js';

const fmtUSD = n => {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return `$${v.toFixed(2)}`;
};

// Whitelist position id shape so the copied shell string can't carry surprise characters.
// Our DB ids are UUIDs, ints, or compact hashes — all fit this regex.
const SAFE_ID = /^[\w-]{1,64}$/;

const panelStyle = {
  background: T.card,
  border: `1px solid ${T.border}`,
  padding: 14,
  borderRadius: 2,
  marginBottom: 14,
};

export default function Wallet() {
  const api = useApi();
  const snap = useApiLoader((s) => api.get('/api/wallet/snapshot', { signal: s }));
  // page_size is the real param (not limit); we filter redeemed client-side until hub adds a native filter (audit #217).
  const pending = useApiLoader((s) => api.get('/api/trades?outcome=WIN&page_size=200', { signal: s }));
  // Activity ledger — derived entirely from /api/trades (status + pnl_usd).
  // Tried /api/wallet/redemption-activity first (#217), degrades silently if missing.
  const recent = useApiLoader((s) => api.get('/api/trades?page_size=100', { signal: s }));

  const balance = snap.data ?? {};
  const pendingAll = Array.isArray(pending.data) ? pending.data : [];
  const pendingRows = pendingAll.filter(r => r.redeemed !== true);
  const recentTrades = Array.isArray(recent.data) ? recent.data : [];

  // Extract window timestamp from market_slug (e.g. btc-updown-5m-1776414300 → unix secs).
  const windowOf = (r) => {
    const m = String(r.market_slug || '').match(/-(\d{10,13})$/);
    return m ? m[1] : null;
  };

  // Ledger: one row per resolved trade. Event type = REDEEM (win) / LOSS (loss) / TRADE (open).
  // Detect 2-leg windows (two trades share the same market_slug window key).
  const ledger = useMemo(() => {
    // 2-leg detection: group by market window key; if >1 trade share, flag.
    const byWindow = new Map();
    for (const t of recentTrades) {
      const w = windowOf(t);
      if (!w) continue;
      if (!byWindow.has(w)) byWindow.set(w, []);
      byWindow.get(w).push(t);
    }

    // Build events sorted desc by ts. Use resolved_at if present, else created_at.
    const events = recentTrades.map(t => {
      const tsRaw = t.resolved_at || t.created_at;
      const ts = tsRaw ? new Date(tsRaw).getTime() : 0;
      const pnl = Number(t.pnl_usd);
      const stake = Number(t.stake_usd);
      const w = windowOf(t);
      const group = w ? byWindow.get(w) : null;
      const twoLeg = group && group.length > 1;
      const peerPnl = twoLeg
        ? group
            .filter(g => g.id !== t.id)
            .map(g => Number(g.pnl_usd))
            .find(Number.isFinite)
        : null;

      let kind;
      if (t.outcome === 'WIN' || t.status === 'RESOLVED_WIN') kind = 'REDEEM';
      else if (t.outcome === 'LOSS' || t.status === 'RESOLVED_LOSS') kind = 'LOSS';
      else kind = 'OPEN';

      // Human-readable window range: "3:30-3:35AM" style from unix secs + 5m/15m hint in slug.
      let windowLabel = '';
      if (w) {
        const start = new Date(Number(w) * 1000);
        const slug = String(t.market_slug || '');
        const mmatch = slug.match(/-(\d+)m-/);
        const mins = mmatch ? Number(mmatch[1]) : 5;
        const end = new Date(start.getTime() + mins * 60_000);
        const fmt = d => d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
        windowLabel = `${fmt(start)}–${fmt(end)}`;
      }

      return {
        id: t.id,
        ts,
        tsLabel: tsRaw ? new Date(tsRaw).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false }) : '—',
        kind,
        strategy: t.strategy_id || t.strategy || '—',
        market: t.market_slug || '—',
        direction: t.direction || '—',
        stake: Number.isFinite(stake) ? stake : null,
        pnl: Number.isFinite(pnl) ? pnl : null,
        windowLabel,
        twoLeg,
        peerPnl: Number.isFinite(peerPnl) ? peerPnl : null,
      };
    }).filter(e => e.ts > 0).sort((a, b) => b.ts - a.ts);

    // Running total: walk events in chronological order, accumulate pnl on resolve events.
    const chron = [...events].reverse();
    let run = 0;
    const runById = new Map();
    for (const e of chron) {
      if (e.pnl != null && (e.kind === 'REDEEM' || e.kind === 'LOSS')) run += e.pnl;
      runById.set(e.id, run);
    }
    return events.map(e => ({ ...e, running: runById.get(e.id) }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recentTrades]);

  const ledgerStats = useMemo(() => {
    const settled = ledger.filter(e => e.kind !== 'OPEN');
    const net = settled.reduce((s, e) => s + (e.pnl || 0), 0);
    const wins = settled.filter(e => e.kind === 'REDEEM').length;
    return { n: settled.length, net, wins, losses: settled.length - wins };
  }, [ledger]);

  // Copy-button state per row so the button can toggle to ✓ briefly.
  const [copied, setCopied] = useState(null);
  const copyCli = useCallback((positionId) => {
    if (!positionId || !SAFE_ID.test(String(positionId))) return;
    const cmd = `python scripts/ops/redeem.py --position-id=${positionId}`;
    if (!navigator.clipboard?.writeText) {
      setCopied({ id: positionId, ok: false });
      setTimeout(() => setCopied(null), 1800);
      return;
    }
    navigator.clipboard.writeText(cmd)
      .then(() => { setCopied({ id: positionId, ok: true }); setTimeout(() => setCopied(null), 1800); })
      .catch(() => { setCopied({ id: positionId, ok: false }); setTimeout(() => setCopied(null), 1800); });
  }, []);

  // Consensus rendering — use agreed_count / total_count if hub sends them,
  // fall back to boolean `sources_agreed`, then single-source.
  const consensusCell = (() => {
    const agreed = balance.agreed_count;
    const total = balance.total_count ?? balance.source_count;
    if (total != null && agreed != null) {
      const ok = agreed === total;
      return <span style={{ color: ok ? T.profit : T.warn }}>{ok ? '✓ ' : '⚠ '}{agreed} of {total} agree</span>;
    }
    if (balance.sources_agreed === true) return <span style={{ color: T.profit }}>✓ sources agree</span>;
    if (balance.sources_agreed === false) return <span style={{ color: T.warn }}>⚠ disagree</span>;
    return <span style={{ color: T.label }}>single-source</span>;
  })();

  return (
    <div>
      <PageHeader
        tag="WALLET · /wallet"
        title="Wallet"
        subtitle="Read-only. Balance · pending-win redemptions · recent activity. Writes are disabled pending SIWE auth (audit-task #214)."
      />

      {/* Panel 1 — Balance */}
      <div style={panelStyle}>
        <div style={{ fontSize: 13, marginBottom: 10 }}>Balance</div>
        {snap.loading && !snap.data ? <Loading /> : null}
        {snap.error && !snap.data ? (
          <EmptyState
            message="Balance snapshot unavailable."
            hint="Backend endpoint /api/wallet/snapshot not yet shipped — tracked by audit-task #217."
          />
        ) : null}
        {snap.data ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
            <Stat lbl="USDC (proxy)" val={fmtUSD(balance.usdc_proxy)} />
            <Stat lbl="USDC (EOA)" val={fmtUSD(balance.usdc_eoa)} />
            <Stat lbl="MATIC (EOA)" val={balance.matic_eoa != null ? `${Number(balance.matic_eoa).toFixed(4)} MATIC` : '—'} />
            <Stat lbl="RPC consensus" val={consensusCell} sub={balance.block_number ? `block ${balance.block_number}` : null} />
          </div>
        ) : null}
      </div>

      {/* Panel 2 — Pending wins */}
      <div style={panelStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ fontSize: 13 }}>Pending wins · unredeemed</div>
          <div style={{ fontSize: 11, color: T.label2 }}>
            {pendingRows.length} positions · {fmtUSD(pendingRows.reduce((s, r) => {
              const v = Number(r.payout ?? r.pnl_usd);
              return Number.isFinite(v) ? s + v : s;
            }, 0))} total
            {pendingAll.length !== pendingRows.length ? (
              <span style={{ color: T.label, marginLeft: 6 }}>(filtered {pendingAll.length - pendingRows.length} already-redeemed client-side)</span>
            ) : null}
          </div>
        </div>
        {pending.loading && pendingRows.length === 0 ? <Loading /> : null}
        {pending.error ? <div style={{ color: T.loss, fontSize: 12 }}>Load error: {pending.error}</div> : null}
        <DataTable
          emptyText="No pending wins. All resolved positions are redeemed."
          columns={[
            { key: 'market', label: 'market', render: r => <span style={{ color: T.label2 }}>{r.market_slug || r.market || r.question || '—'}</span> },
            { key: 'outcome_side', label: 'side', render: r => r.direction || r.side || r.outcome_side || '—' },
            { key: 'size', label: 'size', num: true, render: r => {
              const v = Number(r.stake_usd ?? r.size);
              return Number.isFinite(v) ? `$${v.toFixed(2)}` : '—';
            }},
            { key: 'payout', label: 'payout', num: true, render: r => {
              const v = Number(r.payout ?? r.pnl_usd);
              return <span style={{ color: T.profit }}>{Number.isFinite(v) ? fmtUSD(v) : '—'}</span>;
            }},
            { key: 'age', label: 'age', render: r => ageOf(r.resolved_at || r.created_at || r.ts) },
            { key: 'relayer_remaining', label: 'relayer today', num: true, render: r => r.relayer_remaining != null && r.relayer_cap != null ? `${r.relayer_remaining}/${r.relayer_cap}` : r.relayer_remaining != null ? `${r.relayer_remaining}` : '—' },
            { key: '_act', label: '', render: r => {
              const isCopied = copied && copied.id === r.id;
              const label = isCopied ? (copied.ok ? 'copied ✓' : 'copy failed') : 'copy CLI cmd';
              return (
                <button
                  type="button"
                  aria-label={`copy redeem command for position ${r.id}`}
                  onClick={() => copyCli(r.id)}
                  style={{ ...btnStyle, color: isCopied ? (copied.ok ? T.profit : T.loss) : T.text }}
                >
                  {label}
                </button>
              );
            }},
          ]}
          rows={pendingRows.map((r, i) => ({ ...r, _key: r.id ?? i }))}
        />
      </div>

      {/* Panel 3 — Activity ledger. TG-style: interleaved REDEEM + LOSS + OPEN events
          with running P&L and 2-leg detection. Derived client-side from /api/trades. */}
      <div style={{ ...panelStyle, marginBottom: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ fontSize: 13 }}>Activity ledger · last {Math.min(ledger.length, 60)} events</div>
          <div style={{ fontSize: 11, color: T.label2 }}>
            <span style={{ color: ledgerStats.net >= 0 ? T.profit : T.loss }}>
              net {fmtUSD(ledgerStats.net).replace('$', ledgerStats.net >= 0 ? '+$' : '-$').replace('+-', '-')}
            </span>
            {' · '}
            <span style={{ color: T.profit }}>{ledgerStats.wins}W</span>
            {' / '}
            <span style={{ color: T.loss }}>{ledgerStats.losses}L</span>
            {' of '}
            {ledgerStats.n} settled
          </div>
        </div>
        {recent.loading && ledger.length === 0 ? <Loading /> : null}
        {recent.error ? <div style={{ color: T.loss, fontSize: 12 }}>Load error: {recent.error}</div> : null}
        <DataTable
          emptyText="No activity yet."
          columns={[
            { key: 'tsLabel', label: 'time', render: r => <span style={{ color: T.label2 }}>{r.tsLabel}</span> },
            { key: 'kind', label: 'event', render: r => {
              if (r.kind === 'REDEEM') return <span style={{ color: T.profit, fontSize: 10, fontWeight: 600 }}>REDEEM</span>;
              if (r.kind === 'LOSS') return <span style={{ color: T.loss, fontSize: 10, fontWeight: 600 }}>TRADE</span>;
              return <span style={{ color: T.label, fontSize: 10 }}>OPEN</span>;
            }},
            { key: 'pnl', label: 'pnl', num: true, render: r => {
              if (r.pnl == null || r.kind === 'OPEN') return <span style={{ color: T.label }}>—</span>;
              const sign = r.pnl >= 0 ? '+' : '-';
              return <span style={{ color: r.pnl >= 0 ? T.profit : T.loss, fontWeight: 600 }}>{sign}${Math.abs(r.pnl).toFixed(2)}</span>;
            }},
            { key: 'running', label: 'running', num: true, render: r => {
              if (r.running == null) return <span style={{ color: T.label }}>—</span>;
              const sign = r.running >= 0 ? '+' : '-';
              return <span style={{ color: r.running >= 0 ? T.profit : T.loss }}>{sign}${Math.abs(r.running).toFixed(2)}</span>;
            }},
            { key: 'window', label: 'window', render: r => <span style={{ color: T.label2 }}>{r.windowLabel || '—'}</span> },
            { key: 'direction', label: 'dir', render: r => r.direction === 'YES' ? <span style={{ color: T.cyan, fontSize: 10 }}>YES</span> : r.direction === 'NO' ? <span style={{ color: T.purple, fontSize: 10 }}>NO</span> : '—' },
            { key: 'stake', label: 'stake', num: true, render: r => r.stake != null ? `$${r.stake.toFixed(2)}` : '—' },
            { key: 'strategy', label: 'strategy', render: r => <span style={{ fontSize: 10, color: T.label2 }}>{r.strategy}</span> },
            { key: 'twoLeg', label: 'notes', render: r => r.twoLeg
              ? <span style={{ color: T.warn, fontSize: 10 }}>
                  2-leg{r.peerPnl != null ? ` · peer ${r.peerPnl >= 0 ? '+' : '-'}$${Math.abs(r.peerPnl).toFixed(2)}` : ''}
                </span>
              : '' },
          ]}
          rows={ledger.slice(0, 60).map(e => ({ ...e, _key: e.id }))}
        />
      </div>
    </div>
  );
}

function Stat({ lbl, val, sub }) {
  return (
    <div>
      <div style={{ fontSize: 10, letterSpacing: '0.12em', color: T.label, textTransform: 'uppercase' }}>{lbl}</div>
      <div style={{ fontSize: 18, marginTop: 4 }}>{val}</div>
      {sub ? <div style={{ fontSize: 11, color: T.label2, marginTop: 2 }}>{sub}</div> : null}
    </div>
  );
}

function ageOf(iso) {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

const btnStyle = {
  fontSize: 10,
  padding: '2px 8px',
  background: 'transparent',
  border: `1px solid ${T.borderStrong}`,
  color: T.text,
  cursor: 'pointer',
  fontFamily: T.font,
  borderRadius: 2,
};
