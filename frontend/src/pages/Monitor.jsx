import React, { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import Loading from '../components/shared/Loading.jsx';
import { T } from '../theme/tokens.js';
import { formatSkipReason } from './GateTraces.jsx';

import HealthStrip from '../components/monitor/HealthStrip.jsx';
import EnsembleSignalCard from '../components/monitor/EnsembleSignalCard.jsx';
import CircuitBreakerCard from '../components/monitor/CircuitBreakerCard.jsx';
import ClassifierHistogram from '../components/monitor/ClassifierHistogram.jsx';
import ModeShareCard from '../components/monitor/ModeShareCard.jsx';
import DisagreementPlot from '../components/monitor/DisagreementPlot.jsx';
import SaturationMeter from '../components/monitor/SaturationMeter.jsx';
import LgbVsClsPlot from '../components/monitor/LgbVsClsPlot.jsx';
import ComparativeWRCard from '../components/monitor/ComparativeWRCard.jsx';
import KillConfirmModal from '../components/monitor/KillConfirmModal.jsx';
import { useSnapshotStream } from '../components/monitor/useSnapshotStream.js';

// V4Surface is 317 LOC; Assembler1 is 1079. Lazy-load both — they're tab content.
const V4Surface = lazy(() => import('./data-surfaces/V4Surface.jsx'));
const Assembler1 = lazy(() => import('./data-surfaces/Assembler1.jsx'));

function utcHHMM(tsSec) {
  if (!tsSec) return '—';
  const d = new Date(tsSec * 1000);
  const h = String(d.getUTCHours()).padStart(2, '0');
  const m = String(d.getUTCMinutes()).padStart(2, '0');
  return `${h}:${m}Z`;
}

function usePolled(fetcher, intervalMs, deps = []) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    const run = async () => {
      if (document.visibilityState !== 'visible') {
        timer = setTimeout(run, intervalMs);
        return;
      }
      try {
        const r = await fetcher();
        if (cancelled) return;
        setData(r);
        setErr(null);
      } catch (e) {
        if (cancelled) return;
        setErr(e.message || 'failed');
      } finally {
        if (!cancelled) timer = setTimeout(run, intervalMs);
      }
    };
    run();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error: err };
}

// ── Trade activity ──────────────────────────────────────────────────────
function TradeActivityCard({ api }) {
  const { data } = usePolled(
    () => api.get('/api/v58/strategy-decisions?strategy_id=v5_ensemble&limit=20').then(r => r.data),
    5000,
    [api],
  );
  const rows = (data?.decisions ?? []).slice(0, 8);
  return (
    <div style={{
      background: T.card, border: `1px solid ${T.border}`, borderRadius: 4,
      padding: 14,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}>
        <div style={{ fontSize: 11, color: T.label2, fontWeight: 600, letterSpacing: '0.08em' }}>
          v5_ensemble · recent decisions
        </div>
        <div style={{ fontSize: 9, color: T.label, fontFamily: T.font }}>5s</div>
      </div>
      {rows.length === 0 ? (
        <div style={{ color: T.label, fontSize: 11, padding: 8 }}>no decisions yet…</div>
      ) : (
        <div style={{ fontFamily: T.font, fontSize: 10 }}>
          {rows.map((d) => {
            const isTrade = d.action === 'TRADE';
            const dirColor = d.direction === 'UP' ? '#10b981' : d.direction === 'DOWN' ? '#ef4444' : T.label;
            const pnlColor = d.pnl_usd == null ? T.label2 : d.pnl_usd >= 0 ? '#10b981' : '#ef4444';
            return (
              <div key={d.id} style={{
                display: 'grid',
                gridTemplateColumns: '52px 58px 46px 70px 1fr 60px',
                gap: 8, padding: '4px 0', borderBottom: `1px solid ${T.border}`,
                alignItems: 'center',
              }}>
                <span style={{ color: T.label }}>{utcHHMM(d.window_ts)}</span>
                <span style={{ color: isTrade ? '#10b981' : T.label2, fontWeight: 600 }}>
                  {d.action}
                </span>
                <span style={{ color: dirColor }}>{d.direction ?? '—'}</span>
                <span style={{ color: T.text }}>
                  conf {d.confidence != null ? d.confidence.toFixed(3) : '—'}
                </span>
                <span style={{ color: T.label2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {isTrade
                    ? (d.entry_reason ?? '')
                    : formatSkipReason(d.skip_reason)}
                </span>
                <span style={{ color: pnlColor, textAlign: 'right' }}>
                  {d.pnl_usd != null ? `${d.pnl_usd >= 0 ? '+' : ''}$${d.pnl_usd.toFixed(2)}` : '—'}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Notes ticker (marquee-ish, bottom) ─────────────────────────────────
function NotesTicker({ api }) {
  const { data } = usePolled(
    () => api.get('/api/notes?limit=5').then(r => r.data),
    60000,
    [api],
  );
  const rows = data?.rows ?? [];
  if (rows.length === 0) return null;
  return (
    <div style={{
      display: 'flex', gap: 16, overflow: 'hidden', whiteSpace: 'nowrap',
      padding: '6px 10px', borderTop: `1px solid ${T.border}`,
      fontSize: 10, color: T.label2, fontFamily: T.font,
    }}>
      <span style={{ color: T.label, letterSpacing: '0.1em' }}>NOTES</span>
      {rows.map((n) => (
        <span key={n.id} style={{ color: T.text }} title={n.body?.slice(0, 200)}>
          #{n.id} · {n.title}
        </span>
      ))}
    </div>
  );
}

// ── Accuracy badge in header ───────────────────────────────────────────
function AccuracyBadge({ api }) {
  const { data } = usePolled(
    () => api.get('/api/v58/accuracy').then(r => r.data),
    30000,
    [api],
  );
  if (!data) return null;
  const a = data.v58_accuracy;
  const n = data.v58_trades_count;
  const pnl = data.cumulative_pnl;
  const color = a == null ? T.label : a >= 0.7 ? '#10b981' : a >= 0.55 ? '#f59e0b' : '#ef4444';
  return (
    <div style={{ display: 'flex', gap: 10, alignItems: 'center', fontFamily: T.font, fontSize: 10 }}>
      <span title={`v58 rolling accuracy over last ${n ?? '?'} trades`}>
        <span style={{ color: T.label }}>acc</span>{' '}
        <b style={{ color }}>
          {a != null ? `${(a * 100).toFixed(0)}%` : '—'}
        </b>{' '}
        <span style={{ color: T.label2 }}>({n ?? '0'})</span>
      </span>
      {pnl != null ? (
        <span>
          <span style={{ color: T.label }}>pnl</span>{' '}
          <b style={{ color: pnl >= 0 ? '#10b981' : '#ef4444' }}>
            {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}
          </b>
        </span>
      ) : null}
    </div>
  );
}

// ── Tier-3 tabs ────────────────────────────────────────────────────────
const TABS = [
  { id: 'snapshot', label: 'Raw snapshot' },
  { id: 'envelope', label: 'Envelope (/predict)' },
  { id: 'decisions', label: 'Decision log' },
  { id: 'consensus', label: 'Consensus' },
  { id: 'regime', label: 'Regime + Macro' },
];

function DecisionLogPanel({ api }) {
  const { data } = usePolled(
    () => api.get('/api/v58/strategy-decisions?strategy_id=v5_ensemble&limit=200').then(r => r.data),
    10000,
    [api],
  );
  const rows = data?.decisions ?? [];
  return (
    <div style={{ fontFamily: T.font, fontSize: 10 }}>
      <div style={{ color: T.label, marginBottom: 8 }}>Last 200 v5_ensemble decisions (polled 10s):</div>
      <div style={{ maxHeight: 520, overflowY: 'auto', border: `1px solid ${T.border}`, borderRadius: 2 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead style={{ position: 'sticky', top: 0, background: '#0b1220' }}>
            <tr style={{ color: T.label, fontSize: 9 }}>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>window</th>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>action</th>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>dir</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>conf</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>off</th>
              <th style={{ textAlign: 'left', padding: '6px 8px' }}>why</th>
              <th style={{ textAlign: 'right', padding: '6px 8px' }}>pnl</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => {
              const dirColor = d.direction === 'UP' ? '#10b981' : d.direction === 'DOWN' ? '#ef4444' : T.label;
              const pnlColor = d.pnl_usd == null ? T.label2 : d.pnl_usd >= 0 ? '#10b981' : '#ef4444';
              return (
                <tr key={d.id} style={{ borderTop: `1px solid ${T.border}` }}>
                  <td style={{ padding: '4px 8px', color: T.label2 }}>{utcHHMM(d.window_ts)}</td>
                  <td style={{ padding: '4px 8px', color: d.action === 'TRADE' ? '#10b981' : T.label2 }}>{d.action}</td>
                  <td style={{ padding: '4px 8px', color: dirColor }}>{d.direction ?? '—'}</td>
                  <td style={{ padding: '4px 8px', textAlign: 'right', color: T.text }}>
                    {d.confidence != null ? d.confidence.toFixed(3) : '—'}
                  </td>
                  <td style={{ padding: '4px 8px', textAlign: 'right', color: T.label2 }}>
                    {d.eval_offset ?? '—'}
                  </td>
                  <td style={{ padding: '4px 8px', color: T.label2, maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {d.action === 'TRADE' ? d.entry_reason : formatSkipReason(d.skip_reason)}
                  </td>
                  <td style={{ padding: '4px 8px', textAlign: 'right', color: pnlColor }}>
                    {d.pnl_usd != null ? `${d.pnl_usd >= 0 ? '+' : ''}$${d.pnl_usd.toFixed(2)}` : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ConsensusPanel({ snapshot }) {
  const c = snapshot?.consensus;
  if (!c) return <div style={{ color: T.label, fontSize: 11, fontFamily: T.font }}>no consensus data</div>;
  const sources = c.sources ?? {};
  return (
    <div style={{ fontFamily: T.font, fontSize: 11 }}>
      {!c.safe_to_trade ? (
        <div style={{
          padding: '10px 12px', marginBottom: 12, border: '1px solid #ef4444',
          background: 'rgba(239,68,68,0.08)', color: '#fca5a5', borderRadius: 2,
        }}>
          UNSAFE TO TRADE: {c.safe_to_trade_reason}
        </div>
      ) : null}
      <div style={{ display: 'flex', gap: 16, color: T.label2, fontSize: 10, marginBottom: 10 }}>
        <span>reference <b style={{ color: T.text }}>${c.reference_price?.toLocaleString()}</b></span>
        <span>max div <b style={{ color: c.max_divergence_bps > 30 ? '#ef4444' : c.max_divergence_bps > 10 ? '#f59e0b' : '#10b981' }}>{c.max_divergence_bps?.toFixed(1)} bps</b></span>
        <span>agreement <b style={{ color: T.text }}>{(c.source_agreement_score * 100).toFixed(0)}%</b></span>
      </div>
      <div style={{ border: `1px solid ${T.border}`, borderRadius: 2 }}>
        {Object.entries(sources).map(([name, s]) => (
          <div key={name} style={{
            display: 'grid', gridTemplateColumns: '150px 120px 90px 60px',
            padding: '4px 10px', borderBottom: `1px solid ${T.border}`,
            color: s.available ? T.text : T.label, fontSize: 10,
          }}>
            <span>{name}</span>
            <span>{s.price != null ? `$${s.price.toLocaleString()}` : '—'}</span>
            <span style={{ color: s.age_ms > 10000 ? '#f59e0b' : T.label2 }}>{s.age_ms}ms</span>
            <span style={{ color: s.available ? '#10b981' : '#ef4444' }}>{s.available ? 'OK' : 'DOWN'}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function RegimeMacroPanel({ snapshot }) {
  const tf5 = snapshot?.timescales?.['5m'];
  const macro = snapshot?.macro;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, fontFamily: T.font, fontSize: 11 }}>
      <div style={{ padding: 10, border: `1px solid ${T.border}`, borderRadius: 2 }}>
        <div style={{ color: T.label2, fontSize: 11, fontWeight: 600, marginBottom: 8 }}>Regime (5m)</div>
        <div style={{ color: T.text }}>regime: <b>{tf5?.regime ?? '—'}</b></div>
        <div style={{ color: T.text }}>confidence: <b>{tf5?.regime_confidence?.toFixed(3) ?? '—'}</b></div>
        <div style={{ color: T.text }}>persistence: <b>{tf5?.regime_persistence?.toFixed(0) ?? '—'}</b></div>
      </div>
      <div style={{ padding: 10, border: `1px solid ${T.border}`, borderRadius: 2 }}>
        <div style={{ color: T.label2, fontSize: 11, fontWeight: 600, marginBottom: 8 }}>Macro (top-level)</div>
        <div style={{ color: T.text }}>bias: <b>{macro?.bias ?? '—'}</b></div>
        <div style={{ color: T.text }}>gate: <b>{macro?.direction_gate ?? '—'}</b></div>
        <div style={{ color: T.text }}>confidence: <b>{macro?.confidence?.toFixed(3) ?? '—'}</b></div>
        <div style={{ color: T.label2, marginTop: 6, fontSize: 10, whiteSpace: 'pre-wrap' }}>
          {macro?.reasoning ?? '—'}
        </div>
      </div>
    </div>
  );
}

function RawSnapshotPanel() {
  return (
    <Suspense fallback={<Loading label="Loading V4 surface…" />}>
      <V4Surface />
    </Suspense>
  );
}

function EnvelopePanel() {
  return (
    <Suspense fallback={<Loading label="Loading envelope…" />}>
      <Assembler1 />
    </Suspense>
  );
}

// ── Page ────────────────────────────────────────────────────────────────
export default function Monitor() {
  const api = useApi();
  const { snapshot, error: snapError, lastFetchTs, buffer } = useSnapshotStream({ asset: 'BTC', timescales: '5m,15m,1h' });
  const [tab, setTab] = useState('snapshot');
  const [killOpen, setKillOpen] = useState(false);
  const [killedBanner, setKilledBanner] = useState(null);

  const { data: status } = usePolled(
    () => api.get('/api/system/status').then(r => r.data),
    10000,
    [api],
  );

  const isPaper = status?.engine_status === 'paper' || status?.data?.status === 'paper';
  const isKilled = status?.engine_status === 'killed' || status?.data?.status === 'killed' || killedBanner;

  return (
    <div>
      <PageHeader
        tag="LIVE MONITOR"
        title="v5_ensemble Monitor"
        subtitle="Real-time health of the BTC v5_ensemble LIVE strategy and ML box. Source: /v4/snapshot (2s) + hub."
        right={<AccuracyBadge api={api} />}
      />

      {isKilled ? (
        <div style={{
          padding: '10px 14px', marginBottom: 14, borderRadius: 2,
          background: 'rgba(239,68,68,0.08)', border: '1px solid #ef4444',
          color: '#fca5a5', fontFamily: T.font, fontSize: 11, fontWeight: 600,
        }}>
          ENGINE KILLED{killedBanner ? ` at ${new Date(killedBanner).toISOString().slice(11,19)}Z` : ''} —
          no new trades. Resume via /system page.
        </div>
      ) : isPaper ? (
        <div style={{
          padding: '8px 14px', marginBottom: 14, borderRadius: 2,
          background: 'rgba(245,158,11,0.08)', border: '1px solid #f59e0b',
          color: '#fbbf24', fontFamily: T.font, fontSize: 11, fontWeight: 600,
        }}>
          PAPER MODE — no real orders. Expected LIVE for v5_ensemble.
        </div>
      ) : null}

      <HealthStrip />

      {/* Tier 1 */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr) minmax(0, 1fr)',
        gap: 14, marginBottom: 18,
      }}>
        <EnsembleSignalCard snapshot={snapshot} error={snapError} lastFetchTs={lastFetchTs} />
        <CircuitBreakerCard snapshot={snapshot} />
        <TradeActivityCard api={api} />
      </div>

      {/* Tier 1b — classifier health row (saturation + WR head-to-head). */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 2fr)',
        gap: 14, marginBottom: 18,
      }}>
        <SaturationMeter buffer={buffer} />
        <ComparativeWRCard buffer={buffer} />
      </div>

      {/* Tier 2 — rolling buffer distributions + head-to-head line */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
        gap: 14, marginBottom: 14,
      }}>
        <LgbVsClsPlot buffer={buffer} />
        <DisagreementPlot buffer={buffer} />
      </div>
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
        gap: 14, marginBottom: 18,
      }}>
        <ClassifierHistogram buffer={buffer} />
        <ModeShareCard buffer={buffer} />
      </div>

      {/* Tier 3 tabs */}
      <div style={{
        background: T.card, border: `1px solid ${T.border}`, borderRadius: 4,
        padding: 14, marginBottom: 18,
      }}>
        <div style={{ display: 'flex', gap: 2, marginBottom: 14, borderBottom: `1px solid ${T.border}` }}>
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              onClick={() => setTab(t.id)}
              style={{
                padding: '8px 14px',
                background: 'transparent',
                border: 'none',
                borderBottom: `2px solid ${tab === t.id ? T.cyan : 'transparent'}`,
                color: tab === t.id ? T.text : T.label2,
                fontFamily: T.font, fontSize: 11, cursor: 'pointer',
                letterSpacing: '0.04em',
              }}
            >{t.label}</button>
          ))}
        </div>

        {tab === 'snapshot' ? <RawSnapshotPanel /> : null}
        {tab === 'envelope' ? <EnvelopePanel /> : null}
        {tab === 'decisions' ? <DecisionLogPanel api={api} /> : null}
        {tab === 'consensus' ? <ConsensusPanel snapshot={snapshot} /> : null}
        {tab === 'regime' ? <RegimeMacroPanel snapshot={snapshot} /> : null}
      </div>

      {/* Footer */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 14, padding: '12px 0',
        borderTop: `1px solid ${T.border}`,
        fontFamily: T.font, fontSize: 10, color: T.label2,
      }}>
        <button
          type="button"
          onClick={() => setKillOpen(true)}
          style={{
            padding: '10px 20px',
            background: '#ef4444', color: '#fff',
            fontFamily: T.font, fontSize: 11, fontWeight: 800,
            border: 'none', borderRadius: 2, cursor: 'pointer',
            letterSpacing: '0.12em',
          }}
        >
          ⚠ EMERGENCY KILL
        </button>
        <span>
          last snapshot: {lastFetchTs ? new Date(lastFetchTs).toISOString().slice(11,19) + 'Z' : '—'}
        </span>
      </div>
      <NotesTicker api={api} />

      <KillConfirmModal
        isOpen={killOpen}
        onClose={() => setKillOpen(false)}
        onKilled={() => setKilledBanner(Date.now())}
        systemStatus={status}
      />
    </div>
  );
}
