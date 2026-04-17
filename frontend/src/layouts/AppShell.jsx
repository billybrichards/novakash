import React from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '../auth/AuthContext.jsx';
import LiveToggle from '../components/LiveToggle.jsx';
import { NAV_SECTIONS } from '../nav/navigation.js';
import { T } from '../theme/tokens.js';

const SIDEBAR_WIDTH = 220;

export default function AppShell() {
  const { logout } = useAuth();
  const location = useLocation();

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
            type="button"
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
