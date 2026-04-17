import React, { useState } from 'react';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import EmptyState from '../components/shared/EmptyState.jsx';
import { T } from '../theme/tokens.js';

// Stable string form for equality + display. Handles objects/arrays safely;
// primitives pass through unchanged so numeric/string equality still works.
function valOf(v) {
  if (v == null) return v;
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return v;
}

// Accept multiple plausible response shapes. Normalize to:
//   [{ strategy, param, yaml, runtime, effective, source }]
function normalize(raw) {
  if (!raw) return [];
  // Shape A: already an array of trace rows.
  if (Array.isArray(raw)) return raw;
  // Shape B: { strategies: { v4_down: { yaml: {...}, runtime: {...}, effective: {...} } } }
  if (raw.strategies && typeof raw.strategies === 'object') {
    const rows = [];
    for (const [strategy, v] of Object.entries(raw.strategies)) {
      const yaml = v.yaml ?? {};
      const runtime = v.runtime ?? {};
      const effective = v.effective ?? {};
      const keys = new Set([...Object.keys(yaml), ...Object.keys(runtime), ...Object.keys(effective)]);
      for (const k of keys) {
        const yv = yaml[k];
        const rv = runtime[k];
        const ev = effective[k] ?? rv ?? yv;
        const yvS = valOf(yv);
        const rvS = valOf(rv);
        const evS = valOf(ev);
        let source = 'YAML';
        if (rv != null && evS === rvS) source = 'runtime';
        else if (yv != null && rv != null && evS === yvS && rvS !== yvS) source = 'YAML wins';
        rows.push({ strategy, param: k, yaml: yv, runtime: rv, effective: ev, source });
      }
    }
    return rows;
  }
  // Shape C: flat { v4_fusion.stake: 0.025, ... } — not useful; return [].
  return [];
}

const cell = (v) => {
  if (v == null) return <span style={{ color: T.label }}>—</span>;
  if (typeof v === 'object') {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
};

const tabBtnStyle = (active) => ({
  fontSize: 11,
  padding: '6px 14px',
  background: active ? 'rgba(168,85,247,0.15)' : 'transparent',
  border: `1px solid ${active ? T.purple : T.borderStrong}`,
  color: active ? T.text : T.label2,
  cursor: 'pointer',
  fontFamily: T.font,
  borderRadius: 2,
  letterSpacing: '0.08em',
  textTransform: 'uppercase',
});

function OverridesPane() {
  const { data: raw, error: err, loading } = useApiLoader(
    async (signal, api) => {
      let r;
      try {
        r = await api.get('/api/trading-config/trace', { signal });
      } catch (e) {
        if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') throw e;
        if (import.meta.env.DEV) console.warn('[ConfigOverrides] /api/trading-config/trace failed, falling through:', e?.message || e);
      }
      if (!r) {
        try {
          r = await api.get('/api/trading-config/resolve', { signal });
        } catch (e) {
          if (e?.name === 'AbortError' || e?.code === 'ERR_CANCELED') throw e;
          if (import.meta.env.DEV) console.warn('[ConfigOverrides] /api/trading-config/resolve failed, falling through:', e?.message || e);
        }
      }
      if (!r) r = await api.get('/api/trading-config', { signal });
      return r;
    }
  );

  const rows = normalize(raw);
  const conflicts = rows.filter(r => r.source === 'YAML wins');

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10, fontSize: 11, color: conflicts.length ? T.loss : T.label2 }}>
        {rows.length} keys · {conflicts.length} conflicts
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      {rows.length === 0 && !loading ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <EmptyState
            message="Backend didn't return a trace-compatible shape."
            hint="Tried /api/trading-config/trace, /api/trading-config/resolve, /api/trading-config. Add a trace endpoint on the hub to populate this page."
          />
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <DataTable
            columns={[
              { key: 'strategy', label: 'strategy' },
              { key: 'param', label: 'param' },
              { key: 'yaml', label: 'YAML', num: true, render: r => cell(r.yaml) },
              { key: 'runtime', label: 'runtime', num: true, render: r => cell(r.runtime) },
              { key: 'effective', label: 'effective', num: true, render: r => {
                const bad = r.source === 'YAML wins';
                return <span style={{ color: bad ? T.loss : undefined, fontWeight: bad ? 600 : undefined }}>{cell(r.effective)}</span>;
              }},
              { key: 'source', label: 'source', render: r => {
                if (r.source === 'runtime') return <span style={{ color: T.profit, fontSize: 10 }}>runtime</span>;
                if (r.source === 'YAML wins') return <span style={{ color: T.loss, fontSize: 10 }}>YAML WINS ⚠</span>;
                return <span style={{ color: T.label2, fontSize: 10 }}>YAML</span>;
              }},
            ]}
            rows={rows.map((r, i) => ({ ...r, _key: `${r.strategy}.${r.param}.${i}` }))}
          />
        </div>
      ) : null}
    </div>
  );
}

// Treat these axios error codes / HTTP statuses as "endpoint not shipped yet"
// so we render the polite EmptyState with the audit-task hint instead of a
// scary raw "Load error: 422" banner.
function isNotShippedError(err) {
  if (!err) return false;
  const s = String(err);
  return /status code (404|405|422|501)/.test(s);
}

function CapsPane() {
  const { data: raw, error: err, loading } = useApiLoader(
    (signal, api) => api.get('/api/trading-config/caps', { signal })
  );

  // Expected shape: array of { param, constants, yaml, runtime, env, effective, source, conflict }
  const rows = Array.isArray(raw) ? raw.filter(r => r && typeof r === 'object' && 'param' in r) : [];
  const conflicts = rows.filter(r => r.conflict === true);

  // Endpoint considered "not shipped yet" on 4xx → render EmptyState with audit hint.
  // Real 5xx / network errors keep the red banner.
  const notShipped = isNotShippedError(err);
  const notAvailable = !loading && rows.length === 0 && (notShipped || !err);

  return (
    <div>
      {rows.length > 0 ? (
        <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 10, fontSize: 11, color: conflicts.length ? T.loss : T.label2 }}>
          {rows.length} caps · {conflicts.length} conflicts
        </div>
      ) : null}

      {err && !notShipped ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}

      {notAvailable ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <EmptyState
            message="Caps endpoint not yet available."
            hint="Tracked by audit-task #216. Check back after backend lands."
          />
        </div>
      ) : null}

      {rows.length > 0 ? (
        <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
          <DataTable
            columns={[
              { key: 'param', label: 'param', render: r => (
                <span style={{
                  display: 'inline-block',
                  borderLeft: r.conflict === true ? `3px solid ${T.loss}` : 'none',
                  paddingLeft: r.conflict === true ? 8 : 0,
                  marginLeft: r.conflict === true ? -11 : 0,
                  color: r.conflict === true ? T.loss : undefined,
                  fontWeight: r.conflict === true ? 600 : undefined,
                }}>{cell(r.param)}</span>
              )},
              { key: 'constants', label: 'constants.py', num: true, render: r => cell(valOf(r.constants)) },
              { key: 'yaml', label: 'YAML', num: true, render: r => cell(valOf(r.yaml)) },
              { key: 'runtime', label: 'runtime', num: true, render: r => cell(valOf(r.runtime)) },
              { key: 'env', label: '.env', num: true, render: r => cell(valOf(r.env)) },
              { key: 'effective', label: 'effective', num: true, render: r => {
                const bad = r.conflict === true;
                return <span style={{ color: bad ? T.loss : undefined, fontWeight: bad ? 600 : undefined }}>{cell(valOf(r.effective))}</span>;
              }},
              { key: 'source', label: 'source', render: r => <span style={{ color: T.label2, fontSize: 10 }}>{cell(r.source)}</span> },
              { key: 'conflict', label: 'conflict', render: r => {
                if (r.conflict === true) return <span style={{ color: T.loss, fontSize: 10 }}>⚠ CONFLICT</span>;
                return <span style={{ color: T.label, fontSize: 10 }}>—</span>;
              }},
            ]}
            rows={rows.map((r, i) => ({ ...r, _key: `${r.param}.${i}` }))}
          />
        </div>
      ) : null}
    </div>
  );
}

export default function ConfigOverrides() {
  const [tab, setTab] = useState('overrides');

  return (
    <div>
      <PageHeader
        tag="CONFIG · /config"
        title="Config Overrides"
        subtitle="YAML vs runtime trace per (strategy, param), plus bet-size caps. Red cells flag silent overrides or conflicts."
      />

      <div role="tablist" aria-label="Config views" style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'overrides'}
          onClick={() => setTab('overrides')}
          style={tabBtnStyle(tab === 'overrides')}
        >
          Overrides
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'caps'}
          onClick={() => setTab('caps')}
          style={tabBtnStyle(tab === 'caps')}
        >
          Caps
        </button>
      </div>

      {tab === 'overrides' ? <OverridesPane /> : <CapsPane />}
    </div>
  );
}
