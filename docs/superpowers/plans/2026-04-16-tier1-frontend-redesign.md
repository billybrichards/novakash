# Tier-1 Frontend Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the sprawling Novakash frontend (27 routes, 5 overlapping dashboards) into a lean 6-route active surface backed by an Archive Center for legacy pages, with a new unified navigation and five Tier-1 pages that turn existing hub API data into real operator UX.

**Architecture:**
Zero new backend endpoints. A new `AppShell` layout replaces the old `Layout.jsx` sidebar with a tight 4-section nav (Trading · Analysis · Config · Archive). Five new pages (`UnifiedDashboard`, `TradesEnhanced`, `SignalExplorer`, `ConfigOverrides`, `AuditTasks`) consume existing endpoints (`/api/dashboard`, `/api/trades`, `/api/signals`, `/api/v58/*`, `/api/audit-tasks`, `/api/trading-config/*`). All 22 legacy pages are left on disk but moved off the main nav into `/archive/*` routes served by a landing `ArchiveCenter` page with clear "replaced by" pointers. Theme continuity preserved: IBM Plex Mono, `#07070c` background, purple/cyan accents, existing `useApi` + `StatCard` primitives.

**Tech Stack:** React 18, React Router 6, Vite, Tailwind + inline style tokens (existing mix), `recharts`, `lucide-react`, `axios` via `useApi`.

**Scope guardrails:**
- No inter-account / multi-account UI (single Polymarket account — see memory `feedback_single_account_only.md`).
- No new DB migrations.
- No new Python routes in this plan — if a FE page needs data not exposed by an existing endpoint, it degrades gracefully (empty state + note) and spawns an audit-task-style TODO. Backend follow-ups are out of scope for Tier-1.
- Archive is a holding pen, not a deletion. Pages remain importable and routable under `/archive/*`.

---

## File Structure

**Created:**
- `frontend/src/layouts/AppShell.jsx` — new shell (replaces Layout for new routes).
- `frontend/src/nav/navigation.js` — single source of truth for active + archived nav entries.
- `frontend/src/pages/UnifiedDashboard.jsx` — one dashboard for paper + live + execution state.
- `frontend/src/pages/TradesEnhanced.jsx` — new trades page with dedup / skip-reason / regime / conviction / CLOB oid.
- `frontend/src/pages/SignalExplorer.jsx` — slice 111k `signal_evaluations` rows by strategy × band × conviction × regime.
- `frontend/src/pages/ConfigOverrides.jsx` — YAML vs runtime effective values + conflict flagging.
- `frontend/src/pages/AuditTasks.jsx` — inbox over `/api/audit-tasks`.
- `frontend/src/pages/archive/ArchiveCenter.jsx` — landing page listing archived routes with replacements.
- `frontend/src/pages/archive/ArchivedPageBanner.jsx` — yellow banner wrapper component.
- `frontend/src/components/shared/PageHeader.jsx` — reusable header with tag/breadcrumb.
- `frontend/src/components/shared/FilterPills.jsx` — reusable filter-chip row.
- `frontend/src/components/shared/DataTable.jsx` — reusable dark-theme table.

**Modified:**
- `frontend/src/App.jsx` — route table swapped to new shell + archive namespace.
- `frontend/src/components/Layout.jsx` — kept for archive routes, removed from default `/` shell.

**Deleted:** Nothing. Old pages are preserved under `/archive/*` and kept on disk.

---

## Task 1: Nav + AppShell + Archive Center foundation

**Files:**
- Create: `frontend/src/nav/navigation.js`
- Create: `frontend/src/layouts/AppShell.jsx`
- Create: `frontend/src/pages/archive/ArchiveCenter.jsx`
- Create: `frontend/src/pages/archive/ArchivedPageBanner.jsx`
- Create: `frontend/src/components/shared/PageHeader.jsx`
- Modify: `frontend/src/App.jsx`

**Context for the subagent:**
Novakash frontend has accumulated 27 pages and 5 overlapping dashboards. This task installs the new navigation scaffolding so subsequent tasks can attach pages to named slots cleanly. The old `Layout.jsx` remains in the repo; we do not modify or delete it — it's used only for archive routes. Theme tokens (`#07070c` bg, IBM Plex Mono, purple `#a855f7` / cyan `#06b6d4`) must stay identical to `Dashboard.jsx` lines 28–43 so the aesthetic is continuous.

- [ ] **Step 1: Create nav config**

Write `frontend/src/nav/navigation.js`:

```js
// Single source of truth for the new navigation.
// Active entries show in AppShell sidebar. Archive entries only appear
// inside /archive. When a page is promoted/demoted, flip its `archived`
// flag — no route edits required elsewhere.

export const NAV_SECTIONS = [
  {
    title: 'TRADING',
    color: '#a855f7',
    items: [
      { path: '/',            label: 'Dashboard',      icon: '📊' },
      { path: '/trades',      label: 'Trades',         icon: '📋' },
      { path: '/pnl',         label: 'P&L',            icon: '💰' },
    ],
  },
  {
    title: 'ANALYSIS',
    color: '#06b6d4',
    items: [
      { path: '/signals',     label: 'Signal Explorer', icon: '📡' },
    ],
  },
  {
    title: 'CONTROL',
    color: '#f59e0b',
    items: [
      { path: '/config',      label: 'Config',         icon: '⚙️' },
      { path: '/audit',       label: 'Audit Tasks',    icon: '🔔' },
      { path: '/system',      label: 'System',         icon: '🖥️' },
    ],
  },
  {
    title: 'ARCHIVE',
    color: '#64748b',
    items: [
      { path: '/archive',     label: 'Archive Center', icon: '📦' },
    ],
  },
];

// Every legacy page that the new shell retires. The Archive Center renders
// this list and injects the ArchivedPageBanner on each route.
// `replacedBy` is the human-facing label the banner points operators at.
export const ARCHIVED_PAGES = [
  { path: '/archive/paper',          label: 'Paper Dashboard',       importName: 'PaperDashboard',       replacedBy: 'Dashboard (paper mode toggle)' },
  { path: '/archive/playwright',     label: 'Playwright Dashboard',  importName: 'PlaywrightDashboard',  replacedBy: 'Dashboard' },
  { path: '/archive/execution-hq',   label: 'Execution HQ',          importName: 'ExecutionHQ',          replacedBy: 'Dashboard' },
  { path: '/archive/live',           label: 'Live Trading',          importName: 'LiveTrading',          replacedBy: 'Dashboard + Trades' },
  { path: '/archive/factory',        label: 'Factory Floor',         importName: 'FactoryFloor',         replacedBy: 'Signal Explorer' },
  { path: '/archive/v58',            label: 'V58 Monitor',           importName: 'V58Monitor',           replacedBy: 'Signal Explorer' },
  { path: '/archive/windows',        label: 'Window Results',        importName: 'WindowResults',        replacedBy: 'Signal Explorer' },
  { path: '/archive/strategy',       label: 'Strategy Analysis',     importName: 'StrategyAnalysis',     replacedBy: 'Signal Explorer' },
  { path: '/archive/analysis',       label: 'Analysis Library',      importName: 'AnalysisLibrary',      replacedBy: 'Signal Explorer' },
  { path: '/archive/indicators',     label: 'Indicators',            importName: 'Indicators',           replacedBy: 'Signal Explorer' },
  { path: '/archive/timesfm',        label: 'TimesFM',               importName: 'TimesFM',              replacedBy: 'Signal Explorer (forecast pane)' },
  { path: '/archive/composite',      label: 'Composite Signals',     importName: 'CompositeSignals',     replacedBy: 'Signal Explorer' },
  { path: '/archive/margin',         label: 'Margin Engine',         importName: 'MarginEngine',         replacedBy: null, note: 'Separate subsystem — still live, pending own redesign.' },
  { path: '/archive/recommendations',label: 'Recommendations',       importName: 'Recommendations',      replacedBy: 'Signal Explorer' },
  { path: '/archive/positions',      label: 'Positions',             importName: 'Positions',            replacedBy: 'Dashboard (open positions pane)' },
  { path: '/archive/risk',           label: 'Risk',                  importName: 'Risk',                 replacedBy: 'Config + Dashboard' },
  { path: '/archive/signals',        label: 'Signals (legacy)',      importName: 'Signals',              replacedBy: 'Signal Explorer' },
  { path: '/archive/setup',          label: 'Setup',                 importName: 'Setup',                replacedBy: null, note: 'One-time bootstrap; keep as-is.' },
  { path: '/archive/learn',          label: 'Learn',                 importName: 'Learn',                replacedBy: null, note: 'Reference-only.' },
  { path: '/archive/changelog',      label: 'Changelog',             importName: 'Changelog',            replacedBy: null, note: 'Informational.' },
  { path: '/archive/trading-config', label: 'Trading Config (raw)',  importName: 'TradingConfig',        replacedBy: 'Config' },
];
```

- [ ] **Step 2: Create PageHeader shared component**

Write `frontend/src/components/shared/PageHeader.jsx`:

```jsx
import React from 'react';

const PAGETAG_STYLE = {
  display: 'inline-block',
  fontSize: 10,
  letterSpacing: '0.15em',
  color: '#a855f7',
  border: '1px solid #a855f7',
  padding: '2px 8px',
  borderRadius: 2,
  marginBottom: 6,
};

export default function PageHeader({ tag, title, subtitle, right }) {
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-end',
      padding: '20px 0 16px',
      borderBottom: '1px solid rgba(255,255,255,0.06)',
      marginBottom: 16,
    }}>
      <div>
        {tag ? <div style={PAGETAG_STYLE}>{tag}</div> : null}
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 500, letterSpacing: '0.02em' }}>{title}</h1>
        {subtitle ? (
          <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 12, marginTop: 4 }}>
            {subtitle}
          </div>
        ) : null}
      </div>
      <div>{right}</div>
    </div>
  );
}
```

- [ ] **Step 3: Create AppShell layout**

Write `frontend/src/layouts/AppShell.jsx`. Mirror `Layout.jsx` structurally but read from `navigation.js` and drop the config dropdown + all legacy tab bar items:

```jsx
import React, { useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext.jsx';
import LiveToggle from '../components/LiveToggle.jsx';
import { NAV_SECTIONS } from '../nav/navigation.js';

const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  text: 'rgba(255,255,255,0.9)',
  label: 'rgba(255,255,255,0.3)',
  font: "'IBM Plex Mono', monospace",
};

const SIDEBAR_WIDTH = 220;

export default function AppShell() {
  const { logout } = useAuth();
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: T.bg, color: T.text, fontFamily: T.font }}>
      <aside style={{
        width: SIDEBAR_WIDTH,
        borderRight: `1px solid ${T.border}`,
        padding: '16px 12px',
        position: 'sticky',
        top: 0,
        height: '100vh',
        overflowY: 'auto',
        flexShrink: 0,
      }}>
        <div style={{ fontSize: 11, letterSpacing: '0.18em', color: T.label, marginBottom: 18 }}>
          NOVAKASH · v2
        </div>

        {NAV_SECTIONS.map(section => (
          <div key={section.title} style={{ marginBottom: 20 }}>
            <div style={{
              fontSize: 9,
              letterSpacing: '0.2em',
              color: section.color,
              marginBottom: 6,
              opacity: 0.8,
            }}>
              {section.title}
            </div>
            {section.items.map(item => {
              const active = location.pathname === item.path
                || (item.path !== '/' && location.pathname.startsWith(item.path));
              return (
                <Link
                  key={item.path}
                  to={item.path}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    padding: '6px 8px',
                    borderRadius: 2,
                    fontSize: 12,
                    color: active ? '#fff' : 'rgba(255,255,255,0.65)',
                    background: active ? 'rgba(168,85,247,0.12)' : 'transparent',
                    borderLeft: active ? `2px solid ${section.color}` : '2px solid transparent',
                    textDecoration: 'none',
                    marginBottom: 2,
                  }}
                >
                  <span>{item.icon}</span>
                  <span>{item.label}</span>
                </Link>
              );
            })}
          </div>
        ))}

        <div style={{ marginTop: 32, paddingTop: 12, borderTop: `1px solid ${T.border}` }}>
          <LiveToggle />
          <button
            onClick={logout}
            style={{
              marginTop: 12,
              width: '100%',
              padding: '6px 10px',
              background: 'transparent',
              border: `1px solid ${T.border}`,
              color: T.label,
              fontFamily: T.font,
              fontSize: 11,
              cursor: 'pointer',
              borderRadius: 2,
            }}
          >
            Sign out
          </button>
        </div>
      </aside>

      <main style={{ flex: 1, padding: '16px 28px 80px', overflowX: 'hidden' }}>
        <Outlet />
      </main>
    </div>
  );
}
```

- [ ] **Step 4: Create ArchivedPageBanner wrapper**

Write `frontend/src/pages/archive/ArchivedPageBanner.jsx`:

```jsx
import React from 'react';
import { Link } from 'react-router-dom';

export default function ArchivedPageBanner({ replacedBy, note, children }) {
  return (
    <div>
      <div style={{
        background: 'rgba(245,158,11,0.1)',
        border: '1px solid rgba(245,158,11,0.35)',
        borderRadius: 2,
        padding: '10px 14px',
        margin: '12px 0',
        fontSize: 12,
        color: '#f59e0b',
        fontFamily: "'IBM Plex Mono', monospace",
      }}>
        <strong>ARCHIVED PAGE.</strong>{' '}
        {replacedBy
          ? <>Replaced by <em style={{ color: '#fff' }}>{replacedBy}</em>. This route is preserved for reference only.</>
          : note || 'Preserved for reference only.'}
        {' · '}
        <Link to="/archive" style={{ color: '#06b6d4' }}>Back to Archive Center</Link>
      </div>
      {children}
    </div>
  );
}
```

- [ ] **Step 5: Create ArchiveCenter page**

Write `frontend/src/pages/archive/ArchiveCenter.jsx`:

```jsx
import React from 'react';
import { Link } from 'react-router-dom';
import { ARCHIVED_PAGES } from '../../nav/navigation.js';
import PageHeader from '../../components/shared/PageHeader.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  label: 'rgba(255,255,255,0.55)',
};

export default function ArchiveCenter() {
  return (
    <div>
      <PageHeader
        tag="ARCHIVE · /archive"
        title="Archive Center"
        subtitle="Legacy pages from pre-redesign. Preserved in the codebase and routable; not shown in the main nav."
      />

      <div style={{
        background: T.card,
        border: `1px solid ${T.border}`,
        padding: 16,
        borderRadius: 2,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, letterSpacing: '0.12em' }}>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Legacy page</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Replaced by</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Note</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Route</th>
            </tr>
          </thead>
          <tbody>
            {ARCHIVED_PAGES.map(p => (
              <tr key={p.path} style={{ borderTop: `1px solid ${T.border}` }}>
                <td style={{ padding: '8px 10px' }}>{p.label}</td>
                <td style={{ padding: '8px 10px', color: p.replacedBy ? '#4ade80' : T.label }}>
                  {p.replacedBy ?? '—'}
                </td>
                <td style={{ padding: '8px 10px', color: T.label }}>{p.note ?? ''}</td>
                <td style={{ padding: '8px 10px' }}>
                  <Link to={p.path} style={{ color: '#06b6d4', fontSize: 11 }}>{p.path}</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p style={{ color: T.label, fontSize: 11, marginTop: 14 }}>
        These files remain in <code>src/pages/</code> — nothing deleted. To promote a page back into the main
        nav, add it to <code>NAV_SECTIONS</code> in <code>src/nav/navigation.js</code> and remove the
        matching entry from <code>ARCHIVED_PAGES</code>.
      </p>
    </div>
  );
}
```

- [ ] **Step 6: Rewire App.jsx**

Rewrite `frontend/src/App.jsx` (replace the entire file contents) so new pages use `AppShell` and legacy pages move under `/archive/*` via the old `Layout`:

```jsx
import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext.jsx';
import ProtectedRoute from './auth/ProtectedRoute.jsx';
import LoginPage from './auth/LoginPage.jsx';

// New shell + pages
import AppShell from './layouts/AppShell.jsx';
import UnifiedDashboard from './pages/UnifiedDashboard.jsx';
import TradesEnhanced from './pages/TradesEnhanced.jsx';
import SignalExplorer from './pages/SignalExplorer.jsx';
import ConfigOverrides from './pages/ConfigOverrides.jsx';
import AuditTasks from './pages/AuditTasks.jsx';
import PnL from './pages/PnL.jsx';
import System from './pages/System.jsx';

// Archive shell + pages
import Layout from './components/Layout.jsx';
import ArchiveCenter from './pages/archive/ArchiveCenter.jsx';
import ArchivedPageBanner from './pages/archive/ArchivedPageBanner.jsx';
import PaperDashboard from './pages/PaperDashboard.jsx';
import PlaywrightDashboard from './pages/PlaywrightDashboard.jsx';
import ExecutionHQ from './pages/execution-hq/ExecutionHQ.jsx';
import LiveTrading from './pages/LiveTrading.jsx';
import FactoryFloor from './pages/FactoryFloor.jsx';
import V58Monitor from './pages/V58Monitor.jsx';
import WindowResults from './pages/WindowResults.jsx';
import StrategyAnalysis from './pages/StrategyAnalysis.jsx';
import AnalysisLibrary from './pages/AnalysisLibrary.jsx';
import Indicators from './pages/Indicators.jsx';
import TimesFM from './pages/TimesFM.jsx';
import CompositeSignals from './pages/CompositeSignals.jsx';
import MarginEngine from './pages/margin-engine/MarginEngine.jsx';
import Recommendations from './pages/Recommendations.jsx';
import Positions from './pages/Positions.jsx';
import Risk from './pages/Risk.jsx';
import Signals from './pages/Signals.jsx';
import Setup from './pages/Setup.jsx';
import Learn from './pages/Learn.jsx';
import Changelog from './pages/Changelog.jsx';
import TradingConfig from './pages/TradingConfig.jsx';
import { ARCHIVED_PAGES } from './nav/navigation.js';

const ARCHIVE_COMPONENTS = {
  PaperDashboard, PlaywrightDashboard, ExecutionHQ, LiveTrading, FactoryFloor,
  V58Monitor, WindowResults, StrategyAnalysis, AnalysisLibrary, Indicators, TimesFM,
  CompositeSignals, MarginEngine, Recommendations, Positions, Risk, Signals,
  Setup, Learn, Changelog, TradingConfig,
};

function wrapArchived(meta) {
  const Component = ARCHIVE_COMPONENTS[meta.importName];
  if (!Component) return null;
  return (
    <ArchivedPageBanner replacedBy={meta.replacedBy} note={meta.note}>
      <Component />
    </ArchivedPageBanner>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />

          {/* New lean shell */}
          <Route path="/" element={<ProtectedRoute><AppShell /></ProtectedRoute>}>
            <Route index element={<UnifiedDashboard />} />
            <Route path="trades" element={<TradesEnhanced />} />
            <Route path="signals" element={<SignalExplorer />} />
            <Route path="config" element={<ConfigOverrides />} />
            <Route path="audit" element={<AuditTasks />} />
            <Route path="pnl" element={<PnL />} />
            <Route path="system" element={<System />} />
            <Route path="archive" element={<ArchiveCenter />} />
          </Route>

          {/* Archive namespace — legacy Layout, each page wrapped in banner */}
          <Route path="/archive" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
            {ARCHIVED_PAGES.map(meta => {
              const element = wrapArchived(meta);
              if (!element) return null;
              const sub = meta.path.replace(/^\/archive\//, '');
              return <Route key={meta.path} path={sub} element={element} />;
            })}
          </Route>

          {/* Hard redirects for the old entry points most likely to be bookmarked */}
          <Route path="/dashboard" element={<Navigate to="/" replace />} />
          <Route path="/paper" element={<Navigate to="/archive/paper" replace />} />
          <Route path="/live" element={<Navigate to="/archive/live" replace />} />
          <Route path="/v58" element={<Navigate to="/archive/v58" replace />} />
          <Route path="/execution-hq" element={<Navigate to="/archive/execution-hq" replace />} />
          <Route path="/playwright" element={<Navigate to="/archive/playwright" replace />} />
          <Route path="/factory" element={<Navigate to="/archive/factory" replace />} />
          <Route path="/windows" element={<Navigate to="/archive/windows" replace />} />
          <Route path="/strategy" element={<Navigate to="/archive/strategy" replace />} />
          <Route path="/analysis" element={<Navigate to="/archive/analysis" replace />} />
          <Route path="/indicators" element={<Navigate to="/archive/indicators" replace />} />
          <Route path="/timesfm" element={<Navigate to="/archive/timesfm" replace />} />
          <Route path="/composite" element={<Navigate to="/archive/composite" replace />} />
          <Route path="/margin" element={<Navigate to="/archive/margin" replace />} />
          <Route path="/recommendations" element={<Navigate to="/archive/recommendations" replace />} />
          <Route path="/positions" element={<Navigate to="/archive/positions" replace />} />
          <Route path="/risk" element={<Navigate to="/archive/risk" replace />} />
          <Route path="/trading-config" element={<Navigate to="/config" replace />} />
          <Route path="/setup" element={<Navigate to="/archive/setup" replace />} />
          <Route path="/learn" element={<Navigate to="/archive/learn" replace />} />
          <Route path="/changelog" element={<Navigate to="/archive/changelog" replace />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
```

- [ ] **Step 7: Create page stubs so the app compiles**

Before merging this task, the five new pages referenced by `AppShell` must exist as stubs so `npm run build` succeeds. Create these five files with only a "Coming soon" body. Each task below replaces one stub with the real implementation.

For each of these files create a stub that looks like the template below (replace `TAG`, `TITLE`, `DESC` per file):

```jsx
// frontend/src/pages/UnifiedDashboard.jsx
import React from 'react';
import PageHeader from '../components/shared/PageHeader.jsx';

export default function UnifiedDashboard() {
  return (
    <div>
      <PageHeader tag="DASHBOARD · /" title="Unified Dashboard" subtitle="Stub — implemented in Task 2." />
    </div>
  );
}
```

Stubs required (same pattern):
- `UnifiedDashboard.jsx` — tag `DASHBOARD · /`, title `Unified Dashboard`
- `TradesEnhanced.jsx` — tag `TRADES · /trades`, title `Trades`
- `SignalExplorer.jsx` — tag `SIGNALS · /signals`, title `Signal Explorer`
- `ConfigOverrides.jsx` — tag `CONFIG · /config`, title `Config Overrides`
- `AuditTasks.jsx` — tag `AUDIT · /audit`, title `Audit Tasks`

- [ ] **Step 8: Build + lint**

Run from the worktree root:

```bash
cd frontend && npm run build
```
Expected: `✓ built` with no errors. Warnings are acceptable.

```bash
cd frontend && npm run lint
```
Expected: zero errors. Warnings in legacy files are acceptable; new files must be clean.

- [ ] **Step 9: Manual smoke**

Start the dev server (`cd frontend && npm run dev`) and verify:
- `/` loads the AppShell with 4 nav sections and shows the `UnifiedDashboard` stub.
- Sidebar highlight lands on the active route.
- `/archive` loads the Archive Center table.
- `/archive/paper` loads the old paper dashboard wrapped in the yellow banner with a back link.
- `/paper` redirects to `/archive/paper`.
- `/dashboard` redirects to `/`.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/nav/navigation.js \
        frontend/src/layouts/AppShell.jsx \
        frontend/src/pages/archive/ArchiveCenter.jsx \
        frontend/src/pages/archive/ArchivedPageBanner.jsx \
        frontend/src/components/shared/PageHeader.jsx \
        frontend/src/pages/UnifiedDashboard.jsx \
        frontend/src/pages/TradesEnhanced.jsx \
        frontend/src/pages/SignalExplorer.jsx \
        frontend/src/pages/ConfigOverrides.jsx \
        frontend/src/pages/AuditTasks.jsx \
        frontend/src/App.jsx
git commit -m "feat(fe): lean nav + archive center scaffolding (tier-1 foundation)"
```

---

## Task 2: Unified Dashboard

**Files:**
- Modify: `frontend/src/pages/UnifiedDashboard.jsx` (replace stub)
- Create: `frontend/src/components/shared/DataTable.jsx`

**Context for the subagent:**
This page replaces the five overlapping dashboards (`Dashboard`, `PaperDashboard`, `PlaywrightDashboard`, `LiveTrading`, `ExecutionHQ`). It is the first thing an operator sees. It must be a single scroll with four panes: system/connectivity header, top-line KPIs (PnL today, win rate 7d, open exposure, drawdown), equity curve, and open positions. Data comes from existing hub endpoints: `GET /api/dashboard` and `GET /api/trades/stats` and `GET /api/system/status`. If a field is missing, fall back to `—` with a `muted` style — do not crash.

Read `frontend/src/pages/Dashboard.jsx:28-43` for the exact theme tokens, then `frontend/src/pages/Dashboard.jsx` entirely (it is ~20KB) to see which fields the existing API returns. Reuse the `useApi` hook exactly as Dashboard does. Do not invent new endpoints.

- [ ] **Step 1: Create DataTable shared component**

Write `frontend/src/components/shared/DataTable.jsx`:

```jsx
import React from 'react';

const T = {
  border: 'rgba(255,255,255,0.06)',
  label: 'rgba(255,255,255,0.3)',
};

export default function DataTable({ columns, rows, emptyText = 'No data.' }) {
  if (!rows || rows.length === 0) {
    return <div style={{ color: T.label, fontSize: 12, padding: '12px 0' }}>{emptyText}</div>;
  }
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
      <thead>
        <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
          {columns.map(c => (
            <th key={c.key} style={{
              textAlign: c.num ? 'right' : 'left',
              padding: '7px 10px',
              textTransform: 'uppercase',
              fontWeight: 500,
            }}>{c.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={r._key ?? i} style={{ borderTop: `1px solid ${T.border}` }}>
            {columns.map(c => (
              <td key={c.key} style={{
                padding: '7px 10px',
                textAlign: c.num ? 'right' : 'left',
                fontVariantNumeric: c.num ? 'tabular-nums' : undefined,
              }}>
                {c.render ? c.render(r) : r[c.key] ?? <span style={{ color: T.label }}>—</span>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 2: Implement UnifiedDashboard**

Replace `frontend/src/pages/UnifiedDashboard.jsx` entirely:

```jsx
import React, { useEffect, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import EquityCurve from '../components/EquityCurve.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80',
  loss: '#f87171',
  warn: '#f59e0b',
  label: 'rgba(255,255,255,0.3)',
  label2: 'rgba(255,255,255,0.55)',
};

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
  const [dash, setDash] = useState(null);
  const [stats, setStats] = useState(null);
  const [sys, setSys] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const [d, s, y] = await Promise.all([
          api.get('/api/dashboard').catch(() => null),
          api.get('/api/trades/stats').catch(() => null),
          api.get('/api/system/status').catch(() => null),
        ]);
        if (!alive) return;
        setDash(d?.data ?? d);
        setStats(s?.data ?? s);
        setSys(y?.data ?? y);
      } catch (e) {
        if (alive) setErr(e.message || 'load failed');
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, [api]);

  const positions = dash?.open_positions || [];
  const equity = dash?.equity_curve || [];

  return (
    <div>
      <PageHeader
        tag="DASHBOARD · /"
        title="Dashboard"
        subtitle="Unified operator view · replaces Paper · Playwright · Execution HQ · Live Trading"
        right={
          <div style={{ fontSize: 11, color: T.label2 }}>
            <StatusChip label={sys?.engine_mode === 'LIVE' ? 'ENGINE · LIVE' : 'ENGINE · PAPER'} ok={sys?.engine_ok} warn={!sys?.engine_ok} />
            <StatusChip label={`HUB · ${sys?.hub_ok ? 'OK' : 'DOWN'}`} ok={sys?.hub_ok} />
            <StatusChip label={`POLY WS · ${sys?.poly_ws_ok ? 'OK' : 'DOWN'}`} ok={sys?.poly_ws_ok} />
            <StatusChip label={`TF · ${sys?.timesfm_ok ? 'OK' : 'DEGRADED'}`} ok={sys?.timesfm_ok} warn={!sys?.timesfm_ok} />
          </div>
        }
      />

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
        <Stat lbl="Net today" val={fmtUSD(dash?.pnl_today)} sub={dash?.trades_today ? `${dash.trades_today} trades` : null} tone={dash?.pnl_today >= 0 ? 'good' : 'bad'} />
        <Stat lbl="Win rate · 7d" val={fmtPct(stats?.win_rate_7d)} sub={stats?.trades_7d ? `${stats.win_count_7d}/${stats.trades_7d}` : null} tone="good" />
        <Stat lbl="Open exposure" val={dash?.open_exposure != null ? `$${dash.open_exposure.toFixed(0)}` : '—'} sub={dash?.exposure_pct != null ? fmtPct(dash.exposure_pct) + ' of bankroll' : null} />
        <Stat lbl="Drawdown" val={fmtPct(dash?.drawdown)} sub="kill · 45%" tone={dash?.drawdown < -0.2 ? 'warn' : undefined} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 12, marginBottom: 14 }}>
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <div style={{ fontSize: 13, marginBottom: 8 }}>Equity · 30d</div>
          {equity.length > 0
            ? <EquityCurve data={equity} />
            : <div style={{ color: T.label, fontSize: 11, padding: '18px 0' }}>No equity curve data yet. Hub will populate after first closed trade.</div>}
        </div>

        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <div style={{ fontSize: 13, marginBottom: 8 }}>Active alerts</div>
          {(dash?.alerts?.length ?? 0) === 0
            ? <div style={{ color: T.label, fontSize: 11 }}>No alerts.</div>
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
```

- [ ] **Step 3: Build + lint**

```bash
cd frontend && npm run build && npm run lint
```
Expected: build succeeds; lint errors ONLY in legacy files are acceptable.

- [ ] **Step 4: Manual smoke**

Load `/` in the browser. Verify: four stat cards, equity pane, alerts pane, open-positions table. Endpoints may be empty → empty states render, no crash.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/UnifiedDashboard.jsx frontend/src/components/shared/DataTable.jsx
git commit -m "feat(fe): unified dashboard (replaces 5 legacy dashboards)"
```

---

## Task 3: Audit Tasks Inbox

**Files:**
- Modify: `frontend/src/pages/AuditTasks.jsx` (replace stub)
- Create: `frontend/src/components/shared/FilterPills.jsx`

**Context for the subagent:**
The engine already emits audit tasks (anomalies, model-eval requests, feed drift, redemption alerts). They land in the `audit_tasks` table and are served by `GET /api/audit-tasks?limit=&status=&task_type=` and mutated by `PATCH /api/audit-tasks/:id` with `{status}`. Today nobody reads them in-UI — operators curl them. This page is that inbox.

Severity values are `LOW|MED|HIGH`. Status values are `OPEN|IN_PROGRESS|CLOSED`. The API already supports filtering — do the filtering server-side by passing query params. Do not fetch all 200+ and filter client-side.

- [ ] **Step 1: Create FilterPills shared component**

Write `frontend/src/components/shared/FilterPills.jsx`:

```jsx
import React from 'react';

const T = { border: 'rgba(255,255,255,0.12)', label2: 'rgba(255,255,255,0.55)' };

export default function FilterPills({ options, value, onChange, label }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      {label ? <span style={{ fontSize: 10, color: 'rgba(255,255,255,0.3)', letterSpacing: '0.12em', textTransform: 'uppercase', marginRight: 4 }}>{label}</span> : null}
      {options.map(opt => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value ?? 'all'}
            onClick={() => onChange(opt.value)}
            style={{
              fontSize: 10,
              padding: '2px 10px',
              borderRadius: 10,
              border: `1px solid ${active ? '#a855f7' : T.border}`,
              background: active ? 'rgba(168,85,247,0.15)' : 'transparent',
              color: active ? '#fff' : T.label2,
              cursor: 'pointer',
              fontFamily: "'IBM Plex Mono', monospace",
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
```

- [ ] **Step 2: Implement AuditTasks**

Replace `frontend/src/pages/AuditTasks.jsx`:

```jsx
import React, { useEffect, useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80', loss: '#f87171', warn: '#f59e0b',
  label: 'rgba(255,255,255,0.3)', label2: 'rgba(255,255,255,0.55)',
};

const SEV_COLOR = { LOW: T.label2, MED: T.warn, HIGH: T.loss };
const STATUS_FILTERS = [
  { label: 'all', value: null },
  { label: 'open', value: 'OPEN' },
  { label: 'in progress', value: 'IN_PROGRESS' },
  { label: 'closed', value: 'CLOSED' },
];
const SEV_FILTERS = [
  { label: 'any', value: null },
  { label: 'HIGH', value: 'HIGH' },
  { label: 'MED', value: 'MED' },
  { label: 'LOW', value: 'LOW' },
];

const ageOf = iso => {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
};

export default function AuditTasks() {
  const api = useApi();
  const [status, setStatus] = useState('OPEN');
  const [sev, setSev] = useState(null);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const params = new URLSearchParams({ limit: '200' });
      if (status) params.set('status', status);
      if (sev) params.set('severity', sev);
      const r = await api.get(`/api/audit-tasks?${params.toString()}`);
      const data = r?.data ?? r;
      setRows(data?.rows ?? []);
    } catch (e) {
      setErr(e.message || 'load failed');
    } finally {
      setLoading(false);
    }
  }, [api, status, sev]);

  useEffect(() => { load(); }, [load]);

  const patchStatus = async (id, next) => {
    try {
      await api.patch(`/api/audit-tasks/${id}`, { status: next });
      await load();
    } catch (e) {
      alert(`Update failed: ${e.message || e}`);
    }
  };

  const counts = rows.reduce((acc, r) => {
    acc.total += 1;
    acc[r.severity] = (acc[r.severity] ?? 0) + 1;
    return acc;
  }, { total: 0, HIGH: 0, MED: 0, LOW: 0 });

  return (
    <div>
      <PageHeader
        tag="AUDIT · /audit"
        title="Audit Tasks"
        subtitle="Anomaly inbox — engine-emitted tasks backed by /api/audit-tasks."
        right={<div style={{ fontSize: 11, color: T.label2 }}>{counts.total} shown · {counts.HIGH} HIGH · {counts.MED} MED · {counts.LOW} LOW</div>}
      />

      <div style={{ display: 'flex', gap: 18, marginBottom: 12, flexWrap: 'wrap' }}>
        <FilterPills label="status" options={STATUS_FILTERS} value={status} onChange={setStatus} />
        <FilterPills label="severity" options={SEV_FILTERS} value={sev} onChange={setSev} />
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        {loading && rows.length === 0 ? (
          <div style={{ color: T.label, fontSize: 12 }}>Loading…</div>
        ) : (
          <DataTable
            emptyText="No audit tasks match these filters."
            columns={[
              { key: 'id', label: '#', num: true, render: r => <span style={{ color: T.label2 }}>{r.id}</span> },
              { key: 'task_type', label: 'type' },
              { key: 'severity', label: 'severity', render: r => (
                <span style={{ color: SEV_COLOR[r.severity] || T.label2, fontSize: 10, letterSpacing: '0.1em' }}>
                  {r.severity}
                </span>
              )},
              { key: 'title', label: 'title', render: r => <span style={{ color: T.label2 }}>{r.title}</span> },
              { key: 'category', label: 'category' },
              { key: 'priority', label: 'pri', num: true },
              { key: 'age', label: 'age', render: r => <span style={{ color: T.label2 }}>{ageOf(r.created_at)}</span> },
              { key: 'status', label: 'status', render: r => <span style={{ fontSize: 10 }}>{r.status}</span> },
              { key: '_actions', label: '', render: r => (
                <div style={{ display: 'flex', gap: 6 }}>
                  {r.status === 'OPEN' && <button onClick={() => patchStatus(r.id, 'IN_PROGRESS')} style={btnStyle}>start</button>}
                  {r.status !== 'CLOSED' && <button onClick={() => patchStatus(r.id, 'CLOSED')} style={btnStyle}>close</button>}
                </div>
              )},
            ]}
            rows={rows.map(r => ({ ...r, _key: r.id }))}
          />
        )}
      </div>
    </div>
  );
}

const btnStyle = {
  fontSize: 10,
  padding: '2px 8px',
  background: 'transparent',
  border: '1px solid rgba(255,255,255,0.12)',
  color: 'rgba(255,255,255,0.9)',
  cursor: 'pointer',
  fontFamily: "'IBM Plex Mono', monospace",
  borderRadius: 2,
};
```

- [ ] **Step 3: Build + lint**

```bash
cd frontend && npm run build && npm run lint
```
Expected: clean on new files.

- [ ] **Step 4: Manual smoke**

Log in, visit `/audit`. Verify filters work (clicking `open` changes URL params on the network tab), status-update buttons PATCH and reload the list.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/AuditTasks.jsx frontend/src/components/shared/FilterPills.jsx
git commit -m "feat(fe): audit-tasks inbox page"
```

---

## Task 4: Config Overrides

**Files:**
- Modify: `frontend/src/pages/ConfigOverrides.jsx` (replace stub)

**Context for the subagent:**
The engine resolves strategy params from two layers: per-strategy YAML files in `engine/config/strategies/` and an optional runtime override stored in the DB (resolved by `hub/api/trading_config.py`). When both set the same key the resolver picks one according to precedence — and a known bug previously let YAML silently override runtime on `v4_fusion.stake` (memory: `project_overnight_verification_apr14.md`). This page surfaces YAML value, runtime value, and effective value side-by-side per `(strategy, param)` row, and flags the cell red when YAML wins over a runtime value (which is usually a misconfiguration today).

Check what the API exposes. Start with `GET /api/trading-config` (the existing dropdown uses it). Then try `GET /api/trading-config/resolve` or `GET /api/trading-config/trace` — if either exists, prefer the one that returns per-key trace. If neither exists, fall back to showing `effective` only with a clear note that trace detail is pending backend work and emit no red flags.

- [ ] **Step 1: Read the existing config router**

Read `frontend/src/pages/TradingConfig.jsx` to see what the backend returns and how the existing page renders it. Extract the exact shape of the dashboard response.

- [ ] **Step 2: Implement ConfigOverrides**

Replace `frontend/src/pages/ConfigOverrides.jsx`:

```jsx
import React, { useEffect, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80', loss: '#f87171', warn: '#f59e0b',
  label: 'rgba(255,255,255,0.3)', label2: 'rgba(255,255,255,0.55)',
};

// Accept multiple plausible response shapes. Normalize to:
//   [{ strategy, param, yaml, runtime, effective, source }]
function normalize(raw) {
  if (!raw) return [];
  // Shape A: already an array of trace rows.
  if (Array.isArray(raw)) return raw;
  // Shape B: { strategies: { v4_down: { yaml: {...}, runtime: {...}, effective: {...} } } }
  if (raw.strategies && typeof raw.strategies === 'object') {
    const rows = [];
    for (const [strategy, v] of Object.entries(raw.strategies)) {
      const yaml = v.yaml ?? {};
      const runtime = v.runtime ?? {};
      const effective = v.effective ?? {};
      const keys = new Set([...Object.keys(yaml), ...Object.keys(runtime), ...Object.keys(effective)]);
      for (const k of keys) {
        const yv = yaml[k];
        const rv = runtime[k];
        const ev = effective[k] ?? rv ?? yv;
        let source = 'YAML';
        if (rv != null && ev === rv) source = 'runtime';
        else if (yv != null && rv != null && ev === yv && rv !== yv) source = 'YAML wins';
        rows.push({ strategy, param: k, yaml: yv, runtime: rv, effective: ev, source });
      }
    }
    return rows;
  }
  // Shape C: flat { v4_fusion.stake: 0.025, ... } — not useful; return [].
  return [];
}

const cell = (v) => (v == null ? <span style={{ color: T.label }}>—</span> : String(v));

export default function ConfigOverrides() {
  const api = useApi();
  const [raw, setRaw] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    (async () => {
      setLoading(true);
      try {
        // Prefer a trace endpoint if it exists.
        let r = null;
        try { r = await api.get('/api/trading-config/trace'); } catch { /* fall through */ }
        if (!r) {
          try { r = await api.get('/api/trading-config/resolve'); } catch { /* fall through */ }
        }
        if (!r) r = await api.get('/api/trading-config');
        if (!alive) return;
        setRaw(r?.data ?? r);
      } catch (e) {
        if (alive) setErr(e.message || 'load failed');
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [api]);

  const rows = normalize(raw);
  const conflicts = rows.filter(r => r.source === 'YAML wins');

  return (
    <div>
      <PageHeader
        tag="CONFIG · /config"
        title="Config Overrides"
        subtitle="YAML vs runtime trace per (strategy, param). Red cells flag YAML silently winning over a runtime value."
        right={<div style={{ fontSize: 11, color: conflicts.length ? T.loss : T.label2 }}>
          {rows.length} keys · {conflicts.length} conflicts
        </div>}
      />

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      {rows.length === 0 && !loading ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, color: T.label2, fontSize: 12 }}>
          Backend didn't return a trace-compatible shape (tried <code>/api/trading-config/trace</code>, <code>/api/trading-config/resolve</code>, <code>/api/trading-config</code>). Add a trace endpoint on the hub to populate this page. No changes made to YAML/runtime state.
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <DataTable
            columns={[
              { key: 'strategy', label: 'strategy' },
              { key: 'param', label: 'param' },
              { key: 'yaml', label: 'YAML', num: true, render: r => cell(r.yaml) },
              { key: 'runtime', label: 'runtime', num: true, render: r => cell(r.runtime) },
              { key: 'effective', label: 'effective', num: true, render: r => {
                const bad = r.source === 'YAML wins';
                return <span style={{ color: bad ? T.loss : undefined, fontWeight: bad ? 600 : undefined }}>{cell(r.effective)}</span>;
              }},
              { key: 'source', label: 'source', render: r => {
                if (r.source === 'runtime') return <span style={{ color: T.profit, fontSize: 10 }}>runtime</span>;
                if (r.source === 'YAML wins') return <span style={{ color: T.loss, fontSize: 10 }}>YAML WINS ⚠</span>;
                return <span style={{ color: T.label2, fontSize: 10 }}>YAML</span>;
              }},
            ]}
            rows={rows.map((r, i) => ({ ...r, _key: `${r.strategy}.${r.param}.${i}` }))}
          />
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 3: Build + lint + smoke**

```bash
cd frontend && npm run build && npm run lint
```

Load `/config`. If trace endpoint is missing, confirm the "backend didn't return a trace-compatible shape" message renders cleanly.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/ConfigOverrides.jsx
git commit -m "feat(fe): config-overrides trace page with YAML-wins conflict flag"
```

---

## Task 5: Signal Explorer

**Files:**
- Modify: `frontend/src/pages/SignalExplorer.jsx` (replace stub)

**Context for the subagent:**
The `signal_evaluations` and `strategy_decisions` tables carry >100k rows per day. Today's UIs (`Signals.jsx`, `V58Monitor.jsx`, `FactoryFloor.jsx`, `StrategyAnalysis.jsx`) each slice part of this data. This page is the unified slicer. Expected endpoint: `GET /api/v58/strategy-decisions?limit=&timeframe=&strategy_id=&regime=&conviction=` (see `reference_hub_api.md` memory). Aggregate in the browser into the following matrix: strategy × regime × conviction → win rate, sample count, avg edge.

Start with a reasonable default filter (`timeframe=5m`, `limit=1000`, `conviction=STRONG`) and let the user switch. Cap rendered table rows at 500; aggregate the full 1000 for the matrix.

- [ ] **Step 1: Implement SignalExplorer**

Replace `frontend/src/pages/SignalExplorer.jsx`:

```jsx
import React, { useEffect, useMemo, useState, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80', loss: '#f87171', warn: '#f59e0b',
  label: 'rgba(255,255,255,0.3)', label2: 'rgba(255,255,255,0.55)',
};

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

function wrOf(rows) {
  const n = rows.length;
  if (n === 0) return { n, wr: null };
  const wins = rows.filter(r => r.outcome === 'WIN' || r.won === true).length;
  return { n, wr: wins / n };
}

export default function SignalExplorer() {
  const api = useApi();
  const [tf, setTf] = useState('5m');
  const [conv, setConv] = useState(null);
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true); setErr(null);
    try {
      const params = new URLSearchParams({ limit: '1000', timeframe: tf });
      if (conv) params.set('conviction', conv);
      const r = await api.get(`/api/v58/strategy-decisions?${params.toString()}`);
      const data = r?.data ?? r;
      setRows(data?.rows ?? []);
    } catch (e) {
      setErr(e.message || 'load failed');
    } finally {
      setLoading(false);
    }
  }, [api, tf, conv]);
  useEffect(() => { load(); }, [load]);

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
        right={<div style={{ fontSize: 11, color: T.label2 }}>
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
          <div style={{ color: T.label, fontSize: 11 }}>{loading ? 'Loading…' : 'No decisions for these filters.'}</div>
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
                    const color = c.wr >= 0.65 ? T.profit : c.wr < 0.5 ? T.loss : T.warn;
                    return <td key={rg} style={{ textAlign: 'right', padding: '6px 10px', color, fontVariantNumeric: 'tabular-nums' }}>
                      {(c.wr * 100).toFixed(1)}% <span style={{ color: T.label, fontSize: 10 }}>({c.n})</span>
                    </td>;
                  })}
                  <td style={{ textAlign: 'right', padding: '6px 10px', fontVariantNumeric: 'tabular-nums' }}>
                    {matrix.cells[s].__total.wr == null ? '—' : `${(matrix.cells[s].__total.wr * 100).toFixed(1)}%`}
                    <span style={{ color: T.label, fontSize: 10 }}> ({matrix.cells[s].__total.n})</span>
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
```

- [ ] **Step 2: Build + lint + smoke**

```bash
cd frontend && npm run build && npm run lint
```

Load `/signals`. Matrix should render with color-coded WR cells (green ≥65%, amber 50-65%, red <50%). Filters trigger reload.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/SignalExplorer.jsx
git commit -m "feat(fe): signal explorer — strategy × regime WR matrix"
```

---

## Task 6: Trades Enhanced

**Files:**
- Modify: `frontend/src/pages/TradesEnhanced.jsx` (replace stub)

**Context for the subagent:**
Replaces the legacy `Trades.jsx` surface. Data: `GET /api/trades?limit=&strategy=&outcome=&account=primary`. The existing backend returns the expected fields: `id, ts, strategy, market, outcome, size, fill_price, exit_price, pnl, clob_oid, regime, conviction, skip_reason, dedup_key, route`. If a field is missing from the response (older rows), render `—`.

Filters (pills): strategy, outcome, range (24h/7d/30d). Top strip: 4 KPI cards (trades, win rate, net PnL, avg edge). Main table with the extra columns the original memory notes flagged missing (`skip_reason`, `dedup_key`, `regime`, `conviction`, `clob_oid`).

- [ ] **Step 1: Read legacy Trades for endpoint shape**

Read `frontend/src/pages/Trades.jsx` entirely to confirm the endpoint URL and response shape.

- [ ] **Step 2: Implement TradesEnhanced**

Replace `frontend/src/pages/TradesEnhanced.jsx`:

```jsx
import React, { useEffect, useState, useCallback, useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';

const T = {
  card: 'rgba(255,255,255,0.015)', border: 'rgba(255,255,255,0.06)',
  profit: '#4ade80', loss: '#f87171', warn: '#f59e0b',
  label: 'rgba(255,255,255,0.3)', label2: 'rgba(255,255,255,0.55)',
};

const STRATEGIES = [
  { label: 'all', value: null },
  { label: 'v4_down', value: 'v4_down_only' },
  { label: 'v4_fusion', value: 'v4_fusion' },
  { label: 'v4_up', value: 'v4_up_basic' },
  { label: 'v10_ghost', value: 'v10_ghost' },
];
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

const fmtUSD = n => (n == null ? '—' : `${n < 0 ? '-' : '+'}$${Math.abs(Number(n)).toFixed(2)}`);

export default function TradesEnhanced() {
  const api = useApi();
  const [strategy, setStrategy] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [rangeDays, setRangeDays] = useState(7);
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const params = new URLSearchParams({ limit: '500' });
      if (strategy) params.set('strategy', strategy);
      if (outcome) params.set('outcome', outcome);
      params.set('since_days', String(rangeDays));
      const r = await api.get(`/api/trades?${params.toString()}`);
      const data = r?.data ?? r;
      setRows(data?.rows ?? data?.trades ?? []);
    } catch (e) {
      setErr(e.message || 'load failed');
    }
  }, [api, strategy, outcome, rangeDays]);
  useEffect(() => { load(); }, [load]);

  const kpi = useMemo(() => {
    const closed = rows.filter(r => r.outcome === 'WIN' || r.outcome === 'LOSS');
    const wins = closed.filter(r => r.outcome === 'WIN').length;
    const net = closed.reduce((s, r) => s + (Number(r.pnl) || 0), 0);
    const edges = closed.map(r => Number(r.edge) || 0).filter(Boolean);
    const avgEdge = edges.length ? edges.reduce((a, b) => a + b, 0) / edges.length : null;
    return {
      n: rows.length,
      wr: closed.length ? wins / closed.length : null,
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
        <FilterPills label="strategy" options={STRATEGIES} value={strategy} onChange={setStrategy} />
        <FilterPills label="outcome" options={OUTCOMES} value={outcome} onChange={setOutcome} />
        <FilterPills label="range" options={RANGES} value={rangeDays} onChange={setRangeDays} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 12, marginBottom: 14 }}>
        <Stat lbl="Trades" val={kpi.n} sub={`${rangeDays}d window`} />
        <Stat lbl="Win rate" val={kpi.wr == null ? '—' : `${(kpi.wr * 100).toFixed(1)}%`} tone="good" />
        <Stat lbl="Net PnL" val={fmtUSD(kpi.net)} tone={kpi.net >= 0 ? 'good' : 'bad'} />
        <Stat lbl="Avg edge" val={kpi.avgEdge == null ? '—' : `${(kpi.avgEdge * 100).toFixed(1)}¢`} />
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        <DataTable
          emptyText="No trades in range."
          columns={[
            { key: 'ts', label: 'time', render: r => <span style={{ color: T.label2 }}>{(r.ts || r.created_at || '').toString().slice(11, 19)}</span> },
            { key: 'strategy', label: 'strategy' },
            { key: 'regime', label: 'regime' },
            { key: 'conviction', label: 'conv' },
            { key: 'market', label: 'market', render: r => <span style={{ color: T.label2 }}>{r.market || r.question || '—'}</span> },
            { key: 'outcome_side', label: 'side', render: r => r.side || r.outcome_side || '—' },
            { key: 'size', label: 'size', num: true, render: r => r.size != null ? `$${Number(r.size).toFixed(0)}` : '—' },
            { key: 'fill_price', label: 'fill', num: true, render: r => r.fill_price != null ? Number(r.fill_price).toFixed(2) : '—' },
            { key: 'exit_price', label: 'exit', num: true, render: r => r.exit_price != null ? Number(r.exit_price).toFixed(2) : '—' },
            { key: 'pnl', label: 'pnl', num: true, render: r => {
              const v = Number(r.pnl);
              if (Number.isNaN(v)) return <span style={{ color: T.label }}>—</span>;
              return <span style={{ color: v >= 0 ? T.profit : T.loss }}>{fmtUSD(v)}</span>;
            }},
            { key: 'outcome', label: 'out', render: r => r.outcome === 'WIN' ? <span style={{ color: T.profit, fontSize: 10 }}>WIN</span>
              : r.outcome === 'LOSS' ? <span style={{ color: T.loss, fontSize: 10 }}>LOSS</span>
              : <span style={{ color: T.label }}>open</span> },
            { key: 'clob_oid', label: 'CLOB oid', render: r => r.clob_oid ? <code style={{ fontSize: 10, color: T.label2 }}>{r.clob_oid.toString().slice(0, 10)}…</code> : '—' },
            { key: 'dedup_key', label: 'dedup', render: r => r.dedup_key ? <code style={{ fontSize: 10, color: T.label2 }}>{r.dedup_key}</code> : '' },
            { key: 'skip_reason', label: 'skip', render: r => r.skip_reason ? <span style={{ color: T.warn, fontSize: 10 }}>{r.skip_reason}</span> : '' },
          ]}
          rows={rows.map((r, i) => ({ ...r, _key: r.id ?? i }))}
        />
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Build + lint + smoke**

```bash
cd frontend && npm run build && npm run lint
```

Load `/trades`. Filter changes should re-fetch; missing fields should fall back to `—`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/TradesEnhanced.jsx
git commit -m "feat(fe): trades-enhanced with dedup/skip/regime/conviction/CLOB columns"
```

---

## Final review

- [ ] **Step 1: Dispatch final code reviewer**

After Tasks 1–6 are all green, dispatch a fresh code-reviewer subagent over the whole series of commits (`git log --oneline origin/develop..HEAD`) to flag cross-cutting concerns: duplicated theme tokens, inconsistent empty-state text, missing cleanup on `useEffect` polling, etc.

- [ ] **Step 2: Open PR**

Use `superpowers:finishing-a-development-branch` to open a PR against `develop`. Title: `feat(fe): tier-1 lean frontend + archive center`. Body: summary of archived routes, new endpoints consumed (none new), and the Tier-2 follow-ups explicitly deferred.
