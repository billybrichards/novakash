/**
 * LiveTrading.jsx — Real-time Polymarket wallet status, live vs paper trades,
 * pending redemptions, and position tracking.
 * 
 * Refreshes every 10s to stay in sync with engine heartbeat.
 * Shows clear LIVE 🔴 vs PAPER 📄 indicators on every trade.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

const T = {
  bg: '#0a0a0f', card: '#12121a', border: 'rgba(255,255,255,0.06)',
  label: '#666', label2: '#888', mono: "'JetBrains Mono', 'Fira Code', monospace",
  profit: '#22c55e', loss: '#ef4444', purple: '#a855f7', cyan: '#06b6d4',
  warning: '#eab308', live: '#ef4444', paper: '#a855f7',
};

function StatCard({ label, value, sub, color = '#fff', badge }) {
  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: '14px 16px', position: 'relative',
    }}>
      {badge && (
        <span style={{
          position: 'absolute', top: 8, right: 8, fontSize: 7, fontWeight: 800,
          padding: '2px 6px', borderRadius: 4, letterSpacing: '0.08em',
          background: badge === 'LIVE' ? 'rgba(239,68,68,0.15)' : 'rgba(168,85,247,0.15)',
          color: badge === 'LIVE' ? T.live : T.paper,
          border: `1px solid ${badge === 'LIVE' ? 'rgba(239,68,68,0.3)' : 'rgba(168,85,247,0.3)'}`,
        }}>{badge}</span>
      )}
      <div style={{ fontSize: 9, color: T.label, fontWeight: 600, letterSpacing: '0.08em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800, color, fontFamily: T.mono }}>{value}</div>
      {sub && <div style={{ fontSize: 9, color: T.label2, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

export default function LiveTrading() {
  const api = useApi();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await api('GET', '/v58/wallet-status');
      setData(res?.data ?? null);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [api]);

  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 10000); // Every 10s
    return () => clearInterval(interval);
  }, [fetchData]);

  if (loading) return <div style={{ color: T.label, padding: 40, fontFamily: T.mono }}>Loading wallet status...</div>;
  if (!data || data.error) return <div style={{ color: T.loss, padding: 40, fontFamily: T.mono }}>Error: {data?.error || 'Failed'}</div>;

  const { engine, trades, today, recent } = data;
  const isLive = engine.live_enabled && !engine.paper_mode;
  const modeColor = isLive ? T.live : T.paper;
  const modeLabel = isLive ? '🔴 LIVE' : '📄 PAPER';

  return (
    <div style={{ background: T.bg, minHeight: '100vh', fontFamily: T.mono, color: '#fff', padding: '20px 24px 60px' }}>
      {/* Header with mode indicator */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <h1 style={{ fontSize: 18, fontWeight: 800, margin: 0 }}>💰 Live Trading</h1>
        <span style={{
          padding: '4px 12px', borderRadius: 6, fontSize: 12, fontWeight: 800,
          background: isLive ? 'rgba(239,68,68,0.15)' : 'rgba(168,85,247,0.15)',
          color: modeColor,
          border: `1px solid ${isLive ? 'rgba(239,68,68,0.4)' : 'rgba(168,85,247,0.4)'}`,
          animation: isLive ? 'pulse 2s ease-in-out infinite' : 'none',
        }}>
          {modeLabel}
        </span>
        <span style={{ fontSize: 9, color: T.label2 }}>
          Heartbeat: {engine.last_heartbeat ? new Date(engine.last_heartbeat).toLocaleTimeString() : '—'}
        </span>
      </div>

      {/* Engine Status */}
      <section style={{ marginBottom: 24 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.label, letterSpacing: '0.12em', marginBottom: 10 }}>
          ENGINE STATUS
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
          <StatCard label="Mode" value={isLive ? 'LIVE' : 'PAPER'} color={modeColor} />
          <StatCard label="Bankroll" value={`$${engine.balance.toFixed(2)}`} sub={`Peak: $${engine.peak.toFixed(2)}`} color={engine.balance >= engine.peak * 0.9 ? T.profit : T.warning} />
          <StatCard label="Drawdown" value={`${(engine.drawdown_pct * 100).toFixed(1)}%`} color={engine.drawdown_pct < 0.2 ? T.profit : engine.drawdown_pct < 0.3 ? T.warning : T.loss} sub={engine.drawdown_pct >= 0.4 ? '⚠️ KILL SWITCH' : 'Max: 40%'} />
          <StatCard label="Daily P&L" value={`${engine.daily_pnl >= 0 ? '+' : ''}$${engine.daily_pnl.toFixed(2)}`} color={engine.daily_pnl >= 0 ? T.profit : T.loss} />
          {engine.wallet_usdc !== null && (
            <StatCard label="Wallet USDC" value={`$${engine.wallet_usdc.toFixed(2)}`} color="#fff" badge="LIVE" />
          )}
        </div>
      </section>

      {/* Trade Summary */}
      <section style={{ marginBottom: 24 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.label, letterSpacing: '0.12em', marginBottom: 10 }}>
          TRADE SUMMARY
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
          <StatCard label="Total Trades" value={trades.total} sub={`${trades.live_count} live / ${trades.paper_count} paper`} />
          <StatCard label="Win Rate" value={`${trades.total > 0 ? ((trades.wins / (trades.wins + trades.losses || 1)) * 100).toFixed(1) : 0}%`} sub={`${trades.wins}W / ${trades.losses}L`} color={T.profit} />
          <StatCard label="Realized P&L" value={`${trades.realized_pnl >= 0 ? '+' : ''}$${trades.realized_pnl.toFixed(2)}`} color={trades.realized_pnl >= 0 ? T.profit : T.loss} />
          <StatCard label="Open Exposure" value={`$${trades.open_exposure.toFixed(2)}`} sub={`${trades.pending} pending`} color={T.warning} />
          <StatCard label="Redemptions" value={`${trades.redeemed}/${trades.redeemed + trades.pending_redemption}`} sub={`${trades.pending_redemption} pending`} color={trades.pending_redemption > 0 ? T.warning : T.profit} />
        </div>
      </section>

      {/* Today's Breakdown */}
      {today?.length > 0 && (
        <section style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 10, fontWeight: 700, color: T.label, letterSpacing: '0.12em', marginBottom: 10 }}>
            TODAY'S BREAKDOWN
          </div>
          <div style={{ display: 'flex', gap: 10 }}>
            {today.map(t => (
              <div key={t.mode} style={{
                flex: 1, background: T.card, border: `1px solid ${t.mode === 'live' ? 'rgba(239,68,68,0.2)' : T.border}`,
                borderRadius: 10, padding: '12px 16px',
              }}>
                <div style={{
                  fontSize: 10, fontWeight: 800, marginBottom: 8,
                  color: t.mode === 'live' ? T.live : T.paper,
                }}>
                  {t.mode === 'live' ? '🔴 LIVE' : '📄 PAPER'}
                </div>
                <div style={{ fontSize: 18, fontWeight: 800, color: t.pnl >= 0 ? T.profit : T.loss, fontFamily: T.mono }}>
                  {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(2)}
                </div>
                <div style={{ fontSize: 9, color: T.label2 }}>{t.trades} trades · {t.wins}W/{t.losses}L</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Recent Trades */}
      <section>
        <div style={{ fontSize: 10, fontWeight: 700, color: T.label, letterSpacing: '0.12em', marginBottom: 10 }}>
          RECENT TRADES
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                {['Time', 'Mode', 'Asset', 'Signal', 'Actual', 'Entry', 'Outcome', 'P&L', 'Redeemed', 'CLOB ID'].map(h => (
                  <th key={h} style={{ padding: '6px 8px', textAlign: 'left', color: T.label, fontSize: 9, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={i} style={{
                  borderBottom: `1px solid ${T.border}`,
                  background: r.is_live ? 'rgba(239,68,68,0.03)' : 'transparent',
                }}>
                  <td style={{ padding: '6px 8px', color: '#fff' }}>{r.time ? new Date(r.time + 'Z').toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : '—'}</td>
                  <td style={{ padding: '6px 8px' }}>
                    <span style={{
                      padding: '1px 5px', borderRadius: 3, fontSize: 8, fontWeight: 800,
                      background: r.is_live ? 'rgba(239,68,68,0.15)' : 'rgba(168,85,247,0.1)',
                      color: r.is_live ? T.live : T.paper,
                    }}>
                      {r.is_live ? 'LIVE' : 'PAPER'}
                    </span>
                  </td>
                  <td style={{ padding: '6px 8px' }}>{r.asset}</td>
                  <td style={{ padding: '6px 8px', color: r.direction === 'YES' ? T.profit : T.loss, fontWeight: 700 }}>
                    {r.direction === 'YES' ? '▲ UP' : '▼ DN'}
                  </td>
                  <td style={{ padding: '6px 8px', fontWeight: 700 }}>
                    {r.outcome ? (() => {
                      const actualUp = (r.direction === 'YES' && r.outcome === 'WIN') || (r.direction === 'NO' && r.outcome === 'LOSS');
                      const actualDir = actualUp ? 'UP' : 'DN';
                      const mismatch = (r.direction === 'YES') !== actualUp;
                      return (
                        <span style={{
                          color: actualUp ? T.profit : T.loss,
                          background: mismatch ? 'rgba(248,113,113,0.12)' : 'transparent',
                          borderRadius: 3,
                          padding: '0 4px',
                        }}>
                          {actualUp ? '▲' : '▼'} {actualDir}
                        </span>
                      );
                    })() : <span style={{ color: T.label }}>—</span>}
                  </td>
                  <td style={{ padding: '6px 8px', fontFamily: T.mono }}>${r.entry_price?.toFixed(3) || '—'}</td>
                  <td style={{ padding: '6px 8px' }}>
                    {r.outcome ? (
                      <span style={{
                        padding: '2px 6px', borderRadius: 4, fontSize: 9, fontWeight: 700,
                        background: r.outcome === 'WIN' ? 'rgba(74,222,128,0.15)' : 'rgba(248,113,113,0.15)',
                        color: r.outcome === 'WIN' ? T.profit : T.loss,
                      }}>
                        {r.outcome === 'WIN' ? '✅ WIN' : '❌ LOSS'}
                      </span>
                    ) : (
                      <span style={{ color: T.warning, fontSize: 9 }}>⏳ pending</span>
                    )}
                  </td>
                  <td style={{
                    padding: '6px 8px', fontFamily: T.mono, fontWeight: 700,
                    color: r.pnl != null ? (r.pnl >= 0 ? T.profit : T.loss) : T.label,
                  }}>
                    {r.pnl != null ? `${r.pnl >= 0 ? '+' : ''}$${r.pnl.toFixed(2)}` : '—'}
                  </td>
                  <td style={{ padding: '6px 8px' }}>
                    {r.outcome && !r.redeemed ? (
                      <span style={{ color: T.warning, fontSize: 9 }}>⏳ pending</span>
                    ) : r.redeemed ? (
                      <span style={{ color: T.profit, fontSize: 9 }}>✅</span>
                    ) : (
                      <span style={{ color: T.label, fontSize: 9 }}>—</span>
                    )}
                  </td>
                  <td style={{ padding: '6px 8px', fontSize: 8, color: T.label2 }}>
                    {r.clob_id ? r.clob_id.substring(0, 12) + '…' : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
