import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider } from './auth/AuthContext.jsx';
import ProtectedRoute from './auth/ProtectedRoute.jsx';
import LoginPage from './auth/LoginPage.jsx';
import Layout from './components/Layout.jsx';
import Dashboard from './pages/Dashboard.jsx';
import Trades from './pages/Trades.jsx';
import Signals from './pages/Signals.jsx';
import PnL from './pages/PnL.jsx';
import System from './pages/System.jsx';
import Setup from './pages/Setup.jsx';
import PaperDashboard from './pages/PaperDashboard.jsx';
import TradingConfig from './pages/TradingConfig.jsx';
import Positions from './pages/Positions.jsx';
import Risk from './pages/Risk.jsx';
import Learn from './pages/Learn.jsx';
import Changelog from './pages/Changelog.jsx';
import PlaywrightDashboard from './pages/PlaywrightDashboard.jsx';
import TimesFM from './pages/TimesFM.jsx';
import Indicators from './pages/Indicators.jsx';
import V58Monitor from './pages/V58Monitor.jsx';
import WindowResults from './pages/WindowResults.jsx';
import StrategyAnalysis from './pages/StrategyAnalysis.jsx';
import LiveTrading from './pages/LiveTrading.jsx';
import AnalysisLibrary from './pages/AnalysisLibrary.jsx';
import FactoryFloor from './pages/FactoryFloor.jsx';
import Recommendations from './pages/Recommendations.jsx';
import ExecutionHQ from './pages/execution-hq/ExecutionHQ.jsx';
import MarginEngine from './pages/margin-engine/MarginEngine.jsx';
import CompositeSignals from './pages/CompositeSignals.jsx';
import AuditChecklist from './pages/AuditChecklist.jsx';
import V1Surface from './pages/data-surfaces/V1Surface.jsx';
import V2Surface from './pages/data-surfaces/V2Surface.jsx';
import V3Surface from './pages/data-surfaces/V3Surface.jsx';
import V4Surface from './pages/data-surfaces/V4Surface.jsx';
import Assembler1 from './pages/data-surfaces/Assembler1.jsx';
import Deployments from './pages/Deployments.jsx';
import Notes from './pages/Notes.jsx';
import Schema from './pages/Schema.jsx';
// CFG-05: new DB-config browser. The legacy 13-key page is preserved at
// /legacy-config so any in-flight bookmarks survive.
import Config from './pages/Config.jsx';
import LegacyConfig from './pages/LegacyConfig.jsx';

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public */}
          <Route path="/login" element={<LoginPage />} />

          {/* Protected — all behind Layout */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="paper" element={<PaperDashboard />} />
            {/* PaperTrading removed — duplicate of Dashboard, use /paper instead */}
            <Route path="positions" element={<Positions />} />
            <Route path="trades" element={<Trades />} />
            <Route path="signals" element={<Signals />} />
            <Route path="pnl" element={<PnL />} />
            <Route path="risk" element={<Risk />} />
            <Route path="system" element={<System />} />
            {/* CFG-05: /config now points at the new DB-config browser.
                The legacy 13-key page lives at /legacy-config; the bundle
                editor still owns /trading-config until CFG-06 lands. */}
            <Route path="config" element={<Config />} />
            <Route path="legacy-config" element={<LegacyConfig />} />
            <Route path="trading-config" element={<TradingConfig />} />
            <Route path="setup" element={<Setup />} />
            <Route path="learn" element={<Learn />} />
            <Route path="changelog" element={<Changelog />} />
            <Route path="playwright" element={<PlaywrightDashboard />} />
            <Route path="timesfm" element={<TimesFM />} />
            <Route path="indicators" element={<Indicators />} />
            <Route path="v58" element={<V58Monitor />} />
            <Route path="windows" element={<WindowResults />} />
            <Route path="strategy" element={<StrategyAnalysis />} />
            <Route path="live" element={<LiveTrading />} />
            <Route path="analysis" element={<AnalysisLibrary />} />
            <Route path="factory" element={<FactoryFloor />} />
            {/* UI-02: Multi-market HQ monitors — 4 assets × 2 timeframes.
                The legacy /execution-hq path redirects to the BTC 5m default
                so bookmarks and the old sidebar link keep working. */}
            <Route path="execution-hq" element={<Navigate to="/execution-hq/btc/5m" replace />} />
            <Route path="execution-hq/:asset/:timeframe" element={<ExecutionHQ />} />
            <Route path="margin" element={<MarginEngine />} />
            <Route path="composite" element={<CompositeSignals />} />
            <Route path="recommendations" element={<Recommendations />} />
            <Route path="audit" element={<AuditChecklist />} />
            <Route path="data/v1" element={<V1Surface />} />
            <Route path="data/v2" element={<V2Surface />} />
            <Route path="data/v3" element={<V3Surface />} />
            <Route path="data/v4" element={<V4Surface />} />
            <Route path="data/assembler1" element={<Assembler1 />} />
            <Route path="deployments" element={<Deployments />} />
            <Route path="notes" element={<Notes />} />
            <Route path="schema" element={<Schema />} />
          </Route>

          {/* 404 fallback */}
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
