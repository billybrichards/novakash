/**
 * WindowsTable.jsx — UI-04: Per-Window Aggregation View
 *
 * Fetches /v58/factory-windows and displays one row per 5-minute window,
 * collapsing the ~20-40 signal_evaluations rows into a single aggregated
 * row per window. Shows: time, asset, direction, outcome (WIN/LOSS/OPEN),
 * trade count, and PnL summary.
 *
 * Used on both FactoryFloor (pipeline tab) and ExecutionHQ (retro section).
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme tokens (match FactoryFloor T) ─────────────────────────────────────
const T = {
  bg:      '#07070c',
  card:    'rgba(255,255,255,0.018)',
  border:  'rgba(255,255,255,0.07)',
  purple:  '#a855f7',
  cyan:    '#06b6d4',
  profit:  '#4ade80',
  loss:    '#f87171',
  warning: '#f59e0b',
  label:   'rgba(255,255,255,0.35)',
  label2:  'rgba(255,255,255,0.55)',
  mono:    "'IBM Plex Mono', monospace",
};

// ─── Helpers ─────────────────────────────────────────────────────────────────
function fmt(v, decimals = 2) {
  if (v == null || isNaN(v)) return '\u2014';
  return Number(v).toFixed(decimals);
}

function utcHHMM(ts) {
  if (!ts) return '\u2014';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(11, 16);
}

function utcDate(ts) {
  if (!ts) return '';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(5, 10); // MM-DD
}

// ─── Result badge color ──────────────────────────────────────────────────────
function resultColor(result) {
  if (!result) return T.label;
  const r = result.toUpperCase();
  if (r === 'WIN') return T.profit;
  if (r === 'LOSS') return T.loss;
  return T.label; // SKIP / OPEN
}

function resultBg(result) {
  if (!result) return 'transparent';
  const r = result.toUpperCase();
  if (r === 'WIN') return 'rgba(74,222,128,0.08)';
  if (r === 'LOSS') return 'rgba(248,113,113,0.08)';
  return 'transparent';
}

/**
 * WindowsTable — self-contained component that fetches + renders the
 * per-window aggregation. Accepts optional asset/timeframe/limit props
 * to customise the query.
 *
 * @param {string}  [asset='btc']       - Asset slug
 * @param {string}  [timeframe='5m']    - Timeframe
 * @param {number}  [limit=50]          - Max windows to fetch
 * @param {boolean} [compact=false]     - Compact mode (fewer columns)
 * @param {string}  [title]             - Override section title
 */
export default function WindowsTable({
  asset = 'btc',
  timeframe = '5m',
  limit = 50,
  compact = false,
  title,
}) {
  const api = useApi();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchWindows = useCallback(async () => {
    try {
      const url = `/v58/factory-windows?asset=${encodeURIComponent(asset)}&timeframe=${encodeURIComponent(timeframe)}&limit=${limit}`;
      const res = await api('GET', url);
      const payload = res?.data || res;
      setData(payload);
      setError(null);
    } catch (err) {
      setError(err.message || 'Failed to fetch window data');
    } finally {
      setLoading(false);
    }
  }, [api, asset, timeframe, limit]);

  // Initial fetch + poll every 30s
  useEffect(() => {
    fetchWindows();
    const interval = setInterval(fetchWindows, 30000);
    return () => clearInterval(interval);
  }, [fetchWindows]);

  const windows = data?.windows || [];
  const summary = data?.summary || {};

  // ─── Summary strip ────────────────────────────────────────────────────────
  const summaryStrip = () => {
    const { total_windows, wins, losses, trades, skips, win_rate_pct } = summary;
    if (!total_windows) return null;

    return (
      <div style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 16,
        padding: '8px 12px',
        marginBottom: 10,
        background: 'rgba(168,85,247,0.06)',
        border: '1px solid rgba(168,85,247,0.2)',
        borderRadius: 6,
        fontSize: 11,
        fontFamily: T.mono,
        alignItems: 'center',
      }}>
        <span style={{ color: T.label2 }}>
          <strong style={{ color: '#fff' }}>{total_windows}</strong> windows
        </span>
        <span style={{ color: T.label2 }}>
          <strong style={{ color: '#fff' }}>{trades || 0}</strong> trades
        </span>
        <span style={{ color: T.label2 }}>
          <strong style={{ color: '#fff' }}>{skips || 0}</strong> skips
        </span>
        <span style={{ color: T.profit }}>
          <strong>{wins || 0}</strong> wins
        </span>
        <span style={{ color: T.loss }}>
          <strong>{losses || 0}</strong> losses
        </span>
        {win_rate_pct != null && (
          <span style={{
            color: win_rate_pct >= 55 ? T.profit : win_rate_pct >= 45 ? T.warning : T.loss,
            fontWeight: 700,
          }}>
            {win_rate_pct}% WR
          </span>
        )}
      </div>
    );
  };

  // ─── Loading / Error states ───────────────────────────────────────────────
  if (loading && !data) {
    return (
      <div style={{
        background: T.card,
        border: '1px solid ' + T.border,
        borderRadius: 10,
        padding: '14px 16px',
        fontFamily: T.mono,
      }}>
        <SectionLabel>{title || 'PER-WINDOW AGGREGATION'}</SectionLabel>
        <div style={{ fontSize: 11, color: T.label, padding: '10px 0' }}>
          Loading window aggregation...
        </div>
      </div>
    );
  }

  // ─── Main render ──────────────────────────────────────────────────────────
  const gridCols = compact
    ? '56px 42px 46px 50px 56px'
    : '56px 42px 46px 46px 50px 56px 50px 50px 1fr';

  return (
    <div style={{
      background: T.card,
      border: '1px solid ' + T.border,
      borderRadius: 10,
      padding: '14px 16px',
      fontFamily: T.mono,
    }}>
      <SectionLabel>{title || 'PER-WINDOW AGGREGATION'}</SectionLabel>

      {error && (
        <div style={{
          fontSize: 10, color: T.loss, marginBottom: 8,
          padding: '4px 8px', background: 'rgba(248,113,113,0.08)',
          borderRadius: 4,
        }}>
          {error}
        </div>
      )}

      {summaryStrip()}

      {windows.length === 0 ? (
        <div style={{ fontSize: 11, color: T.label, padding: '10px 0' }}>
          No window data available for {asset.toUpperCase()} {timeframe}.
        </div>
      ) : (
        <>
          {/* Header */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: gridCols,
            gap: 8,
            padding: '4px 0 6px',
            borderBottom: '1px solid ' + T.border,
            fontSize: 9,
            color: T.label,
            letterSpacing: '0.08em',
          }}>
            <span>TIME</span>
            <span>DIR</span>
            <span>ACTUAL</span>
            {!compact && <span>DECISION</span>}
            <span>RESULT</span>
            <span>EVALS</span>
            {!compact && <span>P(UP)</span>}
            {!compact && <span>VPIN</span>}
            {!compact && <span>BLOCKING GATE</span>}
          </div>

          {/* Rows — sorted by window_ts descending (API already returns this) */}
          {windows.map((w, i) => {
            const result = w.result || (w.final_decision === 'SKIP' ? 'SKIP' : null);
            const rColor = resultColor(result);
            const rowBg = resultBg(result);

            return (
              <div key={w.window_ts || i} style={{
                display: 'grid',
                gridTemplateColumns: gridCols,
                gap: 8,
                padding: '5px 0',
                borderBottom: '1px solid ' + T.border,
                fontSize: 10,
                background: rowBg,
              }}>
                {/* TIME */}
                <span style={{ color: T.label2 }} title={w.window_start || ''}>
                  {utcDate(w.window_ts)}{' '}{utcHHMM(w.window_ts)}
                </span>

                {/* DIR (engine direction) */}
                <span style={{
                  fontWeight: 600,
                  color: w.final_direction === 'UP' ? T.profit
                    : w.final_direction === 'DOWN' ? T.loss
                    : T.label,
                }}>
                  {w.final_direction || '\u2014'}
                </span>

                {/* ACTUAL (market outcome) */}
                <span style={{
                  fontWeight: 600,
                  color: w.actual_close_direction === 'UP' ? T.profit
                    : w.actual_close_direction === 'DOWN' ? T.loss
                    : T.label,
                }}>
                  {w.actual_close_direction || '\u2014'}
                </span>

                {/* DECISION (TRADE/SKIP) — full mode only */}
                {!compact && (
                  <span style={{
                    fontWeight: 600,
                    fontSize: 9,
                    color: w.final_decision === 'TRADE' ? T.cyan : T.label,
                  }}>
                    {w.final_decision || '\u2014'}
                  </span>
                )}

                {/* RESULT */}
                <span style={{
                  fontWeight: 700,
                  color: rColor,
                  padding: '0 4px',
                  borderRadius: 3,
                  background: result === 'WIN' ? 'rgba(74,222,128,0.12)'
                    : result === 'LOSS' ? 'rgba(248,113,113,0.12)'
                    : 'transparent',
                }}>
                  {result || '\u2014'}
                </span>

                {/* EVALS count */}
                <span style={{ color: T.label2, textAlign: 'center' }}>
                  {w.eval_count || '\u2014'}
                </span>

                {/* P(UP) — full mode only */}
                {!compact && (
                  <span style={{
                    color: w.v2_p_up_final != null
                      ? (w.v2_p_up_final >= 0.5 ? T.profit : T.loss)
                      : T.label,
                    fontSize: 9,
                  }}>
                    {w.v2_p_up_final != null ? fmt(w.v2_p_up_final, 3) : '\u2014'}
                  </span>
                )}

                {/* VPIN — full mode only */}
                {!compact && (
                  <span style={{ color: T.label2, fontSize: 9 }}>
                    {w.vpin_final != null ? fmt(w.vpin_final, 3) : '\u2014'}
                  </span>
                )}

                {/* BLOCKING GATE — full mode only */}
                {!compact && (
                  <span style={{
                    fontSize: 9,
                    color: w.first_blocking_gate ? T.warning : T.label,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }} title={w.blocking_reason || w.first_blocking_gate || ''}>
                    {w.blocking_reason || w.first_blocking_gate || '\u2014'}
                  </span>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}

// ─── SectionLabel (local, same style as FactoryFloor) ────────────────────────
function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 9,
      color: T.purple,
      letterSpacing: '0.14em',
      fontWeight: 700,
      fontFamily: T.mono,
      marginBottom: 10,
      textTransform: 'uppercase',
    }}>
      {children}
    </div>
  );
}
