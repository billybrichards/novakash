// /desk — current-window strategy decisions panel.
//
// Pulls GET /api/v58/strategy-decisions?timeframe=5m and groups every BTC 5m
// strategy registered in DESK_TRACKED_FAMILIES (ensemble / lgb_only / combo).
// Single fetch — server returns one row per strategy_id; we dedup to the
// freshest evaluated_at per id, then render under each family heading.
//
// Columns: Strategy · Dir · Conviction (numeric + tier) · |pc−pl| · Stake ·
// Mode · Action. The skip_reason becomes the row tooltip so hovering reveals
// why a strategy didn't fire (cooldown, dist below floor, regime block, …).
//
// "No current decision" rows are intentionally rendered dimmed rather than
// hidden — the operator wants to see the full lineup, including who *isn't*
// firing this window.
//
// Spec: hub note #218 + the desk-rich-signals refactor.

import React, { useEffect, useMemo, useState } from 'react';
import { useApi } from '../../../hooks/useApi.js';
import { T } from '../../../theme/tokens.js';
import {
  DESK_TRACKED_FAMILIES,
  DESK_ALL_STRATEGY_IDS,
  STRATEGIES,
  getStrategyMeta,
} from '../../../constants/strategies.js';

const TRACKED_SET = new Set(DESK_ALL_STRATEGY_IDS);

export default function StrategyDecisions({ windowEpoch, pollMs = 4_000 }) {
  const api = useApi();
  const [rows, setRows] = useState([]);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        // limit=200 covers ~2-3 windows of decisions across the full lineup
        // (~12 strategies × 2 windows). Server already orders DESC.
        const r = await api.get('/api/v58/strategy-decisions?timeframe=5m&limit=200');
        const body = r?.data ?? r;
        const raw = Array.isArray(body?.rows)
          ? body.rows
          : Array.isArray(body?.decisions)
            ? body.decisions
            : Array.isArray(body) ? body : [];
        if (!cancelled) {
          setRows(raw);
          setError(null);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled) {
          setError(e.message || 'decisions fetch failed');
          setLoading(false);
        }
      }
    };
    load();
    const h = setInterval(load, pollMs);
    return () => { cancelled = true; clearInterval(h); };
  }, [api, pollMs]);

  const byStrategy = useMemo(() => {
    const m = new Map();
    for (const r of rows) {
      const sid = r.strategy_id;
      if (!sid || !TRACKED_SET.has(sid)) continue;
      const prev = m.get(sid);
      const t = new Date(r.evaluated_at ?? r.window_ts ?? 0).getTime();
      const pt = prev ? new Date(prev.evaluated_at ?? prev.window_ts ?? 0).getTime() : -1;
      if (!prev || t > pt) m.set(sid, r);
    }
    return m;
  }, [rows]);

  return (
    <div style={panelStyle}>
      <div style={headerStyle}>
        <span style={{ letterSpacing: '0.1em' }}>STRATEGY DECISIONS · BTC 5m</span>
        <span style={{ color: T.label2, fontSize: 9 }}>
          {byStrategy.size}/{DESK_ALL_STRATEGY_IDS.length} firing
        </span>
      </div>
      {error ? <div style={{ color: T.loss, fontSize: 11, marginBottom: 4 }}>{error}</div> : null}
      {loading && !rows.length
        ? <div style={{ color: T.label, fontSize: 11 }}>Loading…</div>
        : null}

      {DESK_TRACKED_FAMILIES.map(family => (
        <FamilyTable
          key={family.family}
          family={family}
          byStrategy={byStrategy}
        />
      ))}
    </div>
  );
}

function FamilyTable({ family, byStrategy }) {
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={familyHeaderStyle}>
        <span style={{ color: T.text }}>{family.label}</span>
        <span style={{ color: T.label2, fontSize: 9 }}>{family.description}</span>
      </div>
      <table style={tableStyle}>
        <thead>
          <tr style={{ color: T.label, textAlign: 'left' }}>
            <th style={thStyle}>Strategy</th>
            <th style={thStyle}>Dir</th>
            <th style={thStyle}>Conv</th>
            <th style={thStyleN}>|pc−pl|</th>
            <th style={thStyleN}>Stake</th>
            <th style={thStyle}>Mode</th>
            <th style={thStyle}>Action</th>
          </tr>
        </thead>
        <tbody>
          {family.ids.map(sid => {
            const meta = STRATEGIES[sid] || getStrategyMeta(sid);
            const r = byStrategy.get(sid);
            if (!r) {
              return (
                <tr key={sid}>
                  <td style={tdLabel(meta.color)}>
                    <span style={{ color: meta.color }}>■ </span>{meta.shortLabel || sid}
                  </td>
                  <td style={{ ...tdStyle, color: T.label2 }} colSpan={6}>
                    no current decision
                  </td>
                </tr>
              );
            }
            const dir = r.direction;
            const dirColour = dir === 'UP' ? T.profit : dir === 'DOWN' ? T.loss : T.label;
            const stake = num(r.entry_cap) ?? num(r.collateral_pct);
            const action = r.action || (r.executed ? 'EXEC' : r.skip_reason ? 'SKIP' : '?');
            const confTier = r.confidence || '';
            const confScore = num(r.confidence_score);
            const conv = confScore != null
              ? `${confTier ? confTier + ' · ' : ''}${confScore.toFixed(2)}`
              : confTier || '—';
            const pcMinusPl = num(r.pc_minus_pl)
              ?? num(r.disagreement_magnitude)
              ?? num(r.classifier_lgb_gap);
            const skipReason = r.skip_reason || '';

            return (
              <tr key={sid} title={skipReason || conv}>
                <td style={tdLabel(meta.color)}>
                  <span style={{ color: meta.color }}>■ </span>{meta.shortLabel || sid}
                </td>
                <td style={{ ...tdStyle, color: dirColour, fontWeight: 600 }}>
                  {dir || '—'}
                </td>
                <td style={tdStyle}>{conv}</td>
                <td style={tdStyleN}>
                  {pcMinusPl != null
                    ? <span style={{ color: pcMinusPl > 0.25 ? T.loss : T.text }}>
                        {pcMinusPl.toFixed(3)}
                      </span>
                    : '—'}
                </td>
                <td style={tdStyleN}>{stake != null ? stake.toFixed(2) : '—'}</td>
                <td style={tdStyle}>{r.mode || '—'}</td>
                <td style={{ ...tdStyle, color: action === 'SKIP' ? T.label2 : T.text }}>
                  {action}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const panelStyle = {
  border: `1px solid ${T.border}`,
  padding: 10,
  background: T.card,
  marginBottom: 12,
};

const headerStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  fontSize: 10,
  color: T.label,
  marginBottom: 8,
};

const familyHeaderStyle = {
  display: 'flex',
  justifyContent: 'space-between',
  fontSize: 10,
  letterSpacing: '0.05em',
  marginBottom: 4,
  paddingBottom: 2,
  borderBottom: `1px solid ${T.border}`,
};

const tableStyle = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 11,
  fontFamily: T.font,
};

const thStyle = { fontWeight: 500, padding: '2px 4px', letterSpacing: '0.05em' };
const thStyleN = { ...thStyle, textAlign: 'right' };
const tdStyle = { padding: '3px 4px', borderTop: `1px solid ${T.border}` };
const tdStyleN = { ...tdStyle, textAlign: 'right', fontVariantNumeric: 'tabular-nums' };
const tdLabel = (_color) => ({ ...tdStyle, fontWeight: 500 });

function num(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
