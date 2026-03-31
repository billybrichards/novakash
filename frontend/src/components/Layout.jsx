import React from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext.jsx';

/**
 * Layout — Dark sidebar nav + main content area.
 *
 * Nav items: Dashboard, Trades, Signals, P&L, System, Config
 * Header: Logo + user menu (logout)
 */
export default function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();

  const navItems = [
    { path: '/dashboard', label: 'Dashboard' },
    { path: '/trades', label: 'Trades' },
    { path: '/signals', label: 'Signals' },
    { path: '/pnl', label: 'P&L' },
    { path: '/system', label: 'System' },
    { path: '/config', label: 'Config' },
  ];

  return (
    <div style={{ background: 'var(--bg)', minHeight: '100vh' }} className="flex flex-col lg:flex-row">
      {/* Sidebar */}
      <aside
        style={{
          background: 'rgba(0, 0, 0, 0.2)',
          borderRight: '1px solid var(--border)',
        }}
        className="w-full lg:w-64 flex-shrink-0 p-6"
      >
        {/* Logo */}
        <Link to="/dashboard" className="block mb-8">
          <div style={{ color: 'var(--accent-purple)' }} className="text-2xl font-bold tracking-tight">
            ₿ BTC Trader
          </div>
        </Link>

        {/* Nav */}
        <nav className="space-y-2">
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
                className="block px-4 py-3 rounded transition-colors"
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* Footer */}
        <div style={{ borderTop: '1px solid var(--border)' }} className="mt-auto pt-6">
          <div style={{ color: 'var(--text-secondary)' }} className="text-xs mb-3">
            Logged in as {user?.username}
          </div>
          <button
            onClick={logout}
            className="w-full py-2 rounded text-sm font-semibold transition-opacity"
            style={{
              background: 'rgba(255,255,255,0.05)',
              color: 'var(--text-primary)',
            }}
          >
            Logout
          </button>
        </div>
      </aside>

      {/* Main Content */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
