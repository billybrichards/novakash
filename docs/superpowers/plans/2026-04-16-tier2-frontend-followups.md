# Tier-2 Frontend Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify fetch patterns and empty/loading copy, add lint gate, ship three new read-only pages (`/wallet`, `/strategies`) + caps panel into `/config`, and wire 15-minute strategies across the entire frontend so they are never silently blended into 5m aggregates again.

**Architecture:** Tier-2 builds on the Tier-1 shell (`AppShell` + `nav/navigation.js` + shared primitives). One new hook (`useApiLoader`) replaces the 3 fetch patterns that drifted across Tier-1. Two new shared UI components standardize copy. One ESLint config. Four page-level changes (+`/wallet`, +`/strategies`, `/config` tab, `/trades` dynamic pills). One `Dashboard` timeframe filter. All read-only — any write feature waits for audit-task #214 SIWE.

**Tech stack:** Same as Tier-1 (React 18, Vite, react-router-dom, recharts, axios via `useApi`, tailwind + inline tokens via `theme/tokens.js`).

**Non-goals:**
- No write actions (withdraw/transfer/redeem buttons) — blocked by audit-task #214.
- No multi-account UI — memory `feedback_single_account_only.md`.
- No React.lazy() archive split — Tier-3.
- No retirement of legacy `Layout.jsx` — needs archive audit first.
- No new backend endpoints created by frontend tasks. Frontend degrades gracefully if `/api/trading-config/caps`, `/api/strategies`, or `/api/wallet/snapshot` return empty. Backend work tracked by new audit-task #216.

---

## File Structure

**Created:**
- `frontend/src/hooks/useApiLoader.js`
- `frontend/src/components/shared/EmptyState.jsx`
- `frontend/src/components/shared/Loading.jsx`
- `frontend/.eslintrc.cjs`
- `frontend/src/pages/Wallet.jsx`
- `frontend/src/pages/Strategies.jsx`

**Modified:**
- `frontend/src/pages/UnifiedDashboard.jsx` — migrate to `useApiLoader`, add timeframe filter
- `frontend/src/pages/TradesEnhanced.jsx` — migrate to `useApiLoader`, derive strategies dynamically
- `frontend/src/pages/SignalExplorer.jsx` — migrate to `useApiLoader`
- `frontend/src/pages/AuditTasks.jsx` — migrate to `useApiLoader`
- `frontend/src/pages/ConfigOverrides.jsx` — migrate to `useApiLoader`, add Caps tab
- `frontend/src/nav/navigation.js` — add `/wallet`, `/strategies`
- `frontend/src/App.jsx` — register new routes

---

## Task P2-1: `useApiLoader` hook + migrate 5 pages

**Files:**
- Create: `frontend/src/hooks/useApiLoader.js`
- Modify: `TradesEnhanced.jsx`, `SignalExplorer.jsx`, `AuditTasks.jsx`, `ConfigOverrides.jsx`, `UnifiedDashboard.jsx`

**Context:** Tier-1 review flagged 3 drifting fetch patterns. Consolidate. Hook returns `{data, error, loading, reload}`, owns AbortController, handles `AbortError`/`ERR_CANCELED`, unwraps axios envelope + `{rows: [...]}` + `{trades: [...]}` + bare arrays.

- [ ] **Step 1: Write `useApiLoader.js`**

```js
// frontend/src/hooks/useApiLoader.js
import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi } from './useApi.js';

/**
 * Unified fetch hook for Tier-1+ pages.
 *
 * Usage:
 *   const { data, error, loading, reload } = useApiLoader(
 *     (signal) => api.get(`/api/trades?limit=500`, { signal }),
 *     [limit]
 *   );
 *
 * - Aborts previous request on new fetch + unmount.
 * - Filters AbortError / ERR_CANCELED from reported errors.
 * - Unwraps axios envelope (r.data ?? r) and normalizes to array
 *   when response is {rows:[...]}, {trades:[...]}, or already an array.
 *   Returns raw object for non-array shapes.
 */
export function useApiLoader(fetcher, deps = []) {
  const api = useApi();
  const acRef = useRef(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (acRef.current) acRef.current.abort();
    const ac = new AbortController();
    acRef.current = ac;
    setLoading(true);
    setError(null);
    try {
      const r = await fetcher(ac.signal, api);
      if (ac.signal.aborted) return;
      const raw = r?.data ?? r;
      if (Array.isArray(raw)) {
        setData(raw);
      } else if (raw && typeof raw === 'object') {
        if (Array.isArray(raw.rows)) setData(raw.rows);
        else if (Array.isArray(raw.trades)) setData(raw.trades);
        else setData(raw);
      } else {
        setData(raw);
      }
    } catch (e) {
      if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') return;
      setError(e.message || 'load failed');
    } finally {
      if (acRef.current === ac) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, ...deps]);

  useEffect(() => {
    load();
    return () => { if (acRef.current) acRef.current.abort(); };
  }, [load]);

  return { data, error, loading, reload: load };
}
```

- [ ] **Step 2: Migrate `AuditTasks.jsx`**

Replace the `load = useCallback(...)` + `useEffect(() => { load(); }, [load])` block with:

```jsx
const { data: rows, error: err, loading, reload } = useApiLoader(
  (signal) => {
    const params = new URLSearchParams({ limit: '200' });
    if (status) params.set('status', status);
    if (sev) params.set('severity', sev);
    return api.get(`/api/audit-tasks?${params.toString()}`, { signal });
  },
  [status, sev]
);
```

Keep `patchStatus`; replace `await load()` with `await reload()`. Default `rows` to `[]` at render: `const visible = rows ?? [];`.

Remove: `useRef`, `acRef`, `setLoading`, manual `setErr`, manual `setRows` state declarations.

- [ ] **Step 3: Migrate `SignalExplorer.jsx`, `TradesEnhanced.jsx`, `ConfigOverrides.jsx`** using the same pattern. For `ConfigOverrides.jsx` the fetcher is the 3-endpoint fallback chain — wrap inside the `fetcher` callback so the hook still sees one logical fetch.

- [ ] **Step 4: Migrate `UnifiedDashboard.jsx`**

This one is special — it polls 3 endpoints on 5s interval. The hook handles single-shot + abort; we still need the interval. Compose:

```jsx
const dashLoader = useApiLoader((s) => api.get('/api/dashboard', { signal: s }));
const statsLoader = useApiLoader((s) => api.get('/api/trades/stats', { signal: s }));
const sysLoader = useApiLoader((s) => api.get('/api/system/status', { signal: s }));

useEffect(() => {
  const t = setInterval(() => {
    dashLoader.reload();
    statsLoader.reload();
    sysLoader.reload();
  }, 5000);
  return () => clearInterval(t);
}, []);
```

Name the destructured fields (`dash`, `stats`, `sys`, `err`) consistently with the previous implementation so JSX doesn't change.

- [ ] **Step 5: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/hooks/useApiLoader.js \
        frontend/src/pages/AuditTasks.jsx \
        frontend/src/pages/SignalExplorer.jsx \
        frontend/src/pages/TradesEnhanced.jsx \
        frontend/src/pages/ConfigOverrides.jsx \
        frontend/src/pages/UnifiedDashboard.jsx
git commit -m "refactor(fe): unify fetch pattern via useApiLoader hook"
```

---

## Task P2-2: `EmptyState` + `Loading` primitives

**Files:**
- Create: `frontend/src/components/shared/EmptyState.jsx`
- Create: `frontend/src/components/shared/Loading.jsx`
- Modify: all 5 Tier-1 pages

**Context:** Tier-1 review: "No data." / "No X." / "No X match these filters." / "No Y found." mixed across pages. Standardize.

- [ ] **Step 1: `EmptyState.jsx`**

```jsx
import React from 'react';
import { T } from '../../theme/tokens.js';

/** Standard empty-state placeholder used across every list/table. */
export default function EmptyState({ message, hint }) {
  return (
    <div style={{
      color: T.label2,
      fontSize: 12,
      padding: '16px 0',
      textAlign: 'center',
    }}>
      <div>{message}</div>
      {hint ? <div style={{ color: T.label, fontSize: 11, marginTop: 4 }}>{hint}</div> : null}
    </div>
  );
}
```

- [ ] **Step 2: `Loading.jsx`**

```jsx
import React from 'react';
import { T } from '../../theme/tokens.js';

export default function Loading({ label = 'Loading…' }) {
  return <div style={{ color: T.label, fontSize: 12, padding: '12px 0' }}>{label}</div>;
}
```

- [ ] **Step 3: Standardize copy**

- `DataTable`: keep its `emptyText` API but default to `'No rows.'` — every caller passes a filter-aware message.
- `UnifiedDashboard`: equity empty → `No equity curve yet.` · hint `First closed trade will populate.` · alerts empty → `No active alerts.`
- `TradesEnhanced`: empty → `No trades match these filters.`
- `SignalExplorer`: matrix + list empty → `No decisions match these filters.`
- `AuditTasks`: empty → `No audit tasks match these filters.`
- `ConfigOverrides`: placeholder for shape-mismatch becomes EmptyState with hint.

- [ ] **Step 4: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/components/shared/EmptyState.jsx \
        frontend/src/components/shared/Loading.jsx \
        frontend/src/pages/*.jsx
git commit -m "feat(fe): EmptyState + Loading primitives, standardized copy"
```

---

## Task P2-3: `.eslintrc.cjs` + fix errors

**Files:**
- Create: `frontend/.eslintrc.cjs`
- Modify: any file with lint errors after config lands

**Context:** `package.json` has `"lint": "eslint src --ext .js,.jsx"` but no config file — `npm run lint` has failed every review cycle. Add minimal config, fix whatever new Tier-1 files trip.

- [ ] **Step 1: `.eslintrc.cjs`**

```js
module.exports = {
  root: true,
  env: { browser: true, es2022: true },
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: 'module',
    ecmaFeatures: { jsx: true },
  },
  settings: { react: { version: '18' } },
  extends: [
    'eslint:recommended',
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
  ],
  rules: {
    'react/prop-types': 'off',
    'no-unused-vars': ['warn', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    'no-empty': ['error', { allowEmptyCatch: true }],
  },
  ignorePatterns: ['dist/', 'node_modules/'],
};
```

- [ ] **Step 2: Run lint**

```bash
cd frontend && npm run lint
```

Fix only errors in files touched by Tier-1 or Tier-2 (new shell, new pages, shared primitives, hooks). Warnings in legacy archived pages are acceptable.

- [ ] **Step 3: Commit**

```bash
git add frontend/.eslintrc.cjs frontend/src/
git commit -m "chore(fe): add minimal eslint config; fix new-file errors"
```

---

## Task P2-4: `/wallet` read-only page

**Files:**
- Create: `frontend/src/pages/Wallet.jsx`
- Modify: `frontend/src/nav/navigation.js`, `frontend/src/App.jsx`

**Context:** Satisfies audit-tasks #212 (read-only wallet + positions + pending-redemption) and #213 (wallet read-only, 3 panels). #211 folds in. Backend: `GET /api/wallet/snapshot` (exists), `GET /api/trades?outcome=WIN&redeemed=false&limit=100`, `GET /api/wallet/redemption-activity?limit=20` (may not exist — degrade gracefully).

Three panels (top to bottom): Balance · Pending Wins (redeem-verification) · Recent Redemption Activity.

**Read-only:** Each pending-win row has a "Copy CLI cmd" button that puts `python scripts/ops/redeem.py --position-id=<id>` on the clipboard. NO actual redeem button — that waits for audit-task #214 SIWE.

- [ ] **Step 1: Add route + nav entry**

`navigation.js` — under `TRADING` add `{ path: '/wallet', label: 'Wallet', icon: '👛' }` after `/trades` and before `/pnl`.

`App.jsx` — add `<Route path="wallet" element={<Wallet />} />` under the AppShell route, plus `import Wallet from './pages/Wallet.jsx';`.

- [ ] **Step 2: `Wallet.jsx`**

Structure:

```jsx
import React from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import Loading from '../components/shared/Loading.jsx';
import { T } from '../theme/tokens.js';

const fmtUSD = n => {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return `$${v.toFixed(2)}`;
};

function copyCli(positionId) {
  const cmd = `python scripts/ops/redeem.py --position-id=${positionId}`;
  navigator.clipboard.writeText(cmd).catch(() => {});
}

export default function Wallet() {
  const api = useApi();
  const snap = useApiLoader((s) => api.get('/api/wallet/snapshot', { signal: s }));
  const pending = useApiLoader((s) => api.get('/api/trades?outcome=WIN&redeemed=false&limit=100', { signal: s }));
  const activity = useApiLoader((s) => api.get('/api/wallet/redemption-activity?limit=20', { signal: s }).catch(() => ({ data: { rows: [] } })));

  const balance = snap.data ?? {};
  const pendingRows = Array.isArray(pending.data) ? pending.data : [];
  const activityRows = Array.isArray(activity.data) ? activity.data : [];

  return (
    <div>
      <PageHeader
        tag="WALLET · /wallet"
        title="Wallet"
        subtitle="Read-only. Balance · pending-win redemptions · recent activity. Writes are disabled pending SIWE auth (audit-task #214)."
      />

      {/* Panel 1 — Balance */}
      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 14 }}>
        <div style={{ fontSize: 13, marginBottom: 10 }}>Balance</div>
        {snap.loading && !snap.data ? <Loading /> : null}
        {snap.error ? <div style={{ color: T.loss, fontSize: 12 }}>Load error: {snap.error}</div> : null}
        {snap.data ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12 }}>
            <Stat lbl="USDC (proxy)" val={fmtUSD(balance.usdc_proxy)} />
            <Stat lbl="USDC (EOA)" val={fmtUSD(balance.usdc_eoa)} />
            <Stat lbl="MATIC (EOA)" val={balance.matic_eoa != null ? `${Number(balance.matic_eoa).toFixed(4)} MATIC` : '—'} />
            <Stat
              lbl="RPC consensus"
              val={balance.sources_agreed === false
                ? <span style={{ color: T.warn }}>⚠ disagree</span>
                : balance.sources_agreed === true
                  ? <span style={{ color: T.profit }}>{balance.source_count ?? '?'} of {balance.source_count ?? '?'} agree</span>
                  : <span style={{ color: T.label }}>single-source</span>}
              sub={balance.block_number ? `block ${balance.block_number}` : null}
            />
          </div>
        ) : null}
      </div>

      {/* Panel 2 — Pending wins */}
      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 14 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
          <div style={{ fontSize: 13 }}>Pending wins · unredeemed</div>
          <div style={{ fontSize: 11, color: T.label2 }}>{pendingRows.length} positions · {fmtUSD(pendingRows.reduce((s, r) => s + (Number(r.payout) || 0), 0))} total</div>
        </div>
        {pending.loading && pendingRows.length === 0 ? <Loading /> : null}
        {pending.error ? <div style={{ color: T.loss, fontSize: 12 }}>Load error: {pending.error}</div> : null}
        <DataTable
          emptyText="No pending wins. All resolved positions are redeemed."
          columns={[
            { key: 'market', label: 'market', render: r => <span style={{ color: T.label2 }}>{r.market || r.question || '—'}</span> },
            { key: 'outcome_side', label: 'side', render: r => r.side || r.outcome_side || '—' },
            { key: 'size', label: 'size', num: true, render: r => r.size != null ? `$${Number(r.size).toFixed(0)}` : '—' },
            { key: 'payout', label: 'payout', num: true, render: r => <span style={{ color: T.profit }}>{fmtUSD(r.payout)}</span> },
            { key: 'age', label: 'age', render: r => ageOf(r.resolved_at || r.ts) },
            { key: 'relayer_remaining', label: 'relayer cap', num: true, render: r => r.relayer_remaining != null ? `${r.relayer_remaining}/20` : '—' },
            { key: '_act', label: '', render: r => (
              <button
                type="button"
                onClick={() => copyCli(r.id)}
                style={btnStyle}
              >
                copy CLI cmd
              </button>
            )},
          ]}
          rows={pendingRows.map((r, i) => ({ ...r, _key: r.id ?? i }))}
        />
      </div>

      {/* Panel 3 — Recent redemption activity */}
      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        <div style={{ fontSize: 13, marginBottom: 10 }}>Recent redemption activity · last 20</div>
        <DataTable
          emptyText="No recent redemptions on file."
          columns={[
            { key: 'ts', label: 'time', render: r => <span style={{ color: T.label2 }}>{(r.ts || '').toString().slice(11, 19)}</span> },
            { key: 'path', label: 'path', render: r => r.path === 'direct' ? <span style={{ color: T.profit, fontSize: 10 }}>direct on-chain</span> : <span style={{ color: T.label2, fontSize: 10 }}>relayer</span> },
            { key: 'payout', label: 'payout', num: true, render: r => <span style={{ color: T.profit }}>{fmtUSD(r.payout)}</span> },
            { key: 'tx_hash', label: 'tx', render: r => r.tx_hash ? <code style={{ fontSize: 10, color: T.label2 }}>{r.tx_hash.slice(0, 10)}…</code> : '—' },
            { key: 'gas_matic', label: 'gas', num: true, render: r => r.gas_matic != null ? Number(r.gas_matic).toFixed(4) : '—' },
          ]}
          rows={activityRows.map((r, i) => ({ ...r, _key: r.tx_hash ?? i }))}
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
```

- [ ] **Step 3: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/pages/Wallet.jsx frontend/src/nav/navigation.js frontend/src/App.jsx
git commit -m "feat(fe): /wallet read-only page — balance, pending wins, redemption activity"
```

---

## Task P2-5: `/config` Caps tab

**Files:**
- Modify: `frontend/src/pages/ConfigOverrides.jsx`

**Context:** Add a second tab inside `/config`. First tab keeps the Overrides table (unchanged). Second tab = Caps table pulled from `GET /api/trading-config/caps` (backend work tracked by new audit-task #216). If endpoint missing, render placeholder.

Columns: `param | constants.py | YAML | runtime | .env | effective | source | conflict`. Precedence: strictest wins (min of all defined). Red row when `min_bet > max_bet` OR `MAX_STAKE × BET_FRACTION > HARD_CAP` OR runtime value exceeds constants.py value.

- [ ] **Step 1: Tab switcher**

Add local state `const [tab, setTab] = useState('overrides')`. Render two buttons at top of page: Overrides · Caps. Wrap existing body in `{tab === 'overrides' ? <OverridesPane /> : <CapsPane />}`.

- [ ] **Step 2: `CapsPane`**

New component in the same file. Uses `useApiLoader` against `/api/trading-config/caps`. Normalizes to `[{ param, constants, yaml, runtime, env, effective, source, conflict }]`. Render via `DataTable` with red-row styling when `conflict === true`.

If the endpoint 404s (backend not shipped yet), render `<EmptyState message="Caps endpoint not yet available." hint="Tracked by audit-task #216. Check back after backend lands."/>`.

- [ ] **Step 3: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/pages/ConfigOverrides.jsx
git commit -m "feat(fe): /config Caps tab — all bet-size caps in one place"
```

---

## Task P2-6: `/strategies` comparison page

**Files:**
- Create: `frontend/src/pages/Strategies.jsx`
- Modify: `frontend/src/nav/navigation.js`, `frontend/src/App.jsx`

**Context:** Side-by-side strategy comparison. Row-grouped by timeframe (5m block + 15m block). Left = YAML param grid with cell-diff highlighting. Right = perf overlay (WR 7d/30d + net PnL + trade count + avg edge).

Data: `GET /api/strategies` (audit-task #216) returns `{strategy_id: {timeframe, yaml, runtime}}`. If endpoint missing, derive strategy list from recent `/api/v58/strategy-decisions` rows and render perf-only.

- [ ] **Step 1: Add route + nav entry**

`navigation.js` — under `ANALYSIS` add `{ path: '/strategies', label: 'Strategies', icon: '🧬' }` after `/signals`.

`App.jsx` — add `<Route path="strategies" element={<Strategies />} />` + import.

- [ ] **Step 2: `Strategies.jsx`**

Pseudocode structure:

```jsx
import React, { useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import Loading from '../components/shared/Loading.jsx';
import { T, wrColor } from '../theme/tokens.js';

const TIMEFRAMES = ['5m', '15m', '1h'];

export default function Strategies() {
  const api = useApi();
  const strategies = useApiLoader((s) => api.get('/api/strategies', { signal: s }).catch(() => ({ data: {} })));
  const decisions = useApiLoader((s) => api.get('/api/v58/strategy-decisions?limit=2000', { signal: s }));

  // Fallback: if /api/strategies empty, derive from decisions
  const strategyMap = useMemo(() => {
    const raw = strategies.data;
    if (raw && typeof raw === 'object' && Object.keys(raw).length > 0) return raw;
    const rows = decisions.data ?? [];
    const m = {};
    for (const r of rows) {
      const id = r.strategy_id || r.strategy;
      if (!id || m[id]) continue;
      m[id] = { timeframe: r.timeframe || '5m', yaml: {}, runtime: {} };
    }
    return m;
  }, [strategies.data, decisions.data]);

  // Group by timeframe
  const groups = useMemo(() => {
    const g = {};
    for (const [id, meta] of Object.entries(strategyMap)) {
      const tf = meta.timeframe || '5m';
      (g[tf] ??= []).push(id);
    }
    for (const tf of Object.keys(g)) g[tf].sort();
    return g;
  }, [strategyMap]);

  // Compute per-strategy perf from decisions
  const perf = useMemo(() => { /* ... aggregate WR 7d/30d + PnL + count + edge per strategy ... */ }, [decisions.data]);

  // Compute union of yaml+runtime param keys across all strategies
  const paramKeys = useMemo(() => { /* ... */ }, [strategyMap]);

  // Detect cell-differs-from-row-median for highlighting
  function isDivergent(key, vals) { /* ... */ }

  // Render tables per timeframe group
  return (
    <div>
      <PageHeader
        tag="STRATEGIES · /strategies"
        title="Strategies"
        subtitle="Per-timeframe side-by-side comparison: YAML params + live performance overlay."
      />

      {Object.keys(groups).length === 0 ? (
        strategies.loading || decisions.loading
          ? <Loading label="Loading strategy registry…" />
          : <EmptyState message="No strategies registered." hint="Check /api/strategies endpoint (audit-task #216) or strategy_decisions table." />
      ) : null}

      {TIMEFRAMES.map(tf => groups[tf] ? (
        <section key={tf} style={{ marginBottom: 24 }}>
          <h3 style={{ fontSize: 12, letterSpacing: '0.15em', color: T.cyan, textTransform: 'uppercase', marginBottom: 8 }}>
            {tf} strategies · {groups[tf].length}
          </h3>

          {/* LEFT: param grid */}
          <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 8 }}>
            <div style={{ fontSize: 13, marginBottom: 8, color: T.label2 }}>YAML params</div>
            {/* table with param rows × strategy columns, red cell when divergent */}
          </div>

          {/* RIGHT: perf overlay */}
          <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
            <div style={{ fontSize: 13, marginBottom: 8, color: T.label2 }}>Performance · last 7d / 30d</div>
            {/* table with WR-7d, WR-30d, PnL, trades, avg edge per strategy */}
          </div>
        </section>
      ) : null)}
    </div>
  );
}
```

Fill the `// ...` sections. The grid renders keys vertically × strategies horizontally. Divergence highlight = any cell whose stringified value differs from the row's modal value gets red border-left. Perf pulls from `decisions.data` via `wrOf` (move that helper into a shared module later — for this task just inline).

- [ ] **Step 3: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/pages/Strategies.jsx frontend/src/nav/navigation.js frontend/src/App.jsx
git commit -m "feat(fe): /strategies comparison page — 5m + 15m blocks, YAML + perf"
```

---

## Task P2-7: `/trades` dynamic strategy pills

**Files:**
- Modify: `frontend/src/pages/TradesEnhanced.jsx`

**Context:** Current `STRATEGIES` pill list is hardcoded to 5m only. Derive from loaded rows so 15m strategies appear automatically.

- [ ] **Step 1: Derive pills from data**

Replace the `STRATEGIES` const with a `useMemo` over `rows`:

```jsx
const strategyOptions = useMemo(() => {
  const set = new Set(rows.map(r => r.strategy || r.strategy_id).filter(Boolean));
  const opts = [{ label: 'all', value: null }];
  for (const s of Array.from(set).sort()) {
    opts.push({ label: s.replace(/^v4_/, 'v4/').replace(/^v10_/, 'v10/'), value: s });
  }
  return opts;
}, [rows]);
```

Pass `strategyOptions` instead of the const to `FilterPills`.

**Catch:** if the current strategy filter is active and the user's data no longer contains it, the pill row won't render it. Preserve the currently-selected value:

```jsx
const strategyOptions = useMemo(() => {
  const set = new Set(rows.map(r => r.strategy).filter(Boolean));
  if (strategy) set.add(strategy);
  // …
}, [rows, strategy]);
```

- [ ] **Step 2: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/pages/TradesEnhanced.jsx
git commit -m "feat(fe): /trades strategy pills derive from data (incl. 15m)"
```

---

## Task P2-8: `/` Dashboard timeframe filter

**Files:**
- Modify: `frontend/src/pages/UnifiedDashboard.jsx`

**Context:** Dashboard KPIs + equity curve currently blend 5m + 15m. Add a `FilterPills` row under the header: `all / 5m / 15m`. Query `/api/dashboard?timeframe=` if backend supports; else client-filter positions and recompute KPIs locally.

- [ ] **Step 1: Add filter state + UI**

```jsx
const [tf, setTf] = useState(null);
// FilterPills under PageHeader:
<FilterPills
  label="timeframe"
  value={tf}
  onChange={setTf}
  options={[{label:'all', value:null}, {label:'5m', value:'5m'}, {label:'15m', value:'15m'}]}
/>
```

- [ ] **Step 2: Wire into fetchers**

Update the dashboard fetcher to append `?timeframe=` when `tf` is set:

```jsx
const dashLoader = useApiLoader(
  (s) => api.get(`/api/dashboard${tf ? `?timeframe=${tf}` : ''}`, { signal: s }),
  [tf]
);
```

If the backend ignores the query param (unknown endpoint support), locally filter `positions` by `p.timeframe === tf` before rendering. KPI net today / win-rate card behavior: if `stats.by_timeframe[tf]` exists, use it; else leave the card value unchanged with a small `muted` "filter not applied" note.

- [ ] **Step 3: Build + commit**

```bash
cd frontend && npm run build
git add frontend/src/pages/UnifiedDashboard.jsx
git commit -m "feat(fe): dashboard timeframe filter (all/5m/15m)"
```

---

## Final review

- [ ] **Step 1: Dispatch cross-cutting reviewer** over `a02e9f7..HEAD` to check consistency (fetch patterns, empty-state copy, lint clean, 15m coverage).

- [ ] **Step 2: Open PR via `superpowers:finishing-a-development-branch`** — target `develop`, title `feat(fe): tier-2 frontend follow-ups`, body references audit-tasks closed (#211 folded, #212, #213) and opened (#216 backend).
