/**
 * Assembler1 — canonical reference dashboard for the POST /predict
 * envelope + target ensemble schema.
 *
 * This page consumes the Phase 1 /predict endpoint on the timesfm
 * service (proxied via hub /api/predict) and renders every prediction
 * layer (v1 TimesFM, v2 LightGBM, v3 composite, v4 fusion) plus the
 * `best_probability` pick and the full raw introspection bundle for
 * every horizon.
 *
 * Six tabs:
 *   1. Overview           — best_probability, all 4 layers, fallbacks
 *   2. Microstructure     — VPIN + order flow + price + volatility
 *                           surfaces (tagged ✓have / ⏳not-yet against
 *                           the target ensemble schema)
 *   3. Gates              — 12-gate entry-logic table + pipeline funnel
 *   4. Live Ticks         — ticks_v2_probability joined to window
 *                           outcomes per horizon (5m/15m/1h/4h), live
 *                           predicted-vs-actual tape
 *   5. API Docs           — inline reference for POST /predict
 *   6. Schema             — full target variable catalog with ✓/⏳ flags
 *
 * Data flow:
 *   POST /api/predict                            → full envelope
 *   GET  /api/predict/ticks_vs_outcomes          → tick-vs-actual tape
 *
 * Refresh cadence: 4s for the envelope (matches V4Surface), 10s for
 * the tick tape. Hard-coded so the UI doesn't accidentally hammer the
 * service.
 */

import { useEffect, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';

// ─── Theme ────────────────────────────────────────────────────────────────

const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: 'rgba(30, 41, 59, 1)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  blue: '#3b82f6',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const TIMEFRAMES = ['5m', '15m', '1h', '4h', '24h'];
const DEFAULT_TF = '15m';
const POLL_ENVELOPE_MS = 4000;
const POLL_TICKS_MS = 10_000;

// ─── Small primitives ─────────────────────────────────────────────────────

function Chip({ color = T.cyan, label, value, title, bg }) {
  const background = bg || `${color}1a`;
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        fontSize: 9, fontWeight: 800, padding: '3px 8px', borderRadius: 3,
        background, color, border: `1px solid ${color}55`,
        fontFamily: T.mono, letterSpacing: '0.04em', whiteSpace: 'nowrap',
      }}
    >
      {label && <span style={{ opacity: 0.65, textTransform: 'uppercase' }}>{label}</span>}
      <span>{value}</span>
    </span>
  );
}

function StatusDot({ status }) {
  const color =
    status === 'ok' ? T.green :
    status === 'cold_start' ? T.amber :
    status === 'stale' ? T.amber :
    status === 'degraded' ? T.amber :
    status === 'unavailable' ? T.red :
    T.textDim;
  return (
    <span style={{
      display: 'inline-block',
      width: 8, height: 8, borderRadius: '50%',
      background: color,
      boxShadow: `0 0 6px ${color}66`,
    }} />
  );
}

function Card({ title, subtitle, children, badge, badgeColor }) {
  return (
    <div style={{
      background: T.card, border: `1px solid ${T.cardBorder}`,
      borderRadius: 8, padding: 14, marginBottom: 14,
    }}>
      {(title || badge) && (
        <div style={{
          display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
          marginBottom: 10,
        }}>
          <div>
            {title && <span style={{
              fontSize: 11, fontWeight: 800, color: T.white,
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}>{title}</span>}
            {subtitle && <span style={{
              fontSize: 9, color: T.textMuted, marginLeft: 8, fontFamily: T.mono,
            }}>{subtitle}</span>}
          </div>
          {badge && <Chip color={badgeColor || T.cyan} value={badge} />}
        </div>
      )}
      {children}
    </div>
  );
}

function KV({ label, value, color = T.text, mono = true, width }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '4px 0',
                  borderBottom: `1px solid ${T.cardBorder}`, minWidth: width }}>
      <span style={{ fontSize: 10, color: T.textMuted, textTransform: 'uppercase',
                     letterSpacing: '0.05em' }}>{label}</span>
      <span style={{
        fontSize: 11, color, fontFamily: mono ? T.mono : 'inherit',
        textAlign: 'right', fontWeight: 600,
      }}>{value ?? '—'}</span>
    </div>
  );
}

function JsonPeek({ data, maxHeight = 300 }) {
  return (
    <pre style={{
      fontSize: 10, fontFamily: T.mono, color: T.textMuted,
      background: 'rgba(0,0,0,0.3)', padding: 10, borderRadius: 4,
      margin: 0, overflow: 'auto', maxHeight,
      border: `1px solid ${T.cardBorder}`,
    }}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}

// ─── Tab 1: Overview ──────────────────────────────────────────────────────

function OverviewTab({ env, error }) {
  if (error) return <ErrorPanel error={error} />;
  if (!env) return <LoadingPanel />;

  const bp = env.best_probability || {};
  const v1 = env.v1 || {};
  const v2 = env.v2 || {};
  const v3 = env.v3 || {};
  const v4 = env.v4 || {};
  const service = env.service || {};
  const sequoia = env.sequoia || {};

  const bpColor =
    bp.direction === 'LONG' ? T.green :
    bp.direction === 'SHORT' ? T.red : T.amber;

  return (
    <div>
      {/* Headline: best_probability */}
      <div style={{
        background: T.card, border: `2px solid ${bpColor}`,
        borderRadius: 10, padding: 20, marginBottom: 16,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between',
                      alignItems: 'baseline', marginBottom: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 800, color: T.white,
                         textTransform: 'uppercase', letterSpacing: '0.08em' }}>
            Best Probability
          </span>
          <Chip color={bpColor} label="Source" value={bp.source || 'none'} />
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 16 }}>
          <BigStat label="Value" value={bp.value?.toFixed(4) ?? '—'} color={bpColor} />
          <BigStat label="Direction" value={bp.direction} color={bpColor} />
          <BigStat label="Conviction" value={bp.conviction?.toFixed(3) ?? '—'} color={T.cyan} />
          <BigStat label="Confidence" value={bp.confidence ? `${bp.confidence}%` : '—'} color={T.cyan} />
        </div>
        {bp.source_reason && (
          <div style={{ fontSize: 10, color: T.textMuted, marginTop: 12, fontFamily: T.mono }}>
            Reason: <span style={{ color: T.text }}>{bp.source_reason}</span>
            {bp.model_version && (
              <> · Model: <span style={{ color: T.purple }}>{bp.model_version}</span></>
            )}
          </div>
        )}
        {bp.fallbacks_skipped && bp.fallbacks_skipped.length > 0 && (
          <div style={{ marginTop: 10, padding: 8, background: 'rgba(245,158,11,0.08)',
                        borderRadius: 4, border: `1px solid ${T.amber}44` }}>
            <span style={{ fontSize: 9, color: T.amber, fontWeight: 700,
                           textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Fallbacks skipped
            </span>
            <ul style={{ margin: '6px 0 0 16px', padding: 0, fontSize: 10, color: T.text, fontFamily: T.mono }}>
              {bp.fallbacks_skipped.map((s, i) => (
                <li key={i}>
                  <span style={{ color: T.amber }}>{s.source}</span>: {s.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {/* Layer grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 14 }}>
        <Card title="v1 — TimesFM Quantiles"
              badge={v1.status} badgeColor={v1.status === 'ok' ? T.green : T.amber}>
          <KV label="Predicted close" value={v1.predicted_close?.toFixed(2) ?? '—'} />
          <KV label="Expected move" value={v1.expected_move_bps != null ? `${v1.expected_move_bps.toFixed(2)} bps` : '—'}
              color={v1.expected_move_bps > 0 ? T.green : T.red} />
          <KV label="Vol forecast" value={v1.vol_forecast_bps != null ? `${v1.vol_forecast_bps.toFixed(1)} bps` : '—'} />
          <KV label="Horizon" value={v1.horizon_s ? `${v1.horizon_s}s` : '—'} />
          {v1.quantiles && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>
                Quantiles (last step)
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between',
                            fontSize: 10, fontFamily: T.mono, color: T.text }}>
                <span>p10 {v1.quantiles.p10?.toFixed(1)}</span>
                <span>p25 {v1.quantiles.p25?.toFixed(1)}</span>
                <span style={{ color: T.cyan, fontWeight: 700 }}>p50 {v1.quantiles.p50?.toFixed(1)}</span>
                <span>p75 {v1.quantiles.p75?.toFixed(1)}</span>
                <span>p90 {v1.quantiles.p90?.toFixed(1)}</span>
              </div>
            </div>
          )}
        </Card>

        <Card title="v2 — LightGBM Probability"
              badge={v2.status} badgeColor={v2.status === 'ok' ? T.green : T.amber}>
          <KV label="P(UP)" value={v2.probability_up?.toFixed(4) ?? '—'}
              color={v2.probability_up > 0.5 ? T.green : T.red} />
          <KV label="Raw (pre-calib)" value={v2.probability_raw?.toFixed(4) ?? '—'} />
          <KV label="Conviction" value={v2.conviction?.toFixed(3) ?? '—'} />
          <KV label="Delta bucket" value={v2.delta_bucket ?? '—'} />
          <KV label="Model family" value={v2.model_family ?? '—'} color={T.purple} />
          <KV label="Features used" value={v2.features_used ?? '—'} />
          {v2.scorer_stuck_detection && (
            <div style={{ marginTop: 8, padding: 6, fontSize: 10, fontFamily: T.mono,
                          background: v2.scorer_stuck_detection.is_stuck ? 'rgba(239,68,68,0.1)' : 'rgba(16,185,129,0.08)',
                          borderRadius: 3,
                          border: `1px solid ${v2.scorer_stuck_detection.is_stuck ? T.red : T.green}44` }}>
              <span style={{ color: v2.scorer_stuck_detection.is_stuck ? T.red : T.green, fontWeight: 700 }}>
                {v2.scorer_stuck_detection.is_stuck ? '⚠ SCORER STUCK' : '✓ scorer healthy'}
              </span>
              <span style={{ color: T.textMuted, marginLeft: 8 }}>
                span={v2.scorer_stuck_detection.span_last_30?.toExponential(2) ?? 'warming up'}
              </span>
            </div>
          )}
        </Card>

        <Card title="v3 — Composite Fusion"
              badge={v3.status} badgeColor={v3.status === 'ok' ? T.green : T.amber}>
          <KV label="Composite" value={v3.composite?.toFixed(4) ?? '—'}
              color={v3.composite > 0 ? T.green : v3.composite < 0 ? T.red : T.amber} />
          <KV label="Direction" value={v3.direction ?? '—'} />
          {v3.signals && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 9, color: T.textMuted, marginBottom: 4, textTransform: 'uppercase' }}>
                7-signal breakdown
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 4 }}>
                {Object.entries(v3.signals).map(([k, v]) => (
                  <div key={k} style={{ display: 'flex', justifyContent: 'space-between',
                                         fontSize: 10, fontFamily: T.mono,
                                         color: v3.signal_health?.[k] === 'ok' ? T.text : T.textMuted }}>
                    <span>{k}</span>
                    <span style={{ color: v > 0 ? T.green : v < 0 ? T.red : T.amber }}>
                      {v != null ? v.toFixed(3) : 'nan'}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>

        <Card title="v4 — Fusion Decision"
              badge={v4.status} badgeColor={v4.status === 'ok' ? T.green : T.amber}>
          <KV label="Regime" value={v4.regime_primary ?? '—'} />
          <KV label="Consensus" value={v4.consensus?.safe_to_trade ? '✓ safe' : '✗ unsafe'}
              color={v4.consensus?.safe_to_trade ? T.green : T.red} />
          <KV label="Max divergence" value={v4.consensus?.max_divergence_bps != null ?
              `${v4.consensus.max_divergence_bps.toFixed(2)} bps` : '—'} />
          {v4.macro && (
            <>
              <KV label="Macro bias" value={`${v4.macro.bias} (${v4.macro.confidence})`}
                  color={v4.macro.bias === 'BULL' ? T.green : v4.macro.bias === 'BEAR' ? T.red : T.amber} />
              <KV label="Gate" value={v4.macro.direction_gate ?? '—'} />
            </>
          )}
          {v4.recommended_action && (
            <div style={{ marginTop: 10, padding: 8, background: 'rgba(6,182,212,0.08)',
                          borderRadius: 4, border: `1px solid ${T.cyan}44` }}>
              <div style={{ fontSize: 10, color: T.cyan, fontWeight: 700,
                            textTransform: 'uppercase', marginBottom: 4 }}>
                Recommended Action
              </div>
              <div style={{ fontSize: 11, color: T.text, fontFamily: T.mono }}>
                {v4.recommended_action.side ? (
                  <>
                    <span style={{ color: v4.recommended_action.side === 'LONG' ? T.green : T.red }}>
                      {v4.recommended_action.side}
                    </span>
                    {' · size '}{(v4.recommended_action.collateral_pct * 100)?.toFixed(1)}%
                    {' · '}{v4.recommended_action.conviction}
                  </>
                ) : (
                  <span style={{ color: T.textMuted }}>skip: {v4.recommended_action.reason}</span>
                )}
              </div>
            </div>
          )}
        </Card>
      </div>

      {/* Service footer */}
      <div style={{ marginTop: 12, fontSize: 9, color: T.textMuted, fontFamily: T.mono,
                    display: 'flex', justifyContent: 'space-between' }}>
        <span>API v{service.api_version} · server {service.server_version} · uptime {service.uptime_s}s</span>
        <span>sequoia: {sequoia.current_sha || '—'} ({sequoia.current_family || '—'})</span>
      </div>
    </div>
  );
}

function BigStat({ label, value, color }) {
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 9, color: T.textMuted, textTransform: 'uppercase',
                    letterSpacing: '0.08em', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 800, color, fontFamily: T.mono }}>{value}</div>
    </div>
  );
}

function LoadingPanel() {
  return (
    <div style={{ padding: 40, textAlign: 'center', color: T.textMuted,
                  background: T.card, border: `1px solid ${T.cardBorder}`,
                  borderRadius: 8, fontSize: 11 }}>
      Loading /predict envelope…
    </div>
  );
}

function ErrorPanel({ error }) {
  return (
    <div style={{ padding: 20, background: 'rgba(239,68,68,0.1)',
                  border: `1px solid ${T.red}55`, borderRadius: 8,
                  color: T.red, fontSize: 11, fontFamily: T.mono }}>
      <strong>Error fetching /predict:</strong> {error}
    </div>
  );
}

// ─── Tab 2: Microstructure (VPIN + raw bundle) ────────────────────────────

function MicrostructureTab({ env, rawEnv, rawError }) {
  if (rawError) return <ErrorPanel error={rawError} />;
  if (!rawEnv) return <LoadingPanel />;

  const raw = rawEnv.raw || {};
  const vpin = raw.vpin || {};
  const coinglass = raw.coinglass || {};
  const prices = raw.prices || {};
  const v2_features = raw.v2_features || {};

  return (
    <div>
      <Card title="VPIN & Flow" subtitle="from /predict raw bundle">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 14 }}>
          <div>
            <KV label="VPIN value" value={vpin.value?.toFixed(4) ?? '—'}
                color={vpin.value > 0.7 ? T.red : vpin.value > 0.4 ? T.amber : T.green} />
            <KV label="Method" value={vpin.method ?? '—'} />
            <KV label="Status" value={vpin.status ?? '—'} />
            <VariableTag status="have" note="Signed flow imbalance proxy from PriceFeed" />
          </div>
          <div>
            <VariableRow name="vpin_percentile" status="todo"
                         note="Needs 1000-bucket rolling CDF" />
            <VariableRow name="vpin_8bar_hold" status="todo"
                         note="Needs volume-clock bucketing" />
            <VariableRow name="cvd" status="todo"
                         note="Needs buy-vs-sell volume classification" />
            <VariableRow name="cvd_slope_5" status="todo" />
            <VariableRow name="ofi" status="todo"
                         note="Needs L2 order book feed" />
            <VariableRow name="obi" status="todo"
                         note="Needs L2 order book feed" />
          </div>
          <div>
            <VariableRow name="funding_rate" status="have"
                         value={coinglass.funding_rate?.toFixed(6) ?? '—'} />
            <VariableRow name="oi_delta_pct" status="have"
                         value={coinglass.oi_delta_pct?.toFixed(4) ?? '—'} />
            <VariableRow name="taker_buy_usd" status="have"
                         value={coinglass.taker_buy_usd != null ? `$${(coinglass.taker_buy_usd / 1e6).toFixed(2)}M` : '—'} />
            <VariableRow name="taker_sell_usd" status="have"
                         value={coinglass.taker_sell_usd != null ? `$${(coinglass.taker_sell_usd / 1e6).toFixed(2)}M` : '—'} />
            <VariableRow name="liq_long_usd" status="have"
                         value={coinglass.liq_long_usd != null ? `$${(coinglass.liq_long_usd / 1e3).toFixed(0)}k` : '—'} />
            <VariableRow name="liq_short_usd" status="have"
                         value={coinglass.liq_short_usd != null ? `$${(coinglass.liq_short_usd / 1e3).toFixed(0)}k` : '—'} />
          </div>
        </div>
      </Card>

      <Card title="Price & Volatility" subtitle="computed in-process">
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 20 }}>
          <div>
            <KV label="Last price" value={prices.last?.toFixed(2) ?? '—'} color={T.cyan} />
            <KV label="Buffer size" value={prices.buffer_size ?? '—'} />
            {prices.stats_60s && (
              <>
                <KV label="60s min" value={prices.stats_60s.min?.toFixed(2)} />
                <KV label="60s max" value={prices.stats_60s.max?.toFixed(2)} />
                <KV label="60s mean" value={prices.stats_60s.mean?.toFixed(2)} />
                <KV label="60s std" value={prices.stats_60s.std?.toFixed(2)} />
              </>
            )}
          </div>
          <div>
            <VariableRow name="mid_price" status="have" value={prices.last?.toFixed(2)} />
            <VariableRow name="log_return_1b" status="todo"
                         note="Needs volume-bucketed returns" />
            <VariableRow name="rv_realised_vol" status="todo"
                         note="Needs 1h/4h/24h windows" />
            <VariableRow name="parkinson_vol" status="todo"
                         note="Needs OHLC aggregation" />
            <VariableRow name="vol_regime" status="todo"
                         note="Needs HMM classifier" />
            <VariableRow name="atr_14" status="todo" />
            <VariableRow name="bb_width" status="todo" />
            <VariableRow name="price_impact_coeff" status="todo"
                         note="Kyle's lambda from OFI/price regression" />
          </div>
        </div>
      </Card>

      <Card title="ML features consumed by v2 scorer" subtitle="raw feature dict from last score() call">
        {Object.keys(v2_features).length > 0 ? (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6 }}>
            {Object.entries(v2_features)
              .filter(([k]) => k !== 'status')
              .map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between',
                                       padding: '3px 6px', fontSize: 10, fontFamily: T.mono,
                                       borderBottom: `1px solid ${T.cardBorder}` }}>
                  <span style={{ color: T.textMuted }}>{k}</span>
                  <span style={{ color: v == null ? T.textDim : T.text }}>
                    {v == null ? 'null' : typeof v === 'number' ? v.toFixed(4) : String(v)}
                  </span>
                </div>
              ))}
          </div>
        ) : (
          <div style={{ color: T.textMuted, fontSize: 11 }}>Waiting for v2 features…</div>
        )}
      </Card>
    </div>
  );
}

function VariableRow({ name, status, value, note }) {
  const color = status === 'have' ? T.green : T.amber;
  const label = status === 'have' ? '✓ have' : '⏳ not yet';
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between',
                  padding: '4px 0', borderBottom: `1px solid ${T.cardBorder}`,
                  fontSize: 10 }}>
      <span>
        <Chip color={color} value={label} />
        <span style={{ marginLeft: 8, color: T.text, fontFamily: T.mono }}>{name}</span>
        {note && <span style={{ marginLeft: 8, color: T.textDim, fontSize: 9 }}>{note}</span>}
      </span>
      <span style={{ color: T.text, fontFamily: T.mono }}>{value ?? ''}</span>
    </div>
  );
}

function VariableTag({ status, note }) {
  const color = status === 'have' ? T.green : T.amber;
  return (
    <div style={{ marginTop: 6, fontSize: 9 }}>
      <Chip color={color} value={status === 'have' ? '✓ HAVE' : '⏳ NOT YET'} />
      {note && <span style={{ marginLeft: 6, color: T.textDim }}>{note}</span>}
    </div>
  );
}

// ─── Tab 3: Gates ─────────────────────────────────────────────────────────

const GATES = [
  { id: 'G1', name: 'VPIN threshold',          cond: 'vpin < 0.70',                       onFail: 'Block entry',              status: 'partial', note: 'Have VPIN proxy, need volume-clock' },
  { id: 'G2', name: 'VPIN hold',                cond: 'vpin_8bar_hold == false',           onFail: 'Block entry',              status: 'todo',    note: 'Needs volume-clock bucketing' },
  { id: 'G3', name: 'Signal quorum',            cond: 'signal_quorum ≥ 4',                 onFail: 'Block entry',              status: 'partial', note: 'Computable from /predict layers' },
  { id: 'G4', name: 'Ensemble confidence',      cond: 'ensemble_confidence ≥ 0.55',        onFail: 'Block entry',              status: 'have',    note: 'best_probability.confidence' },
  { id: 'G5', name: 'TimesFM edge',             cond: '|timesfm_prob_up − 0.5| ≥ 0.06',    onFail: 'Reduce size 50%',          status: 'have',    note: 'Derived from v1.expected_move_bps' },
  { id: 'G6', name: 'Spread',                   cond: 'spread_bps < spread_p75',           onFail: 'Block or widen TP',        status: 'partial', note: 'Have from coinglass, need p75 calibration' },
  { id: 'G7', name: 'Liquidity depth',          cond: 'depth_bid/ask_5 > 3× order size',   onFail: 'Reduce size to 0.1× depth', status: 'todo',    note: 'Needs L2 order book feed' },
  { id: 'G8', name: 'Liquidation risk',          cond: 'liquidation_cascade_risk == false', onFail: 'Hard block',              status: 'todo',    note: 'Needs VPIN + OBI combo' },
  { id: 'G9', name: 'OTT ratio',                cond: 'ott_ratio < ott_p90',               onFail: 'Block (manipulation)',     status: 'todo',    note: 'Needs order-to-trade tracking' },
  { id: 'G10', name: 'Drawdown',                cond: 'current_drawdown_pct < 0.02',       onFail: 'Halve size; block at 5%',  status: 'have',    note: 'From margin engine portfolio state' },
  { id: 'G11', name: 'Regime match',            cond: 'model_regime_match ≥ 0.65',         onFail: 'Reduce size 50%',          status: 'todo',    note: 'Needs in-sample cosine distance' },
  { id: 'G12', name: 'Funding rate',            cond: '|funding_rate| < funding_p95',      onFail: 'Carry cost warning',       status: 'partial', note: 'Have value, need p95 calibration' },
];

function GatesTab({ env }) {
  return (
    <div>
      <Card title="12-Gate Entry Logic"
            subtitle="target ensemble gate stack from the reference spec">
        <div style={{ color: T.textMuted, fontSize: 10, marginBottom: 12 }}>
          Gates are evaluated in order. Each shows current status: <Chip color={T.green} value="✓ have" />
          {' '}<Chip color={T.amber} value="~ partial" />
          {' '}<Chip color={T.red} value="⏳ not yet" />
        </div>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
          <thead>
            <tr style={{ color: T.textMuted, textAlign: 'left',
                         borderBottom: `2px solid ${T.cardBorder}` }}>
              <th style={{ padding: 6 }}>ID</th>
              <th style={{ padding: 6 }}>Gate</th>
              <th style={{ padding: 6 }}>Condition</th>
              <th style={{ padding: 6 }}>On fail</th>
              <th style={{ padding: 6 }}>Status</th>
              <th style={{ padding: 6 }}>Notes</th>
            </tr>
          </thead>
          <tbody>
            {GATES.map((g) => {
              const statusColor =
                g.status === 'have' ? T.green :
                g.status === 'partial' ? T.amber : T.red;
              const statusLabel =
                g.status === 'have' ? '✓ have' :
                g.status === 'partial' ? '~ partial' : '⏳ not yet';
              return (
                <tr key={g.id} style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
                  <td style={{ padding: 6, color: T.purple, fontFamily: T.mono, fontWeight: 700 }}>{g.id}</td>
                  <td style={{ padding: 6, color: T.white, fontWeight: 600 }}>{g.name}</td>
                  <td style={{ padding: 6, color: T.text, fontFamily: T.mono }}>{g.cond}</td>
                  <td style={{ padding: 6, color: T.amber }}>{g.onFail}</td>
                  <td style={{ padding: 6 }}><Chip color={statusColor} value={statusLabel} /></td>
                  <td style={{ padding: 6, color: T.textDim, fontSize: 9 }}>{g.note}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

// ─── Tab 4: Live Ticks vs Outcomes ────────────────────────────────────────

function LiveTicksTab({ asset, timeframe, setTimeframe }) {
  const api = useApi();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    const fetchTicks = async () => {
      try {
        const resp = await api('GET',
          `/predict/ticks_vs_outcomes?asset=${asset}&timeframe=${timeframe}&limit=100`);
        if (live) {
          setData(resp?.data || resp);
          setError(null);
        }
      } catch (e) {
        if (live) setError(e.message || String(e));
      }
    };
    fetchTicks();
    const id = setInterval(fetchTicks, POLL_TICKS_MS);
    return () => { live = false; clearInterval(id); };
  }, [api, asset, timeframe]);

  if (error) return <ErrorPanel error={error} />;
  if (!data) return <LoadingPanel />;

  const rows = data.rows || [];
  const summary = data.summary || {};

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
        <span style={{ fontSize: 10, color: T.textMuted, textTransform: 'uppercase' }}>
          Horizon:
        </span>
        {TIMEFRAMES.map((tf) => (
          <button
            key={tf}
            onClick={() => setTimeframe(tf)}
            style={{
              padding: '6px 12px', fontSize: 10, fontFamily: T.mono,
              background: tf === timeframe ? T.cyan : 'transparent',
              color: tf === timeframe ? T.white : T.text,
              border: `1px solid ${tf === timeframe ? T.cyan : T.cardBorder}`,
              borderRadius: 4, cursor: 'pointer', fontWeight: 700,
            }}
          >
            {tf}
          </button>
        ))}
      </div>

      <Card title={`Live prediction ticks vs outcomes — ${asset} ${timeframe}`}
            subtitle={`${summary.n_total ?? 0} ticks · ${summary.n_with_outcome ?? 0} resolved · hit rate ${
              summary.hit_rate != null ? (summary.hit_rate * 100).toFixed(1) + '%' : '—'
            }`}>
        {data.note && (
          <div style={{ padding: 10, fontSize: 11, color: T.amber,
                        background: 'rgba(245,158,11,0.08)', borderRadius: 4, marginBottom: 10 }}>
            {data.note}
          </div>
        )}
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 10 }}>
          <thead>
            <tr style={{ color: T.textMuted, textAlign: 'left',
                         borderBottom: `2px solid ${T.cardBorder}` }}>
              <th style={{ padding: 5 }}>ts</th>
              <th style={{ padding: 5 }}>p_up</th>
              <th style={{ padding: 5 }}>pred</th>
              <th style={{ padding: 5 }}>actual</th>
              <th style={{ padding: 5 }}>correct</th>
              <th style={{ padding: 5 }}>move (bps)</th>
              <th style={{ padding: 5 }}>source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => {
              const correctColor =
                r.correct === true ? T.green :
                r.correct === false ? T.red : T.textDim;
              return (
                <tr key={i} style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
                  <td style={{ padding: 5, color: T.textDim, fontFamily: T.mono, fontSize: 9 }}>
                    {r.ts ? new Date(r.ts).toLocaleTimeString('en-GB') : '—'}
                  </td>
                  <td style={{ padding: 5, color: T.text, fontFamily: T.mono }}>
                    {r.probability_up?.toFixed(4) ?? '—'}
                  </td>
                  <td style={{ padding: 5, fontFamily: T.mono,
                                color: r.predicted_direction === 'UP' ? T.green :
                                       r.predicted_direction === 'DOWN' ? T.red : T.textDim }}>
                    {r.predicted_direction ?? '—'}
                  </td>
                  <td style={{ padding: 5, fontFamily: T.mono,
                                color: r.actual_direction === 'UP' ? T.green :
                                       r.actual_direction === 'DOWN' ? T.red : T.textDim }}>
                    {r.actual_direction ?? '—'}
                  </td>
                  <td style={{ padding: 5, color: correctColor, fontWeight: 700 }}>
                    {r.correct === true ? '✓' : r.correct === false ? '✗' : '—'}
                  </td>
                  <td style={{ padding: 5, fontFamily: T.mono,
                                color: r.window_move_bps > 0 ? T.green :
                                       r.window_move_bps < 0 ? T.red : T.textDim }}>
                    {r.window_move_bps?.toFixed(1) ?? '—'}
                  </td>
                  <td style={{ padding: 5, color: T.textMuted, fontSize: 9 }}>
                    {r.outcome_source ?? '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
    </div>
  );
}

// ─── Tab 5: API Docs ──────────────────────────────────────────────────────

function ApiDocsTab({ env }) {
  return (
    <Card title="POST /api/predict — Canonical Versioned Envelope">
      <div style={{ color: T.text, fontSize: 11, lineHeight: 1.6 }}>
        <p style={{ marginTop: 0 }}>
          One canonical endpoint that returns every prediction layer plus a
          <span style={{ color: T.cyan, fontFamily: T.mono }}> best_probability </span>
          pick and an optional <span style={{ color: T.cyan, fontFamily: T.mono }}>raw</span> introspection bundle.
        </p>
        <h4 style={{ color: T.white, fontSize: 12, marginTop: 16 }}>Request</h4>
        <JsonPeek data={{
          asset: 'BTC',
          timeframe: '15m',
          seconds_to_close: 480,
          accept_versions: ['v1', 'v2', 'v3', 'v4'],
          include_raw: false,
          strategy: 'fee_aware_15m',
          tolerate_stale_ms: 2000,
        }} maxHeight={200} />

        <h4 style={{ color: T.white, fontSize: 12, marginTop: 16 }}>Field reference</h4>
        <table style={{ width: '100%', fontSize: 10, borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ color: T.textMuted, textAlign: 'left',
                         borderBottom: `2px solid ${T.cardBorder}` }}>
              <th style={{ padding: 5 }}>Field</th>
              <th style={{ padding: 5 }}>Type</th>
              <th style={{ padding: 5 }}>Default</th>
              <th style={{ padding: 5 }}>Description</th>
            </tr>
          </thead>
          <tbody>
            <ApiField name="asset" type="string" required="✓"
                     desc="BTC | ETH | SOL | XRP. Case-insensitive, lowercased server-side." />
            <ApiField name="timeframe" type="literal" required="✓"
                     desc='"5m" | "15m" | "1h" | "4h" | "24h"' />
            <ApiField name="seconds_to_close" type="int?" default="computed"
                     desc="Seconds until window closes. Default: server computes from active window boundary." />
            <ApiField name="accept_versions" type="string[]" default='["v1","v2","v3","v4"]'
                     desc="Which layers to build. Others omitted from response." />
            <ApiField name="include_raw" type="bool" default="false"
                     desc="Opt-in raw feature bundle (~30KB). prices, coinglass, vpin, v2_features, v3_signals_per_timescale, feature_freshness_ms_all." />
            <ApiField name="strategy" type="string" default="fee_aware_15m"
                     desc="Named v4 strategy template for recommended_action." />
            <ApiField name="tolerate_stale_ms" type="int" default="2000"
                     desc="Max feature-source age (ms) before flagging stale." />
          </tbody>
        </table>

        <h4 style={{ color: T.white, fontSize: 12, marginTop: 16 }}>Response layers</h4>
        <ul style={{ fontSize: 10, color: T.text, fontFamily: T.mono, lineHeight: 1.8 }}>
          <li><span style={{ color: T.cyan }}>request</span> — echo of the input request + request_ts</li>
          <li><span style={{ color: T.cyan }}>service</span> — api_version, server_version, uptime_s</li>
          <li><span style={{ color: T.cyan }}>best_probability</span> — source, value, direction, conviction, confidence, fallbacks_skipped</li>
          <li><span style={{ color: T.cyan }}>v1</span> — TimesFM quantiles, expected_move_bps, vol_forecast_bps</li>
          <li><span style={{ color: T.cyan }}>v2</span> — LightGBM probability_up/raw, model_version, scorer_stuck_detection</li>
          <li><span style={{ color: T.cyan }}>v3</span> — composite, 7 signals, signal_health, cascade_state, timescales_agreement</li>
          <li><span style={{ color: T.cyan }}>v4</span> — consensus (6 sources), macro (per-horizon), events, recommended_action</li>
          <li><span style={{ color: T.cyan }}>sequoia</span> — model registry snapshot</li>
          <li><span style={{ color: T.cyan }}>raw</span> — <em>present iff include_raw=true</em> — prices.last_120, coinglass full dict, vpin computed, v2_features, v3_signals_per_timescale</li>
        </ul>

        <h4 style={{ color: T.white, fontSize: 12, marginTop: 16 }}>Error responses</h4>
        <ul style={{ fontSize: 10, color: T.text, fontFamily: T.mono, lineHeight: 1.8 }}>
          <li><span style={{ color: T.red }}>422</span> — pydantic validation failure (bad asset, bad timeframe, unknown version)</li>
          <li><span style={{ color: T.red }}>503</span> — assembler not installed (service starting up)</li>
          <li><span style={{ color: T.red }}>500</span> — assembler raised. Sanitized error (class name only)</li>
        </ul>

        <h4 style={{ color: T.white, fontSize: 12, marginTop: 16 }}>Live sample response</h4>
        {env ? <JsonPeek data={env} maxHeight={400} /> : <LoadingPanel />}
      </div>
    </Card>
  );
}

function ApiField({ name, type, required, default: dflt, desc }) {
  return (
    <tr style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
      <td style={{ padding: 5, color: T.cyan, fontFamily: T.mono, fontWeight: 700 }}>{name}</td>
      <td style={{ padding: 5, color: T.purple, fontFamily: T.mono }}>{type}</td>
      <td style={{ padding: 5, color: required ? T.red : T.textDim, fontFamily: T.mono }}>
        {required || dflt}
      </td>
      <td style={{ padding: 5, color: T.text }}>{desc}</td>
    </tr>
  );
}

// ─── Tab 6: Schema (full variable catalog with ✓have / ⏳not-yet) ─────────

const SCHEMA_GROUPS = [
  {
    name: 'Order flow & microstructure',
    vars: [
      { v: 'vpin',              s: 'partial', note: 'Proxy from price feed; needs volume-clock' },
      { v: 'vpin_percentile',   s: 'todo',    note: 'Rolling 1000-bucket CDF' },
      { v: 'vpin_8bar_hold',    s: 'todo',    note: 'Needs volume-bar sequence' },
      { v: 'cvd',               s: 'todo',    note: 'Cumulative volume delta from bulk classification' },
      { v: 'cvd_slope_5',       s: 'todo' },
      { v: 'ofi',               s: 'todo',    note: 'Order flow imbalance; needs L2 book' },
      { v: 'obi',               s: 'todo',    note: 'LOB imbalance; needs L2 book' },
      { v: 'obi_levels',        s: 'todo',    note: 'Depth 1–10 per level' },
      { v: 'ott_ratio',         s: 'todo',    note: 'Order-to-trade; needs order tracking' },
      { v: 'tto_ratio',         s: 'todo' },
      { v: 'market_resilience', s: 'todo',    note: 'LOB recovery speed' },
      { v: 'spread_bps',        s: 'partial', note: 'Have binance bid/ask; need percentile gate' },
      { v: 'depth_bid_5',       s: 'todo',    note: 'Needs L2 book' },
      { v: 'depth_ask_5',       s: 'todo' },
      { v: 'vpin_bucket_size',  s: 'todo',    note: 'Metadata for volume-clock' },
    ],
  },
  {
    name: 'Price & volatility',
    vars: [
      { v: 'mid_price',         s: 'have',    note: '(bid+ask)/2' },
      { v: 'log_return_1b',     s: 'todo',    note: 'Volume-bucketed returns' },
      { v: 'rv_realised_vol',   s: 'todo',    note: '1h/4h/24h windows' },
      { v: 'parkinson_vol',     s: 'todo',    note: 'High-low range estimator' },
      { v: 'vol_regime',        s: 'todo',    note: 'HMM classifier' },
      { v: 'atr_14',            s: 'todo' },
      { v: 'bb_width',          s: 'todo' },
      { v: 'price_impact_coeff', s: 'todo',   note: "Kyle's lambda" },
    ],
  },
  {
    name: 'TWAP / execution',
    vars: [
      { v: 'twap_delta',        s: 'have',    note: 'From signal_evaluations' },
      { v: 'twap_5m',           s: 'partial' },
      { v: 'twap_15m',          s: 'partial' },
      { v: 'vwap',              s: 'todo' },
      { v: 'vwap_delta',        s: 'todo' },
      { v: 'exec_strategy',     s: 'partial', note: 'Auto-select per VPIN state' },
      { v: 'slippage_est_bps',  s: 'todo' },
    ],
  },
  {
    name: 'ML model signals',
    vars: [
      { v: 'timesfm_prob_up',        s: 'have',    note: 'Via v1.expected_move_bps mapping' },
      { v: 'timesfm_conf_interval',  s: 'have',    note: 'p10/p90 from quantiles' },
      { v: 'model_brier_score',      s: 'todo',    note: 'Rolling 200-prediction window' },
      { v: 'ensemble_prob_up',       s: 'have',    note: 'best_probability.value' },
      { v: 'ensemble_confidence',    s: 'have',    note: 'best_probability.confidence' },
      { v: 'signal_quorum',          s: 'partial', note: 'Derivable from v1/v2/v3 agreement' },
      { v: 'feature_importance',     s: 'todo',    note: 'Per-prediction Shapley values' },
      { v: 'model_regime_match',     s: 'todo',    note: 'Cosine distance to training distribution' },
    ],
  },
  {
    name: 'Gamma / derivatives',
    vars: [
      { v: 'gamma_exposure',     s: 'todo',    note: 'GEX from options OI' },
      { v: 'gex_flip_level',     s: 'todo' },
      { v: 'delta_exposure',     s: 'todo' },
      { v: 'iv_rank',            s: 'todo',    note: 'IV rank 0-100' },
      { v: 'put_call_ratio',     s: 'todo' },
      { v: 'funding_rate',       s: 'have',    note: 'CoinGlass perps' },
      { v: 'open_interest_delta', s: 'have',   note: 'oi_delta_pct' },
    ],
  },
  {
    name: 'Risk management & sizing',
    vars: [
      { v: 'kelly_fraction',          s: 'todo',    note: 'f* = (bp − q) / b' },
      { v: 'kelly_multipliers[7]',    s: 'todo',    note: '7 scaling factors' },
      { v: 'current_drawdown_pct',    s: 'have',    note: 'From margin engine portfolio' },
      { v: 'max_drawdown_pct',        s: 'have' },
      { v: 'var_95',                  s: 'todo' },
      { v: 'expected_shortfall',      s: 'todo',    note: 'CVaR' },
      { v: 'stop_loss_dynamic',       s: 'partial', note: 'ATR-scaled dynamic' },
      { v: 'trailing_stop_pct',       s: 'have' },
      { v: 'edge_reversal_signal',    s: 'partial', note: 'When best_probability direction flips' },
      { v: 'liquidation_cascade_risk', s: 'todo',   note: 'VPIN > 0.80 AND OBI one-directional' },
      { v: 'correlation_shift',       s: 'todo',    note: 'Rolling BTC/ETH correlation' },
    ],
  },
  {
    name: 'Market regime',
    vars: [
      { v: 'regime_state',        s: 'partial', note: 'v3 regime via 5 enums' },
      { v: 'regime_confidence',   s: 'todo' },
      { v: 'trend_strength',      s: 'todo',    note: 'ADX-derived' },
      { v: 'hurst_exponent',      s: 'todo',    note: 'Rolling 512-tick Hurst' },
      { v: 'session_context',     s: 'todo',    note: 'Asia/London/NY session label' },
      { v: 'macro_event_flag',    s: 'have',    note: 'macro_events table' },
      { v: 'crypto_specific_event', s: 'todo',  note: 'Exchange maintenance / halvings' },
    ],
  },
  {
    name: 'Performance & monitoring',
    vars: [
      { v: 'win_rate_rolling',    s: 'partial', note: 'From margin_positions' },
      { v: 'sharpe_session',      s: 'todo' },
      { v: 'sortino_ratio',       s: 'todo' },
      { v: 'profit_factor',       s: 'todo' },
      { v: 'avg_hold_time',       s: 'partial' },
      { v: 'tp_sl_ratio',         s: 'partial' },
      { v: 'slippage_realised_bps', s: 'todo' },
      { v: 'pipeline_pass_rate',  s: 'todo',    note: 'Computable from gate_audit' },
      { v: 'gate_failure_counts[12]', s: 'partial', note: 'gate_audit per gate per session' },
      { v: 'paper_vs_live_divergence', s: 'todo' },
    ],
  },
];

function SchemaTab() {
  const totals = SCHEMA_GROUPS.reduce(
    (acc, g) => {
      g.vars.forEach((v) => {
        acc.total++;
        if (v.s === 'have') acc.have++;
        else if (v.s === 'partial') acc.partial++;
        else acc.todo++;
      });
      return acc;
    },
    { total: 0, have: 0, partial: 0, todo: 0 },
  );

  return (
    <div>
      <Card title="Target ensemble variable catalog"
            subtitle={`${totals.total} variables · ${totals.have} have · ${totals.partial} partial · ${totals.todo} not yet`}>
        <div style={{ display: 'flex', gap: 10, marginBottom: 14 }}>
          <Chip color={T.green} value={`${totals.have} ✓ HAVE`} />
          <Chip color={T.amber} value={`${totals.partial} ~ PARTIAL`} />
          <Chip color={T.red} value={`${totals.todo} ⏳ NOT YET`} />
        </div>
        <div style={{ color: T.textMuted, fontSize: 10, marginBottom: 16 }}>
          Full reference schema from the VPIN-based ensemble spec. Variables are tagged against
          what /predict currently exposes. Items marked <Chip color={T.red} value="⏳ NOT YET" />
          {' '}need new infrastructure (L2 order book feed, options GEX feed, HMM regime classifier,
          volume-clock bucketing). See the VPIN advanced plan doc (separate) for the roadmap.
        </div>
        {SCHEMA_GROUPS.map((group) => (
          <div key={group.name} style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 11, color: T.white, fontWeight: 800,
                          textTransform: 'uppercase', letterSpacing: '0.05em',
                          marginBottom: 8, borderBottom: `1px solid ${T.cardBorder}`,
                          paddingBottom: 4 }}>
              {group.name}
            </div>
            {group.vars.map((v, i) => {
              const color = v.s === 'have' ? T.green : v.s === 'partial' ? T.amber : T.red;
              const label = v.s === 'have' ? '✓ have' : v.s === 'partial' ? '~ partial' : '⏳ not yet';
              return (
                <div key={i} style={{ display: 'flex', alignItems: 'center',
                                       padding: '4px 0', borderBottom: `1px solid ${T.cardBorder}`,
                                       fontSize: 10 }}>
                  <div style={{ width: 90 }}><Chip color={color} value={label} /></div>
                  <div style={{ width: 220, color: T.text, fontFamily: T.mono }}>{v.v}</div>
                  <div style={{ color: T.textDim, fontSize: 9 }}>{v.note}</div>
                </div>
              );
            })}
          </div>
        ))}
      </Card>
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────

export default function Assembler1() {
  const api = useApi();
  const [activeTab, setActiveTab] = useState('overview');
  const [timeframe, setTimeframe] = useState(DEFAULT_TF);
  const [env, setEnv] = useState(null);        // POST /predict (slim)
  const [rawEnv, setRawEnv] = useState(null);  // POST /predict (with raw)
  const [envError, setEnvError] = useState(null);
  const [rawError, setRawError] = useState(null);

  // Slim envelope poll — every tab uses this
  useEffect(() => {
    let live = true;
    const fetchEnv = async () => {
      try {
        const resp = await api('POST', '/predict', {
          data: { asset: 'BTC', timeframe, include_raw: false },
        });
        if (live) {
          setEnv(resp?.data || resp);
          setEnvError(null);
        }
      } catch (e) {
        if (live) setEnvError(e?.response?.data?.detail || e.message || String(e));
      }
    };
    fetchEnv();
    const id = setInterval(fetchEnv, POLL_ENVELOPE_MS);
    return () => { live = false; clearInterval(id); };
  }, [api, timeframe]);

  // Raw envelope poll — slower cadence because the payload is bigger
  // and only Microstructure + API Docs tabs consume it
  useEffect(() => {
    if (activeTab !== 'microstructure' && activeTab !== 'api') return;
    let live = true;
    const fetchRaw = async () => {
      try {
        const resp = await api('POST', '/predict', {
          data: { asset: 'BTC', timeframe, include_raw: true },
        });
        if (live) {
          setRawEnv(resp?.data || resp);
          setRawError(null);
        }
      } catch (e) {
        if (live) setRawError(e?.response?.data?.detail || e.message || String(e));
      }
    };
    fetchRaw();
    const id = setInterval(fetchRaw, POLL_ENVELOPE_MS * 2);
    return () => { live = false; clearInterval(id); };
  }, [api, timeframe, activeTab]);

  return (
    <div style={{ padding: '20px 24px', maxWidth: 1400, margin: '0 auto',
                  background: T.bg, minHeight: '100vh', color: T.text }}>
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 20, fontWeight: 800, color: T.white, margin: 0 }}>
          Assembler1 <span style={{ fontSize: 12, color: T.cyan, marginLeft: 12,
                                      fontFamily: T.mono, letterSpacing: '0.05em' }}>
            /data/assembler1
          </span>
        </h1>
        <div style={{ fontSize: 11, color: T.textMuted, marginTop: 4 }}>
          Canonical reference dashboard for POST /predict envelope + target ensemble schema
        </div>
      </div>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 16,
                    borderBottom: `1px solid ${T.cardBorder}` }}>
        {[
          { id: 'overview',       label: 'Overview' },
          { id: 'microstructure', label: 'Microstructure' },
          { id: 'gates',          label: 'Gates' },
          { id: 'ticks',          label: 'Live Ticks vs Outcomes' },
          { id: 'api',            label: 'API Docs' },
          { id: 'schema',         label: 'Schema' },
        ].map((t) => (
          <button
            key={t.id}
            onClick={() => setActiveTab(t.id)}
            style={{
              padding: '8px 16px', fontSize: 11, fontWeight: 700,
              background: activeTab === t.id ? T.card : 'transparent',
              color: activeTab === t.id ? T.white : T.textMuted,
              border: 'none',
              borderBottom: `2px solid ${activeTab === t.id ? T.cyan : 'transparent'}`,
              cursor: 'pointer',
              textTransform: 'uppercase', letterSpacing: '0.05em',
            }}
          >
            {t.label}
          </button>
        ))}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 8 }}>
          <StatusDot status={env?.v2?.status} />
          <span style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>
            {timeframe} · {env?.service?.uptime_s ? `up ${env.service.uptime_s}s` : 'connecting…'}
          </span>
        </div>
      </div>

      {/* Tab body */}
      {activeTab === 'overview' && <OverviewTab env={env} error={envError} />}
      {activeTab === 'microstructure' && <MicrostructureTab env={env} rawEnv={rawEnv} rawError={rawError} />}
      {activeTab === 'gates' && <GatesTab env={env} />}
      {activeTab === 'ticks' && <LiveTicksTab asset="BTC" timeframe={timeframe} setTimeframe={setTimeframe} />}
      {activeTab === 'api' && <ApiDocsTab env={rawEnv || env} />}
      {activeTab === 'schema' && <SchemaTab />}
    </div>
  );
}
