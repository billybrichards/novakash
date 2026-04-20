// Wallet page v2 — spec hub-note #189.
//
// Replaces the audit-task #217 stub. Three tabs under one route:
//   /wallet          → Summary + Balance card (polls every 15s)
//   /wallet?tab=pending  → Unredeemed wins
//   /wallet?tab=history  → Redemption history (default 24h, 7d toggle)
//
// Data sources, ranked by trust (spec §3):
//   1 on-chain Polygon RPC (2-of-3 consensus) — via hub-proxied endpoint only
//   2 data-api.polymarket.com                 — via hub only
//   3 local DB (enrichment only)
//
// Hub endpoints consulted (all hub-proxied — never a direct call to
// polymarket.com or Polygon RPC from the FE, per memory rule):
//
//   GET /api/wallet/snapshot       (spec §4.2 — 404 today, audit-task #217)
//   GET /api/wallet/pending        (spec §4.3 — 404 today)
//   GET /api/wallet/history?days=N (spec §4.4 — 404 today)
//
// Each is guarded — when the hub returns 404 we fall back to:
//   - /api/positions/snapshot  (DB-only balance + pending_wins)
//   - /api/trades?outcome=WIN  (DB-only history)
// and the UI renders a db-only SotBadge + red trust banner so operators
// don't blindly trust the numbers.

import React, { useMemo, useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import Loading from '../components/shared/Loading.jsx';
import { T } from '../theme/tokens.js';
import BalanceCard from './wallet-v2/sections/BalanceCard.jsx';
import SummaryTab from './wallet-v2/sections/SummaryTab.jsx';
import UnredeemedTab from './wallet-v2/sections/UnredeemedTab.jsx';
import HistoryTab from './wallet-v2/sections/HistoryTab.jsx';
import TrustBanner from './wallet-v2/sections/TrustBanner.jsx';
import {
  mapWalletSnapshotToBalance,
  mapPositionsSnapshotToBalance,
  deriveUnredeemedRows,
  deriveHistoryRows,
} from './wallet-v2/reconcile.js';

const TABS = [
  { id: 'summary', label: 'Summary' },
  { id: 'pending', label: 'Unredeemed' },
  { id: 'history', label: 'History' },
];

// Hub returns plain axios error objects via useApi. The envelope sets
// `error.response.status`; useApiLoader flattens to `err.message` which
// for 404s tends to include "404". We treat any non-successful resolution
// as "endpoint missing" and fall back silently.
function is404(err) {
  if (!err) return false;
  const s = String(err);
  return s.includes('404') || /not.?found/i.test(s);
}

export default function Wallet() {
  const api = useApi();
  const [tab, setTab] = useState(() => readTabFromQuery());
  const [historyDays, setHistoryDays] = useState(1);

  // Tab → URL query so a bookmarked /wallet?tab=pending works.
  useEffect(() => {
    const url = new URL(window.location.href);
    if (tab === 'summary') url.searchParams.delete('tab');
    else url.searchParams.set('tab', tab);
    window.history.replaceState({}, '', url.toString());
  }, [tab]);

  // Primary: /api/wallet/snapshot (spec #189 §4.2). Always fetched —
  // balance card sits above every tab.
  const walletSnap = useApiLoader(
    (signal) => api.get('/api/wallet/snapshot', { signal }),
  );

  // Fallback: /api/positions/snapshot. Always fetched — cheap and it
  // doubles as the pending-wins fallback source.
  const posSnap = useApiLoader(
    (signal) => api.get('/api/positions/snapshot', { signal }),
  );

  // Pending + history scoped to tab to avoid unnecessary 404 spam
  // while the spec endpoints are unshipped (audit-task #217).
  const walletPending = useApiLoader(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    (signal) => tab === 'pending'
      ? api.get('/api/wallet/pending', { signal })
      : Promise.resolve(null),
    [tab],
  );
  const walletHistory = useApiLoader(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    (signal) => tab === 'history'
      ? api.get(`/api/wallet/history?days=${historyDays}`, { signal })
      : Promise.resolve(null),
    [tab, historyDays],
  );

  // Trade fallback for the pending-wins panel: WIN rows only.
  const winTrades = useApiLoader(
    (signal) => api.get('/api/trades?outcome=WIN&page_size=200', { signal }),
  );

  // Trade fallback for the history tab: WIN + LOSS rows, sized for the 7d
  // window. Scoped to the history tab so other tabs don't pay for it.
  const historyTrades = useApiLoader(
    // eslint-disable-next-line react-hooks/exhaustive-deps
    (signal) => tab === 'history'
      ? api.get('/api/trades?page_size=300', { signal })
      : Promise.resolve(null),
    [tab],
  );

  // Auto-refresh snapshot every 15s while on summary tab.
  useEffect(() => {
    if (tab !== 'summary') return undefined;
    const h = setInterval(() => {
      walletSnap.reload();
      posSnap.reload();
    }, 15_000);
    return () => clearInterval(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // Reconcile balance — prefer spec endpoint, fall back to positions.
  const { balance, rank } = useMemo(() => {
    if (walletSnap.data) {
      const b = mapWalletSnapshotToBalance(walletSnap.data);
      return { balance: b, rank: b?.rank || 'data-api' };
    }
    if (posSnap.data) {
      return { balance: mapPositionsSnapshotToBalance(posSnap.data), rank: 'db-only' };
    }
    return { balance: null, rank: 'db-only' };
  }, [walletSnap.data, posSnap.data]);

  const pending = useMemo(() => {
    const m = [];
    if (walletPending.data) {
      return { ...deriveUnredeemedRows({ walletPending: walletPending.data }), missing: m };
    }
    if (walletPending.error && is404(walletPending.error)) {
      m.push('/api/wallet/pending');
    }
    // Fall back to positions snapshot's pending_wins array.
    const posFallback = deriveUnredeemedRows({ positionsSnap: posSnap.data });
    if (posFallback.rows.length > 0) {
      return { ...posFallback, missing: m };
    }
    // Last resort — win trades filtered client-side.
    return { ...deriveUnredeemedRows({ trades: winTrades.data }), missing: m };
  }, [walletPending.data, walletPending.error, posSnap.data, winTrades.data]);

  const history = useMemo(() => {
    const m = [];
    if (walletHistory.data) {
      return { ...deriveHistoryRows({ walletHistory: walletHistory.data, days: historyDays }), missing: m };
    }
    if (walletHistory.error && is404(walletHistory.error)) {
      m.push('/api/wallet/history');
    }
    return { ...deriveHistoryRows({ trades: historyTrades.data ?? winTrades.data, days: historyDays }), missing: m };
  }, [walletHistory.data, walletHistory.error, historyTrades.data, winTrades.data, historyDays]);

  // Aggregate "what's missing" across endpoints for the trust banner.
  const allMissing = useMemo(() => {
    const m = new Set();
    if (walletSnap.error && is404(walletSnap.error)) m.add('/api/wallet/snapshot');
    if (walletPending.error && is404(walletPending.error)) m.add('/api/wallet/pending');
    if (walletHistory.error && is404(walletHistory.error)) m.add('/api/wallet/history');
    return [...m];
  }, [walletSnap.error, walletPending.error, walletHistory.error]);

  const loadingAny =
    (walletSnap.loading || posSnap.loading) && !balance;

  return (
    <div>
      <PageHeader
        tag="WALLET · /wallet"
        title="Wallet"
        subtitle="CLOB-first wallet view. Every number carries a source-of-truth badge. Read-only: redemption is CLI-only via scripts/ops/manual_redeem.py (spec #189 §7.4). Writes gated by SIWE (audit-task #214)."
      />

      <TrustBanner
        rank={rank}
        redeemerHealth={balance?.redeemer_health}
        missingEndpoints={allMissing}
      />

      <BalanceCard balance={balance} rank={rank} />

      <TabNav tab={tab} setTab={setTab} />

      {loadingAny ? <Loading label="Loading wallet snapshot…" /> : null}

      {tab === 'summary' ? (
        <SummaryTab balance={balance} rank={rank} missing={allMissing} />
      ) : null}

      {tab === 'pending' ? (
        <UnredeemedTab
          rows={pending.rows}
          rank={pending.rank}
          legend={pending.legend}
          missing={pending.missing}
        />
      ) : null}

      {tab === 'history' ? (
        <HistoryTab
          rows={history.rows}
          rank={history.rank}
          days={historyDays}
          setDays={setHistoryDays}
          missing={history.missing}
        />
      ) : null}
    </div>
  );
}

function TabNav({ tab, setTab }) {
  return (
    <div style={{
      display: 'flex', gap: 4, borderBottom: `1px solid ${T.border}`,
      marginBottom: 14,
    }}>
      {TABS.map(t => {
        const active = t.id === tab;
        return (
          <button
            key={t.id}
            type="button"
            aria-pressed={active}
            onClick={() => setTab(t.id)}
            style={{
              fontSize: 11,
              padding: '8px 14px',
              background: 'transparent',
              border: 'none',
              borderBottom: `2px solid ${active ? T.purple : 'transparent'}`,
              color: active ? '#fff' : T.label2,
              fontFamily: T.font,
              cursor: 'pointer',
              letterSpacing: '0.04em',
            }}
          >{t.label}</button>
        );
      })}
    </div>
  );
}

function readTabFromQuery() {
  try {
    const p = new URLSearchParams(window.location.search);
    const t = p.get('tab');
    if (TABS.some(x => x.id === t)) return t;
  } catch {
    /* ignore */
  }
  return 'summary';
}
