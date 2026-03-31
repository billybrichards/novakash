import React from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext.jsx';
import LiveToggle from './LiveToggle.jsx';

/**
 * Layout — Dark sidebar nav + main content area.
 *
 * Top bar: logo + LiveToggle (PAPER + LIVE independent toggles)
 * When live is active, header gets a subtle red border/glow.
 */
export default function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();

  const navItems = [
    { path: '/dashboard',       label: 'Dashboard',       icon: '📊' },
    { path: '/paper',           label: 'Paper Trading',   icon: '📄' },
    { path: '/trades',          label: 'Trades',          icon: '📋' },
    { path: '/signals',         label: 'Signals',         icon: '📡' },
    { path: '/pnl',             label: 'P&L',             icon: '💰' },
    { path: '/system',          label: 'System',          icon: '🖥️' },
    { path: '/trading-config',  label: 'Trading Config',  icon: '⚙️' },
    { path: '/config',          label: 'Config',          icon: '🔩' },
  ];

  const bottomNavItems = [
    { path: '/setup', label: 'Setup', icon: '🔧' },
  ];

  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh' }} className="flex flex-col lg:flex-row">
      {/* Sidebar */}
      <aside
        style={{
          background: 'rgba(0,0,0,0.2)',
          borderRight: '1px solid var(--border)',
        }}
        className="w-full lg:w-64 flex-shrink-0 p-6 flex flex-col"
      >
        {/* Logo + LiveToggle (stacked vertically on sidebar for mobile) */}
        <div className="mb-6">
          <Link to="/dashboard" className="block mb-4">
            <div style={{ color: 'var(--accent-purple)' }} className="text-2xl font-bold tracking-tight">
              ₿ BTC Trader
            </div>
          </Link>

          {/* Live/Paper toggles — shown in sidebar */}
          <div className="lg:hidden">
            <LiveToggle />
          </div>
        </div>

        {/* Main Nav */}
        <nav className="space-y-1 flex-1">
          {navItems.map(item => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                style={{
                  background: isActive ? 'rgba(168, 85, 247, 0.1)' : 'transparent',
                  color: isActive ? 'var(--accent-purple)' : 'var(--text-secondary)',
                  borderLeft: isActive ? '2px solid var(--accent-purple)' : '2px solid transparent',
                }}
                className="flex items-center gap-2.5 px-4 py-2.5 rounded transition-colors text-sm"
              >
                <span className="text-base leading-none">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* Bottom section: Setup + Logout */}
        <div style={{ borderTop: '1px solid var(--border)' }} className="pt-4 space-y-1">
          {bottomNavItems.map(item => {
            const isActive = location.pathname === item.path;
            return (
              <Link
                key={item.path}
                to={item.path}
                style={{
                  background: isActive ? 'rgba(168, 85, 247, 0.1)' : 'transparent',
                  color: isActive ? 'var(--accent-purple)' : 'var(--text-secondary)',
                  borderLeft: isActive ? '2px solid var(--accent-purple)' : '2px solid transparent',
                }}
                className="flex items-center gap-2.5 px-4 py-2.5 rounded transition-colors text-sm"
              >
                <span className="text-base leading-none">{item.icon}</span>
                {item.label}
              </Link>
            );
          })}

          <div className="pt-2 mt-1">
            <div style={{ color: 'var(--text-secondary)' }} className="text-xs px-4 mb-2">
              Logged in as {user?.username}
            </div>
            <button
              onClick={logout}
              className="w-full py-2 px-4 rounded text-sm font-medium transition-opacity text-left"
              style={{
                background: 'rgba(255,255,255,0.05)',
                color: 'rgba(255,255,255,0.5)',
              }}
            >
              Logout
            </button>
          </div>
        </div>
      </aside>

      {/* Main Content + Top Bar */}
      <div className="flex-1 flex flex-col overflow-hidden">
        {/* Top bar — desktop only — contains LiveToggle */}
        <LiveToggleHeader />

        {/* Page content */}
        <main className="flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/**
 * LiveToggleHeader — the thin top bar on desktop that hosts the two mode toggles.
 * Reads live status from LiveToggle's internal state to apply red glow.
 */
function LiveToggleHeader() {
  return (
    <LiveToggleHeaderInner />
  );
}

function LiveToggleHeaderInner() {
  // We render LiveToggle inside the header — it manages its own state.
  // The red-border effect when live is on is handled via the LiveToggle component
  // rendering a sibling style element.
  return (
    <div
      id="live-toggle-header"
      className="hidden lg:flex"
      style={{
        borderBottom: '1px solid rgba(255,255,255,0.05)',
        padding: '8px 20px',
        justifyContent: 'flex-end',
        alignItems: 'center',
        background: 'rgba(0,0,0,0.15)',
        minHeight: 48,
        gap: 12,
      }}
    >
      <LiveToggle />
    </div>
  );
}
