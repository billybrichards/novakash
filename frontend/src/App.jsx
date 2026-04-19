import React, { Suspense, lazy } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext.jsx';
import ProtectedRoute from './auth/ProtectedRoute.jsx';
import LoginPage from './auth/LoginPage.jsx';

// New shell + pages (eager, critical path)
import AppShell from './layouts/AppShell.jsx';
import UnifiedDashboard from './pages/UnifiedDashboard.jsx';
import TradesEnhanced from './pages/TradesEnhanced.jsx';
import SignalExplorer from './pages/SignalExplorer.jsx';
import GateTraces from './pages/GateTraces.jsx';
import ConfigOverrides from './pages/ConfigOverrides.jsx';
import AuditTasks from './pages/AuditTasks.jsx';
import PnL from './pages/PnL.jsx';
import System from './pages/System.jsx';
import Wallet from './pages/Wallet.jsx';
import Strategies from './pages/Strategies.jsx';
import Monitor from './pages/Monitor.jsx';

// Archive wrapper
import ArchiveCenter from './pages/archive/ArchiveCenter.jsx';
import ArchivedPageBanner from './pages/archive/ArchivedPageBanner.jsx';
import Loading from './components/shared/Loading.jsx';

// Archive components (lazy-loaded — ~650KB off the main chunk)
const PaperDashboard      = lazy(() => import('./pages/PaperDashboard.jsx'));
const PlaywrightDashboard = lazy(() => import('./pages/PlaywrightDashboard.jsx'));
const ExecutionHQ         = lazy(() => import('./pages/execution-hq/ExecutionHQ.jsx'));
const LiveTrading         = lazy(() => import('./pages/LiveTrading.jsx'));
const FactoryFloor        = lazy(() => import('./pages/FactoryFloor.jsx'));
const V58Monitor          = lazy(() => import('./pages/V58Monitor.jsx'));
const WindowResults       = lazy(() => import('./pages/WindowResults.jsx'));
const StrategyAnalysis    = lazy(() => import('./pages/StrategyAnalysis.jsx'));
const TimesFM             = lazy(() => import('./pages/TimesFM.jsx'));
const CompositeSignals    = lazy(() => import('./pages/CompositeSignals.jsx'));
const MarginEngine        = lazy(() => import('./pages/margin-engine/MarginEngine.jsx'));
const Positions           = lazy(() => import('./pages/Positions.jsx'));
const Risk                = lazy(() => import('./pages/Risk.jsx'));
const Signals             = lazy(() => import('./pages/Signals.jsx'));
const Trades              = lazy(() => import('./pages/Trades.jsx'));
const Dashboard           = lazy(() => import('./pages/Dashboard.jsx'));
const Setup               = lazy(() => import('./pages/Setup.jsx'));
const TradingConfig       = lazy(() => import('./pages/TradingConfig.jsx'));
const LegacyConfig        = lazy(() => import('./pages/LegacyConfig.jsx'));
const Config              = lazy(() => import('./pages/Config.jsx'));
const AuditChecklist      = lazy(() => import('./pages/AuditChecklist.jsx'));
const MarginStrategies    = lazy(() => import('./pages/MarginStrategies.jsx'));
const Deployments         = lazy(() => import('./pages/Deployments.jsx'));
const Notes               = lazy(() => import('./pages/Notes.jsx'));
const Schema              = lazy(() => import('./pages/Schema.jsx'));
const SignalComparison    = lazy(() => import('./pages/SignalComparison.jsx'));
const AgentOps            = lazy(() => import('./pages/AgentOps.jsx'));
const Telegram            = lazy(() => import('./pages/Telegram.jsx'));
const FifteenMinMonitor   = lazy(() => import('./pages/FifteenMinMonitor.jsx'));
// Polymarket subtree
const PolymarketOverview  = lazy(() => import('./pages/polymarket/Overview.jsx'));
const PolymarketMonitor   = lazy(() => import('./pages/polymarket/Monitor.jsx'));
const LiveFloor           = lazy(() => import('./pages/polymarket/LiveFloor.jsx'));
const PolymarketEvaluate  = lazy(() => import('./pages/polymarket/Evaluate.jsx'));
const StrategyLab         = lazy(() => import('./pages/polymarket/StrategyLab.jsx'));
const StrategyHistory     = lazy(() => import('./pages/polymarket/StrategyHistory.jsx'));
const StrategyConfigs     = lazy(() => import('./pages/polymarket/StrategyConfigs.jsx'));
const GatePipelineMonitor = lazy(() => import('./pages/polymarket/GatePipelineMonitor.jsx'));
const DataHealth          = lazy(() => import('./pages/polymarket/DataHealth.jsx'));
const StrategyCommand     = lazy(() => import('./pages/polymarket/StrategyCommand.jsx'));
const StrategyFloor       = lazy(() => import('./pages/polymarket/StrategyFloor.jsx'));
// Data surfaces
const V1Surface           = lazy(() => import('./pages/data-surfaces/V1Surface.jsx'));
const V2Surface           = lazy(() => import('./pages/data-surfaces/V2Surface.jsx'));
const V3Surface           = lazy(() => import('./pages/data-surfaces/V3Surface.jsx'));
const V4Surface           = lazy(() => import('./pages/data-surfaces/V4Surface.jsx'));
const Assembler1          = lazy(() => import('./pages/data-surfaces/Assembler1.jsx'));

import { ARCHIVED_PAGES, ARCHIVED_STRATEGY_FLOORS } from './nav/navigation.js';

const ARCHIVE_COMPONENTS = {
  PaperDashboard, PlaywrightDashboard, ExecutionHQ, LiveTrading, FactoryFloor,
  V58Monitor, WindowResults, StrategyAnalysis, TimesFM, CompositeSignals,
  MarginEngine, Positions, Risk, Signals, Trades, Dashboard,
  Setup, TradingConfig, LegacyConfig, Config, AuditChecklist,
  MarginStrategies, Deployments, Notes, Schema, SignalComparison,
  AgentOps, Telegram, FifteenMinMonitor,
  PolymarketOverview, PolymarketMonitor, LiveFloor, PolymarketEvaluate,
  StrategyLab, StrategyHistory, StrategyConfigs, GatePipelineMonitor,
  DataHealth, StrategyCommand,
  V1Surface, V2Surface, V3Surface, V4Surface, Assembler1,
};

function wrapArchived(meta) {
  const Component = ARCHIVE_COMPONENTS[meta.importName];
  if (!Component) {
    if (import.meta.env.DEV) {
      console.warn(`[archive] Missing component for ${meta.path} (importName=${meta.importName})`);
    }
    return null;
  }
  return (
    <Suspense fallback={<Loading label="Loading archived page…" />}>
      <ArchivedPageBanner replacedBy={meta.replacedBy} note={meta.note}>
        <Component />
      </ArchivedPageBanner>
    </Suspense>
  );
}

// Props-bearing archived routes (StrategyFloor needs strategyId).
function wrapArchivedFloor(meta) {
  return (
    <Suspense fallback={<Loading label="Loading archived page…" />}>
      <ArchivedPageBanner replacedBy={meta.replacedBy}>
        <StrategyFloor strategyId={meta.strategyId} />
      </ArchivedPageBanner>
    </Suspense>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />

          {/* New lean shell — Tier-1 active routes */}
          <Route path="/" element={<ProtectedRoute><AppShell /></ProtectedRoute>}>
            <Route index element={<UnifiedDashboard />} />
            <Route path="trades" element={<TradesEnhanced />} />
            <Route path="wallet" element={<Wallet />} />
            <Route path="signals" element={<SignalExplorer />} />
            <Route path="gate-traces" element={<GateTraces />} />
            <Route path="strategies" element={<Strategies />} />
            <Route path="config" element={<ConfigOverrides />} />
            <Route path="audit" element={<AuditTasks />} />
            <Route path="pnl" element={<PnL />} />
            <Route path="system" element={<System />} />
            <Route path="monitor" element={<Monitor />} />
            {/* Pages promoted out of /archive — now first-class nav entries. */}
            <Route path="analysis" element={
              <Suspense fallback={<Loading label="Loading analysis…" />}>
                <StrategyAnalysis />
              </Suspense>
            } />
            <Route path="gate-matrix" element={
              <Suspense fallback={<Loading label="Loading gate matrix…" />}>
                <GatePipelineMonitor />
              </Suspense>
            } />
            <Route path="windows" element={
              <Suspense fallback={<Loading label="Loading windows…" />}>
                <WindowResults />
              </Suspense>
            } />
            <Route path="notes" element={
              <Suspense fallback={<Loading label="Loading notes…" />}>
                <Notes />
              </Suspense>
            } />
            <Route path="schema" element={
              <Suspense fallback={<Loading label="Loading schema…" />}>
                <Schema />
              </Suspense>
            } />
            <Route path="archive" element={<ArchiveCenter />} />
          </Route>

          {/* Archive namespace — AppShell, each page wrapped in banner */}
          <Route path="/archive" element={<ProtectedRoute><AppShell /></ProtectedRoute>}>
            {ARCHIVED_PAGES.map(meta => {
              const element = wrapArchived(meta);
              if (!element) return null;
              const sub = meta.path.replace(/^\/archive\//, '');
              return <Route key={meta.path} path={sub} element={element} />;
            })}
            {ARCHIVED_STRATEGY_FLOORS.map(meta => {
              const sub = meta.path.replace(/^\/archive\//, '');
              return <Route key={meta.path} path={sub} element={wrapArchivedFloor(meta)} />;
            })}
            {/* Execution HQ has a parameterized subroute — preserve for deep links */}
            <Route path="execution-hq/:asset/:timeframe" element={
              <Suspense fallback={<Loading label="Loading archived page…" />}>
                <ArchivedPageBanner replacedBy="Dashboard">
                  <ExecutionHQ />
                </ArchivedPageBanner>
              </Suspense>
            } />
          </Route>

          {/*
           * Redirects for externally-linked legacy URLs (Telegram alerts, saved bookmarks).
           * Internal-only dev pages (playwright, factory, schema, data surfaces, etc.) have
           * no redirect — they fall through to the catch-all below.
           */}
          {/* Core operational pages — most likely to appear in Telegram alerts or user bookmarks */}
          <Route path="/dashboard" element={<Navigate to="/" replace />} />
          <Route path="/live" element={<Navigate to="/archive/live" replace />} />
          <Route path="/paper" element={<Navigate to="/archive/paper" replace />} />
          <Route path="/positions" element={<Navigate to="/archive/positions" replace />} />
          <Route path="/risk" element={<Navigate to="/archive/risk" replace />} />
          <Route path="/execution-hq" element={<Navigate to="/archive/execution-hq" replace />} />
          {/* /windows now active (WindowResults promoted out of archive). */}
          <Route path="/v58" element={<Navigate to="/archive/v58" replace />} />
          <Route path="/strategy" element={<Navigate to="/archive/strategy" replace />} />
          <Route path="/timesfm" element={<Navigate to="/archive/timesfm" replace />} />
          <Route path="/telegram" element={<Navigate to="/archive/telegram" replace />} />
          {/* Config redirect: /trading-config → active Tier-1 /config page */}
          <Route path="/trading-config" element={<Navigate to="/config" replace />} />
          {/* Promoted-out-of-archive redirects (keep bookmarks/TG alerts working). */}
          <Route path="/archive/strategy" element={<Navigate to="/analysis" replace />} />
          <Route path="/archive/notes" element={<Navigate to="/notes" replace />} />
          <Route path="/archive/schema" element={<Navigate to="/schema" replace />} />
          <Route path="/archive/polymarket/gate-monitor" element={<Navigate to="/gate-matrix" replace />} />
          <Route path="/archive/windows" element={<Navigate to="/windows" replace />} />
          {/* Polymarket subtree — main entry point and operational sub-pages */}
          <Route path="/polymarket" element={<Navigate to="/archive/polymarket/monitor" replace />} />
          <Route path="/polymarket/monitor" element={<Navigate to="/archive/polymarket/monitor" replace />} />
          <Route path="/polymarket/overview" element={<Navigate to="/archive/polymarket/overview" replace />} />
          <Route path="/polymarket/floor" element={<Navigate to="/archive/polymarket/floor" replace />} />
          <Route path="/polymarket/strategy-history" element={<Navigate to="/archive/polymarket/strategy-history" replace />} />
          <Route path="/polymarket/strategies" element={<Navigate to="/archive/polymarket/strategies" replace />} />
          <Route path="/polymarket/gate-monitor" element={<Navigate to="/archive/polymarket/gate-monitor" replace />} />
          <Route path="/polymarket/command" element={<Navigate to="/archive/polymarket/command" replace />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
