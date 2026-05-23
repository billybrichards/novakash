// AnalysisPage — single-screen answer to Billy's 5 questions at /monitor/analysis.
//
// Dashboard PR 2 (note #597). Read-only operator page. One API call to
// GET /api/monitor/analysis returns the full envelope; we render all
// sections without any further requests.
//
// Section order is deliberate, matching Billy's brief:
//   1. Last-8h LIVE table (top — Billy lost money via LIVE recently)
//   2. Featured GHOST validation (ETH 9.5, BTC 9.3 raw+tight, XRP 9.5 raw+tight)
//   3. All-GHOST 24h table (scoped to featured IDs — see backend perf notes)
//   4. Wallet card (USDC + pUSD-pending note)
//   5. Bankroll trajectory sparkline (24h, hourly buckets)
//   6. Reconciler-health pill (last_stamp_at, today counts, healthy flag)
//
// Backend: hub/api/monitor_analysis.py
// Theme: only tokens from theme/tokens.js. No emoji, no rainbow palette.

import React, { useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { useApiLoader } from '../../hooks/useApiLoader.js';
import PageHeader from '../../components/shared/PageHeader.jsx';
import EmptyState from '../../components/shared/EmptyState.jsx';
import Loading from '../../components/shared/Loading.jsx';
import { T, wrColor } from '../../theme/tokens.js';

// ─── Formatters ──────────────────────────────────────────────────────────────

function fmtPnl(n) {
  if (n == null || !Number.isFinite(Number(n))) return { text: '—', color: T.label2 };
  const v = Number(n);
  const sign = v > 0 ? '+' : (v < 0 ? '-' : '');
  const color = v > 0 ? T.profit : (v < 0 ? T.loss : T.label2);
  return { text: `${sign}$${Math.abs(v).toFixed(2)}`, color };
}

function fmtWr(pct) {
  if (pct == null || !Number.isFinite(Number(pct))) return { text: '—', color: T.label };
  const v = Number(pct);
  return { text: `${v.toFixed(1)}%`, color: wrColor(v / 100) };
}

function fmtUsd(v, places = 2) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return `$${Number(v).toFixed(places)}`;
}

function fmtAge(iso) {
  if (!iso) return { text: '—', color: T.label2 };
  const ms = new Date(iso).getTime();
  if (!Number.isFinite(ms)) return { text: '—', color: T.label2 };
  const dSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  let text;
  if (dSec < 60) text = `${dSec}s ago`;
  else if (dSec < 3600) text = `${Math.floor(dSec / 60)}m ago`;
  else if (dSec < 86400) text = `${Math.floor(dSec / 3600)}h ago`;
  else text = `${Math.floor(dSec / 86400)}d ago`;
  let color = T.profit;
  if (dSec > 30 * 60) color = T.loss;
  else if (dSec > 5 * 60) color = T.warn;
  return { text, color };
}

// ─── Card shell ──────────────────────────────────────────────────────────────

function Card({ title, hint, right, children }) {
  return (
    <section style={{
      border: `1px solid ${T.border}`,
      borderRadius: 2,
      background: T.card,
      marginBottom: 14,
    }}>
      <header style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        padding: '10px 14px',
        borderBottom: `1px solid ${T.border}`,
      }}>
        <div>
          <div style={{
            fontSize: 11, letterSpacing: '0.14em', textTransform: 'uppercase',
            color: T.label,
          }}>{title}</div>
          {hint ? (
            <div style={{ color: T.label2, fontSize: 11, marginTop: 2 }}>{hint}</div>
          ) : null}
        </div>
        <div>{right}</div>
      </header>
      <div style={{ padding: '12px 14px' }}>{children}</div>
    </section>
  );
}

// ─── 1. LIVE 8h table ────────────────────────────────────────────────────────

function Live8hTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <EmptyState message="No LIVE fires in the last 8 hours." hint="Check engine logs if you expected fires." />;
  }
  const netPnl = rows.reduce((acc, r) => acc + Number(r.net_pnl_usd || 0), 0);
  const netPnlF = fmtPnl(netPnl);
  return (
    <div>
      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        marginBottom: 8,
      }}>
        <div style={{ color: T.label2, fontSize: 11 }}>
          {rows.length} {rows.length === 1 ? 'strategy' : 'strategies'} fired
        </div>
        <div style={{ fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>
          <span style={{ color: T.label, marginRight: 6 }}>net</span>
          <span style={{ color: netPnlF.color, fontWeight: 500 }}>{netPnlF.text}</span>
        </div>
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontFamily: T.font }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${T.borderStrong}` }}>
            {['Strategy', 'Fires', 'W', 'L', 'P', 'WR', 'Net PnL', 'Stake'].map((h, i) => (
              <th key={h} style={{
                padding: '8px 10px', color: T.label,
                fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase',
                textAlign: i === 0 ? 'left' : 'right', fontWeight: 500,
              }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const pnl = fmtPnl(r.net_pnl_usd);
            const wr = fmtWr(r.wr_pct);
            return (
              <tr key={r.strategy} style={{ borderBottom: `1px solid ${T.border}` }}>
                <td style={{ padding: '8px 10px', color: T.text, fontWeight: 500 }}>{r.strategy}</td>
                <td style={{ padding: '8px 10px', color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.fires}</td>
                <td style={{ padding: '8px 10px', color: T.profit, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.wins}</td>
                <td style={{ padding: '8px 10px', color: T.loss, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.losses}</td>
                <td style={{ padding: '8px 10px', color: T.warn, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.pending}</td>
                <td style={{ padding: '8px 10px', color: wr.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{wr.text}</td>
                <td style={{ padding: '8px 10px', color: pnl.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 500 }}>{pnl.text}</td>
                <td style={{ padding: '8px 10px', color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{fmtUsd(r.stake_usd)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ─── 2. Featured GHOST validation cards ──────────────────────────────────────

function FeaturedCard({ f }) {
  const live = f.live_would_wr_pct;
  const train = f.train_projection_wr_pct;
  const skew = f.train_serve_skew_pp;
  const liveF = fmtWr(live);
  const skewColor = (skew == null) ? T.label2
    : (Math.abs(skew) > 5 ? T.loss : T.label2);

  return (
    <div style={{
      border: `1px solid ${f.skew_warning ? T.loss : T.border}`,
      borderRadius: 2,
      padding: '10px 12px',
      background: f.skew_warning ? 'rgba(248,113,113,0.04)' : T.card,
      minWidth: 180,
    }}>
      <div style={{
        fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase',
        color: T.label, marginBottom: 4,
      }}>
        {f.family}
      </div>
      <div style={{ color: T.text, fontSize: 13, fontWeight: 500, marginBottom: 8 }}>
        {f.strategy_id}
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div>
          <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.08em' }}>GHOST WR</div>
          <div style={{ color: liveF.color, fontSize: 18, fontVariantNumeric: 'tabular-nums', fontWeight: 500 }}>
            {liveF.text}
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.08em' }}>training</div>
          <div style={{ color: T.label2, fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>
            {train != null ? `${train.toFixed(1)}%` : '—'}
          </div>
        </div>
      </div>
      <div style={{ marginTop: 8, color: T.label2, fontSize: 11 }}>
        <span style={{ marginRight: 8 }}>fires {f.would_fires_24h}</span>
        <span>resolved {f.resolved_24h}</span>
      </div>
      <div style={{ marginTop: 6, color: skewColor, fontSize: 11, fontVariantNumeric: 'tabular-nums' }}>
        {skew == null ? 'awaiting data'
          : `skew ${skew > 0 ? '−' : '+'}${Math.abs(skew).toFixed(1)} pp`}
        {f.skew_warning ? <span style={{ color: T.loss, marginLeft: 6 }}>· drift</span> : null}
      </div>
    </div>
  );
}

function FeaturedRow({ featured }) {
  if (!featured || featured.length === 0) return null;
  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
      gap: 10,
    }}>
      {featured.map((f) => <FeaturedCard key={f.strategy_id} f={f} />)}
    </div>
  );
}

// ─── 3. All GHOST table (24h) ────────────────────────────────────────────────

function Ghost24hTable({ rows }) {
  if (!rows || rows.length === 0) {
    return (
      <EmptyState
        message="No GHOST fires in the last 24 hours for the featured strategies."
        hint="Scope is the 5 featured strategy IDs — for the full registry view, see /monitor/strategies."
      />
    );
  }
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, fontFamily: T.font }}>
      <thead>
        <tr style={{ borderBottom: `1px solid ${T.borderStrong}` }}>
          {['Strategy', 'Asset', 'Would-fires', 'Resolved', 'W', 'L', 'Would-WR', 'Avg fill', 'Last fire'].map((h, i) => (
            <th key={h} style={{
              padding: '8px 10px', color: T.label,
              fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase',
              textAlign: i < 2 ? 'left' : 'right', fontWeight: 500,
            }}>{h}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const wr = fmtWr(r.would_wr_pct);
          const last = fmtAge(r.last_fire_at);
          return (
            <tr key={r.strategy_id} style={{ borderBottom: `1px solid ${T.border}` }}>
              <td style={{ padding: '8px 10px', color: T.text, fontWeight: 500 }}>{r.strategy_id}</td>
              <td style={{ padding: '8px 10px', color: T.label2 }}>{r.asset || '—'}</td>
              <td style={{ padding: '8px 10px', color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.would_fires}</td>
              <td style={{ padding: '8px 10px', color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.resolved}</td>
              <td style={{ padding: '8px 10px', color: T.profit, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.wins}</td>
              <td style={{ padding: '8px 10px', color: T.loss, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{r.losses}</td>
              <td style={{ padding: '8px 10px', color: wr.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums', fontWeight: 500 }}>{wr.text}</td>
              <td style={{ padding: '8px 10px', color: T.label2, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                {r.avg_fill_price != null ? Number(r.avg_fill_price).toFixed(3) : '—'}
              </td>
              <td style={{ padding: '8px 10px', color: last.color, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>{last.text}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// ─── 4. Wallet card ──────────────────────────────────────────────────────────

function WalletCard({ wallet }) {
  if (!wallet) return <EmptyState message="No wallet data." />;
  const usdc = wallet.balance_usdc;
  const usdcColor = usdc == null
    ? T.label
    : (Number(usdc) < 2.0 ? T.loss : (Number(usdc) < 5.0 ? T.warn : T.profit));
  const snapAge = fmtAge(wallet.snapshot_at);
  return (
    <div style={{
      display: 'flex',
      flexWrap: 'wrap',
      gap: 18,
      alignItems: 'flex-start',
    }}>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>
          USDC (engine)
        </div>
        <div style={{ color: usdcColor, fontSize: 24, fontVariantNumeric: 'tabular-nums', fontWeight: 500 }}>
          {usdc != null ? fmtUsd(usdc, 4) : '—'}
        </div>
        <div style={{ color: T.label, fontSize: 10 }}>
          source {wallet.source || '—'} · snapshot {snapAge.text}
        </div>
      </div>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>
          pUSD (Polymarket)
        </div>
        <div style={{ color: T.label2, fontSize: 18, fontVariantNumeric: 'tabular-nums' }}>~$10</div>
        <div style={{ color: T.warn, fontSize: 10 }}>
          {wallet.pusd_note || 'pUSD pending'}
        </div>
      </div>
    </div>
  );
}

// ─── 5. Bankroll trajectory sparkline ────────────────────────────────────────

function Sparkline({ points, width = 600, height = 80 }) {
  if (!points || points.length === 0) {
    return <EmptyState message="No bankroll snapshots in the last 24h." />;
  }
  const xs = points.map((_, i) => i);
  const ys = points.map((p) => Number(p.avg_usdc ?? 0));
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const range = maxY - minY || 1;
  const pad = 6;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const path = ys.map((y, i) => {
    const x = pad + (xs[i] / (xs.length - 1 || 1)) * w;
    const yy = pad + h - ((y - minY) / range) * h;
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${yy.toFixed(1)}`;
  }).join(' ');
  // y-trend: comparing first vs last avg
  const first = ys[0];
  const last = ys[ys.length - 1];
  const trendColor = (last > first) ? T.profit : (last < first ? T.loss : T.label2);
  return (
    <div>
      <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none"
           style={{ display: 'block', overflow: 'visible' }}>
        <path d={path} fill="none" stroke={trendColor} strokeWidth="1.4" />
        {ys.map((y, i) => {
          const x = pad + (xs[i] / (xs.length - 1 || 1)) * w;
          const yy = pad + h - ((y - minY) / range) * h;
          return (
            <circle key={i} cx={x} cy={yy} r="2" fill={trendColor}>
              <title>{`${new Date(points[i].bucket_at).toLocaleTimeString()}: avg $${y.toFixed(2)}`}</title>
            </circle>
          );
        })}
      </svg>
      <div style={{
        display: 'flex', justifyContent: 'space-between',
        color: T.label, fontSize: 10, marginTop: 4, fontVariantNumeric: 'tabular-nums',
      }}>
        <span>min ${minY.toFixed(2)}</span>
        <span style={{ color: trendColor }}>
          {first.toFixed(2)} → {last.toFixed(2)} ({(((last - first) / (first || 1)) * 100).toFixed(1)}%)
        </span>
        <span>max ${maxY.toFixed(2)}</span>
      </div>
    </div>
  );
}

// ─── 6. Reconciler health pill ───────────────────────────────────────────────

function ReconcilerHealthPill({ h }) {
  if (!h) return <EmptyState message="No reconciler health data." />;
  const ageF = fmtAge(h.last_stamp_at);
  const healthyColor = h.healthy ? T.profit : T.loss;
  return (
    <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'center' }}>
      <div style={{
        display: 'inline-block',
        border: `1px solid ${healthyColor}`,
        color: healthyColor,
        background: h.healthy ? 'rgba(74,222,128,0.06)' : 'rgba(248,113,113,0.06)',
        padding: '4px 10px',
        borderRadius: 2,
        fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase',
      }}>
        {h.healthy ? 'healthy' : 'stale'}
      </div>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>last stamp</div>
        <div style={{ color: ageF.color, fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>{ageF.text}</div>
      </div>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>5-min count</div>
        <div style={{ color: T.text, fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>{h.recent_redeemed_5m}</div>
      </div>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>today redeemed</div>
        <div style={{ color: T.text, fontSize: 13, fontVariantNumeric: 'tabular-nums' }}>{h.today_redeemed}</div>
      </div>
      <div>
        <div style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase' }}>today payout</div>
        <div style={{ color: T.profit, fontSize: 13, fontVariantNumeric: 'tabular-nums', fontWeight: 500 }}>
          {fmtUsd(h.today_payout_usd)}
        </div>
      </div>
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function AnalysisPage() {
  const api = useApi();
  const { data, error, loading, reload } = useApiLoader(
    (signal) => api.get('/api/monitor/analysis', { signal }),
    []
  );

  const payload = data && typeof data === 'object' && !Array.isArray(data) ? data : null;

  const fetchedAt = useMemo(() => {
    if (!payload?.fetched_at) return null;
    try { return new Date(payload.fetched_at).toLocaleTimeString(); }
    catch { return null; }
  }, [payload?.fetched_at]);

  // LIVE 8h header tot — what did the cluster make / lose in the last 8h?
  const live8hTot = useMemo(() => {
    if (!payload?.live_8h) return 0;
    return payload.live_8h.reduce((acc, r) => acc + Number(r.net_pnl_usd || 0), 0);
  }, [payload?.live_8h]);

  return (
    <div>
      <PageHeader
        tag="MONITOR"
        title="Analysis — 8h LIVE + 24h GHOST"
        subtitle={fetchedAt
          ? `Fetched ${fetchedAt}${payload?.cache_hit ? ' (cached)' : ''}`
          : 'Loading…'}
        right={
          <button
            onClick={reload}
            disabled={loading}
            style={{
              background: 'transparent',
              border: `1px solid ${T.border}`,
              color: T.label2,
              padding: '6px 14px',
              fontSize: 11,
              letterSpacing: '0.08em',
              cursor: loading ? 'not-allowed' : 'pointer',
              fontFamily: T.font,
            }}
          >
            {loading ? 'Loading…' : 'Refresh'}
          </button>
        }
      />

      {loading && !payload ? <Loading label="Loading analysis…" /> : null}

      {error ? (
        <div style={{ color: T.loss, fontSize: 12, padding: '12px 0' }}>
          Error loading analysis: {String(error)}
        </div>
      ) : null}

      {payload ? (
        <>
          {/* 1. LIVE 8h table — most-recent fires, top of page */}
          <Card
            title="LIVE strategies · last 8h"
            hint="Gamma-verified outcomes from the trades table (PR #575 reconciler)."
            right={
              <div style={{
                color: fmtPnl(live8hTot).color, fontSize: 13,
                fontVariantNumeric: 'tabular-nums', fontWeight: 500,
              }}>
                {fmtPnl(live8hTot).text} net
              </div>
            }
          >
            <Live8hTable rows={payload.live_8h} />
          </Card>

          {/* 2. Featured GHOST validation — train vs serve */}
          <Card
            title="Featured GHOST validation · 24h"
            hint="ETH 9.5 · BTC 9.3 raw+tight · XRP 9.5 raw+tight. Skew > 5pp flags train-serve drift."
          >
            <FeaturedRow featured={payload.featured} />
          </Card>

          {/* 3. All-GHOST table */}
          <Card
            title="GHOST strategies · 24h"
            hint="Scoped to featured strategy IDs (composite-index path). For the full registry, see /monitor/strategies."
          >
            <Ghost24hTable rows={payload.ghost_24h} />
          </Card>

          {/* 4 + 5 — wallet card + bankroll sparkline side by side */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 2fr', gap: 14 }}>
            <Card title="Wallet">
              <WalletCard wallet={payload.wallet} />
            </Card>
            <Card
              title="Bankroll · 24h"
              hint="Hourly avg USDC from wallet_snapshots. Trend arrow = first→last hour."
            >
              <Sparkline points={payload.bankroll_24h} />
            </Card>
          </div>

          {/* 6 — reconciler */}
          <Card
            title="Reconciler health"
            hint="Healthy = last redeemed stamp within 30 min. 5-min count >0 = ran very recently."
          >
            <ReconcilerHealthPill h={payload.reconciler_health} />
          </Card>
        </>
      ) : null}

      <div style={{ marginTop: 12, color: T.label, fontSize: 10, letterSpacing: '0.06em' }}>
        Read-only view. pUSD wallet plumbing ships in PR 3 (Montreal sidecar → RDS column).
      </div>
    </div>
  );
}
