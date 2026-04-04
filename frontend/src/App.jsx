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
import PaperTrading from './pages/PaperTrading.jsx';
import PaperDashboard from './pages/PaperDashboard.jsx';
import TradingConfig from './pages/TradingConfig.jsx';
import Positions from './pages/Positions.jsx';
import Risk from './pages/Risk.jsx';
import Learn from './pages/Learn.jsx';
import Changelog from './pages/Changelog.jsx';
import PlaywrightDashboard from './pages/PlaywrightDashboard.jsx';
import TimesFM from './pages/TimesFM.jsx';
import Indicators from './pages/Indicators.jsx';

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
            <Route path="paper-trading" element={<PaperTrading />} />
            <Route path="positions" element={<Positions />} />
            <Route path="trades" element={<Trades />} />
            <Route path="signals" element={<Signals />} />
            <Route path="pnl" element={<PnL />} />
            <Route path="risk" element={<Risk />} />
            <Route path="system" element={<System />} />
            <Route path="config" element={<TradingConfig />} />
            <Route path="trading-config" element={<TradingConfig />} />
            <Route path="setup" element={<Setup />} />
            <Route path="learn" element={<Learn />} />
            <Route path="changelog" element={<Changelog />} />
            <Route path="playwright" element={<PlaywrightDashboard />} />
            <Route path="timesfm" element={<TimesFM />} />
            <Route path="indicators" element={<Indicators />} />
          </Route>

          {/* 404 fallback */}
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
