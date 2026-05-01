// /desk — live gate-context tile.
//
// Surfaces the latest gate-evaluation chain from /api/gate-traces/recent
// for a single strategy (default = the LIVE one). Each gate row shows
//   • gate name
//   • pass/fail pill
//   • the observed numeric value (extracted from observed_json) — this is
//     what the operator wanted: "lots of data in gate context that
//     changes" surfaced inline.
//
// Server returns { groups: [{ strategy_id, window_ts, eval_offset,
// gates: [{gate_order, gate_name, passed, reason, observed_json, ... }] }] }.
//
// Strategy selector lives in /gate-traces (this is the compact /desk
// version). Defaults to LIVE_STRATEGY_ID so it "just works" without
// user input — the operator can always click through to /gate-traces for
// the heatmap drill-down.

import React, { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';
import {
  LIVE_STRATEGY_ID,
  DESK_ALL_STRATEGY_IDS,
  STRATEGIES,
  GATES,
} from '../../../constants/strategies.js';

const SELECT_OPTIONS = DESK_ALL_STRATEGY_IDS;

export default function GateContextTile({ pollMs = 8_000 }) {
  const api = useApi();
  const [strategyId, setStrategyId] = useState(LIVE_STRATEGY_ID);
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await api.get(
          `/api/gate-traces/recent?strategy_id=${encodeURIComponent(strategyId)}`
          + `&timeframe=5m&hours=1&limit=4`,
        );
        const body = r?.data ?? r;
        const next = Array.isArray(body?.groups) ? body.groups : [];
        if (!cancelled) {
          setGroups(next);
          setError(null);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e?.message || 'gate trace fetch failed');
          setLoading(false);
        }
      }
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, strategyId, pollMs]);

  const latest = groups[0] || null;
  const meta = STRATEGIES[strategyId] || null;
  const gates = useMemo(() => normaliseGates(latest), [latest]);

  return (
    <div style={panelStyle}>
      <div style={headerRow}>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <span style={{ letterSpacing: '0.1em', color: T.label }}>
            GATE CONTEXT · live evaluation
          </span>
          {meta?.gateLabel ? (
            <span style={{ color: T.label2, fontSize: 9 }}>{meta.gateLabel}</span>
          ) : null}
        </div>
        <select
          value={strategyId}
          onChange={(e) => setStrategyId(e.target.value)}
          style={selectStyle}
        >
          {SELECT_OPTIONS.map(sid => (
            <option key={sid} value={sid}>
              {STRATEGIES[sid]?.shortLabel || sid}
            </option>
          ))}
        </select>
      </div>

      {error ? <div style={{ color: T.loss, fontSize: 11 }}>{error}</div> : null}
      {loading && !groups.length ? (
        <div style={{ color: T.label, fontSize: 11 }}>Loading gate trace…</div>
      ) : null}

      {!loading && !latest ? (
        <div style={{ color: T.label2, fontSize: 11 }}>
          No gate evaluations in the last hour for this strategy.
          {strategyId && (STRATEGIES[strategyId]?.family !== 'lgb_only'
            && STRATEGIES[strategyId]?.family !== 'combo') ? null
            : ' (LGB-only / combo strategies often skip gates entirely.)'}
        </div>
      ) : null}

      {latest ? (
        <>
          <div style={metaRow}>
            <span>
              window {fmtTs(latest.window_ts)} · T-{latest.eval_offset}s
            </span>
            <span>
              {latest.action || '—'}
              {latest.direction ? ` · ${latest.direction}` : ''}
              {latest.mode ? ` · ${latest.mode}` : ''}
            </span>
          </div>
          {gates.length === 0 ? (
            <div style={{ color: T.label2, fontSize: 11, marginTop: 6 }}>
              Strategy ran with empty gate pipeline (custom Python evaluation).
            </div>
          ) : (
            <table style={tableStyle}>
              <thead>
                <tr style={{ color: T.label }}>
                  <th style={thStyle}>#</th>
                  <th style={thStyle}>Gate</th>
                  <th style={thStyle}>Pass</th>
                  <th style={thStyle}>Observed</th>
                  <th style={thStyle}>Why</th>
                </tr>
              </thead>
              <tbody>
                {gates.map(g => (
                  <tr key={`${g.gate_order}-${g.gate_name}`}>
                    <td style={tdStyle}>{g.gate_order}</td>
                    <td style={tdStyle}>
                      <span title={GATES[g.gate_name]?.description || ''}>
                        {GATES[g.gate_name]?.label || g.gate_name}
                      </span>
                    </td>
                    <td style={{
                      ...tdStyle,
                      color: g.passed ? T.profit : T.loss,
                      fontWeight: 600,
                    }}>
                      {g.passed ? '✓' : '✗'}
                    </td>
                    <td style={{ ...tdStyle, fontVariantNumeric: 'tabular-nums' }}>
                      {fmtObserved(g.observed)}
                    </td>
                    <td style={{ ...tdStyle, color: T.label2 }} title={g.reason || ''}>
                      {truncate(g.reason || g.skip_reason || '', 38)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </div>
  );
}

// Normalise the server's "gates" array (already ordered by gate_order). We
// also try to parse observed_json when the FE didn't already get it
// pre-decoded; the recent endpoint returns observed_text which we attempt
// JSON.parse on.
function normaliseGates(group) {
  if (!group) return [];
  const raw = Array.isArray(group.gates)
    ? group.gates
    : Array.isArray(group.rows) ? group.rows : [];
  return raw.map(g => {
    let observed = g.observed;
    if (observed == null && typeof g.observed_text === 'string') {
      try { observed = JSON.parse(g.observed_text); }
      catch { observed = null; }
    }
    if (observed == null && typeof g.observed_json === 'string') {
      try { observed = JSON.parse(g.observed_json); }
      catch { observed = null; }
    }
    return {
      gate_order: g.gate_order,
      gate_name: g.gate_name,
      passed: !!g.passed,
      reason: g.reason || null,
      skip_reason: g.skip_reason || null,
      observed,
    };
  }).sort((a, b) => (a.gate_order || 0) - (b.gate_order || 0));
}

function fmtObserved(obs) {
  if (obs == null) return '—';
  if (typeof obs === 'number') {
    if (Math.abs(obs) >= 1) return obs.toFixed(2);
    return obs.toFixed(4);
  }
  if (typeof obs === 'string') return obs;
  if (typeof obs === 'boolean') return obs ? 'true' : 'false';
  if (typeof obs === 'object') {
    // Pull the most-likely-relevant scalar — value, score, magnitude.
    for (const k of ['value', 'score', 'magnitude', 'delta', 'observed', 'price']) {
      if (k in obs && obs[k] != null && typeof obs[k] !== 'object') {
        return `${k}=${fmtObserved(obs[k])}`;
      }
    }
    // Fall back to a compact JSON-ish summary.
    const keys = Object.keys(obs).slice(0, 2);
    return keys.map(k => `${k}:${fmtObserved(obs[k])}`).join(' ');
  }
  return String(obs);
}

function fmtTs(unixS) {
  const n = Number(unixS);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const d = new Date(n * 1000);
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${hh}:${mm}Z`;
}

function truncate(s, n) {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

const panelStyle = {
  border: `1px solid ${T.border}`,
  padding: 10,
  background: T.card,
  marginBottom: 12,
};

const headerRow = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'flex-start',
  marginBottom: 8,
  fontSize: 10,
  gap: 8,
};

const metaRow = {
  display: 'flex',
  justifyContent: 'space-between',
  fontSize: 10,
  color: T.label2,
  marginBottom: 4,
};

const selectStyle = {
  background: 'transparent',
  border: `1px solid ${T.border}`,
  color: T.text,
  padding: '2px 4px',
  fontSize: 10,
  fontFamily: T.font,
};

const tableStyle = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 11,
  fontFamily: T.font,
  marginTop: 4,
};

const thStyle = { fontWeight: 500, padding: '2px 4px', letterSpacing: '0.05em', textAlign: 'left' };
const tdStyle = { padding: '3px 4px', borderTop: `1px solid ${T.border}` };
