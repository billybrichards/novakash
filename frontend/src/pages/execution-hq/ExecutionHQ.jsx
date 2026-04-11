import React, { useState, useEffect, useRef } from 'react';
import { useParams, Navigate } from 'react-router-dom';
import { Radio, History } from 'lucide-react';
import { useApi } from '../../hooks/useApi.js';
import LiveTab from './components/LiveTab.jsx';
import RetroTab from './components/RetroTab.jsx';
import ManualTradePanel from './components/ManualTradePanel.jsx';
import TradeTicker from './components/TradeTicker.jsx';
import TradeToast from './components/TradeToast.jsx';
import { T } from './components/constants.js';

// UI-02: canonical HQ asset / timeframe sets. Keep in sync with
// hub/api/v58_monitor.py::_HQ_ASSETS / _HQ_TIMEFRAMES.
const HQ_ASSETS = ['btc', 'eth', 'sol', 'xrp'];
const HQ_TIMEFRAMES = ['5m', '15m'];
const HQ_ASSET_SET = new Set(HQ_ASSETS);
const HQ_TIMEFRAME_SET = new Set(HQ_TIMEFRAMES);
// BTC 5m is the only pair where the operator is currently allowed to
// place manual trades from the HQ — the other 7 are monitoring-only
// (see UI-02 task). This gates ManualTradePanel rendering.
const LIVE_TRADING_ASSET = 'btc';
const LIVE_TRADING_TIMEFRAME = '5m';

/**
 * ExecutionHQ — Primary execution monitoring dashboard.
 *
 * Route: /execution-hq/:asset/:timeframe
 *   - :asset in {btc, eth, sol, xrp}
 *   - :timeframe in {5m, 15m}
 *   - BTC 5m is the active trading pair (renders the ManualTradePanel).
 *   - The other 7 routes are monitoring-only views.
 *
 * Tabs:
 *   - Live Execution: Real-time window countdown, gate audit, price charts, feed health
 *   - Retrospective: Full window history with shadow resolution and missed opportunity analysis
 *
 * Data sources:
 *   - /api/v58/execution-hq?asset=...&timeframe=... — Combined endpoint
 *     (windows, shadow stats, trades, system state, gate heartbeat)
 *   - /api/dashboard/stats  — Bankroll and engine status (global)
 */
export default function ExecutionHQ() {
  // Resolve route params first. If the URL is invalid we bounce to
  // /execution-hq/btc/5m, but we compute validity here and render the
  // Navigate at the *bottom* of the function so every hook is still
  // called in a stable order (React's rules-of-hooks).
  const params = useParams();
  const rawAsset = (params.asset || '').toLowerCase();
  const rawTimeframe = (params.timeframe || '').toLowerCase();
  const validParams = HQ_ASSET_SET.has(rawAsset) && HQ_TIMEFRAME_SET.has(rawTimeframe);
  // When params are invalid, fall through to a safe default so the hooks
  // below still run deterministically; we render <Navigate> at the end.
  const asset = validParams ? rawAsset : LIVE_TRADING_ASSET;
  const timeframe = validParams ? rawTimeframe : LIVE_TRADING_TIMEFRAME;
  const isLiveTradingPair =
    asset === LIVE_TRADING_ASSET && timeframe === LIVE_TRADING_TIMEFRAME;

  const api = useApi();
  const [activeTab, setActiveTab] = useState('live');
  const [tick, setTick] = useState(0);

  // API data
  const [hqData, setHqData] = useState(null);
  const [dashStats, setDashStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // POLY-SOT — recent manual trades + their reconciliation state. Polled
  // alongside hqData and passed into TradeTicker so the always-visible
  // strip surfaces engine_optimistic / diverged manual trades immediately.
  const [manualSotRows, setManualSotRows] = useState([]);
  // POLY-SOT-b — same for the automatic-trade table. Pulled in parallel
  // and rendered with an AUTO prefix on the chip.
  const [sotRows, setSotRows] = useState([]);

  // Tick counter for animations
  useEffect(() => {
    const timer = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  // Update the browser tab title so an operator with 8 HQ tabs open can
  // tell them apart at a glance.
  useEffect(() => {
    if (!validParams) return undefined;
    const prev = document.title;
    document.title = `HQ · ${asset.toUpperCase()} ${timeframe} — Novakash`;
    return () => { document.title = prev; };
  }, [asset, timeframe, validParams]);

  // Fetch execution HQ data. The endpoint is parameterised on asset +
  // timeframe so each HQ page only sees its own market's data.
  // POLY-SOT (this PR): also fetches /v58/manual-trades-sot in parallel
  // so the trade ticker can render the SOT chip alongside engine trades.
  // POLY-SOT-b: also fetches /v58/trades-sot in parallel for automatic
  // engine trades. Only requested for the live-trading pair (BTC 5m)
  // since the other 7 routes are monitor-only and never write trade rows.
  const fetchData = async () => {
    try {
      const hqUrl = `/v58/execution-hq?limit=200&asset=${encodeURIComponent(asset)}&timeframe=${encodeURIComponent(timeframe)}`;
      const calls = [
        api('GET', hqUrl),
        api('GET', '/dashboard/stats'),
      ];
      if (isLiveTradingPair) {
        calls.push(api('GET', '/v58/manual-trades-sot?limit=10'));
        calls.push(api('GET', '/v58/trades-sot?limit=10'));
      }
      const results = await Promise.allSettled(calls);
      const [hqRes, statsRes, manualSotRes, autoSotRes] = results;

      if (hqRes && hqRes.status === 'fulfilled') {
        setHqData(hqRes.value?.data || hqRes.value);
      }
      if (statsRes && statsRes.status === 'fulfilled') {
        setDashStats(statsRes.value?.data || statsRes.value);
      }
      if (manualSotRes && manualSotRes.status === 'fulfilled') {
        const sotData = manualSotRes.value?.data || manualSotRes.value;
        const rows = Array.isArray(sotData?.rows) ? sotData.rows : [];
        setManualSotRows(rows);
      }
      if (autoSotRes && autoSotRes.status === 'fulfilled') {
        const autoSotData = autoSotRes.value?.data || autoSotRes.value;
        const rows = Array.isArray(autoSotData?.rows) ? autoSotData.rows : [];
        setSotRows(rows);
      }
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  };

  // Reset data + refetch whenever asset/timeframe changes so stale data
  // from the previous market never leaks into the new one.
  useEffect(() => {
    if (!validParams) return;
    setHqData(null);
    setLoading(true);
    fetchData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, asset, timeframe, validParams]);

  // Poll every 10s on live tab
  useEffect(() => {
    if (!validParams) return undefined;
    if (activeTab !== 'live') return undefined;
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, api, asset, timeframe, validParams]);

  // Re-fetch on tab switch to retro
  const prevTab = useRef(activeTab);
  useEffect(() => {
    if (!validParams) return;
    if (activeTab === 'retro' && prevTab.current === 'live') {
      fetchData();
    }
    prevTab.current = activeTab;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab, validParams]);

  // Bail out *after* all hooks have run. The Navigate renders once and
  // the router replaces this page with the default BTC/5m HQ.
  if (!validParams) {
    return <Navigate to={`/execution-hq/${LIVE_TRADING_ASSET}/${LIVE_TRADING_TIMEFRAME}`} replace />;
  }

  const bankroll = dashStats?.balance ?? hqData?.system?.bankroll ?? 0;
  const windows = hqData?.windows || [];
  const shadowStats = hqData?.shadow_stats || {};
  const v9Stats = hqData?.v9_stats || {};
  const v10Stats = hqData?.v10_stats || {};
  const v9GateData = hqData?.v9_gate_data || {};

  return (
    <div style={{
      minHeight: '100vh', background: T.bg, color: T.text, padding: 8,
      fontFamily: 'sans-serif', overflow: 'hidden', display: 'flex', flexDirection: 'column',
      height: '100vh', userSelect: 'none',
    }}>
      {/* GLOBAL HEADER */}
      <header style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        background: '#0f172a', border: `1px solid ${T.cardBorder}`, padding: 8,
        borderRadius: 2, marginBottom: 8, flexShrink: 0,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          {/* Tab switcher */}
          <div style={{ display: 'flex', alignItems: 'center', background: '#020617', padding: 4, borderRadius: 4, border: `1px solid ${T.cardBorder}` }}>
            <button
              onClick={() => setActiveTab('live')}
              style={{
                padding: '4px 12px', display: 'flex', alignItems: 'center', gap: 8,
                fontSize: 12, fontWeight: 700, letterSpacing: '0.1em', borderRadius: 2,
                border: activeTab === 'live' ? '1px solid rgba(239,68,68,0.3)' : '1px solid transparent',
                background: activeTab === 'live' ? 'rgba(239,68,68,0.1)' : 'transparent',
                color: activeTab === 'live' ? '#ef4444' : T.textDim,
                cursor: 'pointer', fontFamily: 'monospace',
                boxShadow: activeTab === 'live' ? '0 0 10px rgba(239,68,68,0.2)' : 'none',
                transition: 'all 150ms',
              }}
            >
              <Radio size={14} style={activeTab === 'live' ? { animation: 'pulse 2s infinite' } : {}} />
              LIVE EXECUTION
            </button>
            <button
              onClick={() => setActiveTab('retro')}
              style={{
                padding: '4px 12px', display: 'flex', alignItems: 'center', gap: 8,
                fontSize: 12, fontWeight: 700, letterSpacing: '0.1em', borderRadius: 2,
                border: activeTab === 'retro' ? '1px solid rgba(6,182,212,0.3)' : '1px solid transparent',
                background: activeTab === 'retro' ? 'rgba(6,182,212,0.1)' : 'transparent',
                color: activeTab === 'retro' ? T.cyan : T.textDim,
                cursor: 'pointer', fontFamily: 'monospace',
                boxShadow: activeTab === 'retro' ? '0 0 10px rgba(6,182,212,0.2)' : 'none',
                transition: 'all 150ms',
              }}
            >
              <History size={14} />
              RETROSPECTIVE
            </button>
          </div>

          <div style={{ height: 16, width: 1, background: T.cardBorder }} />

          {/* UI-02: Asset / timeframe badge so the operator can tell the 8 HQ pages apart */}
          <div style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '4px 10px', borderRadius: 3,
            background: isLiveTradingPair ? 'rgba(168,85,247,0.12)' : 'rgba(6,182,212,0.08)',
            border: `1px solid ${isLiveTradingPair ? 'rgba(168,85,247,0.4)' : 'rgba(6,182,212,0.3)'}`,
            fontFamily: "'JetBrains Mono', monospace",
          }}>
            <span style={{
              fontSize: 14, fontWeight: 800,
              color: isLiveTradingPair ? T.purple : T.cyan,
              letterSpacing: '0.05em',
            }}>{asset.toUpperCase()}</span>
            <span style={{ color: T.textDim, fontSize: 10 }}>/</span>
            <span style={{
              fontSize: 12, fontWeight: 700,
              color: isLiveTradingPair ? T.purple : T.cyan,
              letterSpacing: '0.05em',
            }}>{timeframe}</span>
            <span style={{
              marginLeft: 6, fontSize: 8,
              color: isLiveTradingPair ? T.purple : T.textMuted,
              textTransform: 'uppercase', letterSpacing: '0.08em',
            }}>
              {isLiveTradingPair ? 'LIVE TRADING' : 'MONITOR'}
            </span>
          </div>

          {/* System info */}
          <div style={{ fontFamily: 'monospace', fontSize: 12, color: T.textMuted, display: 'flex', gap: 16 }}>
            <span>MODE: <span style={{ color: hqData?.system?.paper_mode ? T.amber : T.cyan }}>
              {hqData?.system?.paper_mode ? 'PAPER' : 'LIVE'}
            </span></span>
            <span>MAX_OPEN: <span style={{ color: T.amber }}>45%</span></span>
            <span>DAILY_LOSS: <span style={{ color: T.green }}>$50</span></span>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 16, fontFamily: 'monospace' }}>
          {/* v10 WR counter (shows v10 DUNE stats if available, otherwise combined) */}
          {(() => {
            const stats = (v10Stats.total_trades > 0) ? v10Stats : v9Stats;
            const label = (v10Stats.total_trades > 0) ? 'v10 DUNE' : 'WR';
            if (!stats.wins && !stats.losses) return null;
            return (
              <div style={{
                display: 'flex', flexDirection: 'column', alignItems: 'flex-end',
                background: 'rgba(168,85,247,0.08)', border: '1px solid rgba(168,85,247,0.3)',
                padding: '4px 10px', borderRadius: 4,
              }}>
                <span style={{ fontSize: 9, color: T.purple, textTransform: 'uppercase', letterSpacing: '0.05em' }}>{label}</span>
                <span style={{ fontSize: 14, fontWeight: 700, color: T.purple, fontFamily: "'JetBrains Mono', monospace" }}>
                  {stats.wins}W/{stats.losses}L = {stats.wr_pct}%
                </span>
              </div>
            );
          })()}
          {/* Shadow stats badge */}
          {shadowStats.shadow_wins > 0 && (
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
              <span style={{ fontSize: 9, color: T.amber, textTransform: 'uppercase' }}>Missed Wins</span>
              <span style={{ fontSize: 14, fontWeight: 700, color: T.amber }}>{shadowStats.shadow_wins}</span>
            </div>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
            <span style={{ fontSize: 9, color: T.textMuted, textTransform: 'uppercase' }}>Current Bankroll</span>
            <span style={{ fontSize: 18, fontWeight: 700, color: T.green }}>${bankroll.toFixed(2)}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end' }}>
            <span style={{ fontSize: 9, color: T.textMuted, textTransform: 'uppercase' }}>Sys Time</span>
            <span style={{ fontSize: 14, color: T.text }}>{new Date().toISOString().substring(11, 19)}</span>
          </div>
        </div>
      </header>

      {/* Trade ticker strip — POLY-SOT chips for manual trades + engine trades */}
      <TradeTicker
        recentTrades={hqData?.recent_trades || []}
        manualSotRows={manualSotRows}
        sotRows={sotRows}
      />

      {/* Error banner */}
      {error && (
        <div style={{
          background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)',
          padding: '6px 12px', borderRadius: 2, marginBottom: 8, fontSize: 11,
          fontFamily: 'monospace', color: '#fca5a5', flexShrink: 0,
        }}>
          API Error: {error}
        </div>
      )}

      {/* UI-02: "no data yet" info banner for asset/timeframe combinations
          that the data-collector / engine isn't populating yet (e.g. ETH 15m
          on day 1). Shown instead of a blank dashboard when the hub returns
          empty arrays. BTC 5m always has data so it never shows this. */}
      {!loading && hqData && !isLiveTradingPair && windows.length === 0 && (hqData?.gate_heartbeat?.length || 0) === 0 && (
        <div style={{
          background: 'rgba(6,182,212,0.08)', border: '1px solid rgba(6,182,212,0.3)',
          padding: '10px 14px', borderRadius: 3, marginBottom: 8, fontSize: 12,
          fontFamily: 'monospace', color: T.cyan, flexShrink: 0,
          display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <span style={{ fontWeight: 700 }}>NO DATA</span>
          <span style={{ color: T.textMuted }}>
            No window_snapshots or signal_evaluations rows found for
            {' '}<strong style={{ color: T.cyan }}>{asset.toUpperCase()} {timeframe}</strong>.
            {' '}The data-collector may not be writing this pair yet — contact ops
            or check FIVE_MIN_ASSETS / FIFTEEN_MIN_ASSETS env vars in the engine.
          </span>
        </div>
      )}

      {/* Loading state */}
      {loading && !hqData && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: 'monospace', color: T.textMuted, fontSize: 14,
        }}>
          Loading execution data for {asset.toUpperCase()} {timeframe}...
        </div>
      )}

      {/* Tab content */}
      {(!loading || hqData) && activeTab === 'live' && (
        <LiveTab
          hqData={hqData}
          tick={tick}
          v9Stats={v9Stats}
          v9GateData={v9GateData}
          v10Stats={v10Stats}
          asset={asset}
          timeframe={timeframe}
        />
      )}
      {(!loading || hqData) && activeTab === 'retro' && (
        <RetroTab windows={windows} shadowStats={shadowStats} v9Stats={v9Stats} v10Stats={v10Stats} recentTrades={hqData?.recent_trades || []} />
      )}

      {/* Trade toast notification (portal) */}
      <TradeToast recentTrades={hqData?.recent_trades || []} />

      {/* Floating manual trade panel (portal)
          UI-02: Gated to BTC 5m only — the other 7 pairs are
          monitoring-only. Rendering the panel on them would risk
          cross-market trades since the panel POSTs to /v58/manual-trade
          which currently assumes BTC 5m. Once multi-market manual trading
          ships, drop this guard. */}
      {isLiveTradingPair && <ManualTradePanel hqData={hqData} />}
    </div>
  );
}
