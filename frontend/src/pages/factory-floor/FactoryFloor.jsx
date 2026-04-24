/**
 * FactoryFloor.jsx -- Industrial Control Room Dashboard
 *
 * TIER 1 monitoring page. Visualizes the entire BTC trading engine
 * as a left-to-right pipeline: DATA FEEDS -> SIGNAL PROCESSING ->
 * STRATEGY ENGINE -> EXECUTION -> POST-TRADE.
 *
 * Aesthetic: NASA mission control / Bloomberg terminal / SCADA.
 * Dark background, green/amber/red indicators, monospace data,
 * animated flow lines between pipeline stages.
 */

import React, { useMemo } from 'react';
import MachineCard from './components/MachineCard.jsx';
import PipelineFlow from './components/PipelineFlow.jsx';
import GatePipeline from './components/GatePipeline.jsx';
import StatusPulse from './components/StatusPulse.jsx';
import { useEngineStatus } from './hooks/useEngineStatus.js';

// ── Inject global styles (once) ─────────────────────────────────────────────
if (typeof document !== 'undefined' && !document.getElementById('ff-v2-styles')) {
  const style = document.createElement('style');
  style.id = 'ff-v2-styles';
  style.textContent = `
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&display=swap');

    @keyframes ffPulse {
      0%, 100% { opacity: 1; }
      50%      { opacity: 0.35; }
    }
    @keyframes ffFlowHoriz {
      0%   { transform: translateX(0) translateY(-50%); opacity: 0; }
      10%  { opacity: 1; }
      90%  { opacity: 1; }
      100% { transform: translateX(36px) translateY(-50%); opacity: 0; }
    }
    @keyframes ffFlowVert {
      0%   { transform: translateX(-50%) translateY(0); opacity: 0; }
      10%  { opacity: 1; }
      90%  { opacity: 1; }
      100% { transform: translateX(-50%) translateY(28px); opacity: 0; }
    }
    @keyframes ffScanline {
      0%   { transform: translateY(-100%); }
      100% { transform: translateY(100vh); }
    }
    @keyframes ffGlow {
      0%, 100% { box-shadow: 0 0 4px rgba(0, 255, 136, 0.2); }
      50%      { box-shadow: 0 0 12px rgba(0, 255, 136, 0.4); }
    }
    @keyframes ffCountdown {
      from { width: 100%; }
      to   { width: 0%; }
    }
    @keyframes ffLivePulse {
      0%, 100% { opacity: 1; box-shadow: 0 0 8px rgba(255, 51, 68, 0.4); }
      50%      { opacity: 0.6; box-shadow: 0 0 20px rgba(255, 51, 68, 0.7); }
    }

    .ff-v2-root {
      font-family: 'JetBrains Mono', 'IBM Plex Mono', monospace;
    }

    /* Pipeline layout: 5 columns on desktop, stacked on mobile */
    .ff-pipeline-grid {
      display: grid;
      grid-template-columns: 1fr 28px 1fr 28px 1fr 28px 1fr 28px 1fr;
      gap: 0;
      align-items: start;
    }

    .ff-connector-col {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100%;
      padding-top: 60px;
    }

    @media (max-width: 1200px) {
      .ff-pipeline-grid {
        grid-template-columns: 1fr 20px 1fr 20px 1fr;
      }
      .ff-stage-4, .ff-stage-5, .ff-conn-3, .ff-conn-4 {
        grid-column: span 1;
      }
      .ff-pipeline-grid {
        grid-template-columns: 1fr;
        gap: 0;
      }
      .ff-connector-col { display: none; }
    }

    @media (max-width: 768px) {
      .ff-pipeline-grid {
        grid-template-columns: 1fr;
        gap: 0;
      }
      .ff-connector-col { display: none; }
      .ff-top-bar { flex-wrap: wrap; gap: 6px !important; }
      .ff-top-bar-right { flex-wrap: wrap; gap: 6px !important; }
    }
  `;
  document.head.appendChild(style);
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function utcClock() {
  return new Date().toISOString().slice(11, 19) + ' UTC';
}

function timeAgo(ts) {
  if (!ts) return '\u2014';
  const ms = Date.now() - (typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
  if (ms < 0) return 'now';
  if (ms < 1000) return '<1s';
  if (ms < 60000) return `${Math.floor(ms / 1000)}s ago`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m ago`;
  return `${Math.floor(ms / 3600000)}h ago`;
}

function formatUptime(seconds) {
  if (!seconds) return '\u2014';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function feedStatus(connected) {
  if (connected === true) return 'ok';
  if (connected === false) return 'error';
  return 'offline';
}

// ── Stage Header ─────────────────────────────────────────────────────────────
function StageHeader({ number, title, color = '#94a3b8' }) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      marginBottom: 10,
      paddingBottom: 6,
      borderBottom: '1px solid #1e293b',
    }}>
      <span style={{
        fontSize: 9,
        fontWeight: 800,
        color: '#0a0e14',
        background: color,
        width: 18,
        height: 18,
        borderRadius: 3,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        lineHeight: 1,
      }}>
        {number}
      </span>
      <span style={{
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: '0.12em',
        color,
        textTransform: 'uppercase',
      }}>
        {title}
      </span>
    </div>
  );
}

// ── Feed Card (compact) ──────────────────────────────────────────────────────
function FeedCard({ feed }) {
  const st = feedStatus(feed.connected);
  const metricStr = feed.metric_fmt?.(feed.metric_value) || null;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '5px 8px',
      borderBottom: '1px solid #0f172a',
      borderRadius: 3,
      background: st === 'error' ? 'rgba(255, 51, 68, 0.04)' : 'transparent',
    }}>
      <StatusPulse status={st} size={6} pulse={st === 'error'} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{
          fontSize: 9,
          fontWeight: 600,
          color: '#e2e8f0',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}>
          {feed.name}
        </div>
        <div style={{
          fontSize: 8,
          color: '#475569',
          display: 'flex',
          gap: 6,
        }}>
          <span>{feed.freq}</span>
          {feed.last_ts && <span>{timeAgo(feed.last_ts)}</span>}
        </div>
      </div>
      {metricStr && (
        <span style={{
          fontSize: 9,
          fontWeight: 500,
          color: '#00ccff',
          whiteSpace: 'nowrap',
          textAlign: 'right',
        }}>
          {metricStr}
        </span>
      )}
    </div>
  );
}

// ── VPIN Bar ─────────────────────────────────────────────────────────────────
function VpinBar({ value, bucketFill }) {
  const pct = Math.min(100, (value ?? 0) * 100);
  const color = value >= 0.65 ? '#00ff88' : value >= 0.45 ? '#ffaa00' : '#475569';

  return (
    <div>
      <div style={{
        display: 'flex',
        alignItems: 'baseline',
        justifyContent: 'space-between',
        marginBottom: 4,
      }}>
        <span style={{ fontSize: 9, color: '#475569' }}>VPIN</span>
        <span style={{
          fontSize: 16,
          fontWeight: 700,
          color,
        }}>
          {value != null ? value.toFixed(3) : '\u2014'}
        </span>
      </div>
      <div style={{
        height: 4,
        background: '#0f172a',
        borderRadius: 2,
        overflow: 'hidden',
        border: '1px solid #1e293b',
      }}>
        <div style={{
          height: '100%',
          width: `${pct}%`,
          background: color,
          borderRadius: 2,
          transition: 'width 0.4s ease-out',
          boxShadow: `0 0 6px ${color}44`,
        }} />
      </div>
      {bucketFill != null && (
        <div style={{ fontSize: 8, color: '#475569', marginTop: 2 }}>
          Bucket fill: {(bucketFill * 100).toFixed(0)}%
        </div>
      )}
    </div>
  );
}

// =============================================================================
// Main Component
// =============================================================================
export default function FactoryFloor() {
  const engine = useEngineStatus();
  const {
    system, feeds, signals, strategy, execution,
    postTrade, wallet, window: win, trades,
    loading, tick, wsConnected, isUsingMocks,
  } = engine;

  const clock = utcClock();
  const paperMode = system?.paper_mode ?? true;
  const engineRunning = system?.engine_running ?? system?.running ?? null;

  // Window countdown
  const windowInfo = useMemo(() => {
    if (!win?.window_ts) return null;
    const ts = typeof win.window_ts === 'number' && win.window_ts > 1e12
      ? win.window_ts / 1000
      : typeof win.window_ts === 'number'
        ? win.window_ts
        : new Date(win.window_ts).getTime() / 1000;
    const now = Date.now() / 1000;
    const elapsed = now - ts;
    const remaining = Math.max(0, 300 - elapsed);
    const pct = Math.min(100, (elapsed / 300) * 100);
    return { elapsed, remaining, pct };
  }, [win, tick]);

  // ── Loading state ─────────────────────────────────────────────────────────
  if (loading && !system) {
    return (
      <div className="ff-v2-root" style={{
        background: '#0a0e14',
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#475569',
        fontSize: 12,
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12 }}>
          <StatusPulse status="warn" size={12} />
          <span>INITIALIZING FACTORY FLOOR...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="ff-v2-root" style={{
      background: '#0a0e14',
      minHeight: '100vh',
      color: '#e2e8f0',
      padding: '12px 16px 32px',
      position: 'relative',
    }}>

      {/* ── Subtle scanline overlay ──────────────────────────────────────────── */}
      <div style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        pointerEvents: 'none',
        zIndex: 9999,
        background: 'repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px)',
        opacity: 0.5,
      }} />

      {/* ══════════════════════════════════════════════════════════════════════ */}
      {/* TOP BAR                                                               */}
      {/* ══════════════════════════════════════════════════════════════════════ */}
      <div className="ff-top-bar" style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        marginBottom: 16,
        padding: '8px 12px',
        background: 'rgba(15, 23, 42, 0.6)',
        border: '1px solid #1e293b',
        borderRadius: 6,
      }}>
        {/* Left: title + clock */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <span style={{
            fontSize: 13,
            fontWeight: 800,
            letterSpacing: '0.1em',
            color: '#e2e8f0',
          }}>
            FACTORY FLOOR
          </span>

          {/* Mode badge */}
          {paperMode ? (
            <span style={{
              fontSize: 9,
              fontWeight: 700,
              padding: '3px 10px',
              borderRadius: 3,
              background: 'rgba(0, 204, 255, 0.12)',
              color: '#00ccff',
              letterSpacing: '0.1em',
              border: '1px solid rgba(0, 204, 255, 0.25)',
            }}>
              PAPER
            </span>
          ) : (
            <span style={{
              fontSize: 9,
              fontWeight: 700,
              padding: '3px 10px',
              borderRadius: 3,
              background: 'rgba(255, 51, 68, 0.15)',
              color: '#ff3344',
              letterSpacing: '0.1em',
              border: '1px solid rgba(255, 51, 68, 0.3)',
              animation: 'ffLivePulse 2s ease-in-out infinite',
            }}>
              LIVE
            </span>
          )}

          {/* Engine status */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <StatusPulse
              status={engineRunning === true ? 'ok' : engineRunning === false ? 'error' : 'offline'}
              size={6}
              pulse={engineRunning === true}
            />
            <span style={{ fontSize: 9, color: '#94a3b8' }}>ENGINE</span>
          </div>

          {/* WS indicator */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <StatusPulse status={wsConnected ? 'ok' : 'offline'} size={5} pulse={false} />
            <span style={{ fontSize: 8, color: '#475569' }}>WS</span>
          </div>

          {/* Clock */}
          <span style={{
            fontSize: 10,
            color: '#475569',
            letterSpacing: '0.06em',
          }}>
            {clock}
          </span>
        </div>

        {/* Right: balances + uptime + window countdown */}
        <div className="ff-top-bar-right" style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {/* USDC Balance */}
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 8, color: '#475569', letterSpacing: '0.06em' }}>USDC</div>
            <div style={{ fontSize: 13, fontWeight: 700, color: '#00ff88' }}>
              ${(wallet?.usdc_balance ?? 0).toFixed(2)}
            </div>
          </div>

          {/* MATIC */}
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 8, color: '#475569', letterSpacing: '0.06em' }}>MATIC</div>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#94a3b8' }}>
              {(wallet?.matic_balance ?? 0).toFixed(2)}
            </div>
          </div>

          {/* Pending Wins */}
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 8, color: '#475569', letterSpacing: '0.06em' }}>PENDING</div>
            <div style={{ fontSize: 11, fontWeight: 600, color: '#ffaa00' }}>
              ${(wallet?.pending_wins_usd ?? 0).toFixed(2)}
            </div>
          </div>

          {/* Divider */}
          <div style={{ width: 1, height: 24, background: '#1e293b' }} />

          {/* Uptime */}
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 8, color: '#475569', letterSpacing: '0.06em' }}>UPTIME</div>
            <div style={{ fontSize: 10, fontWeight: 500, color: '#94a3b8' }}>
              {formatUptime(system?.uptime_seconds)}
            </div>
          </div>

          {/* Window Countdown */}
          <div style={{
            textAlign: 'center',
            minWidth: 72,
            padding: '4px 10px',
            background: windowInfo && windowInfo.remaining < 60
              ? 'rgba(255, 51, 68, 0.1)'
              : windowInfo && windowInfo.remaining < 120
                ? 'rgba(255, 170, 0, 0.08)'
                : 'rgba(0, 255, 136, 0.05)',
            border: `1px solid ${
              windowInfo && windowInfo.remaining < 60
                ? 'rgba(255, 51, 68, 0.25)'
                : windowInfo && windowInfo.remaining < 120
                  ? 'rgba(255, 170, 0, 0.2)'
                  : 'rgba(0, 255, 136, 0.15)'
            }`,
            borderRadius: 4,
          }}>
            <div style={{ fontSize: 8, color: '#475569', letterSpacing: '0.06em' }}>WINDOW</div>
            <div style={{
              fontSize: 14,
              fontWeight: 800,
              color: windowInfo && windowInfo.remaining < 60
                ? '#ff3344'
                : windowInfo && windowInfo.remaining < 120
                  ? '#ffaa00'
                  : '#00ff88',
            }}>
              {windowInfo ? `T-${Math.floor(windowInfo.remaining)}s` : '\u2014'}
            </div>
          </div>
        </div>
      </div>

      {/* Mock data indicator */}
      {isUsingMocks && (
        <div style={{
          fontSize: 8,
          color: '#475569',
          textAlign: 'center',
          marginBottom: 8,
          letterSpacing: '0.08em',
        }}>
          DISPLAYING MOCK DATA -- API NOT CONNECTED
        </div>
      )}

      {/* ══════════════════════════════════════════════════════════════════════ */}
      {/* PIPELINE: 5-stage left-to-right flow                                 */}
      {/* ══════════════════════════════════════════════════════════════════════ */}
      <div className="ff-pipeline-grid">

        {/* ── STAGE 1: DATA FEEDS ──────────────────────────────────────────── */}
        <div>
          <StageHeader number="1" title="Data Feeds" color="#00ccff" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {feeds.map(f => <FeedCard key={f.id} feed={f} />)}
          </div>
        </div>

        {/* Connector 1->2 */}
        <div className="ff-connector-col">
          <PipelineFlow color="#00ccff" active={engineRunning !== false} />
        </div>

        {/* ── STAGE 2: SIGNAL PROCESSING ───────────────────────────────────── */}
        <div>
          <StageHeader number="2" title="Signal Processing" color="#ffaa00" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* VPIN */}
            <MachineCard
              title="VPIN Calculator"
              status={signals?.vpin?.value >= 0.45 ? 'ok' : 'warn'}
            >
              <VpinBar
                value={signals?.vpin?.value}
                bucketFill={signals?.vpin?.bucket_fill}
              />
            </MachineCard>

            {/* Regime Classifier */}
            <MachineCard
              title="Regime Classifier"
              status="ok"
              metrics={[
                {
                  label: 'Regime',
                  value: signals?.regime?.label?.toUpperCase() || 'NORMAL',
                  color: signals?.regime?.label === 'cascade'
                    ? '#ff3344'
                    : signals?.regime?.label === 'volatile_trend'
                      ? '#ffaa00'
                      : '#00ccff',
                  large: true,
                },
                {
                  label: 'Confidence',
                  value: signals?.regime?.confidence != null
                    ? `${(signals.regime.confidence * 100).toFixed(0)}%`
                    : null,
                },
              ]}
            />

            {/* Data Surface */}
            <MachineCard
              title="Data Surface"
              status={signals?.data_surface?.freshness_ms < 5000 ? 'ok' : 'warn'}
              compact
              metrics={[
                {
                  label: 'Freshness',
                  value: signals?.data_surface?.freshness_ms != null
                    ? `${signals.data_surface.freshness_ms}ms`
                    : null,
                  color: signals?.data_surface?.freshness_ms < 2000 ? '#00ff88' : '#ffaa00',
                },
              ]}
            />

            {/* Ensemble */}
            <MachineCard
              title="Ensemble"
              status="ok"
              highlight={signals?.ensemble?.dist_from_half > 0.10}
            >
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  justifyContent: 'space-between',
                }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>P(UP)</span>
                  <span style={{
                    fontSize: 18,
                    fontWeight: 800,
                    color: signals?.ensemble?.direction === 'UP' ? '#00ff88' : '#ff3344',
                  }}>
                    {signals?.ensemble?.p_up != null
                      ? `${(signals.ensemble.p_up * 100).toFixed(1)}%`
                      : '\u2014'}
                  </span>
                </div>
                <div style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  justifyContent: 'space-between',
                }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>Dist</span>
                  <span style={{ fontSize: 11, fontWeight: 600, color: '#00ccff' }}>
                    {signals?.ensemble?.dist_from_half != null
                      ? signals.ensemble.dist_from_half.toFixed(3)
                      : '\u2014'}
                  </span>
                </div>
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>Direction</span>
                  <span style={{
                    fontSize: 10,
                    fontWeight: 700,
                    padding: '1px 8px',
                    borderRadius: 3,
                    background: signals?.ensemble?.direction === 'UP'
                      ? 'rgba(0, 255, 136, 0.1)'
                      : signals?.ensemble?.direction === 'DOWN'
                        ? 'rgba(255, 51, 68, 0.1)'
                        : 'transparent',
                    color: signals?.ensemble?.direction === 'UP'
                      ? '#00ff88'
                      : signals?.ensemble?.direction === 'DOWN'
                        ? '#ff3344'
                        : '#475569',
                  }}>
                    {signals?.ensemble?.direction || '\u2014'}
                  </span>
                </div>
              </div>
            </MachineCard>
          </div>
        </div>

        {/* Connector 2->3 */}
        <div className="ff-connector-col">
          <PipelineFlow color="#ffaa00" active={engineRunning !== false} />
        </div>

        {/* ── STAGE 3: STRATEGY ENGINE ─────────────────────────────────────── */}
        <div>
          <StageHeader number="3" title="Strategy Engine" color="#a855f7" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* v8_champion */}
            <MachineCard
              title="v8_champion"
              status={strategy?.v8_champion?.status === 'LIVE' ? 'ok' : 'warn'}
              highlight
            >
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {/* Status badge */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{
                    fontSize: 9,
                    fontWeight: 700,
                    padding: '2px 8px',
                    borderRadius: 3,
                    background: strategy?.v8_champion?.status === 'LIVE'
                      ? 'rgba(0, 255, 136, 0.1)'
                      : 'rgba(255, 170, 0, 0.1)',
                    color: strategy?.v8_champion?.status === 'LIVE' ? '#00ff88' : '#ffaa00',
                    border: `1px solid ${
                      strategy?.v8_champion?.status === 'LIVE'
                        ? 'rgba(0, 255, 136, 0.25)'
                        : 'rgba(255, 170, 0, 0.25)'
                    }`,
                  }}>
                    {strategy?.v8_champion?.status || 'LIVE'}
                  </span>
                  <span style={{
                    fontSize: 10,
                    fontWeight: 600,
                    color: strategy?.v8_champion?.last_decision === 'TRADE'
                      ? '#00ff88'
                      : strategy?.v8_champion?.last_decision === 'SKIP'
                        ? '#ffaa00'
                        : '#475569',
                  }}>
                    {strategy?.v8_champion?.last_decision || '\u2014'}
                  </span>
                  {strategy?.v8_champion?.window_position && (
                    <span style={{ fontSize: 9, color: '#00ccff' }}>
                      {strategy.v8_champion.window_position}
                    </span>
                  )}
                </div>

                {/* Blocked reason */}
                {strategy?.v8_champion?.last_gate_blocked && (
                  <div style={{
                    fontSize: 8,
                    color: '#ff3344',
                    padding: '3px 6px',
                    background: 'rgba(255, 51, 68, 0.06)',
                    borderRadius: 3,
                    border: '1px solid rgba(255, 51, 68, 0.15)',
                  }}>
                    {strategy.v8_champion.last_gate_blocked}
                  </div>
                )}
              </div>
            </MachineCard>

            {/* Gate Pipeline Visualization */}
            <MachineCard title="Gate Pipeline" status="ok">
              <GatePipeline gates={strategy?.v8_champion?.gates || []} />
            </MachineCard>

            {/* Ghost Strategies */}
            <MachineCard
              title="Ghost Strategies"
              status="offline"
              compact
              metrics={[
                {
                  label: 'Active',
                  value: strategy?.ghosts?.count != null
                    ? String(strategy.ghosts.count)
                    : '0',
                  color: '#94a3b8',
                },
                {
                  label: 'Last Shadow',
                  value: strategy?.ghosts?.last_shadow || 'none',
                  color: '#475569',
                },
              ]}
            />
          </div>
        </div>

        {/* Connector 3->4 */}
        <div className="ff-connector-col">
          <PipelineFlow color="#a855f7" active={engineRunning !== false} />
        </div>

        {/* ── STAGE 4: EXECUTION ───────────────────────────────────────────── */}
        <div>
          <StageHeader number="4" title="Execution" color="#00ff88" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* FAK Executor */}
            <MachineCard
              title="FAK Executor"
              status={execution?.fak_executor?.success_rate > 0.75 ? 'ok' : 'warn'}
              metrics={[
                {
                  label: 'Last Fill',
                  value: execution?.fak_executor?.last_fill_price != null
                    ? `$${execution.fak_executor.last_fill_price.toFixed(3)}`
                    : null,
                  large: true,
                },
                {
                  label: 'Success Rate',
                  value: execution?.fak_executor?.success_rate != null
                    ? `${(execution.fak_executor.success_rate * 100).toFixed(0)}%`
                    : null,
                  color: execution?.fak_executor?.success_rate > 0.75 ? '#00ff88' : '#ffaa00',
                },
                {
                  label: 'Last Fill',
                  value: execution?.fak_executor?.last_fill_ts
                    ? timeAgo(execution.fak_executor.last_fill_ts)
                    : null,
                  color: '#475569',
                },
              ]}
            />

            {/* Risk Manager */}
            <MachineCard
              title="Risk Manager"
              status={
                execution?.risk_manager?.exposure_pct > 0.25 ? 'warn'
                : execution?.risk_manager?.daily_pnl < -20 ? 'error'
                : 'ok'
              }
              highlight
            >
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>Bankroll</span>
                  <span style={{ fontSize: 13, fontWeight: 700, color: '#00ff88' }}>
                    ${(execution?.risk_manager?.bankroll ?? 0).toFixed(2)}
                  </span>
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>Exposure</span>
                  <span style={{
                    fontSize: 10,
                    fontWeight: 600,
                    color: (execution?.risk_manager?.exposure_pct ?? 0) > 0.25 ? '#ffaa00' : '#94a3b8',
                  }}>
                    {execution?.risk_manager?.exposure_pct != null
                      ? `${(execution.risk_manager.exposure_pct * 100).toFixed(1)}%`
                      : '\u2014'}
                  </span>
                </div>
                {/* Exposure bar */}
                <div style={{
                  height: 3,
                  background: '#0f172a',
                  borderRadius: 2,
                  overflow: 'hidden',
                  border: '1px solid #1e293b',
                }}>
                  <div style={{
                    height: '100%',
                    width: `${Math.min(100, (execution?.risk_manager?.exposure_pct ?? 0) * 100 / 0.30 * 100)}%`,
                    background: (execution?.risk_manager?.exposure_pct ?? 0) > 0.25 ? '#ffaa00' : '#00ff88',
                    borderRadius: 2,
                    transition: 'width 0.3s',
                  }} />
                </div>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span style={{ fontSize: 9, color: '#475569' }}>Daily P&L</span>
                  <span style={{
                    fontSize: 11,
                    fontWeight: 700,
                    color: (execution?.risk_manager?.daily_pnl ?? 0) >= 0 ? '#00ff88' : '#ff3344',
                  }}>
                    {execution?.risk_manager?.daily_pnl != null
                      ? `${execution.risk_manager.daily_pnl >= 0 ? '+' : ''}$${execution.risk_manager.daily_pnl.toFixed(2)}`
                      : '\u2014'}
                  </span>
                </div>
              </div>
            </MachineCard>

            {/* Trade Claims */}
            <MachineCard
              title="Active Claims"
              status={execution?.trade_claims?.active_count > 0 ? 'ok' : 'offline'}
              compact
              metrics={[
                {
                  label: 'Open',
                  value: execution?.trade_claims?.active_count != null
                    ? String(execution.trade_claims.active_count)
                    : '0',
                  color: execution?.trade_claims?.active_count > 0 ? '#00ccff' : '#475569',
                  large: true,
                },
              ]}
            />
          </div>
        </div>

        {/* Connector 4->5 */}
        <div className="ff-connector-col">
          <PipelineFlow color="#00ff88" active={engineRunning !== false} />
        </div>

        {/* ── STAGE 5: POST-TRADE ──────────────────────────────────────────── */}
        <div>
          <StageHeader number="5" title="Post-Trade" color="#e2e8f0" />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* CLOB Reconciler */}
            <MachineCard
              title="CLOB Reconciler"
              status={postTrade?.clob_reconciler?.status === 'ok' ? 'ok' : 'warn'}
              metrics={[
                {
                  label: 'Today',
                  value: postTrade?.clob_reconciler
                    ? `${postTrade.clob_reconciler.wins_today}W / ${postTrade.clob_reconciler.losses_today}L`
                    : null,
                  color: '#e2e8f0',
                },
              ]}
            >
              {/* Win/Loss mini bar */}
              {postTrade?.clob_reconciler && (
                <div style={{ marginTop: 6 }}>
                  <div style={{
                    height: 4,
                    background: '#0f172a',
                    borderRadius: 2,
                    overflow: 'hidden',
                    display: 'flex',
                    border: '1px solid #1e293b',
                  }}>
                    {(() => {
                      const w = postTrade.clob_reconciler.wins_today || 0;
                      const l = postTrade.clob_reconciler.losses_today || 0;
                      const total = w + l || 1;
                      return (
                        <>
                          <div style={{
                            height: '100%',
                            width: `${(w / total) * 100}%`,
                            background: '#00ff88',
                          }} />
                          <div style={{
                            height: '100%',
                            width: `${(l / total) * 100}%`,
                            background: '#ff3344',
                          }} />
                        </>
                      );
                    })()}
                  </div>
                </div>
              )}
            </MachineCard>

            {/* SOT Reconciler */}
            <MachineCard
              title="SOT Reconciler"
              status={postTrade?.sot_reconciler?.status === 'ok' ? 'ok' : 'warn'}
              compact
              metrics={[
                {
                  label: 'Last Pass',
                  value: postTrade?.sot_reconciler?.last_pass_agrees === true
                    ? 'AGREES'
                    : postTrade?.sot_reconciler?.last_pass_agrees === false
                      ? 'DIVERGED'
                      : null,
                  color: postTrade?.sot_reconciler?.last_pass_agrees === true
                    ? '#00ff88'
                    : postTrade?.sot_reconciler?.last_pass_agrees === false
                      ? '#ff3344'
                      : '#475569',
                },
                {
                  label: 'When',
                  value: postTrade?.sot_reconciler?.last_pass_ts
                    ? timeAgo(postTrade.sot_reconciler.last_pass_ts)
                    : null,
                  color: '#475569',
                },
              ]}
            />

            {/* Redeemer */}
            <MachineCard
              title="Redeemer"
              status={postTrade?.redeemer?.pending_wins_usd > 0 ? 'warn' : 'ok'}
              metrics={[
                {
                  label: 'Status',
                  value: (postTrade?.redeemer?.status || 'idle').toUpperCase(),
                  color: postTrade?.redeemer?.status === 'redeeming' ? '#ffaa00' : '#94a3b8',
                },
                {
                  label: 'Pending Wins',
                  value: postTrade?.redeemer?.pending_wins_usd != null
                    ? `$${postTrade.redeemer.pending_wins_usd.toFixed(2)}`
                    : '$0.00',
                  color: postTrade?.redeemer?.pending_wins_usd > 0 ? '#ffaa00' : '#475569',
                  large: true,
                },
                {
                  label: 'Transport',
                  value: postTrade?.redeemer?.on_chain_transport || 'polygon',
                  color: '#475569',
                },
              ]}
            />

            {/* On-Chain Scanner */}
            <MachineCard
              title="On-Chain Scanner"
              status={postTrade?.on_chain_scanner?.last_scan_result === 'clean' ? 'ok' : 'warn'}
              compact
              metrics={[
                {
                  label: 'RPCs',
                  value: postTrade?.on_chain_scanner?.connected_rpcs != null
                    ? String(postTrade.on_chain_scanner.connected_rpcs)
                    : null,
                },
                {
                  label: 'Last Scan',
                  value: postTrade?.on_chain_scanner?.last_scan_result?.toUpperCase() || null,
                  color: postTrade?.on_chain_scanner?.last_scan_result === 'clean'
                    ? '#00ff88'
                    : '#ffaa00',
                },
                {
                  label: 'When',
                  value: postTrade?.on_chain_scanner?.last_scan_ts
                    ? timeAgo(postTrade.on_chain_scanner.last_scan_ts)
                    : null,
                  color: '#475569',
                },
              ]}
            />
          </div>
        </div>
      </div>

      {/* ══════════════════════════════════════════════════════════════════════ */}
      {/* WINDOW PROGRESS BAR                                                   */}
      {/* ══════════════════════════════════════════════════════════════════════ */}
      <div style={{
        marginTop: 16,
        padding: '10px 14px',
        background: 'rgba(15, 23, 42, 0.4)',
        border: '1px solid #1e293b',
        borderRadius: 6,
      }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 6,
        }}>
          <span style={{
            fontSize: 9,
            fontWeight: 700,
            letterSpacing: '0.1em',
            color: '#475569',
          }}>
            WINDOW PROGRESS
          </span>
          <div style={{ display: 'flex', gap: 16, fontSize: 9, color: '#475569' }}>
            <span>
              ID: {win?.window_id || '\u2014'}
            </span>
            <span>
              Elapsed: {windowInfo ? `${Math.floor(windowInfo.elapsed)}s / 300s` : '\u2014'}
            </span>
          </div>
        </div>
        <div style={{
          height: 6,
          background: '#0f172a',
          borderRadius: 3,
          overflow: 'hidden',
          border: '1px solid #1e293b',
        }}>
          <div style={{
            height: '100%',
            width: `${windowInfo?.pct ?? 0}%`,
            background: !windowInfo ? '#334155'
              : windowInfo.remaining < 60 ? '#ff3344'
              : windowInfo.remaining < 120 ? '#ffaa00'
              : '#00ff88',
            borderRadius: 3,
            transition: 'width 1s linear',
            boxShadow: windowInfo
              ? `0 0 8px ${
                windowInfo.remaining < 60 ? 'rgba(255,51,68,0.4)'
                : windowInfo.remaining < 120 ? 'rgba(255,170,0,0.3)'
                : 'rgba(0,255,136,0.3)'
              }`
              : 'none',
          }} />
        </div>
      </div>

      {/* ══════════════════════════════════════════════════════════════════════ */}
      {/* RECENT TRADES STRIP                                                   */}
      {/* ══════════════════════════════════════════════════════════════════════ */}
      <div style={{
        marginTop: 12,
        padding: '10px 14px',
        background: 'rgba(15, 23, 42, 0.4)',
        border: '1px solid #1e293b',
        borderRadius: 6,
      }}>
        <div style={{
          fontSize: 9,
          fontWeight: 700,
          letterSpacing: '0.1em',
          color: '#475569',
          marginBottom: 8,
        }}>
          RECENT TRADES
        </div>

        {trades.length > 0 ? (
          <div style={{
            display: 'grid',
            gridTemplateColumns: '80px 50px 70px 60px 1fr',
            gap: '4px 12px',
            fontSize: 9,
          }}>
            {/* Header */}
            <span style={{ color: '#334155', letterSpacing: '0.06em' }}>TIME</span>
            <span style={{ color: '#334155', letterSpacing: '0.06em' }}>DIR</span>
            <span style={{ color: '#334155', letterSpacing: '0.06em' }}>PRICE</span>
            <span style={{ color: '#334155', letterSpacing: '0.06em' }}>RESULT</span>
            <span style={{ color: '#334155', letterSpacing: '0.06em' }}>STRATEGY</span>

            {/* Rows */}
            {trades.slice(0, 5).map((t, i) => {
              const isWin = t.outcome === 'WIN' || t.poly_outcome === 'WIN';
              const isLoss = t.outcome === 'LOSS' || t.poly_outcome === 'LOSS';
              return (
                <React.Fragment key={i}>
                  <span style={{ color: '#94a3b8' }}>
                    {t.created_at
                      ? new Date(t.created_at).toISOString().slice(11, 19)
                      : t.window_ts
                        ? new Date(typeof t.window_ts === 'number' ? t.window_ts * 1000 : t.window_ts).toISOString().slice(11, 19)
                        : '\u2014'}
                  </span>
                  <span style={{
                    fontWeight: 600,
                    color: t.direction === 'UP' ? '#00ff88' : t.direction === 'DOWN' ? '#ff3344' : '#475569',
                  }}>
                    {t.direction || '\u2014'}
                  </span>
                  <span style={{ color: '#00ccff' }}>
                    ${t.entry_price?.toFixed(3) || t.fill_price?.toFixed(3) || '\u2014'}
                  </span>
                  <span style={{
                    fontWeight: 700,
                    color: isWin ? '#00ff88' : isLoss ? '#ff3344' : '#475569',
                  }}>
                    {t.outcome || t.poly_outcome || 'OPEN'}
                  </span>
                  <span style={{ color: '#475569' }}>
                    {t.strategy || t.config_name || '\u2014'}
                  </span>
                </React.Fragment>
              );
            })}
          </div>
        ) : (
          <div style={{ fontSize: 10, color: '#334155' }}>No recent trades</div>
        )}
      </div>
    </div>
  );
}
