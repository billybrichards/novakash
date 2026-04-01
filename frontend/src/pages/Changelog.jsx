/**
 * Changelog.jsx — Release Notes, Roadmap & TODO
 *
 * Living document showing what's shipped, what's next, and known issues.
 * Data is hardcoded (no API) — update this file directly.
 */

import { useState } from 'react';

const MONO = "'IBM Plex Mono', monospace";

const C = {
  bg: '#07070c',
  card: '#0d0d1a',
  border: 'rgba(255,255,255,0.06)',
  text: 'rgba(255,255,255,0.85)',
  muted: 'rgba(255,255,255,0.45)',
  dim: 'rgba(255,255,255,0.25)',
  purple: '#a855f7',
  green: '#4ade80',
  red: '#f87171',
  amber: '#f59e0b',
  cyan: '#06b6d4',
};

// ─── Data ─────────────────────────────────────────────────────────────────────

const RELEASES = [
  {
    version: '0.4.0',
    date: '2026-04-01',
    title: '5-Minute Polymarket Trading',
    tag: 'current',
    changes: [
      { type: 'feat', text: '5-min BTC Up/Down market discovery via Gamma API' },
      { type: 'feat', text: 'FiveMinVPINStrategy — delta-based trading at T-10s' },
      { type: 'feat', text: 'Delta-based token pricing model (realistic costs)' },
      { type: 'feat', text: 'Paper trading live on Railway' },
      { type: 'feat', text: 'Backtest: 82.3% win rate over 7 days (1,990 trades)' },
      { type: 'fix', text: 'Open price uses real Binance price (was random $45K)' },
      { type: 'fix', text: 'Orchestrator forwards window signal to strategy' },
      { type: 'fix', text: 'Stake calc uses BET_FRACTION (was hardcoded 25%)' },
      { type: 'fix', text: 'Removed VPIN from confidence (different scale live vs backtest)' },
      { type: 'fix', text: 'Min delta threshold lowered to 0.001% (was 0.005%)' },
      { type: 'fix', text: 'Learn.jsx unterminated string literal + tag nesting' },
    ],
  },
  {
    version: '0.3.0',
    date: '2026-03-31',
    title: 'Multi-Market Backtest & Polymarket Events',
    changes: [
      { type: 'feat', text: '14-day backtest across 150 Polymarket event markets' },
      { type: 'feat', text: 'Multi-market backtest (BTC/ETH/SOL/DOGE/XRP/BNB/AVAX/LINK)' },
      { type: 'feat', text: 'ArbScanner NO-side book fix (derives NO from YES complement)' },
      { type: 'feat', text: 'Positions page (508 lines) and Risk page (712 lines)' },
      { type: 'data', text: 'Result: VPIN strategy unprofitable on event markets (-$9K to -$12K per category)' },
      { type: 'data', text: 'Max VPIN across all crypto: 0.287 (below 0.35 threshold)' },
    ],
  },
  {
    version: '0.2.0',
    date: '2026-03-30',
    title: 'Paper Dashboard & CoinGlass v4',
    changes: [
      { type: 'feat', text: 'PaperDashboard.jsx with 7 canvas charts' },
      { type: 'feat', text: 'CoinGlass v4 API integration' },
      { type: 'feat', text: 'Polymarket API keys derived and authenticated' },
      { type: 'fix', text: 'Updated VPIN thresholds: 0.45/0.55 (from 0.55/0.70)' },
      { type: 'fix', text: 'Arb min spread: 0.005 (from 0.015)' },
    ],
  },
  {
    version: '0.1.0',
    date: '2026-03-29',
    title: 'Initial Architecture',
    changes: [
      { type: 'feat', text: 'Engine: Python 3.12, asyncio, SQLAlchemy' },
      { type: 'feat', text: 'Hub: FastAPI + PostgreSQL 16' },
      { type: 'feat', text: 'Frontend: React 18, Vite, Tailwind, Recharts' },
      { type: 'feat', text: 'Docker Compose + Caddy deployment' },
      { type: 'feat', text: 'Railway deployment pipeline' },
    ],
  },
];

const TODO = [
  {
    priority: 'high',
    category: 'Strategy',
    items: [
      { text: 'Calibrate live VPIN scale to match backtest (bucket-based vs simple ratio)', status: 'todo', note: 'Live VPIN runs ~0.97, backtest ~0.26 — completely different metrics' },
      { text: 'Add VPIN-based earlier entry (T-30s) for cheaper tokens when flow aligns', status: 'todo', note: 'The backtest showed +$4.5K extra from VPIN boost entries' },
      { text: 'Paper trade resolution — verify win/loss matches actual BTC close vs open', status: 'todo', note: 'Need to confirm order_manager resolves correctly for 5-min windows' },
      { text: 'Backtest with realistic slippage model (order book depth simulation)', status: 'todo' },
    ],
  },
  {
    priority: 'high',
    category: 'Reliability',
    items: [
      { text: 'Fix Telegram alerter: Order object missing market_slug attribute', status: 'todo' },
      { text: 'Add health check endpoint for 5-min feed status', status: 'todo' },
      { text: 'Persist bankroll across engine restarts (currently resets to STARTING_BANKROLL)', status: 'todo' },
    ],
  },
  {
    priority: 'medium',
    category: 'Multi-Asset',
    items: [
      { text: 'Enable ETH, SOL, DOGE 5-min markets (feed supports them, strategy needs testing)', status: 'todo' },
      { text: 'Per-asset VPIN calculation and thresholds', status: 'todo' },
      { text: '15-minute market support (btc-updown-15m pattern)', status: 'todo' },
    ],
  },
  {
    priority: 'medium',
    category: 'Dashboard',
    items: [
      { text: 'Real-time 5-min trade feed on Paper Dashboard', status: 'todo' },
      { text: 'Win rate tracker (running accuracy vs backtest benchmark)', status: 'todo' },
      { text: 'Window delta histogram (distribution of signals)', status: 'todo' },
    ],
  },
  {
    priority: 'low',
    category: 'Live Trading',
    items: [
      { text: 'Auto-claim via web3.py contract interaction (not Playwright)', status: 'todo', note: 'Manual daily claiming is fine for v1' },
      { text: 'Live mode risk limits (separate from paper)', status: 'todo' },
      { text: 'Funding rate monitoring for position sizing', status: 'todo' },
    ],
  },
];

const KNOWN_ISSUES = [
  { severity: 'info', text: 'Paper data on Dashboard is synthetic (genPaperTrades), not from live engine', since: '0.2.0' },
  { severity: 'warn', text: 'Frontend /api/api/ double prefix in some proxy calls (nginx config)', since: '0.3.0' },
  { severity: 'info', text: 'Bankroll resets to STARTING_BANKROLL on each engine deploy', since: '0.4.0' },
  { severity: 'info', text: 'Telegram trade alerts crash on 5-min orders (missing market_slug)', since: '0.4.0' },
];

// ─── Components ───────────────────────────────────────────────────────────────

function Badge({ type }) {
  const styles = {
    feat: { bg: 'rgba(168,85,247,0.15)', color: C.purple, label: 'NEW' },
    fix: { bg: 'rgba(74,222,128,0.15)', color: C.green, label: 'FIX' },
    data: { bg: 'rgba(6,182,212,0.15)', color: C.cyan, label: 'DATA' },
    break: { bg: 'rgba(248,113,113,0.15)', color: C.red, label: 'BREAKING' },
  };
  const s = styles[type] || styles.feat;
  return (
    <span style={{
      display: 'inline-block',
      padding: '1px 6px',
      borderRadius: 3,
      background: s.bg,
      color: s.color,
      fontSize: 9,
      fontFamily: MONO,
      fontWeight: 700,
      letterSpacing: '0.08em',
      lineHeight: '16px',
    }}>
      {s.label}
    </span>
  );
}

function PriorityDot({ priority }) {
  const color = priority === 'high' ? C.red : priority === 'medium' ? C.amber : C.muted;
  return <span style={{ color, fontSize: 8, marginRight: 6 }}>●</span>;
}

function SeverityIcon({ severity }) {
  if (severity === 'warn') return <span style={{ color: C.amber }}>⚠</span>;
  return <span style={{ color: C.muted }}>ℹ</span>;
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function Changelog() {
  const [tab, setTab] = useState('releases');

  const tabs = [
    { id: 'releases', label: 'Releases', icon: '🚀' },
    { id: 'todo', label: 'TODO', icon: '📋' },
    { id: 'issues', label: 'Known Issues', icon: '🐛' },
  ];

  return (
    <div style={{ padding: '24px 20px', maxWidth: 800, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <h1 style={{
          fontFamily: MONO,
          fontSize: 20,
          fontWeight: 700,
          color: C.text,
          margin: 0,
          letterSpacing: '-0.02em',
        }}>
          Changelog
        </h1>
        <p style={{ color: C.muted, fontSize: 12, fontFamily: MONO, margin: '6px 0 0' }}>
          Release notes, roadmap & known issues
        </p>
      </div>

      {/* Tab bar */}
      <div style={{
        display: 'flex',
        gap: 2,
        marginBottom: 24,
        background: 'rgba(255,255,255,0.03)',
        borderRadius: 8,
        padding: 3,
      }}>
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            style={{
              flex: 1,
              padding: '8px 12px',
              borderRadius: 6,
              border: 'none',
              background: tab === t.id ? 'rgba(168,85,247,0.15)' : 'transparent',
              color: tab === t.id ? C.purple : C.muted,
              fontFamily: MONO,
              fontSize: 12,
              fontWeight: tab === t.id ? 600 : 400,
              cursor: 'pointer',
              transition: 'all 150ms ease-out',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 6,
            }}
          >
            <span>{t.icon}</span>
            {t.label}
          </button>
        ))}
      </div>

      {/* ── Releases Tab ─────────────────────────────────────────────────── */}
      {tab === 'releases' && (
        <div>
          {RELEASES.map((release, i) => (
            <div
              key={release.version}
              style={{
                marginBottom: 20,
                background: C.card,
                border: `1px solid ${release.tag === 'current' ? 'rgba(168,85,247,0.3)' : C.border}`,
                borderRadius: 10,
                overflow: 'hidden',
              }}
            >
              {/* Release header */}
              <div style={{
                padding: '12px 16px',
                borderBottom: `1px solid ${C.border}`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{
                    fontFamily: MONO,
                    fontSize: 14,
                    fontWeight: 700,
                    color: release.tag === 'current' ? C.purple : C.text,
                  }}>
                    v{release.version}
                  </span>
                  {release.tag === 'current' && (
                    <span style={{
                      padding: '1px 8px',
                      borderRadius: 10,
                      background: 'rgba(168,85,247,0.2)',
                      color: C.purple,
                      fontSize: 9,
                      fontFamily: MONO,
                      fontWeight: 700,
                    }}>
                      CURRENT
                    </span>
                  )}
                </div>
                <span style={{ color: C.dim, fontSize: 11, fontFamily: MONO }}>
                  {release.date}
                </span>
              </div>

              {/* Release title + changes */}
              <div style={{ padding: '12px 16px' }}>
                <div style={{
                  color: C.text,
                  fontSize: 13,
                  fontWeight: 600,
                  marginBottom: 10,
                }}>
                  {release.title}
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                  {release.changes.map((change, j) => (
                    <div key={j} style={{
                      display: 'flex',
                      alignItems: 'flex-start',
                      gap: 8,
                      fontSize: 12,
                      color: C.muted,
                      lineHeight: 1.5,
                    }}>
                      <Badge type={change.type} />
                      <span>{change.text}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── TODO Tab ──────────────────────────────────────────────────────── */}
      {tab === 'todo' && (
        <div>
          {TODO.map((group, i) => (
            <div key={i} style={{ marginBottom: 20 }}>
              <div style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                marginBottom: 10,
              }}>
                <PriorityDot priority={group.priority} />
                <span style={{
                  fontFamily: MONO,
                  fontSize: 12,
                  fontWeight: 700,
                  color: C.text,
                  textTransform: 'uppercase',
                  letterSpacing: '0.08em',
                }}>
                  {group.category}
                </span>
                <span style={{
                  padding: '1px 6px',
                  borderRadius: 3,
                  background: group.priority === 'high' ? 'rgba(248,113,113,0.15)' :
                              group.priority === 'medium' ? 'rgba(245,158,11,0.15)' :
                              'rgba(255,255,255,0.05)',
                  color: group.priority === 'high' ? C.red :
                         group.priority === 'medium' ? C.amber : C.dim,
                  fontSize: 9,
                  fontFamily: MONO,
                  fontWeight: 600,
                }}>
                  {group.priority.toUpperCase()}
                </span>
              </div>

              <div style={{
                background: C.card,
                border: `1px solid ${C.border}`,
                borderRadius: 8,
                overflow: 'hidden',
              }}>
                {group.items.map((item, j) => (
                  <div
                    key={j}
                    style={{
                      padding: '10px 14px',
                      borderBottom: j < group.items.length - 1 ? `1px solid ${C.border}` : 'none',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 4,
                    }}
                  >
                    <div style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      fontSize: 12,
                      color: C.text,
                    }}>
                      <span style={{ color: C.dim }}>
                        {item.status === 'done' ? '✅' : item.status === 'wip' ? '🔄' : '○'}
                      </span>
                      {item.text}
                    </div>
                    {item.note && (
                      <div style={{
                        fontSize: 11,
                        color: C.dim,
                        paddingLeft: 22,
                        fontStyle: 'italic',
                      }}>
                        {item.note}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Known Issues Tab ──────────────────────────────────────────────── */}
      {tab === 'issues' && (
        <div style={{
          background: C.card,
          border: `1px solid ${C.border}`,
          borderRadius: 10,
          overflow: 'hidden',
        }}>
          {KNOWN_ISSUES.map((issue, i) => (
            <div
              key={i}
              style={{
                padding: '12px 16px',
                borderBottom: i < KNOWN_ISSUES.length - 1 ? `1px solid ${C.border}` : 'none',
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
              }}
            >
              <SeverityIcon severity={issue.severity} />
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 12, color: C.text, lineHeight: 1.5 }}>
                  {issue.text}
                </div>
                <div style={{ fontSize: 10, color: C.dim, fontFamily: MONO, marginTop: 2 }}>
                  Since v{issue.since}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
