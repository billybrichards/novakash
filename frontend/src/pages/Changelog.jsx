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
    version: '7.1',
    date: '2026-04-05',
    title: 'v7.1 — CoinGlass Modifier Overhaul',
    tag: 'current',
    changes: [
      { type: 'fix', text: 'CG veto threshold: 3+ → 2+ signals required (was too forgiving)' },
      { type: 'fix', text: 'Funding rate bug: extreme positive funding now correctly vetos DOWN bets (was only checking negative funding)' },
      { type: 'fix', text: 'Smart money threshold: 55% → 52% (catch near-majority divergence earlier)' },
      { type: 'fix', text: 'Taker threshold: >65% → >60% (catch clear directional flow divergence)' },
      { type: 'feat', text: 'New: CASCADE + taker divergence rule — if VPIN ≥ 0.65 but takers oppose direction >55%, veto' },
      { type: 'fix', text: 'Order type: Limit FOK @ Gamma+2¢ (cap $0.70) → GTD fallback. Removes market FOK entirely.' },
      { type: 'feat', text: 'Multi-offset eval: FIVE_MIN_EVAL_OFFSETS env var (e.g. 90,60 for T-90 + T-60)' },
      { type: 'feat', text: 'Analysis Library: 5 reports including order strategy, BTC accuracy, gate analysis' },
      { type: 'data', text: 'Apr 5 17:00 trade replay: v7.1 veto fires with 3 independent triggers vs 1 in v5.4d' },
      { type: 'data', text: 'Breakdown: taker_buying=66%, smart_money_long=54%, CASCADE_taker_divergence' },
    ],
  },
  {
    version: '7.0',
    date: '2026-04-05',
    title: 'v7 — Gate Optimisation & Full Dashboard',
    tag: '',
    changes: [
      { type: 'feat', text: 'TIMESFM_ONLY regime disabled — eliminates all 8 historical losses' },
      { type: 'feat', text: 'Delta gates loosened: 0.08%→0.02% (normal), 0.03%→0.01% (cascade)' },
      { type: 'feat', text: 'TimesFM removed from agreement gate — data-only, no gating effect' },
      { type: 'feat', text: 'Win streak tracker on v7 Monitor' },
      { type: 'feat', text: 'Unified Telegram notifications v2 — value-first, skim-optimised alerts' },
      { type: 'feat', text: 'AI Assessment via Claude Haiku — 2-sentence trade analysis with full context' },
      { type: 'feat', text: '5-MIN SITREP — portfolio status, VPIN, CoinGlass, win/loss, drawdown' },
      { type: 'feat', text: 'Chart generator: window sparklines, daily P&L curves, accuracy bars (PNG)' },
      { type: 'feat', text: 'Trade resolution alerts with sparkline chart via sendPhoto' },
      { type: 'feat', text: 'What-if P&L at ALL intervals (T-240/T-180/T-120/T-90/T-60)' },
      { type: 'feat', text: 'Window Results page (/windows) with per-source ✅/❌ and version badges' },
      { type: 'feat', text: 'Manual Paper/Live trade buttons with real-time Gamma preview' },
      { type: 'feat', text: 'Analysis Library (/analysis) with 4 verified reports' },
      { type: 'feat', text: 'All trades/windows tagged with engine_version (v5.0→v7.0)' },
      { type: 'feat', text: '30-day Polymarket backfill (45K+ windows) via data-collector service' },
      { type: 'feat', text: '1-second Gamma price snapshots in market_snapshots table' },
      { type: 'data', text: 'v5.7c accuracy: 100% on real regimes (138/138 in v5.8 era)' },
      { type: 'data', text: 'TimesFM accuracy: 26-45% — worse than random, removed from gate' },
      { type: 'data', text: 'Delta gates blocked 84 correct trades (100% accuracy) — now loosened' },
    ],
  },
  {
    version: '6.0',
    date: '2026-04-05',
    title: 'v6 — TimesFM Integration & Countdown Alerts',
    changes: [
      { type: 'feat', text: 'TimesFM 2.5 200M microservice (Docker, 2048 context, horizon=300)' },
      { type: 'feat', text: 'Countdown alerts at T-180/T-120/T-90 with TimesFM predictions' },
      { type: 'feat', text: 'v5.8 Monitor dashboard with BTC chart, window timeline, accuracy rings' },
      { type: 'feat', text: 'ticks_timesfm table — every-second predictions with dynamic horizon' },
      { type: 'feat', text: 'TimesFM context: 1024→2048 ticks (17→34 min of price history)' },
      { type: 'feat', text: 'Redesigned Telegram alerts with actual signal values' },
      { type: 'fix', text: 'TimesFM was NULL — strategy created before client init (injection fix)' },
      { type: 'fix', text: 'TimesFM agreement was silently failing (string + float TypeError)' },
      { type: 'fix', text: 'Gamma pricing bug — used real outcomePrices instead of 1-bestAsk' },
      { type: 'fix', text: 'outcomePrices JSON string parsing from Gamma API' },
    ],
  },
  {
    version: '5.8',
    date: '2026-04-05',
    title: 'v5.8 — TimesFM Agreement Protocol',
    changes: [
      { type: 'feat', text: 'v5.8 agreement: TimesFM + v5.7c must agree before trading' },
      { type: 'feat', text: 'Real market price vs FOK cap separation (price for records, cap for orders)' },
      { type: 'feat', text: 'TimesFM data stored in window_snapshots DB' },
      { type: 'feat', text: '$4 max bet, $13 bankroll, 23% bet fraction for live' },
      { type: 'fix', text: 'LIVE_TRADING_ENABLED .env fallback for safety check' },
    ],
  },
  {
    version: '5.7c',
    date: '2026-04-04',
    title: 'v5.7c — TWAP Override & Gamma Gate',
    changes: [
      { type: 'feat', text: 'TWAP direction override when TWAP+Gamma agree' },
      { type: 'feat', text: 'Gamma gate: block trades when market strongly disagrees (token <15¢)' },
      { type: 'feat', text: 'Agreement score 0-3 (Point/TWAP/Gamma consensus)' },
      { type: 'feat', text: 'Staggered execution: single-best-signal per window' },
      { type: 'feat', text: 'Claude AI evaluator for post-trade analysis' },
      { type: 'data', text: 'v5.7c accuracy: 100% in real regimes (263 windows)' },
    ],
  },
  {
    version: '5.7',
    date: '2026-04-03',
    title: 'v5.7 — TWAP-Delta Direction Tracking',
    changes: [
      { type: 'feat', text: 'TWAP-Delta tracker — smoothed direction over 5-min window' },
      { type: 'feat', text: 'Confidence boost from TWAP agreement' },
      { type: 'feat', text: '$32 absolute max bet cap' },
      { type: 'feat', text: 'T-60s evaluation (moved from T-10s for real prediction testing)' },
      { type: 'feat', text: '15-minute window support' },
    ],
  },
  {
    version: '5.1',
    date: '2026-04-02',
    title: 'v5.1 — All Momentum (Data-Driven)',
    changes: [
      { type: 'feat', text: '30-day backtest proves: momentum beats contrarian at all VPIN levels' },
      { type: 'feat', text: 'CoinGlass signals zeroed except OI delta (+2.9% WR lift)' },
      { type: 'feat', text: 'CG veto system: 3+ opposing signals → block trade' },
      { type: 'data', text: '8,640 windows backtested, contrarian = coin flip' },
    ],
  },
  {
    version: '5.0',
    date: '2026-04-01',
    title: 'v5.0 — Regime-Aware Direction',
    changes: [
      { type: 'feat', text: 'VPIN regime-aware: CASCADE (momentum), TRANSITION, NORMAL' },
      { type: 'feat', text: 'VPIN-scaled delta bar: higher VPIN = lower delta needed' },
      { type: 'feat', text: 'CoinGlass confirmation layer (L/S ratio, taker, funding)' },
      { type: 'feat', text: 'Confidence tiers: LOW/MODERATE/HIGH with CG modifiers' },
    ],
  },
  {
    version: '0.4.0',
    date: '2026-04-01',
    title: '5-Minute Polymarket Trading',
    changes: [
      { type: 'feat', text: '5-min BTC Up/Down market discovery via Gamma API' },
      { type: 'feat', text: 'FiveMinVPINStrategy — delta-based trading at T-10s' },
      { type: 'feat', text: 'Delta-based token pricing model (realistic costs)' },
      { type: 'feat', text: 'Paper trading live on Railway' },
      { type: 'feat', text: 'Backtest: 82.3% win rate over 7 days (1,990 trades)' },
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
      { type: 'feat', text: 'Positions page and Risk page' },
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
      { type: 'feat', text: 'Docker Compose + Railway deployment pipeline' },
    ],
  },
];

const TODO = [
  {
    priority: 'high',
    category: 'Data & Validation',
    items: [
      { text: 'Collect 48-72h of real Gamma entry prices for accurate P&L', status: 'in-progress', note: 'Engine now records real prices. Need time to accumulate.' },
      { text: 'Validate accuracy in bullish/sideways regime (current data is all bearish)', status: 'todo', note: 'v5.7c 100% may not hold in choppy markets' },
      { text: 'Build adaptive gate: rolling 50-window accuracy → auto-tighten/loosen', status: 'todo' },
      { text: 'Run ungated paper for 24h to validate loosened delta gates', status: 'in-progress' },
    ],
  },
  {
    priority: 'high',
    category: 'Reliability',
    items: [
      { text: 'Fix Binance WebSocket auto-reconnect (VPIN=0 for 7+ hrs on Apr 5)', status: 'todo', note: 'Root cause of TIMESFM_ONLY fallback' },
      { text: 'Persist bankroll across engine restarts', status: 'todo' },
      { text: 'Add engine health check endpoint on Montreal', status: 'todo' },
    ],
  },
  {
    priority: 'medium',
    category: 'Multi-Asset',
    items: [
      { text: 'Enable ETH/SOL/XRP trading (99.8-100% accuracy in data)', status: 'todo', note: 'Feed already supports them, need per-asset VPIN + testing' },
      { text: 'Per-asset VPIN calculation and thresholds', status: 'todo' },
    ],
  },
  {
    priority: 'medium',
    category: 'TimesFM Research',
    items: [
      { text: 'Evaluate TimesFM in bullish regime (current: 26-45%, all bearish)', status: 'todo', note: 'DOWN calls at 72% are interesting but redundant with v5.7c' },
      { text: 'Test TimesFM as contrarian signal (when it disagrees, does it signal reversals?)', status: 'todo' },
      { text: 'Fine-tune on Polymarket-specific 5m data', status: 'todo' },
    ],
  },
  {
    priority: 'low',
    category: 'Live Trading',
    items: [
      { text: 'Auto-claim via web3.py contract interaction', status: 'todo' },
      { text: 'Live mode: increase stake from $4 as accuracy is proven', status: 'todo' },
      { text: 'Multi-window position management (concurrent bets)', status: 'todo' },
    ],
  },
];

const KNOWN_ISSUES = [
  { severity: 'info', text: 'Legacy paper P&L (v5.0-v5.7c) uses inflated stakes — filter to v7+ for real data', since: '7.0' },
  { severity: 'info', text: '98% of backfill Gamma prices are $0/$1 resolved — P&L only valid on recent windows', since: '6.0' },
  { severity: 'warn', text: 'WebSocket "RECONNECTING" on some pages — WS goes through nginx, not hub direct', since: '6.0' },
  { severity: 'info', text: 'TimesFM confidence always 95-100% — not well calibrated for 5m BTC', since: '6.0' },
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
