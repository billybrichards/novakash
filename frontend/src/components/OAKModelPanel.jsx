/**
 * OAKModelPanel.jsx — OAK (v2.2) calibrated probability model display.
 * 
 * Shows:
 *  - Current P(UP) probability with calibrated value
 *  - Model direction (UP/DOWN) based on 0.5 threshold
 *  - Model version info
 *  - Agreement with v8 signal (agrees/disagrees)
 *  - Confidence level (high/low based on 0.35-0.65 range)
 *  - Recent prediction history
 */

import React from 'react';

const T = {
  bg: '#07070c',
  card: 'rgba(255,255,255,0.015)',
  border: 'rgba(255,255,255,0.06)',
  borderBright: 'rgba(255,255,255,0.1)',
  purple: '#a855f7',
  cyan: '#06b6d4',
  profit: '#4ade80',
  loss: '#f87171',
  warning: '#f59e0b',
  text: 'rgba(255,255,255,0.92)',
  textSec: 'rgba(255,255,255,0.45)',
  textMut: 'rgba(255,255,255,0.25)',
  mono: "'IBM Plex Mono', monospace",
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmt(n, decimals = 4) {
  if (n == null) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
}

function fmtPct(n) {
  if (n == null) return '—';
  return `${(n * 100).toFixed(1)}%`;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function SectionLabel({ children }) {
  return (
    <div style={{
      fontSize: 10,
      fontFamily: T.mono,
      color: T.textMut,
      letterSpacing: '0.12em',
      textTransform: 'uppercase',
      marginBottom: 8,
      fontWeight: 600,
    }}>
      {children}
    </div>
  );
}

function Card({ children, style = {}, glowColor = null }) {
  return (
    <div style={{
      background: T.card,
      border: `1px solid ${T.border}`,
      borderRadius: 10,
      padding: '14px 16px',
      marginBottom: 10,
      transition: 'border-color 300ms ease-out, box-shadow 300ms ease-out',
      ...(glowColor ? {
        borderColor: `${glowColor}33`,
        boxShadow: `0 0 20px ${glowColor}0d`,
      } : {}),
      ...style,
    }}>
      {children}
    </div>
  );
}

function DataRow({ label, value, color = T.text, mono = true }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
      <span style={{ fontSize: 11, color: T.textSec, fontFamily: "'Inter', sans-serif" }}>{label}</span>
      <span style={{ fontSize: 12, color, fontFamily: mono ? T.mono : 'inherit', fontWeight: 600 }}>
        {value}
      </span>
    </div>
  );
}

// ── OAK Probability Display ───────────────────────────────────────────────────

function OAKProbability({ oakData }) {
  if (!oakData || oakData.probability_up == null) {
    return (
      <Card>
        <SectionLabel>OAK (v2.2) Probability</SectionLabel>
        <div style={{ textAlign: 'center', padding: '20px 0', color: T.textMut, fontSize: 13 }}>
          Model offline or loading…
        </div>
      </Card>
    );
  }

  const pUp = oakData.probability_up;
  const isUp = pUp > 0.5;
  const color = isUp ? T.profit : T.loss;
  const direction = isUp ? 'UP' : 'DOWN';
  const isHighConf = pUp > 0.65 || pUp < 0.35;
  const confColor = isHighConf ? T.profit : T.warning;
  const confLabel = isHighConf ? 'HIGH' : 'LOW';

  return (
    <Card glowColor={color}>
      <SectionLabel>OAK (v2.2) Calibrated Probability</SectionLabel>

      {/* Big probability */}
      <div style={{ textAlign: 'center', padding: '10px 0 14px' }}>
        <div style={{
          fontSize: 64,
          lineHeight: 1,
          color,
          textShadow: `0 0 30px ${color}66`,
          fontFamily: T.mono,
          fontWeight: 700,
          letterSpacing: '-2px',
          marginBottom: 4,
        }}>
          {fmtPct(pUp)}
        </div>
        <div style={{ fontSize: 13, color: T.textSec, fontFamily: "'Inter', sans-serif", marginBottom: 10 }}>
          P(Win = UP)
        </div>

        {/* Direction */}
        <div style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 12px',
          background: `${color}11`,
          borderRadius: 6,
          border: `1px solid ${color}33`,
        }}>
          <span style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: color,
            boxShadow: `0 0 8px ${color}`,
          }} />
          <span style={{ fontSize: 16, fontFamily: T.mono, fontWeight: 700, color }}>
            {direction}
          </span>
        </div>

        {/* Confidence */}
        <div style={{ marginTop: 12 }}>
          <div style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px',
            background: `${confColor}11`,
            borderRadius: 4,
            border: `1px solid ${confColor}33`,
          }}>
            <span style={{ fontSize: 11, fontFamily: T.mono, fontWeight: 600, color: confColor }}>
              {confLabel} CONF
            </span>
          </div>
          <div style={{ fontSize: 10, color: T.textMut, marginTop: 4, fontFamily: "'Inter', sans-serif" }}>
            {isHighConf ? 'Used for early entry gate' : 'Below confidence threshold'}
          </div>
        </div>
      </div>
    </Card>
  );
}

// ── Model Info Card ───────────────────────────────────────────────────────────

function ModelInfo({ oakData }) {
  if (!oakData) {
    return (
      <Card>
        <SectionLabel>Model Info</SectionLabel>
        <DataRow label="Status" value="OFFLINE" color={T.loss} mono={false} />
      </Card>
    );
  }

  const modelVersion = oakData.model_version || 'Unknown';
  const shortVersion = modelVersion.split('/').pop()?.substring(0, 12) || modelVersion.substring(0, 12);
  const timestamp = oakData.timestamp ? new Date(oakData.timestamp * 1000).toLocaleTimeString() : '—';

  return (
    <Card>
      <SectionLabel>Model Info</SectionLabel>
      <DataRow label="Model" value="OAK (v2.2)" color={T.profit} mono={false} />
      <DataRow label="Version" value={shortVersion} color={T.textSec} />
      <DataRow label="P(UP)" value={fmt(oakData.probability_up, 4)} color={oakData.probability_up > 0.5 ? T.profit : T.loss} />
      <DataRow label="P(DOWN)" value={fmt(oakData.probability_down ?? (1 - oakData.probability_up), 4)} color={oakData.probability_up <= 0.5 ? T.profit : T.loss} />
      <DataRow label="Raw" value={fmt(oakData.probability_raw ?? oakData.probability_up, 4)} color={T.textSec} />
      <DataRow label="Last Update" value={timestamp} color={T.textSec} mono={false} />
    </Card>
  );
}

// ── Agreement Status ──────────────────────────────────────────────────────────

function AgreementStatus({ oakData, v8Direction }) {
  if (!oakData || !v8Direction) {
    return null;
  }

  const oakDir = oakData.probability_up > 0.5 ? 'UP' : 'DOWN';
  const agrees = oakDir === v8Direction;
  const color = agrees ? T.profit : T.loss;
  const label = agrees ? 'AGREE' : 'DISAGREE';

  return (
    <Card glowColor={color}>
      <SectionLabel>Signal Agreement</SectionLabel>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 13, color: T.textSec }}>v8 Direction</span>
        <span style={{ fontSize: 16, fontFamily: T.mono, fontWeight: 700, color: T.text }}>
          {v8Direction}
        </span>
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 13, color: T.textSec }}>OAK Direction</span>
        <span style={{ fontSize: 16, fontFamily: T.mono, fontWeight: 700, color }}>
          {oakDir}
        </span>
      </div>
      <div style={{
        textAlign: 'center',
        padding: '8px 0',
        background: `${color}11`,
        borderRadius: 6,
        border: `1px solid ${color}33`,
      }}>
        <span style={{ fontSize: 14, fontFamily: T.mono, fontWeight: 700, color }}>
          {label}
        </span>
      </div>
      <div style={{ fontSize: 10, color: T.textMut, marginTop: 6, textAlign: 'center', fontFamily: "'Inter', sans-serif" }}>
        {agrees ? 'Early entry gate: PASSED' : 'Early entry gate: BLOCKED'}
      </div>
    </Card>
  );
}

// ── Prediction History ────────────────────────────────────────────────────────

function PredictionHistoryRow({ item }) {
  const isCorrect = item.is_correct;
  const oakColor = item.probability_up > 0.5 ? T.profit : T.loss;
  const resultColor = isCorrect ? T.profit : T.loss;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      padding: '5px 0',
      borderBottom: `1px solid ${T.border}`,
      fontSize: 11,
      fontFamily: T.mono,
    }}>
      <div style={{
        width: 6,
        height: 6,
        borderRadius: '50%',
        background: oakColor,
        boxShadow: `0 0 4px ${oakColor}`,
        flexShrink: 0,
      }} />
      <div style={{ flex: 1, color: T.textSec, fontSize: 10 }}>
        {new Date(item.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
      </div>
      <div style={{ color: oakColor, width: 36, textAlign: 'right' }}>
        {fmtPct(item.probability_up)}
      </div>
      <div style={{ color: resultColor, width: 40, textAlign: 'right' }}>
        {isCorrect ? '✓' : '✗'}
      </div>
    </div>
  );
}

function PredictionHistory({ history = [] }) {
  const accuracy = history.length > 0
    ? (history.filter(h => h.is_correct).length / history.length * 100).toFixed(0)
    : 0;

  if (history.length === 0) {
    return null;
  }

  return (
    <Card>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <SectionLabel>Recent Predictions</SectionLabel>
        <span style={{ fontSize: 12, fontFamily: T.mono, color: T.profit, fontWeight: 700 }}>
          {accuracy}% acc
        </span>
      </div>

      <div style={{
        display: 'flex',
        gap: 8,
        paddingBottom: 4,
        borderBottom: `1px solid ${T.border}`,
        fontSize: 9,
        color: T.textMut,
        fontFamily: T.mono,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
      }}>
        <div style={{ width: 6 }} />
        <div style={{ flex: 1 }}>Time</div>
        <div style={{ width: 36, textAlign: 'right' }}>P(UP)</div>
        <div style={{ width: 40, textAlign: 'right' }}>Result</div>
      </div>

      <div style={{ maxHeight: 150, overflowY: 'auto' }}>
        {history.map((item, i) => (
          <PredictionHistoryRow key={item.id || i} item={item} />
        ))}
      </div>
    </Card>
  );
}

// ── Main Panel ────────────────────────────────────────────────────────────────

export default function OAKModelPanel({
  oakData = null,
  v8Direction = null,
  predictionHistory = [],
  wsStatus = 'DISCONNECTED',
}) {
  return (
    <div style={{ height: '100%', overflowY: 'auto', paddingRight: 2 }}>
      <OAKProbability oakData={oakData} />
      <ModelInfo oakData={oakData} />
      <AgreementStatus oakData={oakData} v8Direction={v8Direction} />
      <PredictionHistory history={predictionHistory} />

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
      `}</style>
    </div>
  );
}
