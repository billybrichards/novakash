import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt } from './components/theme.js';

/**
 * Strategy History — changelog of strategy configuration changes.
 *
 * Sources:
 *   1. config_history table (DB-backed mode changes via UI/API)
 *   2. Static changelog entries (code deploys, threshold changes)
 *
 * Route: /polymarket/strategy-history
 */

// ── Static changelog (code changes not tracked in DB) ────────────────────────
const CODE_CHANGELOG = [
  {
    date: '2026-04-12 21:10',
    strategy: 'v4_down_only',
    change: 'CLOB sizing audit: bump 0.55-0.75 to 2.0x, skip <0.25 (53% WR), NULL CLOB → 1.5x',
    author: 'claude',
    type: 'sizing',
  },
  {
    date: '2026-04-12 20:55',
    strategy: 'v4_down_only',
    change: 'Confidence threshold relaxed from 0.12 to 0.10 (same 90.5% WR, more trades)',
    author: 'claude',
    type: 'threshold',
  },
  {
    date: '2026-04-12 20:21',
    strategy: 'v4_up_asian',
    change: 'Strategy created: UP-only, Asian session 23:00-02:59 UTC, dist 0.15-0.20, 81-99% WR',
    author: 'claude',
    type: 'created',
  },
  {
    date: '2026-04-12 20:21',
    strategy: 'v4_down_only',
    change: 'Registered alongside v4_up_asian. Both LIVE (direction-exclusive).',
    author: 'claude',
    type: 'mode',
  },
  {
    date: '2026-04-12 17:24',
    strategy: 'v4_down_only',
    change: 'Strategy created: DOWN filter + CLOB sizing, T-90-150, dist>=0.12. 90.3% WR (897K samples)',
    author: 'claude',
    type: 'created',
  },
  {
    date: '2026-04-12 17:24',
    strategy: 'v4_down_only',
    change: 'Timing gate: T-90 to T-150 only (validated sweet spot)',
    author: 'claude',
    type: 'gate',
  },
  {
    date: '2026-04-12 17:12',
    strategy: 'v4_down_only',
    change: 'Set as LIVE paper trading primary strategy. V4 Fusion → GHOST. V10 Gate → GHOST.',
    author: 'billy',
    type: 'mode',
  },
  {
    date: '2026-04-12 16:42',
    strategy: 'all',
    change: 'CLOB feed fix: paper mode now gets real Polymarket order book data (was returning NULL)',
    author: 'claude',
    type: 'fix',
  },
  {
    date: '2026-04-12 13:00',
    strategy: 'v4_fusion',
    change: 'V4 Fusion flipped to LIVE paper. V10 Gate → GHOST. First V4 paper session.',
    author: 'billy',
    type: 'mode',
  },
];

const TYPE_COLORS = {
  created: '#10b981',
  mode: '#06b6d4',
  threshold: '#f59e0b',
  sizing: '#f59e0b',
  gate: '#a855f7',
  fix: '#ef4444',
};

const STRAT_COLORS = {
  v4_down_only: '#10b981',
  v4_up_asian: '#f59e0b',
  v4_fusion: '#06b6d4',
  v10_gate: '#a855f7',
  all: '#64748b',
};

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: 12, fontFamily: T.mono, overflowY: 'auto',
  },
  card: {
    background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 4, padding: '12px 16px',
  },
  th: {
    padding: '6px 10px', textAlign: 'left', fontSize: 9,
    fontWeight: 600, letterSpacing: '0.06em', color: T.textMuted,
    borderBottom: `1px solid ${T.border}`, whiteSpace: 'nowrap',
    fontFamily: T.mono, textTransform: 'uppercase',
  },
  td: {
    padding: '6px 10px', fontSize: 10, fontFamily: T.mono,
    borderBottom: `1px solid rgba(51,65,85,0.3)`,
    color: T.text, verticalAlign: 'top',
  },
};

export default function StrategyHistory() {
  const api = useApi();
  const [dbHistory, setDbHistory] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const prev = document.title;
    document.title = 'Strategy History \u2014 Polymarket \u2014 Novakash';
    return () => { document.title = prev; };
  }, []);

  const fetchHistory = useCallback(async () => {
    try {
      // Fetch config history for strategy-related keys
      const res = await api('GET', '/v58/config?service=engine');
      const keys = (res?.data || res)?.keys || [];
      const stratKeys = keys.filter(k =>
        k.key?.includes('MODE') || k.key?.includes('ENABLED') ||
        k.key?.includes('OFFSET') || k.key?.includes('CONFIDENCE')
      );

      // For each key, fetch its history
      const allHistory = [];
      for (const k of stratKeys.slice(0, 20)) {
        try {
          const hRes = await api('GET', `/v58/config/history?service=engine&key=${k.key}&limit=10`);
          const entries = (hRes?.data || hRes)?.history || [];
          for (const e of entries) {
            allHistory.push({
              date: e.changed_at,
              strategy: k.key.includes('DOWN_ONLY') ? 'v4_down_only'
                : k.key.includes('UP_ASIAN') ? 'v4_up_asian'
                : k.key.includes('V4_FUSION') ? 'v4_fusion'
                : k.key.includes('V10') ? 'v10_gate'
                : 'engine',
              change: `${k.key}: ${e.previous_value || 'null'} → ${e.new_value}`,
              author: e.changed_by || 'system',
              type: 'mode',
              source: 'db',
            });
          }
        } catch (_) {}
      }
      setDbHistory(allHistory);
    } catch (_) {}
    setLoading(false);
  }, [api]);

  useEffect(() => { fetchHistory(); }, [fetchHistory]);

  // Merge DB history + code changelog, sort by date desc
  const allEntries = [
    ...CODE_CHANGELOG.map(e => ({ ...e, source: 'code' })),
    ...dbHistory,
  ].sort((a, b) => {
    const da = new Date(a.date);
    const db = new Date(b.date);
    return db - da;
  });

  return (
    <div style={S.page}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 16 }}>
        <h1 style={{ fontSize: 16, fontWeight: 700, color: T.text, margin: 0, fontFamily: T.mono }}>
          Strategy History
        </h1>
        <span style={{ fontSize: 10, color: T.textMuted }}>
          {allEntries.length} changes tracked
        </span>
        {loading && <span style={{ fontSize: 9, color: T.textDim }}>loading DB history...</span>}
      </div>

      {/* Current strategy status */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, marginBottom: 16 }}>
        {[
          { name: 'V4 Down-Only', id: 'v4_down_only', mode: 'LIVE', desc: 'DOWN, dist>=0.10, T-90-150, CLOB sizing' },
          { name: 'V4 Up Asian', id: 'v4_up_asian', mode: 'LIVE', desc: 'UP, Asian 23-02 UTC, dist 0.15-0.20' },
          { name: 'V4 Fusion', id: 'v4_fusion', mode: 'GHOST', desc: 'Full V4 surface, baseline' },
          { name: 'V10 Gate', id: 'v10_gate', mode: 'GHOST', desc: 'Legacy 8-gate pipeline' },
        ].map(s => (
          <div key={s.id} style={{
            ...S.card,
            borderLeft: `3px solid ${STRAT_COLORS[s.id]}`,
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
              <span style={{ fontSize: 10, fontWeight: 700, color: STRAT_COLORS[s.id] }}>{s.name}</span>
              <span style={{
                fontSize: 8, padding: '1px 5px', borderRadius: 2, fontWeight: 700,
                background: s.mode === 'LIVE' ? 'rgba(16,185,129,0.15)' : 'rgba(168,85,247,0.12)',
                color: s.mode === 'LIVE' ? '#10b981' : '#a855f7',
              }}>{s.mode}</span>
            </div>
            <div style={{ fontSize: 9, color: T.textMuted }}>{s.desc}</div>
          </div>
        ))}
      </div>

      {/* Changelog table */}
      <div style={{ ...S.card, padding: 0, overflow: 'auto', maxHeight: 600 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ background: T.headerBg, position: 'sticky', top: 0, zIndex: 1 }}>
              <th style={S.th}>Date</th>
              <th style={S.th}>Strategy</th>
              <th style={S.th}>Type</th>
              <th style={S.th}>Change</th>
              <th style={S.th}>Author</th>
              <th style={S.th}>Source</th>
            </tr>
          </thead>
          <tbody>
            {allEntries.map((e, i) => {
              const stratColor = STRAT_COLORS[e.strategy] || T.textMuted;
              const typeColor = TYPE_COLORS[e.type] || T.textMuted;
              return (
                <tr key={i} style={{ background: i % 2 === 0 ? 'transparent' : 'rgba(15,23,42,0.3)' }}>
                  <td style={{ ...S.td, whiteSpace: 'nowrap', color: T.textMuted, fontSize: 9 }}>
                    {typeof e.date === 'string' ? e.date.replace('T', ' ').slice(0, 16) : ''}
                  </td>
                  <td style={S.td}>
                    <span style={{ color: stratColor, fontWeight: 600, fontSize: 9 }}>
                      {e.strategy === 'v4_down_only' ? 'DOWN-Only'
                        : e.strategy === 'v4_up_asian' ? 'UP Asian'
                        : e.strategy === 'v4_fusion' ? 'V4 Fusion'
                        : e.strategy === 'v10_gate' ? 'V10 Gate'
                        : e.strategy}
                    </span>
                  </td>
                  <td style={S.td}>
                    <span style={{
                      fontSize: 8, padding: '1px 5px', borderRadius: 2,
                      background: `${typeColor}15`, color: typeColor, fontWeight: 600,
                    }}>
                      {e.type}
                    </span>
                  </td>
                  <td style={{ ...S.td, maxWidth: 400, whiteSpace: 'normal', lineHeight: 1.4 }}>
                    {e.change}
                  </td>
                  <td style={{ ...S.td, fontSize: 9, color: T.textMuted }}>{e.author}</td>
                  <td style={S.td}>
                    <span style={{
                      fontSize: 8, padding: '1px 4px', borderRadius: 2,
                      background: e.source === 'db' ? 'rgba(6,182,212,0.12)' : 'rgba(100,116,139,0.15)',
                      color: e.source === 'db' ? T.cyan : T.textDim,
                    }}>
                      {e.source === 'db' ? 'DB' : 'CODE'}
                    </span>
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
