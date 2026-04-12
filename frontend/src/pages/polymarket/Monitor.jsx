import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from './components/theme.js';
import StatusBar from './components/StatusBar.jsx';
import DataHealthStrip from './components/DataHealthStrip.jsx';
import SignalSurface from './components/SignalSurface.jsx';
import GatePipelineBand from './components/GatePipeline.jsx';
import RecentFlow from './components/RecentFlow.jsx';

/**
 * Polymarket Monitor — "Should I Trade?"
 *
 * The primary trading dashboard. Replaces Execution HQ, Factory Floor,
 * and the V1-V4 surface pages with a single unified view.
 *
 * 5 horizontal bands:
 *   1. Status Bar — mode, bankroll, W/L, window countdown, feed health
 *   2. Data Health Strip — signal source health indicators
 *   3. Signal Surface Panel — direction, market context, V4 action (3 cols)
 *   4. Gate Pipeline + Manual Trade — 8-gate strip + trade button
 *   5. Recent Flow — last 20 windows timeline
 *
 * Polls 5 endpoints every 10s using the useApi hook.
 */

// Inject keyframes once
if (typeof document !== 'undefined' && !document.getElementById('pm-monitor-styles')) {
  const style = document.createElement('style');
  style.id = 'pm-monitor-styles';
  style.textContent = `
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.4; }
    }
  `;
  document.head.appendChild(style);
}

export default function Monitor() {
  const api = useApi();

  // --- Data state ---
  const [hqData, setHqData] = useState(null);
  const [dashStats, setDashStats] = useState(null);
  const [v4Snapshot, setV4Snapshot] = useState(null);
  const [v3Snapshot, setV3Snapshot] = useState(null);
  const [accuracy, setAccuracy] = useState(null);
  const [tradeStats, setTradeStats] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // --- Browser tab title ---
  useEffect(() => {
    const prev = document.title;
    document.title = 'Monitor \u2014 Polymarket \u2014 Novakash';
    return () => { document.title = prev; };
  }, []);

  // --- Fetch all endpoints in parallel ---
  const fetchData = useCallback(async () => {
    try {
      const results = await Promise.allSettled([
        api('GET', '/v58/execution-hq?limit=200&asset=btc&timeframe=5m'),
        api('GET', '/dashboard/stats'),
        api('GET', '/v4/snapshot?asset=btc'),
        api('GET', '/v3/snapshot?asset=btc'),
        api('GET', '/v58/accuracy?limit=20'),
        api('GET', '/v58/stats?days=7'),
        api('GET', '/v58/outcomes?limit=20'),
      ]);

      const [hqRes, statsRes, v4Res, v3Res, accRes, tsRes, outRes] = results;

      if (hqRes.status === 'fulfilled') {
        setHqData(hqRes.value?.data || hqRes.value);
      }
      if (statsRes.status === 'fulfilled') {
        setDashStats(statsRes.value?.data || statsRes.value);
      }
      if (v4Res.status === 'fulfilled') {
        setV4Snapshot(v4Res.value?.data || v4Res.value);
      }
      if (v3Res.status === 'fulfilled') {
        setV3Snapshot(v3Res.value?.data || v3Res.value);
      }
      if (accRes.status === 'fulfilled') {
        setAccuracy(accRes.value?.data || accRes.value);
      }
      if (tsRes.status === 'fulfilled') {
        setTradeStats(tsRes.value?.data || tsRes.value);
      }
      if (outRes.status === 'fulfilled') {
        const outData = outRes.value?.data || outRes.value;
        setOutcomes(outData?.outcomes ?? (Array.isArray(outData) ? outData : []));
      }

      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  }, [api]);

  // Initial fetch
  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Poll every 10s
  useEffect(() => {
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [fetchData]);

  return (
    <div style={{
      minHeight: '100vh', height: '100vh',
      background: T.bg, color: T.text,
      padding: 8, fontFamily: T.mono,
      display: 'flex', flexDirection: 'column',
      overflow: 'hidden', userSelect: 'none',
    }}>
      {/* Error banner */}
      {error && (
        <div style={{
          background: 'rgba(239,68,68,0.1)', border: '1px solid rgba(239,68,68,0.3)',
          padding: '5px 10px', borderRadius: 3, marginBottom: 6, fontSize: 10,
          fontFamily: T.mono, color: '#fca5a5', flexShrink: 0,
        }}>
          API Error: {error}
        </div>
      )}

      {/* Loading */}
      {loading && !hqData && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: T.mono, color: T.textMuted, fontSize: 13,
        }}>
          Loading Polymarket Monitor...
        </div>
      )}

      {(!loading || hqData) && (
        <>
          {/* Band 1 — Status Bar */}
          <StatusBar
            hqData={hqData}
            dashStats={dashStats}
            accuracy={accuracy}
            tradeStats={tradeStats}
          />

          {/* Band 2 — Data Health Strip */}
          <DataHealthStrip
            hqData={hqData}
            v4Snapshot={v4Snapshot}
            v3Snapshot={v3Snapshot}
          />

          {/* Band 3 — Signal Surface Panel */}
          <SignalSurface
            hqData={hqData}
            v4Snapshot={v4Snapshot}
            v3Snapshot={v3Snapshot}
          />

          {/* Band 4 — Gate Pipeline + Manual Trade */}
          <GatePipelineBand hqData={hqData} />

          {/* Band 5 — Recent Flow */}
          <RecentFlow outcomes={outcomes} />
        </>
      )}
    </div>
  );
}
