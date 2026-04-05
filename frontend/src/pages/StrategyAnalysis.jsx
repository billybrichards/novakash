/**
 * Strategy Analysis Page — 30-day backtest against real Polymarket outcomes.
 * Shows base rates, v7.1 performance, regime analysis, hourly patterns, multi-asset comparison.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

const T = {
  bg: '#0a0a0f', card: '#12121a', border: 'rgba(255,255,255,0.06)',
  label: '#666', label2: '#888', mono: "'JetBrains Mono', 'Fira Code', monospace",
  profit: '#22c55e', loss: '#ef4444', purple: '#a855f7', cyan: '#06b6d4',
  warning: '#eab308',
};

function StatCard({ label, value, sub, color = '#fff' }) {
  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 10,
      padding: '14px 16px',
    }}>
      <div style={{ fontSize: 9, color: T.label, fontWeight: 600, letterSpacing: '0.08em', marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 800, color, fontFamily: T.mono }}>{value}</div>
      {sub && <div style={{ fontSize: 9, color: T.label2, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function SectionHeader({ children }) {
  return (
    <div style={{
      fontSize: 10, fontWeight: 700, color: T.label, letterSpacing: '0.12em',
      textTransform: 'uppercase', marginBottom: 12, paddingBottom: 6,
      borderBottom: `1px solid ${T.border}`,
    }}>{children}</div>
  );
}

export default function StrategyAnalysis() {
  const api = useApi();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    try {
      const res = await api('GET', '/v58/strategy-analysis');
      setData(res?.data ?? null);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [api]);

  useEffect(() => { fetchData(); }, [fetchData]);

  if (loading) return <div style={{ color: T.label, padding: 40, fontFamily: T.mono }}>Loading strategy analysis...</div>;
  if (!data) return <div style={{ color: T.loss, padding: 40, fontFamily: T.mono }}>Failed to load data</div>;

  const { base_rate, daily, hourly, real_trades, v71, v71_by_regime, multi_asset } = data;

  return (
    <div style={{ background: T.bg, minHeight: '100vh', fontFamily: T.mono, color: '#fff', padding: '20px 24px 60px' }}>
      <h1 style={{ fontSize: 18, fontWeight: 800, marginBottom: 4 }}>📊 Strategy Analysis</h1>
      <div style={{ fontSize: 10, color: T.label, marginBottom: 24 }}>
        30-day backtest against real Polymarket outcomes · BTC 5-min windows
      </div>

      {/* ═══ OVERVIEW STATS ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>OVERVIEW — Real Performance</SectionHeader>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 10 }}>
          <StatCard label="Real Trades" value={real_trades.trades} sub={`${real_trades.wins}W / ${real_trades.losses}L`} />
          <StatCard label="💰 Polymarket WR" value={`${real_trades.wr}%`} color={real_trades.wr >= 70 ? T.profit : real_trades.wr >= 60 ? T.warning : T.loss} sub="Actual trade profit/loss" />
          <StatCard label="Total P&L" value={`$${real_trades.pnl.toLocaleString()}`} color={real_trades.pnl >= 0 ? T.profit : T.loss} sub={`Avg entry: $${real_trades.avg_entry}`} />
          <StatCard label="🧭 Directional WR" value={`${v71.wr}%`} color={v71.wr >= 70 ? T.profit : v71.wr >= 60 ? T.warning : T.loss} sub={`${v71.wins}W / ${v71.losses}L (${v71.resolved} resolved)`} />
        </div>
      </section>

      {/* ═══ WR EXPLAINER ═══ */}
      <section style={{ marginBottom: 28 }}>
        <div style={{
          background: 'rgba(168,85,247,0.06)', border: '1px solid rgba(168,85,247,0.2)',
          borderRadius: 10, padding: '14px 16px', fontSize: 10, lineHeight: 1.8, color: T.label2,
        }}>
          <div style={{ fontWeight: 700, color: '#a855f7', marginBottom: 6, fontSize: 11 }}>⚠️ Why two different win rates?</div>
          <div><strong style={{ color: T.profit }}>💰 Polymarket WR ({real_trades.wr}%)</strong> — Did we actually <em>make money</em>? Based on {real_trades.trades} real trades where Polymarket oracle resolved WIN or LOSS. <strong>This is what matters for your bankroll.</strong></div>
          <div><strong style={{ color: '#a855f7' }}>🧭 Directional WR ({v71.wr}%)</strong> — Did BTC move in our predicted direction? Based on {v71.resolved} windows comparing open→close price. This is inflated (~96%) because we follow the delta — of course BTC goes the way it was already going.</div>
          <div style={{ marginTop: 4, color: T.warning }}>The gap ({(v71.wr - real_trades.wr).toFixed(1)}pp) exists because: entry price spread, oracle timing (~4min delay), and price reversions between T-0 and oracle resolution.</div>
        </div>
      </section>

      {/* ═══ 30-DAY BASE RATE ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>30-DAY BASE RATE — BTC 5-Min Windows</SectionHeader>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10 }}>
          <StatCard label="Total Windows" value={base_rate.total.toLocaleString()} sub="30 days of 5-min data" />
          <StatCard label="UP Outcomes" value={`${base_rate.up_pct}%`} sub={`${base_rate.up.toLocaleString()} windows`} color={T.profit} />
          <StatCard label="DOWN Outcomes" value={`${base_rate.down_pct}%`} sub={`${base_rate.down.toLocaleString()} windows`} color={T.loss} />
        </div>
        <div style={{ fontSize: 10, color: T.label2, marginTop: 8, padding: '8px 12px', background: T.card, borderRadius: 8 }}>
          💡 Base rate is ~50/50 — the market is efficient. Our edge comes from VPIN, regime detection, and entry timing at T-60s.
        </div>
      </section>

      {/* ═══ v7.1 BY REGIME ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>v7.1 PERFORMANCE BY REGIME</SectionHeader>
        <div style={{ display: 'grid', gridTemplateColumns: `repeat(${v71_by_regime.length || 1}, 1fr)`, gap: 10 }}>
          {v71_by_regime.map(r => (
            <div key={r.regime} style={{
              background: r.regime === 'CASCADE' ? 'rgba(239,68,68,0.06)' : r.regime === 'TRANSITION' ? 'rgba(234,179,8,0.06)' : 'rgba(168,85,247,0.06)',
              border: `1px solid ${r.regime === 'CASCADE' ? 'rgba(239,68,68,0.2)' : r.regime === 'TRANSITION' ? 'rgba(234,179,8,0.2)' : 'rgba(168,85,247,0.2)'}`,
              borderRadius: 10, padding: '14px 16px',
            }}>
              <div style={{ fontSize: 11, fontWeight: 800, color: r.regime === 'CASCADE' ? T.loss : r.regime === 'TRANSITION' ? T.warning : T.purple, marginBottom: 8 }}>
                {r.regime}
              </div>
              <div style={{ fontSize: 20, fontWeight: 800, color: r.wr >= 70 ? T.profit : r.wr >= 60 ? T.warning : T.loss, fontFamily: T.mono }}>
                {r.wr}%
              </div>
              <div style={{ fontSize: 9, color: T.label2, marginTop: 4 }}>{r.wins}W / {r.losses}L ({r.eligible} eligible)</div>
            </div>
          ))}
        </div>
      </section>

      {/* ═══ HOURLY PATTERN ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>HOURLY PATTERN — DOWN% by Hour (UTC)</SectionHeader>
        <div style={{ display: 'flex', gap: 3, alignItems: 'flex-end', height: 120, padding: '0 4px' }}>
          {hourly.map(h => {
            const barH = Math.max(4, (h.down_pct / 60) * 100);
            const isHigh = h.down_pct > 53;
            const isLow = h.down_pct < 47;
            return (
              <div key={h.hour} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 2 }}>
                <div style={{ fontSize: 8, color: isHigh ? T.profit : isLow ? T.loss : T.label, fontWeight: isHigh || isLow ? 700 : 400 }}>
                  {h.down_pct}%
                </div>
                <div style={{
                  width: '100%', height: barH, borderRadius: '3px 3px 0 0',
                  background: isHigh ? T.profit : isLow ? T.loss : 'rgba(255,255,255,0.15)',
                  opacity: 0.7,
                }} />
                <div style={{ fontSize: 7, color: T.label }}>{String(h.hour).padStart(2, '0')}</div>
              </div>
            );
          })}
        </div>
        <div style={{ fontSize: 9, color: T.label2, marginTop: 8, textAlign: 'center' }}>
          Green bars = DOWN wins &gt;53% (potential edge) · Red = UP wins &gt;53% · Grey = ~50/50
        </div>
      </section>

      {/* ═══ DAILY HEATMAP ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>DAILY OUTCOMES — 30 Days</SectionHeader>
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${T.border}` }}>
                {['Date', 'Total', 'UP', 'DOWN', 'DOWN%'].map(h => (
                  <th key={h} style={{ padding: '4px 8px', textAlign: 'left', color: T.label, fontSize: 9, fontWeight: 600 }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {daily.slice(-14).map(d => {
                const downPct = d.total > 0 ? (d.down / d.total * 100) : 50;
                return (
                  <tr key={d.day} style={{ borderBottom: `1px solid ${T.border}` }}>
                    <td style={{ padding: '4px 8px', color: '#fff' }}>{d.day}</td>
                    <td style={{ padding: '4px 8px' }}>{d.total}</td>
                    <td style={{ padding: '4px 8px', color: T.profit }}>{d.up}</td>
                    <td style={{ padding: '4px 8px', color: T.loss }}>{d.down}</td>
                    <td style={{ padding: '4px 8px', color: downPct > 53 ? T.profit : downPct < 47 ? T.loss : T.label, fontWeight: 700 }}>
                      {downPct.toFixed(1)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ═══ MULTI-ASSET ═══ */}
      <section style={{ marginBottom: 28 }}>
        <SectionHeader>MULTI-ASSET COMPARISON — 30 Days (5m)</SectionHeader>
        <div style={{ display: 'grid', gridTemplateColumns: `repeat(${multi_asset.length || 1}, 1fr)`, gap: 10 }}>
          {multi_asset.map(a => (
            <div key={a.asset} style={{
              background: T.card, border: `1px solid ${T.border}`, borderRadius: 10,
              padding: '14px 16px',
            }}>
              <div style={{ fontSize: 12, fontWeight: 800, color: '#fff', marginBottom: 6 }}>{a.asset}</div>
              <div style={{ fontSize: 9, color: T.label2 }}>{a.total.toLocaleString()} windows</div>
              <div style={{ display: 'flex', gap: 12, marginTop: 8 }}>
                <div>
                  <div style={{ fontSize: 8, color: T.label }}>UP</div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: T.profit }}>{(100 - a.down_pct).toFixed(1)}%</div>
                </div>
                <div>
                  <div style={{ fontSize: 8, color: T.label }}>DOWN</div>
                  <div style={{ fontSize: 14, fontWeight: 700, color: T.loss }}>{a.down_pct}%</div>
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* ═══ KEY INSIGHTS ═══ */}
      <section>
        <SectionHeader>KEY INSIGHTS</SectionHeader>
        <div style={{ background: T.card, border: `1px solid ${T.border}`, borderRadius: 10, padding: '16px', fontSize: 10, lineHeight: 1.8, color: T.label2 }}>
          <div>📊 <strong style={{ color: '#fff' }}>Base rate is 50/50</strong> — BTC 5-min outcomes are essentially a coin flip. Any WR above 55% is real edge.</div>
          <div>🎯 <strong style={{ color: '#fff' }}>v7.1 achieves {v71.wr}% WR</strong> — {v71.wins}W/{v71.losses}L on {v71.resolved} Polymarket-resolved windows. That's {(v71.wr - 50).toFixed(1)}pp above random.</div>
          <div>💰 <strong style={{ color: '#fff' }}>Real P&L: ${real_trades.pnl.toLocaleString()}</strong> from {real_trades.trades} actual trades at avg entry ${real_trades.avg_entry}.</div>
          <div>⚡ <strong style={{ color: '#fff' }}>Entry price matters</strong> — lower entry = better R/R. Current cap at $0.70 balances trade frequency vs profitability.</div>
          <div>🔬 <strong style={{ color: '#fff' }}>VPIN + regime detection</strong> is our edge. The market is 50/50 but informed flow (VPIN) + CG/TWAP gates filter for higher-probability setups.</div>
        </div>
      </section>
    </div>
  );
}
