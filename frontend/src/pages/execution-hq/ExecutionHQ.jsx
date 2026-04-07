import React, { useState, useEffect, useRef } from 'react';
import { Radio, History } from 'lucide-react';
import { useApi } from '../../hooks/useApi.js';
import LiveTab from './components/LiveTab.jsx';
import RetroTab from './components/RetroTab.jsx';
import { T } from './components/constants.js';

/**
 * ExecutionHQ — Primary execution monitoring dashboard.
 *
 * Tabs:
 *   - Live Execution: Real-time window countdown, gate audit, price charts, feed health
 *   - Retrospective: Full window history with shadow resolution and missed opportunity analysis
 *
 * Data sources:
 *   - /api/v58/execution-hq — Combined endpoint (windows, shadow stats, trades, system state)
 *   - /api/dashboard/stats  — Bankroll and engine status
 */
export default function ExecutionHQ() {
  const api = useApi();
  const [activeTab, setActiveTab] = useState('live');
  const [tick, setTick] = useState(0);

  // API data
  const [hqData, setHqData] = useState(null);
  const [dashStats, setDashStats] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Tick counter for animations
  useEffect(() => {
    const timer = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  // Fetch execution HQ data
  const fetchData = async () => {
    try {
      const [hqRes, statsRes] = await Promise.allSettled([
        api('GET', '/v58/execution-hq?limit=200'),
        api('GET', '/dashboard/stats'),
      ]);

      if (hqRes.status === 'fulfilled') {
        setHqData(hqRes.value?.data || hqRes.value);
      }
      if (statsRes.status === 'fulfilled') {
        setDashStats(statsRes.value?.data || statsRes.value);
      }
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch data');
    } finally {
      setLoading(false);
    }
  };

  // Initial fetch
  useEffect(() => { fetchData(); }, [api]);

  // Poll every 10s on live tab
  useEffect(() => {
    if (activeTab !== 'live') return;
    const interval = setInterval(fetchData, 10000);
    return () => clearInterval(interval);
  }, [activeTab, api]);

  // Re-fetch on tab switch to retro
  const prevTab = useRef(activeTab);
  useEffect(() => {
    if (activeTab === 'retro' && prevTab.current === 'live') {
      fetchData();
    }
    prevTab.current = activeTab;
  }, [activeTab]);

  const bankroll = dashStats?.balance ?? hqData?.system?.bankroll ?? 0;
  const windows = hqData?.windows || [];
  const shadowStats = hqData?.shadow_stats || {};

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

      {/* Loading state */}
      {loading && !hqData && (
        <div style={{
          flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontFamily: 'monospace', color: T.textMuted, fontSize: 14,
        }}>
          Loading execution data...
        </div>
      )}

      {/* Tab content */}
      {(!loading || hqData) && activeTab === 'live' && (
        <LiveTab hqData={hqData} tick={tick} />
      )}
      {(!loading || hqData) && activeTab === 'retro' && (
        <RetroTab windows={windows} shadowStats={shadowStats} />
      )}
    </div>
  );
}
