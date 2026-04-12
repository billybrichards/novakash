import React, { useState, useCallback, useEffect } from 'react';
import { T, fmt, GATE_NAMES } from './theme.js';
import { useApi } from '../../../hooks/useApi.js';

// Read cap mode from localStorage (same keys as StatusBar uses)
function getCapMode()  { return localStorage.getItem('btc-trader-cap-mode')  || 'dynamic'; }
function getCapValue() { return localStorage.getItem('btc-trader-cap-value') || '0.65'; }

/**
 * Band 4 — Gate Pipeline (left) + Manual Trade Panel (right).
 *
 * Left:  8-gate horizontal strip showing actual values vs thresholds.
 * Right: Simplified manual trade panel with auto-filled rationale.
 */

// --- Gate Chip ---

function GateChip({ name, displayName, passed, value, threshold, extraLabel }) {
  const color = passed ? T.green : T.red;
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 2,
      padding: '5px 8px', borderRadius: 4, flex: '1 1 0', minWidth: 90,
      background: passed ? 'rgba(16,185,129,0.06)' : 'rgba(239,68,68,0.06)',
      border: `1px solid ${passed ? 'rgba(16,185,129,0.25)' : 'rgba(239,68,68,0.25)'}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span style={{ fontSize: 11 }}>{passed ? '\u2705' : '\u274C'}</span>
        <span style={{ fontSize: 9, fontWeight: 700, color: T.text, letterSpacing: '0.04em' }}>
          {displayName}
        </span>
        {extraLabel && (
          <span style={{
            fontSize: 7, padding: '1px 4px', borderRadius: 2,
            background: extraLabel.startsWith('MAN') ? 'rgba(168,85,247,0.15)' : 'rgba(6,182,212,0.15)',
            color: extraLabel.startsWith('MAN') ? T.purple : T.cyan,
            fontWeight: 700, letterSpacing: '0.05em',
          }}>{extraLabel}</span>
        )}
      </div>
      <div style={{ fontSize: 8, color: T.textMuted, lineHeight: 1.3 }}>
        {value != null && threshold != null ? (
          <span>
            <span style={{ color, fontWeight: 600 }}>{value}</span>
            <span style={{ color: T.textDim }}> vs </span>
            <span>{threshold}</span>
          </span>
        ) : (
          <span style={{ color }}>{passed ? 'PASS' : 'FAIL'}</span>
        )}
      </div>
    </div>
  );
}

// --- Gate Pipeline Strip ---

function GatePipelineStrip({ hqData }) {
  const hb = hqData?.gate_heartbeat?.[0] || {};
  const gateResults = hb.gate_results || {};

  // Track cap mode from localStorage so chip label updates when user changes it
  const [capMode, setCapMode] = useState(getCapMode);
  const [capValue, setCapValue] = useState(getCapValue);
  useEffect(() => {
    const onStorage = () => { setCapMode(getCapMode()); setCapValue(getCapValue()); };
    window.addEventListener('storage', onStorage);
    // Also poll — same-tab changes don't fire storage events
    const t = setInterval(onStorage, 1000);
    return () => { window.removeEventListener('storage', onStorage); clearInterval(t); };
  }, []);

  // Build gate chips from gate_results. The keys in gate_results vary,
  // so we map known gates and display what we find.
  const gateOrder = [
    'eval_offset', 'gate_agreement', 'gate_delta', 'gate_taker',
    'gate_cg_veto', 'gate_dune', 'gate_spread', 'gate_cap',
  ];

  const chips = gateOrder.map(key => {
    const displayName = GATE_NAMES[key] || key;
    const result = gateResults[key];

    // gate_results can be: true/false, "PASS"/"FAIL", or an object {pass, value, threshold}
    let passed = false;
    let value = null;
    let threshold = null;

    if (typeof result === 'boolean') {
      passed = result;
    } else if (typeof result === 'string') {
      passed = result === 'PASS' || result === 'pass';
    } else if (result && typeof result === 'object') {
      passed = result.pass === true || result.passed === true || result.result === 'PASS';
      value = result.value ?? result.actual ?? null;
      threshold = result.threshold ?? result.required ?? null;
      // Format numeric values
      if (typeof value === 'number') value = fmt(value, 3);
      if (typeof threshold === 'number') threshold = fmt(threshold, 3);
    }

    // For gate_cap: augment threshold label with current cap mode indicator
    let extraLabel = null;
    if (key === 'gate_cap') {
      if (capMode === 'manual') {
        extraLabel = `MAN $${capValue}`;
        // Override threshold display with manual cap when engine result has none
        if (threshold == null) threshold = `$${capValue}`;
      } else {
        extraLabel = 'AUTO';
        if (threshold == null) threshold = 'dynamic';
      }
    }

    return { key, displayName, passed, value, threshold, extraLabel };
  });

  // Also check for any extra gates in the results we didn't list
  const knownKeys = new Set(gateOrder);
  const extraGates = Object.keys(gateResults)
    .filter(k => !knownKeys.has(k) && k !== 'overall')
    .map(key => {
      const result = gateResults[key];
      const passed = result === true || result === 'PASS' || result?.pass === true;
      return { key, displayName: key, passed, value: null, threshold: null };
    });

  return (
    <div style={{
      flex: '1.2 1 0', minWidth: 0,
    }}>
      <div style={{
        fontSize: 8, color: T.purple, letterSpacing: '0.12em',
        fontWeight: 700, textTransform: 'uppercase', marginBottom: 6,
      }}>Gate Pipeline</div>
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 4,
      }}>
        {chips.map(g => (
          <GateChip key={g.key} name={g.key} displayName={g.displayName} passed={g.passed} value={g.value} threshold={g.threshold} extraLabel={g.extraLabel} />
        ))}
        {extraGates.map(g => (
          <GateChip key={g.key} {...g} />
        ))}
      </div>
      {/* Overall result */}
      {hb.skip_reason && (
        <div style={{
          marginTop: 6, padding: '3px 8px', borderRadius: 3,
          background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.2)',
          fontSize: 9, color: T.red, fontFamily: T.mono,
        }}>
          Blocked: {hb.skip_reason}
        </div>
      )}
    </div>
  );
}

// --- Simplified Manual Trade Panel ---

function ManualTradePanel({ hqData }) {
  const api = useApi();
  const [rationale, setRationale] = useState('');
  const [executing, setExecuting] = useState(false);
  const [result, setResult] = useState(null);
  const [showConfirm, setShowConfirm] = useState(false);
  const [tradeDir, setTradeDir] = useState(null); // null = follow signal

  const w = hqData?.windows?.[0] || {};
  const system = hqData?.system || {};
  const signalDirection = w.direction || 'UP';
  const direction = tradeDir || signalDirection;
  const isPaper = system.paper_mode !== false;

  // Reset override when signal direction changes
  React.useEffect(() => {
    setTradeDir(null);
  }, [signalDirection]);

  // Auto-build rationale from current snapshot
  const hb = hqData?.gate_heartbeat?.[0] || {};
  const autoRationale = buildRationale(w, hb);

  const handleTrade = useCallback(async () => {
    setExecuting(true);
    setResult(null);
    try {
      const res = await api.post('/v58/manual-trade', {
        direction,
        order_type: 'FAK',
        stake: '4.00',
        rationale: rationale || autoRationale,
      });
      const data = res?.data || res;
      setResult({ ok: true, msg: data.message || 'Trade submitted' });
      setShowConfirm(false);
    } catch (err) {
      setResult({ ok: false, msg: err.response?.data?.detail || err.message || 'Failed' });
    } finally {
      setExecuting(false);
    }
  }, [api, direction, rationale, autoRationale]);

  return (
    <div style={{ flex: '0.8 1 0', minWidth: 200 }}>
      <div style={{
        fontSize: 8, color: T.purple, letterSpacing: '0.12em',
        fontWeight: 700, textTransform: 'uppercase', marginBottom: 6,
      }}>Manual Trade</div>

      <div style={{
        background: T.card, border: `1px solid ${T.cardBorder}`,
        borderRadius: 6, padding: '8px 10px', fontFamily: T.mono,
      }}>
        {/* Direction toggle */}
        <div style={{ display: 'flex', gap: 4, marginBottom: 6 }}>
          <button
            onClick={() => setTradeDir('UP')}
            style={{
              flex: 1, padding: '4px 0', borderRadius: 3, border: 'none',
              cursor: 'pointer', fontFamily: T.mono, fontSize: 11, fontWeight: 800,
              background: direction === 'UP' ? 'rgba(16,185,129,0.25)' : 'rgba(16,185,129,0.07)',
              color: T.green,
              outline: direction === 'UP' ? `1px solid ${T.green}` : 'none',
            }}
          >
            &#9650; UP
          </button>
          <button
            onClick={() => setTradeDir('DOWN')}
            style={{
              flex: 1, padding: '4px 0', borderRadius: 3, border: 'none',
              cursor: 'pointer', fontFamily: T.mono, fontSize: 11, fontWeight: 800,
              background: direction === 'DOWN' ? 'rgba(239,68,68,0.25)' : 'rgba(239,68,68,0.07)',
              color: T.red,
              outline: direction === 'DOWN' ? `1px solid ${T.red}` : 'none',
            }}
          >
            &#9660; DOWN
          </button>
          {tradeDir && (
            <button
              onClick={() => setTradeDir(null)}
              title="Reset to signal direction"
              style={{
                padding: '4px 6px', borderRadius: 3, border: 'none',
                cursor: 'pointer', fontFamily: T.mono, fontSize: 9,
                background: 'rgba(71,85,105,0.2)', color: T.textMuted,
              }}
            >
              SIG
            </button>
          )}
        </div>

        {/* Direction + mode badge */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
          <span style={{
            fontSize: 12, fontWeight: 800,
            color: direction === 'UP' ? T.green : T.red,
          }}>
            {direction === 'UP' ? '\u25B2 UP' : '\u25BC DOWN'}
          </span>
          {tradeDir && tradeDir !== signalDirection && (
            <span style={{
              fontSize: 8, padding: '2px 5px', borderRadius: 3,
              background: 'rgba(245,158,11,0.15)', color: T.amber, fontWeight: 700,
            }}>
              OVERRIDE
            </span>
          )}
          <span style={{
            fontSize: 8, padding: '2px 6px', borderRadius: 3,
            background: isPaper ? 'rgba(245,158,11,0.1)' : 'rgba(239,68,68,0.1)',
            color: isPaper ? T.amber : T.red, fontWeight: 700,
          }}>
            {isPaper ? 'PAPER' : 'LIVE'}
          </span>
        </div>

        {/* Auto-filled rationale */}
        <div style={{ marginBottom: 6 }}>
          <span style={{ fontSize: 8, color: T.textMuted }}>Rationale (editable)</span>
          <textarea
            value={rationale || autoRationale}
            onChange={e => setRationale(e.target.value)}
            style={{
              width: '100%', minHeight: 50, marginTop: 3,
              background: 'rgba(0,0,0,0.3)', border: `1px solid ${T.cardBorder}`,
              borderRadius: 3, padding: 6, fontSize: 9, color: T.text,
              fontFamily: T.mono, resize: 'vertical',
            }}
          />
        </div>

        {/* Trade / Confirm button */}
        {!showConfirm ? (
          <button
            onClick={() => setShowConfirm(true)}
            disabled={executing}
            style={{
              width: '100%', padding: '6px 0', borderRadius: 4,
              border: 'none', cursor: 'pointer',
              background: 'rgba(168,85,247,0.2)', color: T.purple,
              fontSize: 12, fontWeight: 800, fontFamily: T.mono,
              letterSpacing: '0.1em',
            }}
          >
            TRADE
          </button>
        ) : (
          <div style={{ display: 'flex', gap: 4 }}>
            <button
              onClick={handleTrade}
              disabled={executing}
              style={{
                flex: 1, padding: '6px 0', borderRadius: 4,
                border: 'none', cursor: 'pointer',
                background: 'rgba(16,185,129,0.2)', color: T.green,
                fontSize: 11, fontWeight: 800, fontFamily: T.mono,
              }}
            >
              {executing ? 'SENDING...' : 'CONFIRM'}
            </button>
            <button
              onClick={() => setShowConfirm(false)}
              style={{
                padding: '6px 12px', borderRadius: 4,
                border: 'none', cursor: 'pointer',
                background: 'rgba(239,68,68,0.1)', color: T.red,
                fontSize: 11, fontWeight: 700, fontFamily: T.mono,
              }}
            >
              CANCEL
            </button>
          </div>
        )}

        {/* Result */}
        {result && (
          <div style={{
            marginTop: 6, padding: '3px 6px', borderRadius: 3,
            background: result.ok ? 'rgba(16,185,129,0.08)' : 'rgba(239,68,68,0.08)',
            fontSize: 9, color: result.ok ? T.green : T.red,
          }}>
            {result.msg}
          </div>
        )}
      </div>
    </div>
  );
}

function buildRationale(window, heartbeat) {
  const parts = [];
  const dir = window.direction || '?';
  parts.push(`Signal: ${dir}`);
  if (window.delta_pct != null) parts.push(`delta: ${fmt(window.delta_pct, 4)}%`);
  if (window.vpin != null) parts.push(`VPIN: ${fmt(window.vpin, 3)}`);
  if (window.v2_probability_up != null) parts.push(`Sequoia p_up: ${fmt(window.v2_probability_up, 3)}`);
  const gr = heartbeat.gate_results || {};
  const srcAgree = gr.gate_agreement === true || gr.gate_agreement === 'PASS';
  parts.push(`SrcAgree: ${srcAgree ? 'YES' : 'NO'}`);
  if (heartbeat.skip_reason) parts.push(`Gate: ${heartbeat.skip_reason}`);
  return parts.join('. ');
}

// --- Main export ---

export default function GatePipelineBand({ hqData }) {
  return (
    <div style={{
      display: 'flex', gap: 8, marginBottom: 6,
      flexShrink: 0,
    }}>
      <GatePipelineStrip hqData={hqData} />
      <ManualTradePanel hqData={hqData} />
    </div>
  );
}
