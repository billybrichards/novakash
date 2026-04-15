import React, { useState, useEffect, useRef } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, POLLING_INTERVAL_MS, DEFAULT_ASSET, DEFAULT_TIMESCALES, DEFAULT_STRATEGY } from './components/constants.js';
import PositionsPanel from './components/PositionsPanel.jsx';
import SignalPanel from './components/SignalPanel.jsx';
import TradeTimelinePanel from './components/TradeTimelinePanel.jsx';
import V4Panel from './components/V4Panel.jsx';

function StatusDot({ active, label, activeText, inactiveText, amber }) {
  const color = active ? T.green : amber ? T.amber : T.red;
  const text = active ? activeText : inactiveText;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <div style={{
        width: 6, height: 6, borderRadius: '50%', background: color,
        boxShadow: `0 0 6px ${color}88`,
        animation: active ? 'none' : 'pulse 2s infinite',
      }} />
      <span style={{ fontSize: 9, color: T.textMuted, fontWeight: 600 }}>{label}:</span>
      <span style={{ fontSize: 9, color, fontWeight: 700, fontFamily: T.mono }}>{text}</span>
    </div>
  );
}

export default function MarginEngine() {
  const api = useApi();
  const [activeTab, setActiveTab] = useState('live');
  const [marginData, setMarginData] = useState(null);
  const [signalSnapshot, setSignalSnapshot] = useState(null);
  const [v4Snapshot, setV4Snapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [controlLoading, setControlLoading] = useState({});

  const fetchData = async () => {
    try {
      const [marginRes, signalRes, v4Res] = await Promise.allSettled([
        api('GET', '/margin/status'),
        api('GET', `/v3/snapshot?asset=${DEFAULT_ASSET}`),
        api('GET', `/v4/snapshot?asset=${DEFAULT_ASSET}&timescales=${DEFAULT_TIMESCALES}&strategy=${DEFAULT_STRATEGY}`),
      ]);

      if (marginRes.status === 'fulfilled') {
        setMarginData(marginRes.value?.data || marginRes.value);
      }
      if (signalRes.status === 'fulfilled') {
        setSignalSnapshot(signalRes.value?.data || signalRes.value);
      }
      if (v4Res.status === 'fulfilled') {
        setV4Snapshot(v4Res.value?.data || v4Res.value);
      }
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchData(); }, [api]);

  useEffect(() => {
    if (activeTab !== 'live') return;
    const interval = setInterval(fetchData, POLLING_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [activeTab, api]);

  const handleKillSwitch = async () => {
    if (!confirm('Are you sure you want to trigger the kill switch? This will immediately close all positions.')) return;
    setControlLoading(prev => ({ ...prev, kill: true }));
    try {
      await api('POST', '/system/kill');
      fetchData();
    } catch (err) {
      setError(err.message || 'Failed to trigger kill switch');
    } finally {
      setControlLoading(prev => ({ ...prev, kill: false }));
    }
  };

  const handleResume = async () => {
    setControlLoading(prev => ({ ...prev, resume: true }));
    try {
      await api('POST', '/system/resume');
      fetchData();
    } catch (err) {
      setError(err.message || 'Failed to resume');
    } finally {
      setControlLoading(prev => ({ ...prev, resume: false }));
    }
  };

  const handleTogglePaperMode = async () => {
    const newPaperMode = !marginData?.portfolio?.paper_mode;
    setControlLoading(prev => ({ ...prev, paperMode: true }));
    try {
      await api('PUT', '/system/paper-mode', { data: { paper_mode: newPaperMode } });
      fetchData();
    } catch (err) {
      setError(err.message || 'Failed to toggle paper mode');
    } finally {
      setControlLoading(prev => ({ ...prev, paperMode: false }));
    }
  };

  const portfolio = marginData?.portfolio || {};
  const execution = marginData?.execution || {};
  const positions = marginData?.positions || [];
  const openPositions = positions.filter(p => p.state === 'OPEN');
  const closedPositions = positions.filter(p => p.state === 'CLOSED');
  const totalPnl = closedPositions.reduce((sum, p) => sum + (p.realised_pnl || 0), 0);
  const winRate = closedPositions.length > 0
    ? (closedPositions.filter(p => p.realised_pnl > 0).length / closedPositions.length * 100).toFixed(1)
    : '—';

  const engineOnline = !!marginData;
  const signalsOnline = signalSnapshot?.timescales && Object.values(signalSnapshot.timescales).some(v => v !== null);
  const killSwitch = portfolio.kill_switch;

  // V4 fusion liveness — engine is using v4 actions when both macro and
  // consensus are populated AND the macro signal is reasonably fresh.
  const v4Macro = v4Snapshot?.macro;
  const v4Consensus = v4Snapshot?.consensus;
  const v4Online = !!(v4Macro && v4Consensus);
  const v4Fresh = v4Macro?.age_s != null && v4Macro.age_s < 180;
  const v4Healthy = v4Online && v4Fresh && v4Consensus.safe_to_trade;
  const v4SourceText = v4Macro
    ? `${v4Macro.bias || '?'}/${v4Macro.direction_gate || '?'}`
    : 'No data';

  // Execution-context derived UI helpers — all read with optional chaining
  // so an old engine deploy without the `execution` block falls back cleanly.
  const venue = execution.venue || 'binance';
  const venueLabel = venue === 'hyperliquid' ? 'Hyperliquid' : 'Binance';
  const priceFeed = execution.price_feed || {};
  const priceFeedHealthy = priceFeed.healthy === true;
  const priceFeedSource = (priceFeed.source || venue || 'OK').toUpperCase();
  const rtFeeBps = execution.round_trip_fee_bps;
  const spreadBps = execution.spread_bps;
  const isHyperliquid = venue === 'hyperliquid';

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <h1 style={{ fontSize: 16, fontWeight: 800, color: T.white, margin: 0, display: 'flex', alignItems: 'center', gap: 8 }}>
            {venueLabel} Margin Engine
            <span style={{
              fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
              background: portfolio.paper_mode ? 'rgba(168,85,247,0.15)' : 'rgba(239,68,68,0.15)',
              color: portfolio.paper_mode ? T.purple : T.red,
              border: `1px solid ${portfolio.paper_mode ? 'rgba(168,85,247,0.3)' : 'rgba(239,68,68,0.3)'}`,
            }}>{portfolio.paper_mode ? 'PAPER' : 'LIVE'}</span>
            {/* Venue badge — cyan for Hyperliquid, amber for Binance */}
            {execution.venue && (
              <span
                title={rtFeeBps != null
                  ? `${priceFeed.source || venue} price feed · ${rtFeeBps.toFixed(1)} bps RT fee`
                  : `${venue} venue`}
                style={{
                  fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                  background: isHyperliquid ? 'rgba(6,182,212,0.15)' : 'rgba(245,158,11,0.15)',
                  color: isHyperliquid ? T.cyan : T.amber,
                  border: `1px solid ${isHyperliquid ? 'rgba(6,182,212,0.3)' : 'rgba(245,158,11,0.3)'}`,
                  fontFamily: T.mono, letterSpacing: '0.04em', textTransform: 'uppercase',
                }}
              >{venue}</span>
            )}
            {/* Leverage chip — only for margin-style venues (Binance cross).
                Hyperliquid is perps; "5x CROSS" is meaningless there. */}
            {!isHyperliquid && (
              <span style={{
                fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                background: 'rgba(59,130,246,0.15)', color: T.blue, border: '1px solid rgba(59,130,246,0.3)',
              }}>{portfolio.leverage || 5}x CROSS</span>
            )}
            {signalSnapshot?.model?.model_family && (
              <span
                title={signalSnapshot.model.model_version || ''}
                style={{
                  fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3,
                  background: 'rgba(168,85,247,0.15)', color: T.purple,
                  border: '1px solid rgba(168,85,247,0.3)',
                  fontFamily: T.mono, letterSpacing: '0.04em',
                }}
              >{signalSnapshot.model.model_family}</span>
            )}
          </h1>
          <p style={{ fontSize: 9, color: T.textMuted, margin: '2px 0 0' }}>
            Hyperliquid perpetual futures trading engine. Composite v3 signal &rarr; {venueLabel.toLowerCase()} {isHyperliquid ? 'perps' : 'margin'} | eu-west-2
            <span style={{ color: T.textDim, marginLeft: 6 }}>Data: margin_positions + margin_signals + /v3/snapshot + /v4/snapshot</span>
          </p>
        </div>

        {/* Tabs */}
        <div style={{ display: 'flex', gap: 4 }}>
          {[
            { key: 'live',    label: 'LIVE' },
            { key: 'history', label: 'HISTORY' },
            { key: 'trades',  label: 'TRADE TIMELINE' },
          ].map(({ key, label }) => (
            <button key={key} onClick={() => setActiveTab(key)} style={{
              padding: '5px 12px', borderRadius: 5, fontSize: 10, fontWeight: 700,
              background: activeTab === key ? 'rgba(6,182,212,0.15)' : 'transparent',
              color: activeTab === key ? T.cyan : T.textMuted,
              border: `1px solid ${activeTab === key ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
              cursor: 'pointer', textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>{label}</button>
          ))}
        </div>
      </div>

      {/* Status bar — connection indicators */}
      <div style={{
        display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap',
        padding: '8px 14px', marginBottom: 12, borderRadius: 6,
        background: 'rgba(15,23,42,0.6)', border: `1px solid ${T.cardBorder}`,
      }}>
        <StatusDot active={engineOnline} label="Engine" activeText="Connected" inactiveText="Offline" />
        <StatusDot active={signalsOnline} label="Signal Feed" activeText="Receiving" inactiveText="Waiting" amber={!signalsOnline && engineOnline} />
        <StatusDot
          active={v4Healthy}
          label="V4 Fusion"
          activeText={v4SourceText}
          inactiveText={v4Online ? (v4Fresh ? 'Unsafe' : 'Stale') : 'Waiting'}
          amber={v4Online && !v4Healthy}
        />
        {execution.venue && (
          <StatusDot
            active={priceFeedHealthy}
            label="Price Feed"
            activeText={priceFeedSource}
            inactiveText="Stale"
            amber={!priceFeedHealthy && engineOnline}
          />
        )}
        <StatusDot active={!killSwitch} label="Kill Switch" activeText="OK" inactiveText="TRIGGERED" />
        {portfolio.paper_mode && (
          <span style={{
            marginLeft: 'auto', fontSize: 9, fontWeight: 800, letterSpacing: '0.1em',
            color: T.purple, fontFamily: T.mono,
            padding: '3px 10px', borderRadius: 4,
            background: 'rgba(168,85,247,0.1)', border: '1px solid rgba(168,85,247,0.25)',
            animation: 'pulse 3s infinite',
          }}>PAPER TRADING</span>
        )}
        {portfolio.consecutive_losses > 0 && (
          <span style={{ fontSize: 9, color: T.amber, fontFamily: T.mono }}>
            {portfolio.consecutive_losses} consecutive loss{portfolio.consecutive_losses > 1 ? 'es' : ''}
          </span>
        )}
      </div>

      {/* Control panel */}
      <div style={{
        display: 'flex', gap: 10, marginBottom: 12, flexWrap: 'wrap',
      }}>
        {!killSwitch ? (
          <button
            onClick={handleKillSwitch}
            disabled={controlLoading.kill || !engineOnline}
            style={{
              padding: '8px 16px', borderRadius: 6, fontSize: 10, fontWeight: 700,
              background: 'rgba(239,68,68,0.15)', color: T.red,
              border: `1px solid rgba(239,68,68,0.3)`, fontFamily: T.mono,
              cursor: (!engineOnline || controlLoading.kill) ? 'not-allowed' : 'pointer',
              opacity: (!engineOnline || controlLoading.kill) ? 0.5 : 1,
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}
          >
            {controlLoading.kill ? 'KILLING...' : 'KILL SWITCH'}
          </button>
        ) : (
          <button
            onClick={handleResume}
            disabled={controlLoading.resume || !engineOnline}
            style={{
              padding: '8px 16px', borderRadius: 6, fontSize: 10, fontWeight: 700,
              background: 'rgba(16,185,129,0.15)', color: T.green,
              border: `1px solid rgba(16,185,129,0.3)`, fontFamily: T.mono,
              cursor: (!engineOnline || controlLoading.resume) ? 'not-allowed' : 'pointer',
              opacity: (!engineOnline || controlLoading.resume) ? 0.5 : 1,
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}
          >
            {controlLoading.resume ? 'RESUMING...' : 'RESUME'}
          </button>
        )}
        <button
          onClick={handleTogglePaperMode}
          disabled={controlLoading.paperMode || !engineOnline}
          style={{
            padding: '8px 16px', borderRadius: 6, fontSize: 10, fontWeight: 700,
            background: portfolio.paper_mode ? 'rgba(239,68,68,0.15)' : 'rgba(16,185,129,0.15)',
            color: portfolio.paper_mode ? T.red : T.green,
            border: `1px solid ${portfolio.paper_mode ? 'rgba(239,68,68,0.3)' : 'rgba(16,185,129,0.3)'}`,
            fontFamily: T.mono,
            cursor: (!engineOnline || controlLoading.paperMode) ? 'not-allowed' : 'pointer',
            opacity: (!engineOnline || controlLoading.paperMode) ? 0.5 : 1,
            textTransform: 'uppercase', letterSpacing: '0.05em',
          }}
        >
          {controlLoading.paperMode ? 'TOGGLING...' : (portfolio.paper_mode ? 'GO LIVE' : 'PAPER MODE')}
        </button>
      </div>

      {error && (
        <div style={{ padding: '8px 12px', marginBottom: 12, borderRadius: 6, background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.2)', fontSize: 10, color: T.red }}>
          {error}
        </div>
      )}

      {/* Stats row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 10, marginBottom: 16 }}>
        {[
          { label: 'BALANCE', value: `$${(portfolio.balance || 500).toFixed(2)}`, color: T.white },
          { label: 'EXPOSURE', value: `$${(portfolio.exposure || 0).toFixed(2)}`, sub: `${((portfolio.exposure || 0) / (portfolio.balance || 500) * 100).toFixed(0)}% of capital`, color: T.cyan },
          { label: 'LEVERAGE', value: `${((portfolio.exposure || 0) / (portfolio.balance || 500)).toFixed(1)}x`, sub: `of ${portfolio.leverage || 5}x max`, color: T.blue },
          { label: 'OPEN', value: openPositions.length, color: T.cyan },
          { label: 'TOTAL P&L', value: `${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`, color: totalPnl >= 0 ? T.green : T.red },
          { label: 'WIN RATE', value: typeof winRate === 'string' ? winRate : `${winRate}%`, color: parseFloat(winRate) > 60 ? T.green : T.amber },
          { label: 'DAILY P&L', value: `${(portfolio.daily_pnl || 0) >= 0 ? '+' : ''}$${(portfolio.daily_pnl || 0).toFixed(2)}`, color: (portfolio.daily_pnl || 0) >= 0 ? T.green : T.red },
          { label: 'TRADES', value: closedPositions.length, color: T.textMuted },
          // Venue execution context — only shown when engine reports it
          ...(rtFeeBps != null ? [{
            label: 'FEE RT',
            value: `${rtFeeBps.toFixed(1)}bp`,
            sub: isHyperliquid ? 'hyperliquid taker' : 'binance margin',
            color: isHyperliquid ? T.cyan : T.amber,
          }] : []),
          ...(spreadBps != null ? [{
            label: 'SPREAD',
            value: `${spreadBps.toFixed(1)}bp`,
            sub: priceFeed.source || venue,
            color: T.textMuted,
          }] : []),
        ].map(({ label, value, sub, color }) => (
          <div key={label} style={{ background: T.card, border: `1px solid ${T.cardBorder}`, borderRadius: 8, padding: '10px 12px' }}>
            <div style={{ fontSize: 8, color: T.textMuted, fontWeight: 700, letterSpacing: '0.08em', marginBottom: 4 }}>{label}</div>
            <div style={{ fontSize: 18, fontWeight: 900, fontFamily: T.mono, color }}>{value}</div>
            {sub && <div style={{ fontSize: 8, color: T.textDim, marginTop: 2 }}>{sub}</div>}
          </div>
        ))}
      </div>

      {activeTab === 'live' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* V4 fusion decision surface — this is what the engine consumes */}
          <V4Panel snapshot={v4Snapshot} />

          {/* Composite signals (raw v3 sub-signals) */}
          <SignalPanel snapshot={signalSnapshot} />

          {/* Open positions */}
          <PositionsPanel positions={openPositions} />

          {/* Help section */}
          <div style={{ marginTop: 8, padding: 10, background: 'rgba(6,182,212,0.05)', border: `1px solid ${T.cardBorder}`, borderRadius: 6 }}>
            <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4 }}>Need help understanding this dashboard?</div>
            <div style={{ display: 'flex', gap: 12 }}>
              <a
                href="https://github.com/billybrichards/novakash/wiki/V4-Fusion-Surface"
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 9, color: T.cyan, textDecoration: 'none' }}
              >
                V4 Surface Guide →
              </a>
              <a
                href="https://github.com/billybrichards/novakash/wiki/Position-Management"
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 9, color: T.cyan, textDecoration: 'none' }}
              >
                Position Guide →
              </a>
              <a
                href="https://github.com/billybrichards/novakash/wiki/Trading-Strategy"
                target="_blank"
                rel="noopener noreferrer"
                style={{ fontSize: 9, color: T.cyan, textDecoration: 'none' }}
              >
                Strategy Docs →
              </a>
            </div>
          </div>
        </div>
      )}
      {activeTab === 'history' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {/* Closed positions from current session (memory) */}
          <PositionsPanel positions={closedPositions} />
          <div style={{ marginTop: 8 }}>
            <a
              href="https://github.com/billybrichards/novakash/wiki/Trade-History"
              target="_blank"
              rel="noopener noreferrer"
              style={{ fontSize: 9, color: T.cyan, textDecoration: 'none' }}
            >
              History Guide →
            </a>
          </div>
        </div>
      )}
      {activeTab === 'trades' && (
        <>
          {/* Full DB history with per-trade entry/exit conditions */}
          <TradeTimelinePanel api={api} />
        </>
      )}
    </div>
  );
}
