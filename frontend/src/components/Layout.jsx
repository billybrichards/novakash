import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext.jsx';
import LiveToggle from './LiveToggle.jsx';
import { useApi } from '../hooks/useApi.js';
import { useTheme } from '../contexts/ThemeContext.jsx';

/**
 * Layout — Mobile-first dark/light nav + main content area.
 *
 * Desktop: fixed sidebar (220px) + top bar with config dropdown
 * Mobile:  top bar with hamburger -> side drawer, bottom tab bar
 *
 * NAV-02 (2026-04-12): Restructured to 3 primary sections + collapsible legacy.
 */

// ── Nav Configuration ────────────────────────────────────────────────────────
// 3 primary sections: POLYMARKET, MARGIN ENGINE, SYSTEM
// + 1 collapsible LEGACY section at bottom

const NAV_SECTIONS = [
  {
    id: 'polymarket',
    title: 'POLYMARKET',
    color: '#06b6d4', // cyan accent
    items: [
      { path: '/polymarket/overview', label: 'Overview', icon: 'O' },
      { path: '/polymarket/monitor', label: 'Monitor', icon: 'M' },
      { path: '/polymarket/floor', label: 'Floor', icon: 'F' },
      { path: '/polymarket/evaluate', label: 'Evaluate', icon: 'E' },
      { path: '/polymarket/strategy-lab', label: 'Strategy Lab', icon: 'S' },
      { path: '/config', label: 'Configure', icon: 'C' },
    ],
  },
  {
    id: 'margin',
    title: 'MARGIN ENGINE',
    color: '#a855f7', // purple accent
    items: [
      { path: '/margin', label: 'Monitor', icon: 'M' },
      { path: '/config?service=margin_engine', label: 'Configure', icon: 'C' },
    ],
  },
  {
    id: 'system',
    title: 'SYSTEM',
    color: '#64748b', // grey accent
    items: [
      { path: '/system', label: 'Status', icon: 'St' },
      { path: '/schema', label: 'Schema', icon: 'Sc' },
      { path: '/deployments', label: 'Deploys', icon: 'D' },
      { path: '/config', label: 'Config', icon: 'Cf', sectionDedupe: 'system' },
      { path: '/audit', label: 'Audit', icon: 'A' },
      { path: '/notes', label: 'Notes', icon: 'N' },
    ],
  },
];

const LEGACY_ITEMS = [
  { path: '/dashboard', label: 'Dashboard' },
  { path: '/factory', label: 'Factory Floor' },
  { path: '/execution-hq/btc/5m', label: 'Execution HQ' },
  { path: '/signals', label: 'Signals' },
  { path: '/trades', label: 'Trades' },
  { path: '/pnl', label: 'P&L' },
  { path: '/v58', label: 'V58 Monitor' },
  { path: '/windows', label: 'Windows' },
  { path: '/live', label: 'Live' },
  { path: '/timesfm', label: 'TimesFM' },
];

// Bottom tab bar items (mobile) — most important primary pages
const TAB_ITEMS = [
  { path: '/polymarket/monitor', label: 'Monitor', icon: 'M' },
  { path: '/polymarket/evaluate', label: 'Eval', icon: 'E' },
  { path: '/margin', label: 'Margin', icon: 'P' },
  { path: '/system', label: 'System', icon: 'S' },
  { path: '/config', label: 'Config', icon: 'C' },
];

// ── Config Dropdown ──────────────────────────────────────────────────────────

function ThresholdBar({ label, value, max = 1, color = '#a855f7', unit = '' }) {
  const pct = Math.min(100, (parseFloat(value) / max) * 100);
  return (
    <div style={{ marginBottom: 7 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 3 }}>
        <span style={{ color: 'rgba(255,255,255,0.4)', fontSize: 10 }}>{label}</span>
        <span style={{ color, fontSize: 10, fontFamily: 'IBM Plex Mono, monospace', fontWeight: 600 }}>
          {unit === '%' ? `${(parseFloat(value) * 100).toFixed(1)}%` : parseFloat(value).toFixed(2)}
        </span>
      </div>
      <div style={{ height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, overflow: 'hidden' }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: color,
          borderRadius: 2,
          transition: 'width 300ms ease-out',
          boxShadow: `0 0 6px ${color}55`,
        }} />
      </div>
    </div>
  );
}

function ConfigDropdown({ onClose }) {
  const api = useApi();
  const [data, setData] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    const load = async () => {
      try {
        const [paperRes, liveRes] = await Promise.allSettled([
          api('GET', '/trading-config/active/paper'),
          api('GET', '/trading-config/active/live'),
        ]);
        setData({
          paper: paperRes.status === 'fulfilled' ? paperRes.value?.data?.config : null,
          live: liveRes.status === 'fulfilled' ? liveRes.value?.data?.config : null,
        });
      } catch { setData({}); }
    };
    load();
  }, [api]);

  // Close on outside click
  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) onClose();
    };
    setTimeout(() => document.addEventListener('mousedown', handler), 10);
    return () => document.removeEventListener('mousedown', handler);
  }, [onClose]);

  const paperCfg = data?.paper?.config || {};
  const paperName = data?.paper?.name || '\u2014';
  const paperVersion = data?.paper?.version || '\u2014';
  const liveName = data?.live?.name;
  const liveVersion = data?.live?.version;

  return (
    <div
      ref={ref}
      className="config-dropdown-panel"
      style={{
        position: 'absolute',
        top: 'calc(100% + 8px)',
        right: 0,
        width: 300,
        background: '#0d0d1a',
        border: '1px solid rgba(255,255,255,0.08)',
        borderRadius: 10,
        boxShadow: '0 8px 40px rgba(0,0,0,0.6)',
        zIndex: 1000,
        animation: 'dropdownIn 180ms ease-out',
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div style={{
        padding: '10px 14px',
        borderBottom: '1px solid rgba(255,255,255,0.06)',
        background: 'rgba(168,85,247,0.05)',
      }}>
        <div style={{
          color: '#a855f7',
          fontFamily: 'IBM Plex Mono, monospace',
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: '0.1em',
        }}>
          ACTIVE CONFIGS
        </div>
      </div>

      <div style={{ padding: '12px 14px' }}>
        {/* Paper config */}
        <div style={{ marginBottom: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: 10 }}>Paper:</span>
            <span style={{ color: '#a855f7', fontFamily: 'IBM Plex Mono, monospace', fontSize: 11, fontWeight: 600 }}>
              {paperName}
            </span>
            {paperVersion !== '\u2014' && (
              <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 10 }}>v{paperVersion}</span>
            )}
          </div>
        </div>

        {/* Live config */}
        {liveName && (
          <div style={{ marginBottom: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
              <span style={{ color: 'rgba(255,255,255,0.5)', fontSize: 10 }}>Live:</span>
              <span style={{ color: '#f87171', fontFamily: 'IBM Plex Mono, monospace', fontSize: 11, fontWeight: 600 }}>
                {liveName}
              </span>
              <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 10 }}>v{liveVersion}</span>
            </div>
          </div>
        )}

        {/* Threshold bars */}
        <div style={{
          borderTop: '1px solid rgba(255,255,255,0.05)',
          paddingTop: 10,
          marginBottom: 10,
        }}>
          <ThresholdBar
            label="VPIN Informed"
            value={paperCfg.vpin_informed_threshold ?? 0.55}
            color="#f59e0b"
          />
          <ThresholdBar
            label="VPIN Cascade"
            value={paperCfg.vpin_cascade_threshold ?? 0.70}
            color="#f87171"
          />
          <ThresholdBar
            label="Arb Min Spread"
            value={paperCfg.arb_min_spread ?? 0.015}
            max={0.05}
            color="#4ade80"
            unit="%"
          />
          <ThresholdBar
            label="Bet Fraction"
            value={paperCfg.bet_fraction ?? 0.025}
            max={0.20}
            color="#a855f7"
            unit="%"
          />
          <ThresholdBar
            label="Max Drawdown"
            value={paperCfg.max_drawdown_pct ?? 0.10}
            color="#06b6d4"
            unit="%"
          />
        </div>

        {/* Mode toggles */}
        <div style={{ marginBottom: 12 }}>
          <LiveToggle compact />
        </div>

        {/* Edit link */}
        <Link
          to="/trading-config"
          onClick={onClose}
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '8px 12px',
            borderRadius: 6,
            background: 'rgba(168,85,247,0.08)',
            border: '1px solid rgba(168,85,247,0.2)',
            color: '#a855f7',
            textDecoration: 'none',
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 11,
            fontWeight: 600,
            transition: 'all 150ms',
          }}
        >
          <span>Edit Config</span>
          <span>&rarr;</span>
        </Link>
      </div>
    </div>
  );
}

// ── Config pill button in top bar ────────────────────────────────────────────
function ConfigPill() {
  const api = useApi();
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState(null);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await api('GET', '/trading-config/active/paper');
        const cfg = res.data?.config?.config || {};
        setSummary({
          vpin: cfg.vpin_cascade_threshold ?? 0.70,
          cascade: cfg.vpin_cascade_threshold ?? 0.70,
          arb: cfg.arb_min_spread ?? 0.015,
          bankroll: cfg.starting_bankroll ?? 25,
        });
      } catch { /* use defaults */ }
    };
    load();
  }, [api]);

  const s = summary || { vpin: 0.85, cascade: 0.70, arb: 0.015, bankroll: 25 };

  return (
    <div style={{ position: 'relative' }}>
      <button
        onClick={() => setOpen(o => !o)}
        className="config-pill-btn"
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '5px 10px',
          borderRadius: 6,
          border: `1px solid ${open ? 'rgba(168,85,247,0.4)' : 'rgba(255,255,255,0.08)'}`,
          background: open ? 'rgba(168,85,247,0.08)' : 'rgba(255,255,255,0.03)',
          cursor: 'pointer',
          transition: 'all 200ms ease-out',
          color: 'rgba(255,255,255,0.7)',
          fontFamily: 'IBM Plex Mono, monospace',
          fontSize: 11,
          whiteSpace: 'nowrap',
          minHeight: 36,
        }}
      >
        <span className="pill-icon" style={{ fontSize: 13 }}>&#9881;</span>
        <span className="pill-text">
          VPIN: <span style={{ color: '#f59e0b' }}>{s.vpin.toFixed(2)}</span>
          <span style={{ color: 'rgba(255,255,255,0.2)' }}> | </span>
          Casc: <span style={{ color: '#f87171' }}>{s.cascade.toFixed(2)}</span>
          <span style={{ color: 'rgba(255,255,255,0.2)' }}> | </span>
          Arb: <span style={{ color: '#4ade80' }}>{(s.arb * 100).toFixed(1)}%</span>
          <span style={{ color: 'rgba(255,255,255,0.2)' }}> | </span>
          <span style={{ color: '#a855f7' }}>${s.bankroll}</span>
        </span>
        <span style={{
          color: 'rgba(255,255,255,0.3)',
          fontSize: 9,
          transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
          transition: 'transform 200ms ease-out',
        }}>&#9660;</span>
      </button>

      {open && <ConfigDropdown onClose={() => setOpen(false)} />}
    </div>
  );
}

// ── Theme Toggle Button ──────────────────────────────────────────────────────
function ThemeToggle() {
  const { mode, toggle } = useTheme();
  const isLight = mode === 'light';
  return (
    <button
      onClick={toggle}
      title={isLight ? 'Switch to dark mode' : 'Switch to light mode'}
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        width: 36,
        height: 36,
        borderRadius: 6,
        border: '1px solid rgba(255,255,255,0.08)',
        background: isLight ? 'rgba(15,23,42,0.08)' : 'rgba(255,255,255,0.03)',
        cursor: 'pointer',
        transition: 'all 200ms ease-out',
        color: isLight ? '#0f172a' : 'rgba(255,255,255,0.7)',
        fontSize: 16,
        lineHeight: 1,
        padding: 0,
        flexShrink: 0,
      }}
    >
      {isLight ? '\u263E' : '\u2600'}
    </button>
  );
}

// ── Main Layout ──────────────────────────────────────────────────────────────

export default function Layout() {
  const { user, logout } = useAuth();
  const { mode } = useTheme();
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [legacyExpanded, setLegacyExpanded] = useState(false);

  // Close sidebar on route change (mobile)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  const isActive = (path) => {
    // For paths with query strings, match just the pathname portion
    const basePath = path.split('?')[0];
    return location.pathname === basePath;
  };

  const isLight = mode === 'light';

  // Sidebar colors that adapt to theme
  const sidebarBg = isLight ? 'rgba(241, 245, 249, 0.95)' : 'rgba(0,0,0,0.18)';
  const sidebarBorder = isLight ? 'rgba(203, 213, 225, 0.6)' : 'rgba(255,255,255,0.05)';
  const headerBg = isLight ? 'rgba(255,255,255,0.8)' : 'rgba(0,0,0,0.2)';
  const headerBorder = isLight ? 'rgba(203,213,225,0.4)' : 'rgba(255,255,255,0.05)';
  const textColor = isLight ? 'rgba(15,23,42,0.7)' : 'rgba(255,255,255,0.45)';
  const textMuted = isLight ? 'rgba(100,116,139,0.8)' : 'rgba(255,255,255,0.25)';

  const navLink = (item, sectionColor) => {
    const active = isActive(item.path);
    return (
      <Link
        key={`${item.path}-${item.sectionDedupe || ''}`}
        to={item.path}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 14px',
          borderRadius: 6,
          textDecoration: 'none',
          background: active
            ? `${sectionColor}18`
            : 'transparent',
          color: active
            ? sectionColor
            : textColor,
          borderLeft: `2px solid ${active ? sectionColor : 'transparent'}`,
          transition: 'all 150ms ease-out',
          fontSize: 13,
          minHeight: 38,
        }}
      >
        <span style={{
          width: 20,
          fontSize: 10,
          fontFamily: 'IBM Plex Mono, monospace',
          fontWeight: 700,
          color: active ? sectionColor : (isLight ? 'rgba(100,116,139,0.6)' : 'rgba(255,255,255,0.2)'),
          letterSpacing: '-0.02em',
          textAlign: 'center',
          flexShrink: 0,
        }}>{item.icon}</span>
        <span>{item.label}</span>
      </Link>
    );
  };

  const legacyLink = (item) => {
    const active = isActive(item.path);
    return (
      <Link
        key={item.path}
        to={item.path}
        style={{
          display: 'block',
          padding: '4px 14px 4px 26px',
          borderRadius: 4,
          textDecoration: 'none',
          color: active
            ? '#64748b'
            : (isLight ? 'rgba(100,116,139,0.6)' : 'rgba(100,116,139,0.5)'),
          fontSize: 11,
          transition: 'color 150ms',
          background: active ? 'rgba(100,116,139,0.08)' : 'transparent',
        }}
      >
        {item.label}
      </Link>
    );
  };

  return (
    <div style={{ background: 'var(--bg, #07070c)', minHeight: '100vh', display: 'flex', flexDirection: 'column' }}>

      {/* ── Top Bar ────────────────────────────────────────────────────────── */}
      <header
        id="live-toggle-header"
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 16px',
          height: 52,
          borderBottom: `1px solid ${headerBorder}`,
          background: headerBg,
          position: 'sticky',
          top: 0,
          zIndex: 200,
          flexShrink: 0,
        }}
      >
        {/* Left: hamburger (mobile) + logo */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          {/* Hamburger -- mobile only */}
          <button
            onClick={() => setSidebarOpen(o => !o)}
            className="hamburger-btn"
            style={{
              background: 'none',
              border: 'none',
              color: textColor,
              cursor: 'pointer',
              padding: '6px 4px',
              fontSize: 18,
              lineHeight: 1,
              display: 'none', // shown via CSS media query
              minHeight: 44,
              minWidth: 44,
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            {sidebarOpen ? '\u2715' : '\u2630'}
          </button>

          <Link
            to="/polymarket/monitor"
            style={{ textDecoration: 'none', display: 'flex', alignItems: 'center', gap: 6 }}
          >
            <span style={{ color: '#a855f7', fontSize: 16 }}>\u20BF</span>
            <span
              className="logo-text"
              style={{
                color: '#a855f7',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 14,
                fontWeight: 700,
                letterSpacing: '-0.01em',
              }}
            >
              BTC Trader
            </span>
          </Link>
        </div>

        {/* Right: theme toggle + config pill + live toggle */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <ThemeToggle />
          <div className="config-pill-wrapper">
            <ConfigPill />
          </div>
          <LiveToggle />
        </div>
      </header>

      {/* ── Body: sidebar + main ────────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0 }}>

        {/* ── Mobile drawer overlay ─────────────────────────────────────────── */}
        {sidebarOpen && (
          <div
            className="sidebar-overlay"
            style={{
              position: 'fixed',
              inset: 0,
              background: 'rgba(0,0,0,0.7)',
              zIndex: 150,
              display: 'none', // shown via CSS only on mobile
            }}
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* ── Sidebar ───────────────────────────────────────────────────────── */}
        <aside
          className={`main-sidebar ${sidebarOpen ? 'sidebar-open' : ''}`}
          style={{
            width: 220,
            flexShrink: 0,
            background: sidebarBg,
            borderRight: `1px solid ${sidebarBorder}`,
            display: 'flex',
            flexDirection: 'column',
            padding: '12px 8px',
            overflowY: 'auto',
          }}
        >
          {/* Main nav -- 3 primary sections */}
          <nav style={{ flex: 1 }}>
            {NAV_SECTIONS.map((section, idx) => (
              <div key={section.id} style={{ marginTop: idx > 0 ? 12 : 0 }}>
                <div style={{
                  display: 'flex', alignItems: 'center', gap: 6,
                  padding: '6px 14px 4px', marginBottom: 2,
                }}>
                  <div style={{
                    width: 3, height: 10, borderRadius: 2,
                    background: section.color,
                    boxShadow: `0 0 6px ${section.color}44`,
                  }} />
                  <span style={{
                    fontSize: 9, fontWeight: 800, letterSpacing: '0.12em',
                    color: section.color, fontFamily: 'IBM Plex Mono, monospace',
                    opacity: 0.9,
                  }}>{section.title}</span>
                </div>
                {section.items.map(item => navLink(item, section.color))}
              </div>
            ))}

            {/* ── LEGACY (collapsed by default) ──────────────────────────────── */}
            <div style={{ marginTop: 16 }}>
              <button
                onClick={() => setLegacyExpanded(prev => !prev)}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  padding: '6px 14px',
                  marginBottom: 2,
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  width: '100%',
                  textAlign: 'left',
                }}
              >
                <div style={{
                  width: 3, height: 10, borderRadius: 2,
                  background: '#475569',
                  opacity: 0.5,
                }} />
                <span style={{
                  fontSize: 8, fontWeight: 700, letterSpacing: '0.1em',
                  color: isLight ? 'rgba(100,116,139,0.6)' : 'rgba(100,116,139,0.5)',
                  fontFamily: 'IBM Plex Mono, monospace',
                }}>LEGACY</span>
                <span style={{
                  fontSize: 9,
                  color: isLight ? 'rgba(100,116,139,0.4)' : 'rgba(100,116,139,0.3)',
                  marginLeft: 'auto',
                  transform: legacyExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                  transition: 'transform 150ms ease-out',
                  lineHeight: 1,
                }}>{'\u25B6'}</span>
              </button>
              {legacyExpanded && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 1, padding: '2px 0' }}>
                  {LEGACY_ITEMS.map(legacyLink)}
                </div>
              )}
            </div>
          </nav>

          {/* Bottom: setup + user + logout */}
          <div style={{ borderTop: `1px solid ${sidebarBorder}`, paddingTop: 8, marginTop: 8 }}>
            <Link
              to="/setup"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                padding: '10px 14px',
                borderRadius: 6,
                textDecoration: 'none',
                background: isActive('/setup') ? 'rgba(168,85,247,0.1)' : 'transparent',
                color: isActive('/setup') ? '#a855f7' : textColor,
                borderLeft: `2px solid ${isActive('/setup') ? '#a855f7' : 'transparent'}`,
                fontSize: 13,
                transition: 'all 150ms',
                minHeight: 44,
              }}
            >
              <span style={{ fontSize: 10, fontFamily: 'IBM Plex Mono, monospace', fontWeight: 700, color: isActive('/setup') ? '#a855f7' : (isLight ? 'rgba(100,116,139,0.6)' : 'rgba(255,255,255,0.2)'), width: 20, textAlign: 'center' }}>Se</span>
              <span>Setup</span>
            </Link>

            <div style={{ padding: '8px 14px' }}>
              <div style={{ color: textMuted, fontSize: 11, marginBottom: 6 }}>
                {user?.username}
              </div>
              <button
                onClick={logout}
                style={{
                  width: '100%',
                  padding: '8px 0',
                  borderRadius: 5,
                  border: `1px solid ${isLight ? 'rgba(203,213,225,0.4)' : 'rgba(255,255,255,0.07)'}`,
                  background: isLight ? 'rgba(241,245,249,0.5)' : 'rgba(255,255,255,0.04)',
                  color: isLight ? 'rgba(100,116,139,0.8)' : 'rgba(255,255,255,0.3)',
                  fontSize: 11,
                  cursor: 'pointer',
                  fontFamily: 'IBM Plex Mono, monospace',
                  transition: 'all 150ms',
                  minHeight: 36,
                }}
              >
                Logout
              </button>
            </div>
          </div>
        </aside>

        {/* ── Main content ──────────────────────────────────────────────────── */}
        <main
          className="main-content"
          style={{
            flex: 1,
            overflowY: 'auto',
            overflowX: 'hidden',
            minWidth: 0,
            paddingBottom: 72, // space for mobile tab bar
          }}
        >
          <Outlet />
        </main>
      </div>

      {/* ── Mobile bottom tab bar ─────────────────────────────────────────── */}
      <nav
        className="bottom-tab-bar"
        style={{
          display: 'none', // shown via CSS on mobile
          position: 'fixed',
          bottom: 0,
          left: 0,
          right: 0,
          height: 60,
          background: isLight ? 'rgba(248,250,252,0.97)' : 'rgba(7,7,12,0.97)',
          borderTop: `1px solid ${isLight ? 'rgba(203,213,225,0.4)' : 'rgba(255,255,255,0.06)'}`,
          zIndex: 100,
          backdropFilter: 'blur(12px)',
        }}
      >
        {TAB_ITEMS.map(item => {
          const active = isActive(item.path);
          return (
            <Link
              key={item.path}
              to={item.path}
              style={{
                flex: 1,
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 2,
                textDecoration: 'none',
                color: active ? '#a855f7' : textColor,
                fontSize: 9,
                fontFamily: 'IBM Plex Mono, monospace',
                letterSpacing: '0.04em',
                transition: 'color 150ms',
                minHeight: 60,
              }}
            >
              <span style={{
                fontSize: 13,
                lineHeight: 1,
                fontWeight: 700,
                color: active ? '#a855f7' : (isLight ? 'rgba(100,116,139,0.5)' : 'rgba(255,255,255,0.3)'),
              }}>
                {item.icon}
              </span>
              <span>{item.label}</span>
              {active && (
                <span style={{
                  position: 'absolute',
                  bottom: 0,
                  width: 24,
                  height: 2,
                  background: '#a855f7',
                  borderRadius: 1,
                  boxShadow: '0 0 6px rgba(168,85,247,0.6)',
                }} />
              )}
            </Link>
          );
        })}
      </nav>

      {/* ── Global Styles ─────────────────────────────────────────────────── */}
      <style>{`
        @keyframes dropdownIn {
          from { opacity: 0; transform: translateY(-6px) scale(0.98); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }

        /* Mobile layout overrides */
        @media (max-width: 768px) {
          .hamburger-btn { display: flex !important; }
          .logo-text { font-size: 13px !important; }
          .config-pill-wrapper .pill-text { display: none; }
          .config-pill-wrapper .pill-icon { font-size: 16px; }
          .config-pill-wrapper .config-pill-btn {
            padding: 5px 8px !important;
            gap: 2px !important;
          }

          /* Sidebar: hidden off-screen, slides in */
          .main-sidebar {
            position: fixed !important;
            top: 52px !important;
            left: 0 !important;
            bottom: 0 !important;
            z-index: 160 !important;
            transform: translateX(-100%) !important;
            transition: transform 250ms ease-out !important;
            width: 240px !important;
          }
          .main-sidebar.sidebar-open {
            transform: translateX(0) !important;
          }
          .sidebar-overlay {
            display: block !important;
          }

          /* Main content: full width, no left offset */
          .main-content {
            padding-bottom: 68px !important;
          }

          /* Bottom tab bar: visible */
          .bottom-tab-bar {
            display: flex !important;
          }

          /* Config dropdown: full width on mobile */
          .config-dropdown-panel {
            position: fixed !important;
            top: auto !important;
            bottom: 68px !important;
            left: 0 !important;
            right: 0 !important;
            width: 100% !important;
            border-radius: 12px 12px 0 0 !important;
            border-bottom: none !important;
          }
        }

        /* Very narrow screens */
        @media (max-width: 360px) {
          .bottom-tab-bar a { font-size: 8px !important; }
        }

        /* Scrollbar */
        .main-sidebar::-webkit-scrollbar { width: 3px; }
        .main-sidebar::-webkit-scrollbar-track { background: transparent; }
        .main-sidebar::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
        .main-content::-webkit-scrollbar { width: 4px; }
        .main-content::-webkit-scrollbar-track { background: transparent; }
        .main-content::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }

        /* Light theme scrollbar */
        body.theme-light .main-sidebar::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.1); }
        body.theme-light .main-content::-webkit-scrollbar-thumb { background: rgba(0,0,0,0.1); }
      `}</style>
    </div>
  );
}
