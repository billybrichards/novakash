// /desk — Phase 1 play-along operator HUD.
//
// Spec: hub note #218.
// Classifier context: hub note #226 (pu/pc/pl + VHC threshold).
//
// Layout:
//   Header (sticky)
//   ┌────────────────────────┬──────────────────────┐
//   │ PriceChart (60%)       │ SignalStack (40%)    │
//   │ CrossAssetSparks       │ StrategyDecisions    │
//   │                        │ YourPlay             │
//   └────────────────────────┴──────────────────────┘
//   LastWindowsTable (full width)
//
// Polling cadence (note #218 §2):
//   2s   — /api/v4/snapshot header fields (price + probs)
//   4s   — /api/v58/strategy-decisions (current window)
//   10s  — /api/windows/current (server authority)
//   10s  — /api/ticks/chainlink ETH/SOL/XRP sparks
//   30s  — /api/v58/strategy-decisions?resolved=true (last 20 table)
//   1s   — client countdown tick
//   On demand: POST /api/desk/picks
//
// No Polymarket direct calls — YES/NO mid is read from /v4/snapshot when
// present, else hidden per spec "hide gracefully". CLOB ladder = Phase 2.

import React, { useEffect, useMemo, useState } from 'react';
import PageHeader from '../../components/shared/PageHeader.jsx';
import { T } from '../../theme/tokens.js';
import { useApi } from '../../hooks/useApi.js';
import { DESK_TRACKED_STRATEGY_IDS } from '../../constants/strategies.js';

import Header from './components/Header.jsx';
import PriceChart from './components/PriceChart.jsx';
import CrossAssetSparks from './components/CrossAssetSparks.jsx';
import SignalStack from './components/SignalStack.jsx';
import StrategyDecisions from './components/StrategyDecisions.jsx';
import YourPlay from './components/YourPlay.jsx';
import LastWindowsTable from './components/LastWindowsTable.jsx';

// Phase 2 components
import ClobLadder from './components/ClobLadder.jsx';
import QuantileFan from './components/QuantileFan.jsx';
import VpinChart from './components/VpinChart.jsx';
import DivergenceBanner from './components/DivergenceBanner.jsx';
import RegimeRibbon from './components/RegimeRibbon.jsx';
import LiquidationFlow from './components/LiquidationFlow.jsx';
import { useClobBook } from './hooks/useClobBook.js';

// Phase 3 components (audit #291: surface untapped window_snapshots fields)
import ConvictionStrip from './components/ConvictionStrip.jsx';
import SubSignalsPanel from './components/SubSignalsPanel.jsx';
import CGPositioningTile from './components/CGPositioningTile.jsx';
import ConsensusBadge from './components/ConsensusBadge.jsx';
import AlertBanners from './components/AlertBanners.jsx';

import { useWindow } from './hooks/useWindow.js';
import { useSnapshot, pickProbs } from './hooks/useSnapshot.js';
import { usePicks } from './hooks/usePicks.js';
import { useResolvedDecisions } from './hooks/useResolvedDecisions.js';

const TRACKED_STRATEGIES = DESK_TRACKED_STRATEGY_IDS;

export default function Desk() {
  const api = useApi();

  // Window clock (server + 1s client tick).
  const win = useWindow('BTC');

  // /v4/snapshot — 2s (header fields + signal stack).
  const snap = useSnapshot('BTC', 2_000);
  const { pu, pc, pl } = pickProbs(snap.fiveMin);

  // System status — engine mode for the header chip.
  // Hub now exposes a derived `mode` field (LIVE/PAPER/KILLED) at the top
  // level of /api/system/status; fall back to data.status for older hubs.
  const [systemStatus, setSystemStatus] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get('/api/system/status');
        const body = r?.data ?? r;
        if (cancelled) return;
        const mode = body?.mode;
        const inner = body?.data?.status;
        const pick = (typeof mode === 'string' && mode) ? mode
          : (typeof inner === 'string' && inner) ? inner
          : null;
        setSystemStatus(pick ? pick.toUpperCase() : '?');
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn('[desk] system status fetch failed:', e?.message || e);
        if (!cancelled) setSystemStatus('?');
      }
    };
    load();
    const h = setInterval(load, 10_000);
    return () => { cancelled = true; clearInterval(h); };
  }, [api]);

  // Picks.
  const picks = usePicks({ limit: 50, pollMs: 30_000 });

  // Resolved decisions per tracked strategy.
  const resolved = useResolvedDecisions({
    strategyIds: TRACKED_STRATEGIES,
    timeframe: '5m',
    limit: 30,
    pollMs: 30_000,
  });

  // Header BTC price + 2s delta come from the /v4/snapshot payload when
  // present, else from the most recent Chainlink tick in picks (cheap).
  const [btcPrice, setBtcPrice] = useState(null);
  const [prevBtcPrice, setPrevBtcPrice] = useState(null);
  useEffect(() => {
    const p = pickPrice(snap.fiveMin) ?? pickPrice(snap.data);
    if (p != null) {
      setPrevBtcPrice(prev => (prev == null ? p : (btcPrice ?? prev)));
      setBtcPrice(p);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap.fiveMin, snap.data]);
  const priceDelta2s = btcPrice != null && prevBtcPrice != null
    ? btcPrice - prevBtcPrice : null;

  // Window Δ% vs target (same math as the chart uses).
  const windowDelta = useMemo(() => {
    if (btcPrice == null || win.targetPriceChainlink == null) return null;
    return (btcPrice - win.targetPriceChainlink) / win.targetPriceChainlink;
  }, [btcPrice, win.targetPriceChainlink]);

  // Latest pick (for showing "logged: you → UP @ ..." on YourPlay).
  const latestPick = picks.rows[0] || null;

  // YES / NO mid — pull from /v4/snapshot if it exposes them; else null.
  const yesMid = num(snap.fiveMin?.yes_mid ?? snap.fiveMin?.poly_yes_mid);
  const noMid  = num(snap.fiveMin?.no_mid  ?? snap.fiveMin?.poly_no_mid);

  // Regime label — field is `regime` per /v4/snapshot (confirmed). Prefer
  // 5m block, fall back to top-level.
  const regime = snap.fiveMin?.regime ?? snap.data?.regime ?? null;
  const vol = num(snap.fiveMin?.volatility ?? snap.fiveMin?.vol);

  // Phase 2: CLOB book — fetched once here, passed to both the ladder
  // and the divergence banner to avoid duplicate requests.
  const clob = useClobBook({ windowEpoch: win.windowEpoch, asset: 'BTC', pollMs: 10_000 });
  const impliedPUp = clob.book?.implied_p_up ?? null;

  return (
    <div>
      <PageHeader
        tag="DESK · /desk"
        title="Live Play-Along Desk"
        subtitle="Operator HUD for 5m BTC Polymarket windows. Make a call, then compare against v9_ensemble (LIVE) / v8_champion_lgb_only (GHOST). Phase 3 — conviction + sub-signals + CG positioning + consensus + exit-window indicator."
      />

      <Header
        price={btcPrice}
        priceDelta2s={priceDelta2s}
        secondsRemaining={win.secondsRemaining}
        targetPriceChainlink={win.targetPriceChainlink}
        yesMid={yesMid}
        noMid={noMid}
        systemStatus={systemStatus}
        regime={regime}
        vol={vol}
      />

      {/* Phase 2: sticky divergence banner — fires on server-flagged
          ensemble disagreement OR implied-vs-model > 10pt. */}
      <ErrBoundary label="Divergence banner">
        <DivergenceBanner
          fiveMin={snap.fiveMin}
          pu={pu}
          pc={pc}
          pl={pl}
          impliedPUp={impliedPUp}
        />
      </ErrBoundary>

      {/* Phase 3: VHC bypass + M2M stop-loss alert banners. */}
      <ErrBoundary label="Alert banners">
        <AlertBanners fiveMin={snap.fiveMin} pc={pc} pl={pl} />
      </ErrBoundary>

      {/* Phase 3: cross-source consensus chip strip. */}
      <ErrBoundary label="Consensus badge">
        <ConsensusBadge snapshot={snap.data} />
      </ErrBoundary>

      <div style={gridStyle}>
        <div>
          <ErrBoundary label="Price chart">
            <PriceChart asset="BTC" windowEpoch={win.windowEpoch} pollMs={4_000} />
          </ErrBoundary>
          <ErrBoundary label="Quantile fan">
            <QuantileFan
              fiveMin={snap.fiveMin}
              targetPrice={win.targetPriceChainlink}
            />
          </ErrBoundary>
          <ErrBoundary label="Cross-asset sparks">
            <CrossAssetSparks pollMs={10_000} />
          </ErrBoundary>
          <ErrBoundary label="CLOB ladder">
            <ClobLadder
              book={clob.book}
              unavailable={clob.unavailable}
              error={clob.error}
              modelPUp={pu}
            />
          </ErrBoundary>
        </div>
        <div>
          <ErrBoundary label="Signal stack">
            <SignalStack
              fiveMin={snap.fiveMin}
              pu={pu}
              pc={pc}
              pl={pl}
              windowDelta={windowDelta}
            />
          </ErrBoundary>
          <ErrBoundary label="Conviction strip">
            <ConvictionStrip fiveMin={snap.fiveMin} />
          </ErrBoundary>
          <ErrBoundary label="Sub-signals">
            <SubSignalsPanel fiveMin={snap.fiveMin} />
          </ErrBoundary>
          <ErrBoundary label="CG positioning">
            <CGPositioningTile snapshot={snap.data} fiveMin={snap.fiveMin} />
          </ErrBoundary>
          <ErrBoundary label="Regime ribbon">
            <RegimeRibbon fiveMin={snap.fiveMin} />
          </ErrBoundary>
          <ErrBoundary label="VPIN chart">
            <VpinChart snapshot={snap.data} />
          </ErrBoundary>
          <ErrBoundary label="Liquidation flow">
            <LiquidationFlow snapshot={snap.data} />
          </ErrBoundary>
          <ErrBoundary label="Strategy decisions">
            <StrategyDecisions windowEpoch={win.windowEpoch} pollMs={4_000} />
          </ErrBoundary>
          <ErrBoundary label="Your play">
            <YourPlay
              windowEpoch={win.windowEpoch}
              secondsRemaining={win.secondsRemaining}
              submitPick={picks.submitPick}
              latestPickFromServer={latestPick}
            />
          </ErrBoundary>
        </div>
      </div>

      <ErrBoundary label="Last-20 windows">
        <LastWindowsTable
          picks={picks.rows}
          resolvedByStrategy={resolved.byStrategy}
        />
      </ErrBoundary>
    </div>
  );
}

const gridStyle = {
  display: 'grid',
  gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 2fr)',
  gap: 12,
};

function pickPrice(block) {
  if (!block) return null;
  const candidates = [
    block.btc_price, block.price, block.last_price,
    block.chainlink_price, block.spot,
  ];
  for (const c of candidates) {
    const n = Number(c);
    if (Number.isFinite(n) && n > 0) return n;
  }
  return null;
}

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Small error boundary so one failing child doesn't blank the page.
class ErrBoundary extends React.Component {
  constructor(props) { super(props); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) {
    // eslint-disable-next-line no-console
    console.warn(`[desk:${this.props.label}]`, err?.message || err, info?.componentStack);
  }
  render() {
    if (this.state.err) {
      return (
        <div style={{
          border: `1px solid ${T.loss}`, color: T.loss, padding: 10,
          background: T.card, fontFamily: T.font, fontSize: 11, marginBottom: 12,
        }}>
          {this.props.label}: {String(this.state.err?.message || this.state.err)}
        </div>
      );
    }
    return this.props.children;
  }
}
