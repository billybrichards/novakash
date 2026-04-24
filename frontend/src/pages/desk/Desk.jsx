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
//   30s  — /api/v58/strategy-decisions-resolved (last 20 table)
//   1s   — client countdown tick
//   On demand: POST /api/desk/picks
//
// No Polymarket direct calls — YES/NO mid is read from /v4/snapshot when
// present, else hidden per spec "hide gracefully". CLOB ladder = Phase 2.

import React, { useEffect, useMemo, useState } from 'react';
import PageHeader from '../../components/shared/PageHeader.jsx';
import { T } from '../../theme/tokens.js';
import { useApi } from '../../hooks/useApi.js';

import Header from './components/Header.jsx';
import PriceChart from './components/PriceChart.jsx';
import CrossAssetSparks from './components/CrossAssetSparks.jsx';
import SignalStack from './components/SignalStack.jsx';
import StrategyDecisions from './components/StrategyDecisions.jsx';
import YourPlay from './components/YourPlay.jsx';
import LastWindowsTable from './components/LastWindowsTable.jsx';

import { useWindow } from './hooks/useWindow.js';
import { useSnapshot, pickProbs } from './hooks/useSnapshot.js';
import { usePicks } from './hooks/usePicks.js';
import { useResolvedDecisions } from './hooks/useResolvedDecisions.js';

const TRACKED_STRATEGIES = ['v6_sniper', 'v4_fusion', 'v5_ensemble', 'v5_fresh'];

export default function Desk() {
  const api = useApi();

  // Window clock (server + 1s client tick).
  const win = useWindow('BTC');

  // /v4/snapshot — 2s (header fields + signal stack).
  const snap = useSnapshot('BTC', 2_000);
  const { pu, pc, pl } = pickProbs(snap.fiveMin);

  // System status — engine mode for the header chip.
  const [systemStatus, setSystemStatus] = useState(null);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get('/api/system/status');
        const body = r?.data ?? r;
        if (cancelled) return;
        // Normalise — some deploys return { mode: 'paper' }, others return
        // { paper_enabled: true } etc.
        setSystemStatus(normaliseMode(body));
      } catch {
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

  // Regime label — prefer 5m snapshot; fall back to top-level.
  const regime = snap.fiveMin?.regime
              ?? snap.fiveMin?.regime_label
              ?? snap.data?.regime
              ?? null;
  const vol = num(snap.fiveMin?.volatility ?? snap.fiveMin?.vol);

  return (
    <div>
      <PageHeader
        tag="DESK · /desk"
        title="Live Play-Along Desk"
        subtitle="Operator HUD for 5m BTC Polymarket windows. Make a call, then compare against v6_sniper / fusion / ensemble. Phase 1 — CLOB ladder + TimesFM fan arrive later."
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

      <div style={gridStyle}>
        <div>
          <ErrBoundary label="Price chart">
            <PriceChart asset="BTC" windowEpoch={win.windowEpoch} pollMs={4_000} />
          </ErrBoundary>
          <ErrBoundary label="Cross-asset sparks">
            <CrossAssetSparks pollMs={10_000} />
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

function normaliseMode(body) {
  if (!body) return '?';
  if (typeof body.mode === 'string') {
    return body.mode.toUpperCase();
  }
  if (body.killed === true) return 'KILLED';
  if (body.live_enabled === true) return 'LIVE';
  if (body.paper_enabled === true) return 'PAPER';
  if (typeof body.status === 'string') return body.status.toUpperCase();
  return '?';
}

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
