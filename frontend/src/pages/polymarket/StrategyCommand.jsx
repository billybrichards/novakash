import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T, fmt, utcHHMM, pct } from './components/theme.js';
import {
  STRATEGIES, STRATEGY_LIST, GATES, STRATEGY_GATES,
  DATA_SOURCES, getStrategyMeta,
} from '../../constants/strategies.js';
import {
  Activity, ChevronDown, ChevronRight, Clock, Cpu, Eye, Filter,
  Layers, Radio, Shield, Terminal, TrendingDown, TrendingUp, Zap,
} from 'lucide-react';

/**
 * Strategy Command Center -- The central dashboard for Strategy Engine v2.
 *
 * Layout: 3-column (left selector + center data + right execution)
 * with a bottom strip for orders/data health.
 *
 * Route: /polymarket/command
 */

// ── Constants ───────────────────────────────────────────────────────────────

const POLL_FAST = 5000;
const POLL_MED = 10000;
const POLL_SLOW = 30000;

const MODE_COLORS = {
  LIVE: { bg: 'rgba(20,184,166,0.15)', text: '#14b8a6', border: 'rgba(20,184,166,0.4)' },
  GHOST: { bg: 'rgba(168,85,247,0.12)', text: '#a855f7', border: 'rgba(168,85,247,0.3)' },
  PAPER: { bg: 'rgba(245,158,11,0.12)', text: '#f59e0b', border: 'rgba(245,158,11,0.3)' },
  DISABLED: { bg: 'rgba(100,116,139,0.12)', text: '#64748b', border: 'rgba(100,116,139,0.2)' },
  OFF: { bg: 'rgba(100,116,139,0.12)', text: '#64748b', border: 'rgba(100,116,139,0.2)' },
};

const DIR_COLORS = { UP: '#14b8a6', DOWN: '#f43f5e', ANY: '#06b6d4' };

const TAB_KEYS = ['signal', 'gates', 'decisions', 'outcomes'];
const TAB_LABELS = { signal: 'Signal Surface', gates: 'Gate Detail', decisions: 'Decisions', outcomes: 'Outcomes' };
const BOTTOM_TABS = ['orders', 'positions', 'windows', 'health'];
const BOTTOM_LABELS = { orders: 'Orders', positions: 'Positions', windows: 'Windows', health: 'Data Health' };

// ── Inject keyframes ────────────────────────────────────────────────────────

if (typeof document !== 'undefined' && !document.getElementById('cmd-center-styles')) {
  const style = document.createElement('style');
  style.id = 'cmd-center-styles';
  style.textContent = `
    @keyframes cmd-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
    @keyframes cmd-glow { 0%, 100% { box-shadow: 0 0 4px rgba(20,184,166,0.3); } 50% { box-shadow: 0 0 12px rgba(20,184,166,0.6); } }
    @keyframes cmd-fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
    .cmd-scroll::-webkit-scrollbar { width: 3px; }
    .cmd-scroll::-webkit-scrollbar-track { background: transparent; }
    .cmd-scroll::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }
  `;
  document.head.appendChild(style);
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtNum(v, dec = 2) {
  if (v == null || isNaN(v)) return '\u2014';
  return Number(v).toFixed(dec);
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return '\u2014';
  return (Number(v) * 100).toFixed(1) + '%';
}

function fmtUsd(v) {
  if (v == null || isNaN(v)) return '\u2014';
  const n = Number(v);
  if (Math.abs(n) >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return '$' + (n / 1e3).toFixed(1) + 'K';
  return '$' + n.toFixed(2);
}

function fmtTs(ts) {
  if (!ts) return '\u2014';
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return d.toISOString().slice(11, 19) + 'Z';
}

function fmtCountdown(sec) {
  if (sec == null) return '\u2014';
  const s = Math.max(0, Math.round(sec));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

function dirColor(dir) {
  return DIR_COLORS[dir] || '#64748b';
}

function ago(ts) {
  if (!ts) return null;
  const d = new Date(typeof ts === 'number' ? ts * 1000 : ts);
  return Math.round((Date.now() - d.getTime()) / 1000);
}

function freshness(secs) {
  if (secs == null) return { color: '#64748b', label: '?' };
  if (secs < 5) return { color: '#14b8a6', label: `${secs}s` };
  if (secs < 15) return { color: '#f59e0b', label: `${secs}s` };
  return { color: '#f43f5e', label: `${secs}s` };
}

function extractGateResults(decision) {
  if (!decision) return {};
  const meta = decision.metadata_json || decision.metadata || {};
  const raw = meta.gate_results || meta.gates;
  if (!raw) return {};
  // Normalize array format: [{gate, passed, reason}, ...]
  if (Array.isArray(raw)) {
    const obj = {};
    raw.forEach(item => {
      if (item && item.gate) {
        obj[item.gate] = item.passed === true ? true : item.passed === false ? false : item.passed;
      }
    });
    return obj;
  }
  return raw;
}

function extractGateReasons(decision) {
  if (!decision) return {};
  const meta = decision.metadata_json || decision.metadata || {};
  const raw = meta.gate_results || meta.gates;
  if (!raw || !Array.isArray(raw)) return {};
  const obj = {};
  raw.forEach(item => {
    if (item && item.gate && item.reason) obj[item.gate] = item.reason;
  });
  return obj;
}

function extractCtx(decision) {
  if (!decision) return {};
  const meta = decision.metadata_json || decision.metadata || {};
  return meta._ctx || meta.ctx || meta.context || meta.data_surface || {};
}

// ── Colors ──────────────────────────────────────────────────────────────────

const C = {
  bg: '#020617',
  panel: 'rgba(15,23,42,0.85)',
  panelBorder: 'rgba(51,65,85,0.6)',
  header: '#0f172a',
  teal: '#14b8a6',
  tealDim: 'rgba(20,184,166,0.12)',
  rose: '#f43f5e',
  roseDim: 'rgba(244,63,94,0.12)',
  amber: '#f59e0b',
  amberDim: 'rgba(245,158,11,0.12)',
  green: '#10b981',
  greenDim: 'rgba(16,185,129,0.12)',
  purple: '#a855f7',
  text: '#cbd5e1',
  muted: '#64748b',
  dim: '#475569',
  mono: "'JetBrains Mono', 'IBM Plex Mono', 'Fira Code', monospace",
};

// ── Sub-Components ──────────────────────────────────────────────────────────

function ModeBadge({ mode }) {
  const mc = MODE_COLORS[mode] || MODE_COLORS.DISABLED;
  return (
    <span style={{
      fontSize: 8, fontWeight: 700, padding: '2px 8px', borderRadius: 3,
      background: mc.bg, color: mc.text, border: `1px solid ${mc.border}`,
      letterSpacing: '0.08em', fontFamily: C.mono,
    }}>
      {mode || 'OFF'}
    </span>
  );
}

function DirBadge({ dir }) {
  const color = dirColor(dir);
  return (
    <span style={{
      fontSize: 9, fontWeight: 700, color,
      fontFamily: C.mono, display: 'inline-flex', alignItems: 'center', gap: 3,
    }}>
      {dir === 'UP' ? '\u25B2' : dir === 'DOWN' ? '\u25BC' : '\u25C6'} {dir || 'ANY'}
    </span>
  );
}

function GateChip({ name, status, reason }) {
  const gate = GATES[name] || { label: name, icon: '?' };
  const colors = {
    pass: { bg: 'rgba(20,184,166,0.12)', text: C.teal, border: 'rgba(20,184,166,0.3)' },
    fail: { bg: 'rgba(244,63,94,0.12)', text: C.rose, border: 'rgba(244,63,94,0.3)' },
    skip: { bg: 'rgba(100,116,139,0.1)', text: C.dim, border: 'rgba(100,116,139,0.15)' },
  };
  const c = colors[status] || colors.skip;
  const statusIcon = status === 'pass' ? '\u2705' : status === 'fail' ? '\u274C' : null;
  const tooltipText = reason ? `${gate.label}: ${reason}` : gate.label;
  return (
    <div
      title={tooltipText}
      style={{
        display: 'flex', flexDirection: 'column', gap: 1,
        padding: '3px 8px', borderRadius: 3,
        background: c.bg, border: `1px solid ${c.border}`,
        fontSize: 8, fontWeight: 600, fontFamily: C.mono,
        color: c.text, whiteSpace: 'nowrap',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span style={{ fontSize: 10 }}>{gate.icon}</span>
        <span>{gate.label}</span>
        {statusIcon && <span style={{ fontSize: 9 }}>{statusIcon}</span>}
        {!statusIcon && status !== 'skip' && (
          <span style={{ fontSize: 7, opacity: 0.8 }}>
            {status === 'pass' ? 'PASS' : 'FAIL'}
          </span>
        )}
      </div>
      {reason && (
        <div style={{
          fontSize: 7, color: c.text, opacity: 0.75,
          fontWeight: 400, whiteSpace: 'normal', lineHeight: 1.3,
          maxWidth: 160,
        }}>
          {reason}
        </div>
      )}
    </div>
  );
}

function SectionLabel({ children, icon: Icon }) {
  return (
    <div style={{
      fontSize: 9, fontWeight: 700, color: C.teal,
      letterSpacing: '0.1em', textTransform: 'uppercase',
      marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6,
      fontFamily: C.mono,
    }}>
      {Icon && <Icon size={12} />}
      {children}
    </div>
  );
}

function Panel({ children, style: extraStyle }) {
  return (
    <div style={{
      background: C.panel, border: `1px solid ${C.panelBorder}`,
      borderRadius: 6, padding: 12, ...extraStyle,
    }}>
      {children}
    </div>
  );
}

function ActionBadge({ action }) {
  const isT = action === 'TRADE';
  return (
    <span style={{
      fontSize: 9, fontWeight: 700, padding: '2px 10px', borderRadius: 3,
      fontFamily: C.mono,
      background: isT ? C.greenDim : C.roseDim,
      color: isT ? C.green : (action === 'ERROR' ? C.rose : C.muted),
    }}>
      {action || 'SKIP'}
    </span>
  );
}

// ── Live BTC Price Banner ───────────────────────────────────────────────────

function LiveBtcBanner({ hqData, latestCtxPrice }) {
  // btc_price is close_price of current window — null while window is open.
  // Fall back to current_price from latest decision _ctx, then open_price.
  const rawBtcPrice = hqData?.btc_price ?? hqData?.current_price ?? null;
  const ctxPrice = latestCtxPrice ?? null;
  const btcPrice = rawBtcPrice ?? ctxPrice;
  const openPrice = hqData?.current_window?.open_price ?? hqData?.open_price ?? null;
  const priceLabel = rawBtcPrice == null && ctxPrice != null ? '(live)' : null;

  const delta = (btcPrice != null && openPrice != null) ? btcPrice - openPrice : null;
  const deltaPct = (delta != null && openPrice) ? (delta / openPrice) * 100 : null;
  const isUp = delta != null ? delta >= 0 : null;
  const deltaColor = isUp === true ? C.teal : isUp === false ? C.rose : C.muted;

  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 16,
      padding: '8px 12px',
      background: 'rgba(15,23,42,0.7)',
      borderBottom: `1px solid ${C.panelBorder}`,
      flexShrink: 0,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span style={{ fontSize: 9, color: C.muted, fontFamily: C.mono }}>BTC</span>
        <span style={{
          fontSize: 18, fontWeight: 800, fontFamily: C.mono, color: C.text,
          letterSpacing: '-0.02em',
        }}>
          {btcPrice != null
            ? '$' + Number(btcPrice).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
            : '\u2014'}
        </span>
        {priceLabel && (
          <span style={{ fontSize: 8, color: C.amber, fontFamily: C.mono }}>{priceLabel}</span>
        )}
      </div>
      {delta != null && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span style={{ fontSize: 10, fontWeight: 700, color: deltaColor, fontFamily: C.mono }}>
            {isUp ? '+' : ''}{delta >= 0 ? '$' + delta.toFixed(2) : '-$' + Math.abs(delta).toFixed(2)}
          </span>
          <span style={{
            fontSize: 9, color: deltaColor, fontFamily: C.mono,
            background: isUp ? 'rgba(20,184,166,0.1)' : 'rgba(244,63,94,0.1)',
            padding: '1px 6px', borderRadius: 2,
          }}>
            ({isUp ? '+' : ''}{deltaPct.toFixed(2)}%)
          </span>
          <span style={{ fontSize: 8, color: C.muted, fontFamily: C.mono }}>vs open</span>
        </div>
      )}
      {openPrice != null && (
        <span style={{ fontSize: 9, color: C.dim, fontFamily: C.mono }}>
          Open: ${Number(openPrice).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </span>
      )}
    </div>
  );
}

// ── Signal Surface Tab ──────────────────────────────────────────────────────

function SignalSurfaceTab({ ctx, strategyId, hqData, decisions }) {
  // Merge hqData fields into ctx so regime/vpin show even without full surface.
  // Also pull _ctx from latest decision (any strategy) when own ctx is missing.
  // Must be declared before any conditional returns (Rules of Hooks)
  const enrichedCtx = useMemo(() => {
    // Start with provided ctx, then try to pull _ctx from latest decision
    let base = { ...(ctx || {}) };

    // If ctx is sparse, enrich from the most recent decision's _ctx
    if (decisions?.length) {
      const latestAny = decisions[0]; // decisions sorted newest-first
      const latestCtxAny = extractCtx(latestAny);
      if (Object.keys(base).length === 0 && latestCtxAny && Object.keys(latestCtxAny).length > 0) {
        base = { ...latestCtxAny };
      }
    }

    if (hqData) {
      if (base.regime == null && (hqData.vpin_regime || hqData.regime)) {
        base.regime = hqData.vpin_regime || hqData.regime;
      }
      if (base.vpin == null && hqData.vpin != null) base.vpin = hqData.vpin;
      if (base.current_price == null && hqData.btc_price != null) base.current_price = hqData.btc_price;
      if (base.current_price == null && hqData.open_price != null) base.current_price = hqData.open_price;
    }
    return base;
  }, [ctx, hqData, decisions]);

  // Build a live-data fallback surface from execution-hq when no ctx
  const hasCtx = enrichedCtx && Object.keys(enrichedCtx).length > 0;

  if (!hasCtx) {
    const btc = hqData?.btc_price ?? hqData?.current_price;
    const open = hqData?.current_window?.open_price ?? hqData?.open_price;
    const delta = (btc != null && open != null) ? btc - open : null;
    const deltaPct = (delta != null && open) ? (delta / open) * 100 : null;
    const isUp = delta != null ? delta >= 0 : null;
    const deltaColor = isUp === true ? C.teal : isUp === false ? C.rose : C.muted;

    const vpin = hqData?.vpin;
    const vpinRegime = hqData?.vpin_regime ?? hqData?.regime;
    const clobUpAsk = hqData?.clob_up_ask;
    const clobDownAsk = hqData?.clob_down_ask;
    const gammaUp = hqData?.gamma_up;
    const gammaDown = hqData?.gamma_down;

    const clobImplied = (clobUpAsk != null && clobDownAsk != null)
      ? (clobUpAsk + clobDownAsk) : null;
    const clobBalance = clobImplied != null
      ? Math.abs(clobImplied - 1.0) < 0.02 ? 'Balanced'
        : clobUpAsk > clobDownAsk ? 'DN bias' : 'UP bias'
      : null;

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div style={{
          fontSize: 8, color: C.amber, fontFamily: C.mono,
          padding: '6px 10px', background: 'rgba(245,158,11,0.06)',
          border: '1px solid rgba(245,158,11,0.15)', borderRadius: 3,
        }}>
          Live fallback — strategy_decisions table empty. Showing execution-hq data.
        </div>

        {(btc != null || vpin != null || clobUpAsk != null) && (
          <div style={{
            background: 'rgba(15,23,42,0.5)', border: `1px solid ${C.panelBorder}`,
            borderRadius: 4, padding: '10px 12px',
            display: 'flex', flexDirection: 'column', gap: 4,
          }}>
            <div style={{
              fontSize: 7, fontWeight: 700, color: C.teal, letterSpacing: '0.08em',
              marginBottom: 6, fontFamily: C.mono, borderBottom: `1px solid ${C.panelBorder}`,
              paddingBottom: 4,
            }}>
              LIVE DATA SURFACE
            </div>

            {btc != null && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', fontFamily: C.mono, fontSize: 10 }}>
                <span style={{ color: C.text }}>
                  BTC: <span style={{ fontWeight: 700 }}>
                    ${Number(btc).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </span>
                </span>
                {open != null && (
                  <span style={{ color: C.muted }}>
                    Open: ${Number(open).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </span>
                )}
                {delta != null && (
                  <span style={{ color: deltaColor, fontWeight: 700 }}>
                    {'\u0394'} {isUp ? '+' : ''}{delta >= 0 ? '$' + delta.toFixed(2) : '-$' + Math.abs(delta).toFixed(2)}
                    {' '}({isUp ? '+' : ''}{deltaPct.toFixed(2)}%)
                  </span>
                )}
              </div>
            )}

            {(vpin != null || vpinRegime) && (
              <div style={{ fontFamily: C.mono, fontSize: 10, color: C.text }}>
                VPIN: <span style={{ fontWeight: 700, color: vpin > 0.7 ? C.rose : vpin > 0.55 ? C.amber : C.teal }}>
                  {vpin != null ? Number(vpin).toFixed(3) : '\u2014'}
                </span>
                {vpinRegime && (
                  <span style={{ marginLeft: 8, color: C.purple, fontWeight: 700 }}>| {vpinRegime}</span>
                )}
              </div>
            )}

            {(clobUpAsk != null || clobDownAsk != null) && (
              <div style={{ fontFamily: C.mono, fontSize: 10, color: C.text }}>
                CLOB:{' '}
                {clobUpAsk != null && (
                  <span>
                    <span style={{ color: C.teal }}>
                      {'\u2191'}${Number(clobUpAsk).toFixed(3)}
                    </span>
                    {' '}
                  </span>
                )}
                {clobDownAsk != null && (
                  <span>
                    <span style={{ color: C.rose }}>
                      {'\u2193'}${Number(clobDownAsk).toFixed(3)}
                    </span>
                    {' '}
                  </span>
                )}
                {clobBalance && <span style={{ color: C.muted }}>{clobBalance}</span>}
              </div>
            )}

            {(gammaUp != null || gammaDown != null) && (
              <div style={{ fontFamily: C.mono, fontSize: 10, color: C.text }}>
                Gamma:{' '}
                {gammaUp != null && <span style={{ color: C.teal }}>UP {Number(gammaUp).toFixed(3)}{' '}</span>}
                {gammaDown != null && <span style={{ color: C.rose }}>DN {Number(gammaDown).toFixed(3)}</span>}
              </div>
            )}
          </div>
        )}

        <div style={{ padding: 16, textAlign: 'center', color: C.muted, fontSize: 11, fontFamily: C.mono }}>
          Full signal surface will populate once strategy evaluations begin.
        </div>
      </div>
    );
  }

  const stratGates = STRATEGY_GATES[strategyId] || [];
  const gateFields = new Set();
  const GATE_FIELD_MAP = {
    timing: ['eval_offset', 'seconds_to_close'],
    direction: ['poly_direction', 'v2_probability_up'],
    confidence: ['poly_confidence_distance', 'poly_confidence', 'v2_probability_up'],
    session_hours: ['hour_utc'],
    clob_sizing: ['clob_up_ask', 'clob_down_ask', 'clob_up_bid', 'clob_down_bid', 'clob_implied_up'],
    source_agreement: ['delta_binance', 'delta_tiingo', 'delta_chainlink'],
    delta_magnitude: ['delta_pct', 'delta_binance', 'delta_tiingo'],
    taker_flow: ['cg_taker_buy_vol', 'cg_taker_sell_vol'],
    cg_confirmation: ['cg_oi_usd', 'cg_liq_total', 'cg_funding_rate'],
    spread: ['clob_up_bid', 'clob_up_ask', 'clob_down_bid', 'clob_down_ask'],
    dynamic_cap: ['clob_up_ask', 'clob_down_ask'],
    regime: ['vpin', 'regime'],
    macro_direction: ['v4_macro_bias', 'v4_macro_direction_gate'],
    trade_advised: ['poly_trade_advised', 'poly_direction'],
  };
  stratGates.forEach(g => {
    (GATE_FIELD_MAP[g] || []).forEach(f => gateFields.add(f));
  });
  const isHighlighted = (field) => gateFields.has(field);

  const sections = [
    {
      title: 'V2 PREDICTIONS', fields: [
        { key: 'v2_probability_up', label: 'prob_up', fmt: v => fmtNum(v, 3) },
        { key: 'v2_probability_raw', label: 'prob_raw', fmt: v => fmtNum(v, 3) },
        { key: 'v2_quantiles_p10', label: 'Q10', fmt: v => fmtNum(v, 0) },
        { key: 'v2_quantiles_p50', label: 'Q50', fmt: v => fmtNum(v, 0) },
        { key: 'v2_quantiles_p90', label: 'Q90', fmt: v => fmtNum(v, 0) },
      ],
    },
    {
      title: 'V3 MULTI-HORIZON', fields: [
        { key: 'v3_5m_composite', label: '5m' },
        { key: 'v3_15m_composite', label: '15m' },
        { key: 'v3_1h_composite', label: '1h' },
        { key: 'v3_4h_composite', label: '4h' },
        { key: 'v3_24h_composite', label: '24h' },
        { key: 'v3_48h_composite', label: '48h' },
        { key: 'v3_72h_composite', label: '72h' },
        { key: 'v3_1w_composite', label: '1w' },
        { key: 'v3_2w_composite', label: '2w' },
      ],
    },
    {
      title: 'V3 SUB-SIGNALS', fields: [
        { key: 'v3_sub_elm', label: 'ELM' },
        { key: 'v3_sub_cascade', label: 'Cascade' },
        { key: 'v3_sub_taker', label: 'Taker' },
        { key: 'v3_sub_oi', label: 'OI' },
        { key: 'v3_sub_funding', label: 'Funding' },
        { key: 'v3_sub_vpin', label: 'VPIN' },
        { key: 'v3_sub_momentum', label: 'Momentum' },
      ],
    },
    {
      title: 'V4 REGIME', fields: [
        { key: 'v4_regime', label: 'regime', fmt: v => v || '\u2014' },
        { key: 'v4_regime_confidence', label: 'confidence', fmt: v => fmtNum(v, 2) },
        { key: 'v4_regime_persistence', label: 'persistence', fmt: v => fmtNum(v, 2) },
      ],
    },
    {
      title: 'V4 MACRO', fields: [
        { key: 'v4_macro_bias', label: 'bias', fmt: v => v || '\u2014' },
        { key: 'v4_macro_direction_gate', label: 'dir_gate', fmt: v => v || '\u2014' },
        { key: 'v4_macro_size_modifier', label: 'size_mod', fmt: v => fmtNum(v, 2) },
      ],
    },
    {
      title: 'CONSENSUS', fields: [
        { key: 'v4_consensus_safe_to_trade', label: 'safe', fmt: v => v == null ? '\u2014' : v ? 'YES' : 'NO' },
        { key: 'v4_consensus_agreement_score', label: 'agreement', fmt: v => fmtNum(v, 2) },
        { key: 'poly_confidence_distance', label: 'distance', fmt: v => fmtNum(v, 3) },
      ],
    },
    {
      title: 'POLYMARKET', fields: [
        { key: 'poly_direction', label: 'direction', fmt: v => v || '\u2014' },
        { key: 'poly_trade_advised', label: 'advised', fmt: v => v == null ? '\u2014' : v ? 'YES' : 'NO' },
        { key: 'poly_confidence', label: 'confidence', fmt: v => fmtNum(v, 3) },
        { key: 'poly_timing', label: 'timing', fmt: v => v || '\u2014' },
        { key: 'poly_max_entry_price', label: 'max_entry', fmt: v => fmtNum(v, 3) },
      ],
    },
    {
      title: 'CLOB', fields: [
        { key: 'clob_up_bid', label: 'UP bid', fmt: v => fmtNum(v, 3) },
        { key: 'clob_up_ask', label: 'UP ask', fmt: v => fmtNum(v, 3) },
        { key: 'clob_down_bid', label: 'DN bid', fmt: v => fmtNum(v, 3) },
        { key: 'clob_down_ask', label: 'DN ask', fmt: v => fmtNum(v, 3) },
        { key: 'clob_implied_up', label: 'impl UP', fmt: v => fmtNum(v, 3) },
      ],
    },
    {
      title: 'COINGLASS', fields: [
        { key: 'cg_oi_usd', label: 'OI', fmt: v => fmtUsd(v) },
        { key: 'cg_funding_rate', label: 'Funding', fmt: v => fmtPct(v) },
        { key: 'cg_taker_buy_vol', label: 'Buy Vol', fmt: v => fmtUsd(v) },
        { key: 'cg_taker_sell_vol', label: 'Sell Vol', fmt: v => fmtUsd(v) },
        { key: 'cg_liq_total', label: 'Liq Total', fmt: v => fmtUsd(v) },
      ],
    },
    {
      title: 'PRICE / DELTA', fields: [
        { key: 'current_price', label: 'BTC', fmt: v => v ? '$' + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '\u2014' },
        { key: 'delta_pct', label: 'delta%', fmt: v => fmtPct(v) },
        { key: 'delta_source', label: 'source', fmt: v => v || '\u2014' },
        { key: 'vpin', label: 'VPIN', fmt: v => fmtNum(v, 3) },
        { key: 'regime', label: 'regime', fmt: v => v || '\u2014' },
      ],
    },
  ];

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
      gap: 8,
    }}>
      {sections.map(sec => (
        <div key={sec.title} style={{
          background: 'rgba(15,23,42,0.5)', border: `1px solid ${C.panelBorder}`,
          borderRadius: 4, padding: '8px 10px',
        }}>
          <div style={{
            fontSize: 7, fontWeight: 700, color: C.teal, letterSpacing: '0.08em',
            marginBottom: 6, fontFamily: C.mono, borderBottom: `1px solid ${C.panelBorder}`,
            paddingBottom: 4,
          }}>
            {sec.title}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {sec.fields.map(f => {
              const raw = enrichedCtx[f.key];
              const formatted = f.fmt ? f.fmt(raw) : fmtNum(raw, 3);
              const hl = isHighlighted(f.key);
              return (
                <div key={f.key} style={{
                  display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                  padding: '1px 4px', borderRadius: 2,
                  background: hl ? 'rgba(20,184,166,0.06)' : 'transparent',
                  borderLeft: hl ? '2px solid rgba(20,184,166,0.5)' : '2px solid transparent',
                }}>
                  <span style={{ fontSize: 9, color: C.muted, fontFamily: C.mono }}>{f.label}</span>
                  <span style={{
                    fontSize: 10, fontWeight: 600,
                    color: hl ? C.teal : C.text,
                    fontFamily: C.mono,
                  }}>
                    {formatted}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Gate Detail Tab ─────────────────────────────────────────────────────────

function GateDetailTab({ strategyId, gateResults, decisions }) {
  const gates = STRATEGY_GATES[strategyId] || [];

  const passRates = useMemo(() => {
    const rates = {};
    if (!decisions?.length) return rates;
    gates.forEach(gName => {
      let pass = 0, total = 0;
      decisions.forEach(d => {
        if ((d.strategy_id || d.strategy_name) !== strategyId) return;
        const gr = extractGateResults(d);
        const status = gr[gName];
        if (status === true || status === 'pass') { pass++; total++; }
        else if (status === false || status === 'fail') { total++; }
      });
      rates[gName] = total > 0 ? (pass / total) : null;
    });
    return rates;
  }, [decisions, strategyId, gates]);

  if (!gates.length) {
    return (
      <div style={{ padding: 20, textAlign: 'center', color: C.muted, fontSize: 11, fontFamily: C.mono }}>
        {strategyId === 'v4_fusion' ? 'V4 Fusion uses custom hook-based evaluation (no gate pipeline)' : 'No gates configured'}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {gates.map((gName, i) => {
        const gate = GATES[gName] || { label: gName, icon: '?', description: '' };
        const result = gateResults[gName];
        const passed = result === true || result === 'pass';
        const failed = result === false || result === 'fail';
        const statusColor = passed ? C.teal : failed ? C.rose : C.dim;
        const rate = passRates[gName];

        return (
          <div key={gName} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '8px 12px', borderRadius: 4,
            background: 'rgba(15,23,42,0.5)', border: `1px solid ${C.panelBorder}`,
            borderLeft: `3px solid ${statusColor}`,
          }}>
            <div style={{ fontSize: 16, width: 24, textAlign: 'center' }}>{gate.icon}</div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 2 }}>
                <span style={{ fontSize: 10, fontWeight: 700, color: C.text, fontFamily: C.mono }}>
                  G{i + 1} {gate.label}
                </span>
                <span style={{
                  fontSize: 7, fontWeight: 700, padding: '1px 6px', borderRadius: 2,
                  background: passed ? C.greenDim : failed ? C.roseDim : 'rgba(100,116,139,0.1)',
                  color: statusColor, fontFamily: C.mono,
                }}>
                  {passed ? 'PASS' : failed ? 'FAIL' : 'N/A'}
                </span>
              </div>
              <div style={{ fontSize: 8, color: C.muted, fontFamily: C.mono }}>{gate.description}</div>
            </div>
            {rate != null && (
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: rate > 0.7 ? C.teal : rate > 0.4 ? C.amber : C.rose, fontFamily: C.mono }}>
                  {(rate * 100).toFixed(0)}%
                </div>
                <div style={{ fontSize: 7, color: C.muted, fontFamily: C.mono }}>PASS RATE</div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Decisions Tab ───────────────────────────────────────────────────────────

function DecisionsTab({ decisions, strategyId }) {
  const [expanded, setExpanded] = useState(null);

  const filtered = useMemo(() => {
    if (!decisions?.length) return [];
    return decisions
      .filter(d => (d.strategy_id || d.strategy_name) === strategyId)
      .slice(0, 30);
  }, [decisions, strategyId]);

  // Group decisions by window_ts for header display (must be before early return)
  const grouped = useMemo(() => {
    const groups = [];
    let lastWindow = null;
    filtered.forEach((d, i) => {
      const wts = d.window_ts ?? (d.metadata_json || d.metadata || {}).window_ts;
      if (wts !== lastWindow) {
        groups.push({ type: 'header', wts, key: `hdr-${wts || i}` });
        lastWindow = wts;
      }
      groups.push({ type: 'decision', d, idx: i, key: d.id || i });
    });
    return groups;
  }, [filtered]);

  if (!filtered.length) {
    return (
      <div style={{ padding: 20, textAlign: 'center', color: C.muted, fontSize: 11, fontFamily: C.mono }}>
        No decisions for {STRATEGIES[strategyId]?.label || strategyId}
      </div>
    );
  }

  return (
    <div className="cmd-scroll" style={{ maxHeight: 420, overflowY: 'auto' }}>
      {grouped.map(item => {
        if (item.type === 'header') {
          return (
            <div key={item.key} style={{
              fontSize: 8, color: C.teal, fontFamily: C.mono, fontWeight: 700,
              padding: '6px 8px 2px',
              borderBottom: `1px solid rgba(20,184,166,0.2)`,
              letterSpacing: '0.06em',
              background: 'rgba(20,184,166,0.03)',
            }}>
              WINDOW {item.wts ? fmtTs(item.wts) : 'UNKNOWN'}
            </div>
          );
        }

        const { d, idx: i } = item;
        const meta = d.metadata_json || d.metadata || {};
        const gr = extractGateResults(d);
        const grReasons = extractGateReasons(d);
        const action = d.action || (d.trade_placed ? 'TRADE' : 'SKIP');
        const dir = d.direction || meta.direction;
        const skipReason = d.skip_reason || meta.skip_reason;
        const offset = d.eval_offset ?? meta.eval_offset;
        const isOpen = expanded === i;
        const gateEntries = Object.entries(gr);

        return (
          <div key={item.key} style={{
            borderBottom: `1px solid ${C.panelBorder}`,
            animation: 'cmd-fade 0.2s ease-out',
          }}>
            <div
              onClick={() => setExpanded(isOpen ? null : i)}
              style={{
                display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 8px',
                cursor: 'pointer',
                background: action === 'TRADE' ? 'rgba(20,184,166,0.03)' : 'transparent',
              }}
            >
              {/* Row 1: timestamp, action, dir, offset, skip reason */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 9, color: C.muted, fontFamily: C.mono, minWidth: 60 }}>
                  {fmtTs(d.created_at || d.evaluated_at)}
                </span>
                <ActionBadge action={action} />
                {dir && <DirBadge dir={dir} />}
                {offset != null && (
                  <span style={{ fontSize: 8, color: C.muted, fontFamily: C.mono }}>T-{offset}</span>
                )}
                {skipReason && (
                  <span style={{
                    fontSize: 8, color: C.amber, fontFamily: C.mono,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    maxWidth: 160,
                  }}>
                    {skipReason}
                  </span>
                )}
                <span style={{ marginLeft: 'auto', color: C.dim, fontSize: 10 }}>
                  {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                </span>
              </div>
              {/* Row 2: inline gate chips */}
              {gateEntries.length > 0 && (
                <div style={{ display: 'flex', gap: 3, flexWrap: 'wrap', paddingLeft: 4 }}>
                  {gateEntries.map(([g, val]) => {
                    const status = val === true || val === 'pass' ? 'pass' : val === false || val === 'fail' ? 'fail' : 'skip';
                    const gate = GATES[g] || { label: g, icon: '?' };
                    const reason = grReasons[g];
                    const colors = {
                      pass: { text: C.teal, border: 'rgba(20,184,166,0.3)', bg: 'rgba(20,184,166,0.08)' },
                      fail: { text: C.rose, border: 'rgba(244,63,94,0.3)', bg: 'rgba(244,63,94,0.08)' },
                      skip: { text: C.dim, border: 'rgba(100,116,139,0.2)', bg: 'transparent' },
                    };
                    const c = colors[status];
                    return (
                      <span
                        key={g}
                        title={reason ? `${gate.label}: ${reason}` : gate.label}
                        style={{
                          fontSize: 7, fontFamily: C.mono, fontWeight: 700,
                          padding: '1px 5px', borderRadius: 2,
                          background: c.bg, border: `1px solid ${c.border}`, color: c.text,
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {status === 'pass' ? '\u2705' : status === 'fail' ? '\u274C' : '\u25CB'} {gate.label}
                      </span>
                    );
                  })}
                </div>
              )}
            </div>

            {isOpen && (
              <div style={{
                padding: '6px 12px 10px', background: 'rgba(15,23,42,0.4)',
                borderTop: `1px solid ${C.panelBorder}`,
              }}>
                <div style={{ fontSize: 7, color: C.muted, fontFamily: C.mono, marginBottom: 2 }}>METADATA</div>
                <pre style={{
                  fontSize: 8, color: C.dim, fontFamily: C.mono,
                  background: 'rgba(0,0,0,0.3)', padding: 6, borderRadius: 3,
                  maxHeight: 120, overflow: 'auto', whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}>
                  {JSON.stringify(meta, null, 1).slice(0, 600)}
                </pre>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Outcomes Tab ────────────────────────────────────────────────────────────

function OutcomesTab({ outcomes, decisions, strategyId, stratStats }) {
  const stratOutcomes = useMemo(() => {
    if (!outcomes?.length) return [];
    return outcomes.filter(o =>
      o.strategy_name === strategyId || o.strategy === strategyId
    ).slice(0, 20);
  }, [outcomes, strategyId]);

  const counterfactuals = useMemo(() => {
    if (!decisions?.length) return [];
    return decisions
      .filter(d => (d.strategy_id || d.strategy_name) === strategyId && (d.action === 'SKIP' || !d.trade_placed))
      .slice(0, 10)
      .map(d => {
        const meta = d.metadata_json || d.metadata || {};
        const dir = d.direction || meta.direction;
        const resolved = meta.resolution || meta.actual_outcome;
        let cf = null;
        if (resolved && dir) {
          if ((dir === 'UP' && resolved === 'UP') || (dir === 'DOWN' && resolved === 'DOWN')) {
            cf = 'MISSED_WIN';
          } else if (resolved) {
            cf = 'CORRECT_SKIP';
          }
        }
        return { ...d, counterfactual: cf, direction: dir };
      })
      .filter(d => d.counterfactual);
  }, [decisions, strategyId]);

  const stats = stratStats?.[strategyId] || null;

  return (
    <div>
      {/* Strategy aggregate stats from comparison endpoint */}
      {stats && (
        <div style={{
          background: 'rgba(15,23,42,0.5)', border: `1px solid ${C.panelBorder}`,
          borderRadius: 4, padding: '10px 14px', marginBottom: 12,
          display: 'flex', gap: 24, flexWrap: 'wrap',
        }}>
          {[
            { label: 'TRADES', value: stats.total_trades ?? stats.trades ?? '\u2014' },
            { label: 'WINS', value: stats.wins ?? '\u2014', color: C.teal },
            { label: 'LOSSES', value: stats.losses ?? '\u2014', color: C.rose },
            {
              label: 'WIN RATE',
              value: (stats.win_rate != null)
                ? (stats.win_rate * 100).toFixed(1) + '%'
                : '\u2014',
              color: stats.win_rate >= 0.7 ? C.teal : stats.win_rate >= 0.5 ? C.amber : C.rose,
            },
            {
              label: 'PNL',
              value: stats.total_pnl != null
                ? (stats.total_pnl >= 0 ? '+' : '') + fmtNum(stats.total_pnl, 2)
                : '\u2014',
              color: (stats.total_pnl || 0) >= 0 ? C.teal : C.rose,
            },
          ].map(item => (
            <div key={item.label} style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              <span style={{ fontSize: 7, color: C.muted, fontFamily: C.mono, letterSpacing: '0.08em' }}>
                {item.label}
              </span>
              <span style={{
                fontSize: 14, fontWeight: 800, fontFamily: C.mono,
                color: item.color || C.text,
              }}>
                {String(item.value)}
              </span>
            </div>
          ))}
        </div>
      )}

      <SectionLabel icon={Activity}>TRADE OUTCOMES</SectionLabel>
      {stratOutcomes.length === 0 ? (
        <div style={{ padding: 12, color: C.muted, fontSize: 10, fontFamily: C.mono }}>
          No outcomes yet for {STRATEGIES[strategyId]?.label || strategyId}
        </div>
      ) : (
        <div className="cmd-scroll" style={{ maxHeight: 220, overflowY: 'auto', marginBottom: 12 }}>
          {stratOutcomes.map((o, i) => {
            const won = o.outcome === 'WIN' || o.result === 'WIN';
            return (
              <div key={o.id || i} style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px',
                borderBottom: `1px solid ${C.panelBorder}`,
              }}>
                <span style={{ fontSize: 9, color: C.muted, fontFamily: C.mono, minWidth: 60 }}>
                  {fmtTs(o.resolved_at || o.created_at)}
                </span>
                <span style={{
                  fontSize: 8, fontWeight: 700, padding: '1px 8px', borderRadius: 3,
                  background: won ? C.greenDim : C.roseDim,
                  color: won ? C.green : C.rose,
                  fontFamily: C.mono,
                }}>
                  {won ? 'WIN' : 'LOSS'}
                </span>
                <DirBadge dir={o.direction} />
                <span style={{ fontSize: 9, fontWeight: 600, color: C.text, fontFamily: C.mono }}>
                  {o.entry_price ? `@${fmtNum(o.entry_price, 3)}` : ''}
                </span>
                <span style={{
                  fontSize: 9, fontWeight: 700, fontFamily: C.mono, marginLeft: 'auto',
                  color: (o.pnl || 0) >= 0 ? C.teal : C.rose,
                }}>
                  {o.pnl != null ? (o.pnl >= 0 ? '+' : '') + fmtNum(o.pnl, 2) : '\u2014'}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {counterfactuals.length > 0 && (
        <>
          <SectionLabel icon={Eye}>COUNTERFACTUAL (SKIPPED WINDOWS)</SectionLabel>
          <div className="cmd-scroll" style={{ maxHeight: 160, overflowY: 'auto' }}>
            {counterfactuals.map((d, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '5px 8px',
                borderBottom: `1px solid ${C.panelBorder}`,
              }}>
                <span style={{ fontSize: 9, color: C.muted, fontFamily: C.mono, minWidth: 60 }}>
                  {fmtTs(d.created_at)}
                </span>
                <DirBadge dir={d.direction} />
                <span style={{
                  fontSize: 8, fontWeight: 700, padding: '1px 8px', borderRadius: 3,
                  fontFamily: C.mono,
                  background: d.counterfactual === 'MISSED_WIN' ? C.amberDim : C.greenDim,
                  color: d.counterfactual === 'MISSED_WIN' ? C.amber : C.green,
                }}>
                  {d.counterfactual === 'MISSED_WIN' ? 'WOULD HAVE WON' : 'GOOD SKIP'}
                </span>
                <span style={{ fontSize: 8, color: C.muted, fontFamily: C.mono }}>
                  {d.skip_reason || ''}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// ── Data Health Strip ───────────────────────────────────────────────────────

// ── Window History Tab ──────────────────────────────────────────────────────

const STRATEGY_ORDER = ['v4_down_only', 'v4_up_basic', 'v4_up_asian', 'v4_fusion', 'v10_gate'];

function WindowHistoryTab({ decisions }) {
  // Group decisions by window_ts, build one row per window
  const rows = useMemo(() => {
    if (!decisions?.length) return [];

    // Group by window_ts
    const windowMap = {};
    decisions.forEach(d => {
      const wts = d.window_ts ?? (d.metadata_json || d.metadata || {}).window_ts ?? 'unknown';
      if (!windowMap[wts]) windowMap[wts] = { wts, strategies: {} };
      const sid = d.strategy_id || d.strategy_name;
      // Keep the most definitive decision per strategy per window
      const existing = windowMap[wts].strategies[sid];
      if (!existing || d.action === 'TRADE') {
        const meta = d.metadata_json || d.metadata || {};
        const ctx = meta._ctx || meta.ctx || {};
        windowMap[wts].strategies[sid] = {
          action: d.action || (d.trade_placed ? 'TRADE' : 'SKIP'),
          direction: d.direction || meta.direction,
          skip_reason: d.skip_reason || meta.skip_reason,
          outcome: d.outcome || meta.outcome || meta.resolution,
          open_price: ctx.open_price,
          close_price: ctx.close_price ?? ctx.current_price,
          delta_pct: ctx.delta_pct,
        };
      }
    });

    return Object.values(windowMap)
      .sort((a, b) => {
        if (a.wts === 'unknown') return 1;
        if (b.wts === 'unknown') return -1;
        return b.wts - a.wts;
      })
      .slice(0, 20);
  }, [decisions]);

  if (!rows.length) {
    return (
      <div style={{ padding: '8px 16px', fontSize: 9, color: C.muted, fontFamily: C.mono }}>
        No window history yet — waiting for decisions data
      </div>
    );
  }

  const thStyle = {
    fontSize: 7, fontWeight: 700, color: C.muted, fontFamily: C.mono,
    padding: '4px 8px', textAlign: 'left', letterSpacing: '0.06em',
    borderBottom: `1px solid ${C.panelBorder}`, whiteSpace: 'nowrap',
  };

  return (
    <div className="cmd-scroll" style={{ overflowX: 'auto', overflowY: 'auto', maxHeight: 220 }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 8, fontFamily: C.mono }}>
        <thead>
          <tr style={{ background: 'rgba(15,23,42,0.7)' }}>
            <th style={thStyle}>TIME (UTC)</th>
            <th style={thStyle}>BTC</th>
            {STRATEGY_ORDER.map(sid => (
              <th key={sid} style={{ ...thStyle, color: STRATEGIES[sid]?.color || C.muted }}>
                {STRATEGIES[sid]?.shortLabel || sid}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => {
            // Figure out oracle outcome from any strategy that resolved
            let oracleOutcome = null;
            STRATEGY_ORDER.forEach(sid => {
              const sd = row.strategies[sid];
              if (sd?.outcome && !oracleOutcome) oracleOutcome = sd.outcome;
            });

            // BTC delta from ctx
            const anyStrat = Object.values(row.strategies)[0] || {};
            const openP = anyStrat.open_price;
            const closeP = anyStrat.close_price;
            const dp = anyStrat.delta_pct;
            const deltaDisplay = dp != null
              ? (dp >= 0 ? '+' : '') + (dp * 100).toFixed(2) + '%'
              : (openP && closeP)
                ? ((closeP - openP) / openP * 100 >= 0 ? '+' : '') + ((closeP - openP) / openP * 100).toFixed(2) + '%'
                : '\u2014';
            const deltaColor = dp != null ? (dp >= 0 ? C.teal : C.rose) : C.muted;

            return (
              <tr key={row.wts} style={{
                background: ri % 2 === 0 ? 'rgba(15,23,42,0.3)' : 'transparent',
                borderBottom: `1px solid rgba(51,65,85,0.3)`,
              }}>
                <td style={{ padding: '3px 8px', color: C.muted, whiteSpace: 'nowrap' }}>
                  {row.wts !== 'unknown' ? fmtTs(row.wts) : '\u2014'}
                </td>
                <td style={{ padding: '3px 8px', whiteSpace: 'nowrap' }}>
                  {openP && (
                    <span style={{ color: C.dim }}>
                      ${Number(openP).toLocaleString('en-US', { maximumFractionDigits: 0 })}
                    </span>
                  )}
                  {' '}
                  <span style={{ color: deltaColor, fontWeight: 700 }}>{deltaDisplay}</span>
                </td>
                {STRATEGY_ORDER.map(sid => {
                  const sd = row.strategies[sid];
                  if (!sd) {
                    return <td key={sid} style={{ padding: '3px 8px', color: C.dim }}>—</td>;
                  }
                  const isT = sd.action === 'TRADE';
                  const won = sd.outcome === 'WIN';
                  const lost = sd.outcome === 'LOSS';
                  const dirArrow = sd.direction === 'UP' ? '\u2191' : sd.direction === 'DOWN' ? '\u2193' : '';
                  const chip = isT
                    ? (won ? { bg: 'rgba(20,184,166,0.15)', color: C.teal, label: '\u2705' }
                      : lost ? { bg: 'rgba(244,63,94,0.15)', color: C.rose, label: '\u274C' }
                        : { bg: 'rgba(20,184,166,0.1)', color: C.teal, label: 'TRADE' })
                    : { bg: 'rgba(244,63,94,0.08)', color: C.dim, label: 'SKIP' };

                  return (
                    <td key={sid} style={{ padding: '3px 8px', whiteSpace: 'nowrap' }}
                      title={sd.skip_reason || sd.direction || ''}>
                      <span style={{
                        fontSize: 8, fontWeight: 700, padding: '1px 5px', borderRadius: 2,
                        background: chip.bg, color: chip.color,
                      }}>
                        {chip.label}
                      </span>
                      {dirArrow && (
                        <span style={{ marginLeft: 3, color: sd.direction === 'UP' ? C.teal : C.rose }}>
                          {dirArrow}
                        </span>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Data Health Strip ───────────────────────────────────────────────────────

function DataHealthStripLocal({ hqData }) {
  const sources = Object.entries(DATA_SOURCES);
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', padding: '8px 0' }}>
      {sources.map(([key, src]) => {
        let secs = null;
        if (hqData?.updated_at) {
          secs = ago(hqData.updated_at);
        }
        const f = freshness(secs);
        return (
          <div key={key} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            padding: '4px 10px', borderRadius: 3,
            background: 'rgba(15,23,42,0.5)', border: `1px solid ${C.panelBorder}`,
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: f.color, boxShadow: `0 0 4px ${f.color}`,
            }} />
            <span style={{ fontSize: 8, fontWeight: 600, color: C.text, fontFamily: C.mono }}>
              {src.label}
            </span>
            <span style={{ fontSize: 7, color: f.color, fontFamily: C.mono }}>{f.label}</span>
          </div>
        );
      })}
    </div>
  );
}

// ── MAIN COMPONENT ──────────────────────────────────────────────────────────

export default function StrategyCommand() {
  const api = useApi();

  const [selectedStrategy, setSelectedStrategy] = useState('v4_down_only');
  const [centerTab, setCenterTab] = useState('signal');
  const [bottomTab, setBottomTab] = useState('health');

  const [hqData, setHqData] = useState(null);
  const [decisions, setDecisions] = useState([]);
  const [comparison, setComparison] = useState(null);
  const [outcomes, setOutcomes] = useState([]);
  const [dashStats, setDashStats] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const prev = document.title;
    document.title = 'Command Center \u2014 Strategy Engine v2';
    return () => { document.title = prev; };
  }, []);

  const fetchFast = useCallback(async () => {
    try {
      const [hqRes, decRes] = await Promise.allSettled([
        api('GET', '/v58/execution-hq?limit=200&asset=btc&timeframe=5m'),
        api('GET', `/v58/strategy-decisions?limit=20`),
      ]);
      if (hqRes.status === 'fulfilled') setHqData(hqRes.value?.data || hqRes.value);
      if (decRes.status === 'fulfilled') {
        const d = decRes.value?.data || decRes.value;
        setDecisions(d?.decisions ?? (Array.isArray(d) ? d : []));
      }
    } catch { /* swallow */ }
  }, [api]);

  const fetchMed = useCallback(async () => {
    try {
      const [outRes] = await Promise.allSettled([
        api('GET', '/v58/outcomes?limit=20'),
      ]);
      if (outRes.status === 'fulfilled') {
        const o = outRes.value?.data || outRes.value;
        setOutcomes(o?.outcomes ?? (Array.isArray(o) ? o : []));
      }
    } catch { /* swallow */ }
  }, [api]);

  const fetchSlow = useCallback(async () => {
    try {
      const [compRes, statRes] = await Promise.allSettled([
        api('GET', '/v58/strategy-comparison'),
        api('GET', '/dashboard/stats'),
      ]);
      if (compRes.status === 'fulfilled') setComparison(compRes.value?.data || compRes.value);
      if (statRes.status === 'fulfilled') setDashStats(statRes.value?.data || statRes.value);
    } catch { /* swallow */ }
    setLoading(false);
  }, [api]);

  useEffect(() => {
    fetchFast();
    fetchMed();
    fetchSlow();
    const i1 = setInterval(fetchFast, POLL_FAST);
    const i2 = setInterval(fetchMed, POLL_MED);
    const i3 = setInterval(fetchSlow, POLL_SLOW);
    return () => { clearInterval(i1); clearInterval(i2); clearInterval(i3); };
  }, [fetchFast, fetchMed, fetchSlow]);

  // Derived
  const strat = STRATEGIES[selectedStrategy] || getStrategyMeta(selectedStrategy);
  const stratGates = STRATEGY_GATES[selectedStrategy] || [];

  const stratStats = useMemo(() => {
    if (!comparison) return {};
    const strategies = comparison.strategies || comparison;
    const map = {};
    if (Array.isArray(strategies)) {
      strategies.forEach(s => { map[s.strategy_name || s.id || s.name] = s; });
    } else if (typeof strategies === 'object') {
      Object.assign(map, strategies);
    }
    return map;
  }, [comparison]);

  const latestDecision = useMemo(() => {
    return decisions.find(d => (d.strategy_id || d.strategy_name) === selectedStrategy) || null;
  }, [decisions, selectedStrategy]);

  const latestGateResults = useMemo(() => extractGateResults(latestDecision), [latestDecision]);
  const latestGateReasons = useMemo(() => extractGateReasons(latestDecision), [latestDecision]);
  const latestCtx = useMemo(() => extractCtx(latestDecision), [latestDecision]);

  const currentWindow = hqData?.current_window || hqData?.window || {};
  const position = hqData?.position || hqData?.open_position || null;
  const recentFills = hqData?.recent_fills || hqData?.fills || [];
  const bankroll = dashStats?.bankroll ?? dashStats?.balance ?? null;
  const todayPnl = dashStats?.today_pnl ?? dashStats?.pnl_today ?? null;
  const engineMode = dashStats?.mode || hqData?.mode || 'PAPER';

  return (
    <div style={{
      minHeight: '100vh', background: C.bg, color: C.text,
      fontFamily: C.mono, display: 'flex', flexDirection: 'column',
    }}>

      {/* ════ TOP BAR ════ */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '8px 16px', background: C.header,
        borderBottom: `1px solid ${C.panelBorder}`,
        flexShrink: 0, gap: 12, flexWrap: 'wrap',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Zap size={14} color={C.teal} />
            <span style={{ fontSize: 13, fontWeight: 800, color: C.text }}>STRATEGY COMMAND</span>
          </div>
          <span style={{ fontSize: 9, color: C.muted }}>Engine v2</span>
          <span style={{ fontSize: 9, color: C.dim }}>|</span>
          <span style={{ fontSize: 9, color: C.text }}>BTC-5m</span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <ModeBadge mode={engineMode} />
          {bankroll != null && (
            <span style={{ fontSize: 10, color: C.text }}>
              <span style={{ color: C.muted, fontSize: 8 }}>BANK </span>${fmtNum(bankroll, 2)}
            </span>
          )}
          {todayPnl != null && (
            <span style={{
              fontSize: 10, fontWeight: 700,
              color: todayPnl >= 0 ? C.teal : C.rose,
            }}>
              {todayPnl >= 0 ? '+' : ''}{fmtNum(todayPnl, 2)}
            </span>
          )}
          <div style={{
            width: 8, height: 8, borderRadius: '50%',
            background: C.teal,
            animation: loading ? 'cmd-pulse 1.5s infinite' : 'none',
          }} />
        </div>
      </div>

      {/* ════ 3-COLUMN BODY ════ */}
      <div style={{
        flex: 1, display: 'grid',
        gridTemplateColumns: '260px 1fr 280px',
        gap: 0, minHeight: 0, overflow: 'hidden',
      }}>

        {/* ──── LEFT PANEL ──── */}
        <div className="cmd-scroll" style={{
          borderRight: `1px solid ${C.panelBorder}`,
          padding: 12, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12,
        }}>

          <div>
            <SectionLabel icon={Layers}>STRATEGIES</SectionLabel>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {STRATEGY_LIST.map(s => {
                const isActive = s.id === selectedStrategy;
                const stats = stratStats[s.id] || {};
                const wr = stats.win_rate ?? stats.winRate;
                const mode = stats.mode || s.defaultMode;
                return (
                  <button
                    key={s.id}
                    onClick={() => setSelectedStrategy(s.id)}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 8,
                      padding: '8px 10px', borderRadius: 4, border: 'none',
                      cursor: 'pointer', textAlign: 'left',
                      background: isActive ? `${s.color}15` : 'transparent',
                      borderLeft: `3px solid ${isActive ? s.color : 'transparent'}`,
                      transition: 'all 0.15s',
                    }}
                  >
                    <div style={{
                      width: 6, height: 6, borderRadius: '50%',
                      background: s.color, flexShrink: 0,
                      boxShadow: isActive ? `0 0 8px ${s.color}` : 'none',
                    }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{
                        fontSize: 10, fontWeight: 700, color: isActive ? s.color : C.text,
                        fontFamily: C.mono,
                      }}>
                        {s.label}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
                        <ModeBadge mode={mode} />
                        <DirBadge dir={s.direction} />
                        {wr != null && (
                          <span style={{
                            fontSize: 8, fontWeight: 700, fontFamily: C.mono,
                            color: wr >= 0.7 ? C.teal : wr >= 0.5 ? C.amber : C.rose,
                          }}>
                            {(wr * 100).toFixed(0)}% WR
                          </span>
                        )}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <SectionLabel icon={Filter}>GATE PIPELINE</SectionLabel>
            {stratGates.length === 0 ? (
              <div style={{ fontSize: 9, color: C.muted, padding: '4px 0' }}>
                {selectedStrategy === 'v4_fusion' ? 'Custom hook eval' : 'No gates'}
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {stratGates.map((gName, i) => {
                  const result = latestGateResults[gName];
                  const passed = result === true || result === 'pass';
                  const failed = result === false || result === 'fail';
                  const status = passed ? 'pass' : failed ? 'fail' : 'skip';
                  const reason = latestGateReasons[gName];
                  return (
                    <div key={gName} style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '4px 8px', borderRadius: 3,
                      background: passed ? 'rgba(20,184,166,0.05)' : failed ? 'rgba(244,63,94,0.05)' : 'transparent',
                    }}>
                      <span style={{
                        fontSize: 7, fontWeight: 700, color: C.dim,
                        fontFamily: C.mono, minWidth: 16,
                      }}>
                        G{i + 1}
                      </span>
                      <GateChip name={gName} status={status} reason={reason} />
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div>
            <SectionLabel icon={Shield}>CONTROLS</SectionLabel>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                <span style={{ color: C.muted }}>Mode</span>
                <ModeBadge mode={engineMode} />
              </div>
              {bankroll != null && (
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                  <span style={{ color: C.muted }}>Bankroll</span>
                  <span style={{ color: C.text, fontWeight: 600 }}>${fmtNum(bankroll, 2)}</span>
                </div>
              )}
              {currentWindow?.seconds_to_close != null && (
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                  <span style={{ color: C.muted }}>Window</span>
                  <span style={{ color: C.amber, fontWeight: 600 }}>
                    {fmtCountdown(currentWindow.seconds_to_close)}
                  </span>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* ──── CENTER PANEL ──── */}
        <div style={{
          display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden',
        }}>
          {/* Live BTC price — always visible */}
          <LiveBtcBanner hqData={hqData} latestCtxPrice={latestCtx?.current_price ?? (decisions[0] ? extractCtx(decisions[0])?.current_price : null)} />

          <div style={{
            display: 'flex', gap: 0, borderBottom: `1px solid ${C.panelBorder}`,
            background: C.header, flexShrink: 0,
          }}>
            {TAB_KEYS.map(k => (
              <button
                key={k}
                onClick={() => setCenterTab(k)}
                style={{
                  padding: '8px 16px', border: 'none', cursor: 'pointer',
                  fontSize: 9, fontWeight: 700, fontFamily: C.mono,
                  letterSpacing: '0.06em', textTransform: 'uppercase',
                  background: centerTab === k ? 'rgba(20,184,166,0.08)' : 'transparent',
                  color: centerTab === k ? C.teal : C.muted,
                  borderBottom: `2px solid ${centerTab === k ? C.teal : 'transparent'}`,
                  transition: 'all 0.15s',
                }}
              >
                {TAB_LABELS[k]}
              </button>
            ))}
          </div>

          <div className="cmd-scroll" style={{ flex: 1, padding: 12, overflowY: 'auto' }}>
            {centerTab === 'signal' && (
              <SignalSurfaceTab ctx={latestCtx} strategyId={selectedStrategy} hqData={hqData} decisions={decisions} />
            )}
            {centerTab === 'gates' && (
              <GateDetailTab
                strategyId={selectedStrategy}
                gateResults={latestGateResults}
                decisions={decisions}
              />
            )}
            {centerTab === 'decisions' && (
              <DecisionsTab decisions={decisions} strategyId={selectedStrategy} />
            )}
            {centerTab === 'outcomes' && (
              <OutcomesTab outcomes={outcomes} decisions={decisions} strategyId={selectedStrategy} stratStats={stratStats} />
            )}
          </div>
        </div>

        {/* ──── RIGHT PANEL ──── */}
        <div className="cmd-scroll" style={{
          borderLeft: `1px solid ${C.panelBorder}`,
          padding: 12, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 12,
        }}>

          <div>
            <SectionLabel icon={Radio}>EXECUTION</SectionLabel>
            <Panel>
              {position ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                    <span style={{ color: C.muted }}>Strategy</span>
                    <span style={{ color: C.text, fontWeight: 600 }}>{position.strategy_name || position.strategy || '\u2014'}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                    <span style={{ color: C.muted }}>Direction</span>
                    <DirBadge dir={position.direction} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                    <span style={{ color: C.muted }}>Entry</span>
                    <span style={{ color: C.text, fontWeight: 600 }}>{fmtNum(position.entry_price, 3)}</span>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                    <span style={{ color: C.muted }}>Stake</span>
                    <span style={{ color: C.text, fontWeight: 600 }}>${fmtNum(position.stake || position.amount, 2)}</span>
                  </div>
                  {position.unrealized_pnl != null && (
                    <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, fontFamily: C.mono }}>
                      <span style={{ color: C.muted }}>uPnL</span>
                      <span style={{ color: position.unrealized_pnl >= 0 ? C.teal : C.rose, fontWeight: 700 }}>
                        {position.unrealized_pnl >= 0 ? '+' : ''}{fmtNum(position.unrealized_pnl, 2)}
                      </span>
                    </div>
                  )}
                </div>
              ) : (
                <div style={{ fontSize: 10, color: C.muted, textAlign: 'center', padding: 8 }}>
                  No open position
                </div>
              )}
            </Panel>
          </div>

          <div>
            <SectionLabel icon={Activity}>RECENT FILLS</SectionLabel>
            <div className="cmd-scroll" style={{ maxHeight: 200, overflowY: 'auto' }}>
              {(recentFills.length === 0 && (!outcomes || outcomes.length === 0)) ? (
                <div style={{ fontSize: 10, color: C.muted, padding: 8, textAlign: 'center' }}>
                  No recent fills
                </div>
              ) : (
                (recentFills.length > 0 ? recentFills : outcomes).slice(0, 10).map((fill, i) => {
                  const dir = fill.direction;
                  const pnlVal = fill.pnl;
                  return (
                    <div key={fill.id || i} style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '4px 6px', borderBottom: `1px solid ${C.panelBorder}`,
                      fontSize: 9, fontFamily: C.mono,
                    }}>
                      <span style={{ color: C.muted, minWidth: 48, fontSize: 8 }}>
                        {fmtTs(fill.filled_at || fill.created_at || fill.resolved_at)}
                      </span>
                      <span style={{
                        fontSize: 7, fontWeight: 700,
                        color: STRATEGIES[fill.strategy_id || fill.strategy_name]?.color || C.text,
                        minWidth: 24,
                      }}>
                        {STRATEGIES[fill.strategy_id || fill.strategy_name]?.shortLabel
                          || (fill.strategy_id || fill.strategy_name)?.slice(0, 4)?.toUpperCase()
                          || '??'}
                      </span>
                      <span style={{ color: dirColor(dir), fontWeight: 600, fontSize: 8 }}>
                        {dir === 'UP' ? '\u25B2' : dir === 'DOWN' ? '\u25BC' : '\u25C6'}
                      </span>
                      <span style={{ color: C.text, fontSize: 8 }}>
                        {fill.entry_price ? `@${fmtNum(fill.entry_price, 3)}` : ''}
                      </span>
                      <span style={{
                        marginLeft: 'auto', fontWeight: 700, fontSize: 9,
                        color: (pnlVal || 0) >= 0 ? C.teal : C.rose,
                      }}>
                        {pnlVal != null ? (pnlVal >= 0 ? '+' : '') + fmtNum(pnlVal, 2) : ''}
                      </span>
                    </div>
                  );
                })
              )}
            </div>
          </div>

          <div>
            <SectionLabel icon={Terminal}>TRADE FEED</SectionLabel>
            <div className="cmd-scroll" style={{
              maxHeight: 280, overflowY: 'auto',
              background: 'rgba(0,0,0,0.3)', borderRadius: 4, padding: 6,
              border: `1px solid ${C.panelBorder}`,
            }}>
              {decisions.slice(0, 20).map((d, i) => {
                const meta = d.metadata_json || d.metadata || {};
                const action = d.action || (d.trade_placed ? 'TRADE' : 'SKIP');
                const dir = d.direction || meta.direction;
                // Hub may return strategy_id or strategy_name — try both
                const sid = d.strategy_id || d.strategy_name;
                const stratColor = STRATEGIES[sid]?.color || C.muted;
                const stratShort = STRATEGIES[sid]?.shortLabel || sid?.slice(0, 4)?.toUpperCase() || '??';

                return (
                  <div key={d.id || i} style={{
                    fontSize: 8, fontFamily: C.mono, padding: '2px 4px',
                    color: action === 'TRADE' ? C.teal : C.dim,
                    borderBottom: '1px solid rgba(51,65,85,0.3)',
                  }}>
                    <span style={{ color: C.dim }}>{fmtTs(d.created_at || d.evaluated_at)} </span>
                    <span style={{ color: stratColor, fontWeight: 700 }}>[{stratShort}]</span>
                    {' '}
                    <span style={{ color: action === 'TRADE' ? C.teal : C.muted }}>
                      {action}
                    </span>
                    {dir && <span style={{ color: dirColor(dir) }}> {dir}</span>}
                    {d.skip_reason && (
                      <span style={{ color: C.amber }}> {d.skip_reason}</span>
                    )}
                  </div>
                );
              })}
              {decisions.length === 0 && (
                <div style={{ fontSize: 9, color: C.dim, padding: 8, textAlign: 'center', fontFamily: C.mono }}>
                  <div style={{
                    display: 'inline-block', width: 6, height: 6, borderRadius: '50%',
                    background: C.teal, marginRight: 6,
                    animation: 'cmd-pulse 1.5s infinite', verticalAlign: 'middle',
                  }} />
                  Live — polling /v58/strategy-decisions every 5s
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* ════ BOTTOM STRIP ════ */}
      <div style={{
        borderTop: `1px solid ${C.panelBorder}`,
        background: C.header, flexShrink: 0,
      }}>
        <div style={{
          display: 'flex', gap: 0, borderBottom: `1px solid ${C.panelBorder}`,
        }}>
          {BOTTOM_TABS.map(k => (
            <button
              key={k}
              onClick={() => setBottomTab(k)}
              style={{
                padding: '6px 16px', border: 'none', cursor: 'pointer',
                fontSize: 8, fontWeight: 700, fontFamily: C.mono,
                letterSpacing: '0.06em', textTransform: 'uppercase',
                background: bottomTab === k ? 'rgba(20,184,166,0.08)' : 'transparent',
                color: bottomTab === k ? C.teal : C.muted,
                borderBottom: `2px solid ${bottomTab === k ? C.teal : 'transparent'}`,
              }}
            >
              {BOTTOM_LABELS[k]}
            </button>
          ))}
        </div>

        <div style={{ padding: '8px 16px', minHeight: 60 }}>
          {bottomTab === 'health' && <DataHealthStripLocal hqData={hqData} />}

          {bottomTab === 'orders' && (
            <div style={{ fontSize: 9, color: C.muted, fontFamily: C.mono }}>
              {hqData?.pending_orders?.length > 0 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {hqData.pending_orders.map((o, i) => (
                    <div key={i} style={{ display: 'flex', gap: 12, padding: '2px 0' }}>
                      <span>{o.order_type || 'GTC'}</span>
                      <span style={{ color: dirColor(o.direction) }}>{o.direction}</span>
                      <span>${fmtNum(o.amount, 2)}</span>
                      <span style={{ color: C.dim }}>@{fmtNum(o.price, 3)}</span>
                      <span style={{ color: C.muted }}>{o.status || 'PENDING'}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <span>No pending orders</span>
              )}
            </div>
          )}

          {bottomTab === 'positions' && (
            <div style={{ fontSize: 9, color: C.muted, fontFamily: C.mono }}>
              {position ? (
                <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap' }}>
                  <span>
                    <span style={{ color: C.dim }}>Strategy: </span>
                    <span style={{ color: C.text }}>{position.strategy_name || '\u2014'}</span>
                  </span>
                  <span>
                    <span style={{ color: C.dim }}>Dir: </span>
                    <span style={{ color: dirColor(position.direction) }}>{position.direction}</span>
                  </span>
                  <span>
                    <span style={{ color: C.dim }}>Entry: </span>
                    <span style={{ color: C.text }}>{fmtNum(position.entry_price, 3)}</span>
                  </span>
                  <span>
                    <span style={{ color: C.dim }}>Stake: </span>
                    <span style={{ color: C.text }}>${fmtNum(position.stake || position.amount, 2)}</span>
                  </span>
                  <span>
                    <span style={{ color: C.dim }}>Mark: </span>
                    <span style={{ color: C.text }}>{fmtNum(position.mark_price, 3)}</span>
                  </span>
                </div>
              ) : (
                <span>No open positions</span>
              )}
            </div>
          )}

          {bottomTab === 'windows' && (
            <WindowHistoryTab decisions={decisions} />
          )}
        </div>
      </div>
    </div>
  );
}
