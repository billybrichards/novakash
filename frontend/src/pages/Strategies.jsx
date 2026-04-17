import React, { useMemo } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import Loading from '../components/shared/Loading.jsx';
import { T, wrColor } from '../theme/tokens.js';
import { computeWr as wrOf } from '../lib/wr.js';

const TIMEFRAMES = ['5m', '15m', '1h'];
const DAY_MS = 24 * 60 * 60 * 1000;

// Stable string form for equality + display. Handles objects/arrays safely;
// primitives pass through unchanged so numeric/string equality still works.
function valOf(v) {
  if (v == null) return v;
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return v;
}

function displayVal(v) {
  if (v == null) return '—';
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

function fmtUSD(n) {
  if (n == null) return '—';
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  const sign = v < 0 ? '-' : '';
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function tsOf(r) {
  const t = r.decided_at || r.ts || r.resolved_at || r.created_at;
  if (!t) return null;
  const ms = new Date(t).getTime();
  return Number.isFinite(ms) ? ms : null;
}

export default function Strategies() {
  const api = useApi();
  const strategies = useApiLoader(
    (s) => api.get('/api/strategies', { signal: s }).catch(() => ({ data: {} }))
  );
  const decisions = useApiLoader(
    (s) => api.get('/api/v58/strategy-decisions?limit=500', { signal: s })
  );

  // Fallback: if /api/strategies empty, derive from decisions.
  const strategyMap = useMemo(() => {
    const raw = strategies.data;
    if (raw && typeof raw === 'object' && !Array.isArray(raw) && Object.keys(raw).length > 0) {
      return raw;
    }
    const rows = Array.isArray(decisions.data) ? decisions.data : [];
    const m = {};
    for (const r of rows) {
      const id = r.strategy_id || r.strategy;
      if (!id || m[id]) continue;
      m[id] = { timeframe: r.timeframe || '5m', yaml: {}, runtime: {} };
    }
    return m;
  }, [strategies.data, decisions.data]);

  // Group strategy ids by timeframe.
  const groups = useMemo(() => {
    const g = {};
    for (const [id, meta] of Object.entries(strategyMap)) {
      const tf = (meta && meta.timeframe) || '5m';
      (g[tf] ??= []).push(id);
    }
    for (const tf of Object.keys(g)) g[tf].sort();
    return g;
  }, [strategyMap]);

  // Aggregate per-strategy perf from decisions rows. PnL scoped to 30d window
  // so it matches the "last 7d / 30d" header copy (not a lifetime sum).
  const perf = useMemo(() => {
    const rows = Array.isArray(decisions.data) ? decisions.data : [];
    const now = Date.now();
    const out = {};
    for (const r of rows) {
      const id = r.strategy_id || r.strategy;
      if (!id) continue;
      const bucket = (out[id] ??= { all: [], d7: [], d30: [], pnl30: 0, edgeSum: 0, edgeN: 0 });
      bucket.all.push(r);
      const ms = tsOf(r);
      const in30 = ms == null || now - ms <= 30 * DAY_MS;
      if (ms != null && now - ms <= 7 * DAY_MS) bucket.d7.push(r);
      if (in30) bucket.d30.push(r);
      if (in30) {
        const pnl = Number(r.pnl);
        if (Number.isFinite(pnl)) bucket.pnl30 += pnl;
      }
      const edge = r.edge != null ? Number(r.edge) : r.avg_edge != null ? Number(r.avg_edge) : NaN;
      if (Number.isFinite(edge)) {
        bucket.edgeSum += edge;
        bucket.edgeN += 1;
      }
    }
    const final = {};
    for (const [id, b] of Object.entries(out)) {
      const w7 = wrOf(b.d7);
      const w30 = wrOf(b.d30);
      final[id] = {
        wr7: w7.wr,
        n7: w7.n,
        wr30: w30.wr,
        n30: w30.n,
        pnl: b.pnl30,
        trades: b.all.length,
        avgEdge: b.edgeN > 0 ? b.edgeSum / b.edgeN : null,
      };
    }
    return final;
  }, [decisions.data]);

  // Union of yaml+runtime param keys across every strategy in a group.
  const paramKeysByTf = useMemo(() => {
    const out = {};
    for (const [tf, ids] of Object.entries(groups)) {
      const set = new Set();
      for (const id of ids) {
        const meta = strategyMap[id] || {};
        const yaml = meta.yaml && typeof meta.yaml === 'object' ? meta.yaml : {};
        const runtime = meta.runtime && typeof meta.runtime === 'object' ? meta.runtime : {};
        for (const k of Object.keys(yaml)) set.add(k);
        for (const k of Object.keys(runtime)) set.add(k);
      }
      out[tf] = Array.from(set).sort();
    }
    return out;
  }, [groups, strategyMap]);

  // Divergent = any value in `vals` differs from the most-common value.
  function isDivergent(vals) {
    const counts = new Map();
    for (const v of vals) {
      const key = valOf(v);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    if (counts.size <= 1) return false;
    let top = -1;
    for (const c of counts.values()) if (c > top) top = c;
    return Array.from(counts.values()).some(c => c !== top) || counts.size > 1;
  }

  const nothing =
    Object.keys(groups).length === 0 ||
    Object.values(groups).every(ids => ids.length === 0);

  return (
    <div>
      <PageHeader
        tag="STRATEGIES · /strategies"
        title="Strategies"
        subtitle="Per-timeframe side-by-side comparison: YAML params + live performance overlay."
        right={<div style={{ fontSize: 11, color: T.label2 }}>
          {Object.keys(strategyMap).length} strategies · {Array.isArray(decisions.data) ? decisions.data.length : 0} decisions
        </div>}
      />

      {decisions.error ? (
        <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>
          Decisions load error: {decisions.error}
        </div>
      ) : null}

      {nothing ? (
        strategies.loading || decisions.loading ? (
          <Loading label="Loading strategy registry…" />
        ) : decisions.error ? (
          <EmptyState
            message="Could not load strategy data."
            hint="Retry after the decisions endpoint recovers."
          />
        ) : (
          <EmptyState
            message="No strategies registered."
            hint="Check /api/strategies endpoint (audit-task #216) or strategy_decisions table."
          />
        )
      ) : null}

      {TIMEFRAMES.map(tf => {
        const ids = groups[tf];
        if (!ids || ids.length === 0) return null;
        const keys = paramKeysByTf[tf] || [];

        // Pre-compute per-key values across strategies for divergence detection.
        const rowVals = {};
        for (const k of keys) {
          rowVals[k] = ids.map(id => {
            const meta = strategyMap[id] || {};
            const yaml = meta.yaml && typeof meta.yaml === 'object' ? meta.yaml : {};
            const runtime = meta.runtime && typeof meta.runtime === 'object' ? meta.runtime : {};
            return runtime[k] !== undefined ? runtime[k] : yaml[k];
          });
        }

        return (
          <section key={tf} style={{ marginBottom: 24 }}>
            <h3 style={{
              fontSize: 12,
              letterSpacing: '0.15em',
              color: T.cyan,
              textTransform: 'uppercase',
              marginBottom: 8,
            }}>
              {tf} strategies · {ids.length}
            </h3>

            {/* LEFT: YAML params grid */}
            <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2, marginBottom: 8 }}>
              <div style={{ fontSize: 13, marginBottom: 8, color: T.label2 }}>YAML params</div>
              {keys.length === 0 ? (
                <EmptyState
                  message="No YAML params available for this timeframe."
                  hint="Strategy derived from decisions — register /api/strategies to populate."
                />
              ) : (
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
                    <thead>
                      <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
                        <th style={{ textAlign: 'left', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                          PARAM
                        </th>
                        {ids.map(id => (
                          <th key={id} style={{
                            textAlign: 'right',
                            padding: '7px 10px',
                            textTransform: 'uppercase',
                            fontWeight: 500,
                          }}>
                            {id}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {keys.map(k => {
                        const vals = rowVals[k];
                        const divergent = isDivergent(vals);
                        return (
                          <tr key={k} style={{ borderTop: `1px solid ${T.border}` }}>
                            <td style={{ padding: '7px 10px', color: T.label2 }}>{k}</td>
                            {vals.map((v, i) => {
                              const mark = divergent;
                              return (
                                <td key={ids[i]} style={{
                                  padding: '7px 10px',
                                  textAlign: 'right',
                                  fontVariantNumeric: 'tabular-nums',
                                  borderLeft: mark ? `2px solid ${T.warn}` : undefined,
                                  color: mark ? T.warn : undefined,
                                }}>
                                  {displayVal(v)}
                                </td>
                              );
                            })}
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* RIGHT: perf overlay */}
            <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
              <div style={{ fontSize: 13, marginBottom: 8, color: T.label2 }}>
                Performance · last 7d / 30d
              </div>
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
                  <thead>
                    <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
                      <th style={{ textAlign: 'left', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        STRATEGY
                      </th>
                      <th style={{ textAlign: 'right', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        WR 7D
                      </th>
                      <th style={{ textAlign: 'right', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        WR 30D
                      </th>
                      <th style={{ textAlign: 'right', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        NET PNL
                      </th>
                      <th style={{ textAlign: 'right', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        TRADES
                      </th>
                      <th style={{ textAlign: 'right', padding: '7px 10px', textTransform: 'uppercase', fontWeight: 500 }}>
                        AVG EDGE
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {ids.map(id => {
                      const p = perf[id] || {};
                      const wr7 = p.wr7;
                      const wr30 = p.wr30;
                      const pnl = Number.isFinite(p.pnl) ? p.pnl : null;
                      const pnlColor = pnl == null ? T.label : pnl >= 0 ? T.profit : T.loss;
                      return (
                        <tr key={id} style={{ borderTop: `1px solid ${T.border}` }}>
                          <td style={{ padding: '7px 10px' }}>{id}</td>
                          <td style={{
                            padding: '7px 10px',
                            textAlign: 'right',
                            fontVariantNumeric: 'tabular-nums',
                            color: wrColor(wr7),
                          }}>
                            {wr7 == null ? '—' : `${(wr7 * 100).toFixed(1)}%`}
                            {p.n7 ? <span style={{ color: T.label, fontSize: 10 }}> ({p.n7})</span> : null}
                          </td>
                          <td style={{
                            padding: '7px 10px',
                            textAlign: 'right',
                            fontVariantNumeric: 'tabular-nums',
                            color: wrColor(wr30),
                          }}>
                            {wr30 == null ? '—' : `${(wr30 * 100).toFixed(1)}%`}
                            {p.n30 ? <span style={{ color: T.label, fontSize: 10 }}> ({p.n30})</span> : null}
                          </td>
                          <td style={{
                            padding: '7px 10px',
                            textAlign: 'right',
                            fontVariantNumeric: 'tabular-nums',
                            color: pnlColor,
                          }}>
                            {fmtUSD(pnl)}
                          </td>
                          <td style={{
                            padding: '7px 10px',
                            textAlign: 'right',
                            fontVariantNumeric: 'tabular-nums',
                          }}>
                            {p.trades ?? 0}
                          </td>
                          <td style={{
                            padding: '7px 10px',
                            textAlign: 'right',
                            fontVariantNumeric: 'tabular-nums',
                            color: T.label2,
                          }}>
                            {p.avgEdge == null ? '—' : p.avgEdge.toFixed(3)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}
