import React, { useState, useEffect } from 'react';
import { useApi } from '../hooks/useApi.js';

/**
 * 15-Minute Strategy Monitor
 * 
 * Shows:
 * 1. YAML config viewer (gates, sizing, mode)
 * 2. Window comparison table (all strategies side-by-side)
 * 3. P&L boxes (hypothetical if GHOST)
 */

const STRATEGIES = [
  'v15m_down_only',
  'v15m_up_asian',
  'v15m_up_basic',
  'v15m_fusion',
  'v15m_gate',
];

export default function FifteenMinMonitor() {
  const api = useApi();
  const [selectedConfig, setSelectedConfig] = useState('v15m_down_only');
  const [configData, setConfigData] = useState(null);
  const [windows, setWindows] = useState([]);
  const [loading, setLoading] = useState(true);

  // Load YAML config viewer data
  useEffect(() => {
    const loadConfig = async () => {
      try {
        const res = await api('GET', `/strategy-configs/${selectedConfig}`);
        setConfigData(res?.data || null);
      } catch (err) {
        console.error('Failed to load config:', err);
        setConfigData(null);
      }
    };
    loadConfig();
  }, [api, selectedConfig]);

  // Load window comparison data
  useEffect(() => {
    const loadWindows = async () => {
      try {
        const res = await api('GET', '/strategy-decisions/15m?limit=20');
        setWindows(res?.data?.windows || []);
      } catch (err) {
        console.error('Failed to load windows:', err);
        setWindows([]);
      } finally {
        setLoading(false);
      }
    };
    loadWindows();
    const interval = setInterval(loadWindows, 10000);
    return () => clearInterval(interval);
  }, [api]);

  // Calculate P&L per strategy
  const strategyPnL = STRATEGIES.reduce((acc, strat) => {
    const decisions = windows.flatMap(w => w.decisions.filter(d => d.strategy_id === strat));
    const trades = decisions.filter(d => d.action === 'TRADE');
    const wins = trades.filter(d => d.hypothetical_outcome === 'WIN').length;
    const losses = trades.filter(d => d.hypothetical_outcome === 'LOSS').length;
    const winRate = trades.length > 0 ? (wins / trades.length) : 0;
    const hypotheticalPnL = wins * 10 - losses * 10; // Simplified: $10 per trade
    acc[strat] = { trades: trades.length, wins, losses, winRate, hypotheticalPnL };
    return acc;
  }, {});

  return (
    <div style={{ padding: 20, maxWidth: 1800, margin: '0 auto' }}>
      <h1 style={{ fontSize: 24, fontWeight: 700, marginBottom: 20, color: '#06b6d4' }}>
        15-Minute Strategy Monitor
      </h1>

      {/* Section 1: YAML Config Viewer */}
      <section style={{ marginBottom: 30 }}>
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 15, color: '#a855f7' }}>
          1. Strategy Configs
        </h2>
        <div style={{ display: 'flex', gap: 10, marginBottom: 15 }}>
          {STRATEGIES.map(strat => (
            <button
              key={strat}
              onClick={() => setSelectedConfig(strat)}
              style={{
                padding: '8px 16px',
                background: selectedConfig === strat ? '#06b6d4' : '#1e293b',
                color: '#fff',
                border: 'none',
                borderRadius: 6,
                cursor: 'pointer',
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              {strat.replace('v15m_', '')}
            </button>
          ))}
        </div>
        <div style={{
          background: '#0f172a',
          border: '1px solid #334155',
          borderRadius: 8,
          padding: 20,
        }}>
          {configData ? (
            <div>
              <div style={{ marginBottom: 15 }}>
                <span style={{ color: '#94a3b8', fontSize: 12 }}>MODE:</span>{' '}
                <span style={{
                  color: configData.mode === 'GHOST' ? '#fbbf24' : '#10b981',
                  fontWeight: 600,
                  fontSize: 14,
                }}>
                  {configData.mode}
                </span>
              </div>
              <div style={{ marginBottom: 15 }}>
                <span style={{ color: '#94a3b8', fontSize: 12 }}>TIMESCALE:</span>{' '}
                <span style={{ color: '#06b6d4', fontWeight: 600, fontSize: 14 }}>
                  {configData.timescale}
                </span>
              </div>
              <div style={{ marginBottom: 15 }}>
                <span style={{ color: '#94a3b8', fontSize: 12 }}>GATES:</span>
                <div style={{ marginTop: 8 }}>
                  {configData.gates && configData.gates.length > 0 ? (
                    configData.gates.map((gate, idx) => (
                      <div
                        key={idx}
                        style={{
                          background: '#1e293b',
                          padding: '8px 12px',
                          borderRadius: 6,
                          marginBottom: 6,
                          fontSize: 12,
                          fontFamily: 'IBM Plex Mono, monospace',
                        }}
                      >
                        <span style={{ color: '#a855f7', fontWeight: 600 }}>{gate.type}</span>
                        {gate.params && Object.keys(gate.params).length > 0 && (
                          <span style={{ color: '#94a3b8', marginLeft: 10 }}>
                            {JSON.stringify(gate.params)}
                          </span>
                        )}
                      </div>
                    ))
                  ) : (
                    <span style={{ color: '#64748b', fontSize: 12 }}>No gates (pre-gate hook only)</span>
                  )}
                </div>
              </div>
              <div>
                <span style={{ color: '#94a3b8', fontSize: 12 }}>SIZING:</span>
                <div style={{
                  marginTop: 8,
                  background: '#1e293b',
                  padding: '8px 12px',
                  borderRadius: 6,
                  fontSize: 12,
                  fontFamily: 'IBM Plex Mono, monospace',
                  color: '#e2e8f0',
                }}>
                  type: {configData.sizing?.type || 'N/A'}<br />
                  fraction: {configData.sizing?.fraction || 'N/A'}<br />
                  max_collateral_pct: {configData.sizing?.max_collateral_pct || 'N/A'}
                </div>
              </div>
            </div>
          ) : (
            <div style={{ color: '#64748b', fontSize: 14 }}>Loading config...</div>
          )}
        </div>
      </section>

      {/* Section 2: Window Comparison Table */}
      <section style={{ marginBottom: 30 }}>
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 15, color: '#a855f7' }}>
          2. Window Comparison
        </h2>
        {loading ? (
          <div style={{ color: '#64748b', fontSize: 14 }}>Loading windows...</div>
        ) : windows.length === 0 ? (
          <div style={{ color: '#64748b', fontSize: 14 }}>
            No 15m windows yet. Deploy with FIFTEEN_MIN_ENABLED=true to start collecting data.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{
              width: '100%',
              borderCollapse: 'collapse',
              fontSize: 12,
              fontFamily: 'IBM Plex Mono, monospace',
            }}>
              <thead>
                <tr style={{ background: '#1e293b', color: '#94a3b8' }}>
                  <th style={{ padding: 10, textAlign: 'left', borderBottom: '1px solid #334155' }}>
                    Window
                  </th>
                  {STRATEGIES.map(strat => (
                    <th key={strat} style={{ padding: 10, textAlign: 'center', borderBottom: '1px solid #334155' }}>
                      {strat.replace('v15m_', '')}
                    </th>
                  ))}
                  <th style={{ padding: 10, textAlign: 'center', borderBottom: '1px solid #334155' }}>
                    Outcome
                  </th>
                </tr>
              </thead>
              <tbody>
                {windows.map((win, idx) => (
                  <tr
                    key={idx}
                    style={{
                      background: idx % 2 === 0 ? '#0f172a' : '#1e293b',
                      borderBottom: '1px solid #334155',
                    }}
                  >
                    <td style={{ padding: 10, color: '#e2e8f0' }}>
                      {new Date(win.window_ts * 1000).toLocaleTimeString()}
                    </td>
                    {STRATEGIES.map(strat => {
                      const decision = win.decisions.find(d => d.strategy_id === strat);
                      if (!decision) {
                        return <td key={strat} style={{ padding: 10, textAlign: 'center', color: '#64748b' }}>—</td>;
                      }
                      const isSkip = decision.action === 'SKIP';
                      const color = isSkip ? '#64748b' : (decision.direction === 'UP' ? '#10b981' : '#ef4444');
                      return (
                        <td key={strat} style={{ padding: 10, textAlign: 'center' }}>
                          <div style={{ color, fontWeight: 600 }}>
                            {isSkip ? 'SKIP' : decision.direction}
                          </div>
                          {decision.mode && (
                            <div style={{
                              fontSize: 9,
                              color: decision.mode === 'GHOST' ? '#fbbf24' : '#10b981',
                              marginTop: 2,
                            }}>
                              {decision.mode}
                            </div>
                          )}
                          {isSkip && decision.skip_reason && (
                            <div style={{ fontSize: 9, color: '#64748b', marginTop: 2 }}>
                              {decision.skip_reason.slice(0, 20)}...
                            </div>
                          )}
                        </td>
                      );
                    })}
                    <td style={{ padding: 10, textAlign: 'center' }}>
                      {win.outcome ? (
                        <span style={{
                          color: win.outcome === 'UP' ? '#10b981' : '#ef4444',
                          fontWeight: 600,
                        }}>
                          {win.outcome}
                        </span>
                      ) : (
                        <span style={{ color: '#64748b' }}>pending</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Section 3: P&L Boxes */}
      <section>
        <h2 style={{ fontSize: 18, fontWeight: 600, marginBottom: 15, color: '#a855f7' }}>
          3. Strategy P&L (Hypothetical)
        </h2>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))', gap: 15 }}>
          {STRATEGIES.map(strat => {
            const pnl = strategyPnL[strat];
            return (
              <div
                key={strat}
                style={{
                  background: '#0f172a',
                  border: '1px solid #334155',
                  borderRadius: 8,
                  padding: 15,
                }}
              >
                <div style={{ fontSize: 14, fontWeight: 600, color: '#06b6d4', marginBottom: 10 }}>
                  {strat.replace('v15m_', '')}
                </div>
                <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 5 }}>
                  Trades: <span style={{ color: '#e2e8f0', fontWeight: 600 }}>{pnl.trades}</span>
                </div>
                <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 5 }}>
                  Wins: <span style={{ color: '#10b981', fontWeight: 600 }}>{pnl.wins}</span> | 
                  Losses: <span style={{ color: '#ef4444', fontWeight: 600 }}>{pnl.losses}</span>
                </div>
                <div style={{ fontSize: 12, color: '#94a3b8', marginBottom: 5 }}>
                  Win Rate: <span style={{ color: '#e2e8f0', fontWeight: 600 }}>
                    {pnl.trades > 0 ? `${(pnl.winRate * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
                <div style={{ fontSize: 14, fontWeight: 700, marginTop: 8 }}>
                  P&L: <span style={{
                    color: pnl.hypotheticalPnL >= 0 ? '#10b981' : '#ef4444',
                  }}>
                    {pnl.hypotheticalPnL >= 0 ? '+' : ''}{pnl.hypotheticalPnL.toFixed(2)} USDC
                  </span>
                </div>
                <div style={{ fontSize: 10, color: '#64748b', marginTop: 5 }}>
                  (GHOST mode – not executed)
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
