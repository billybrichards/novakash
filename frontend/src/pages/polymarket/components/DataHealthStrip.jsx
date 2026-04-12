import React from 'react';
import { T, fmt, pct } from './theme.js';

/**
 * Band 2 — Data Health Strip.
 *
 * Horizontal row of signal source health indicators.
 * Each shows: name, RED/YELLOW/GREEN dot, current value, freshness.
 * Status rules from design spec Section 7.
 */

function healthColor(status) {
  if (status === 'green') return T.green;
  if (status === 'yellow') return T.amber;
  if (status === 'red') return T.red;
  return T.textDim; // grey
}

function HealthChip({ name, status, value, detail }) {
  const color = healthColor(status);
  return (
    <div style={{
      display: 'flex', flexDirection: 'column', gap: 3,
      padding: '6px 10px', borderRadius: 4, flex: '1 1 0',
      minWidth: 110, maxWidth: 180,
      background: `${color}08`,
      border: `1px solid ${color}30`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <span style={{
          display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
          background: color, boxShadow: `0 0 5px ${color}55`,
        }} />
        <span style={{
          fontSize: 9, fontWeight: 700, color: T.text,
          letterSpacing: '0.06em', textTransform: 'uppercase',
        }}>{name}</span>
      </div>
      <span style={{
        fontSize: 12, fontWeight: 600, color,
        fontFamily: T.mono,
      }}>{value}</span>
      {detail && (
        <span style={{ fontSize: 8, color: T.textMuted, lineHeight: 1.2 }}>{detail}</span>
      )}
    </div>
  );
}

export default function DataHealthStrip({ hqData, v4Snapshot, v3Snapshot }) {
  // --- Sequoia v5.2 ---
  const hb = hqData?.gate_heartbeat?.[0] || {};
  const gateResults = hb.gate_results || {};
  const probUp = hqData?.windows?.[0]?.v2_probability_up ?? null;
  const sequoiaStatus = probUp != null ? 'green' : 'red';
  const sequoiaValue = probUp != null ? `p_up: ${fmt(probUp, 3)}` : 'NO DATA';

  // --- VPIN ---
  const vpinVal = hqData?.windows?.[0]?.vpin ?? null;
  const vpinStatus = vpinVal != null && vpinVal > 0 ? (vpinVal >= 0.45 ? 'green' : 'yellow') : 'red';
  const vpinValue = vpinVal != null ? fmt(vpinVal, 3) : 'NO DATA';

  // --- Source Agreement ---
  const srcAgree = gateResults.gate_agreement;
  let srcStatus = 'red';
  let srcValue = 'NO DATA';
  if (srcAgree != null) {
    if (srcAgree === true || srcAgree === 'PASS' || srcAgree === 'pass') {
      srcStatus = 'green';
      srcValue = 'AGREE';
    } else {
      srcStatus = 'yellow';
      srcValue = 'DISAGREE';
    }
  }

  // --- Consensus (from v4 snapshot) ---
  const consensus = v4Snapshot?.consensus || {};
  const conSources = consensus.sources || [];
  const conCount = conSources.filter(s => s.price != null && s.price > 0).length;
  const conTotal = conSources.length || 6;
  const conDivergence = consensus.max_divergence_bps ?? consensus.divergence_bps ?? null;
  let conStatus = 'red';
  if (conCount >= 5 && (conDivergence == null || conDivergence < 15)) conStatus = 'green';
  else if (conCount >= 3) conStatus = 'yellow';
  const conValue = `${conCount}/${conTotal} sources`;
  const conDetail = conDivergence != null ? `div: ${fmt(conDivergence, 0)}bps` : null;

  // --- Macro ---
  const macro = v4Snapshot?.macro || {};
  const macroFallback = macro.fallback === true || macro.status === 'fallback' || macro.unreachable === true;
  const macroBias = macro.bias || macro.direction || null;
  let macroStatus = 'red';
  let macroValue = 'UNREACHABLE';
  if (macroFallback) {
    macroStatus = 'red';
    macroValue = 'FALLBACK';
  } else if (macroBias) {
    macroStatus = macro.confidence && macro.confidence > 0 ? 'green' : 'yellow';
    macroValue = macroBias.toUpperCase();
  }
  const macroDetail = macro.gate ? `gate: ${macro.gate}` : (macro.modifier ? `mod: ${macro.modifier}` : null);

  // --- V3 Composite ---
  const v3 = v3Snapshot || {};
  const v3Score = v3.composite_score ?? v3.score ?? null;
  let v3Status = v3Score != null ? 'green' : 'red';
  const v3Value = v3Score != null ? fmt(v3Score, 3) : 'NO DATA';

  // --- V4 Conviction ---
  const timescales = v4Snapshot?.timescales || {};
  const ts5m = timescales['5m'] || {};
  const conviction = ts5m.conviction || null;
  let v4Status = 'grey';
  let v4Value = 'NOT WIRED';
  if (conviction && conviction !== 'NONE') {
    v4Status = 'green';
    v4Value = conviction;
  } else if (conviction === 'NONE') {
    v4Status = 'yellow';
    v4Value = 'NONE';
  }

  return (
    <div style={{
      display: 'flex', gap: 6, marginBottom: 6,
      overflowX: 'auto', flexShrink: 0, fontFamily: T.mono,
    }}>
      <HealthChip name="Sequoia v5.2" status={sequoiaStatus} value={sequoiaValue} />
      <HealthChip name="VPIN" status={vpinStatus} value={vpinValue} detail={vpinVal != null ? `thresh: 0.45` : null} />
      <HealthChip name="Src Agreement" status={srcStatus} value={srcValue} />
      <HealthChip name="Consensus" status={conStatus} value={conValue} detail={conDetail} />
      <HealthChip name="Macro" status={macroStatus} value={macroValue} detail={macroDetail} />
      <HealthChip name="V3 Composite" status={v3Status} value={v3Value} />
      <HealthChip name="V4 Conviction" status={v4Status} value={v4Value} />
    </div>
  );
}
