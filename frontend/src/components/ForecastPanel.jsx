/**
 * ForecastPanel.jsx — TimesFM forecast right-panel.
 *
 * Shows:
 *  - Large direction indicator (UP ↑ / DOWN ↓) with confidence %
 *  - Current BTC price, window open, predicted close, delta
 *  - Confidence quantile meter (P10/P25/P50/P75/P90)
 *  - Last 10 forecast history with accuracy
 *  - Model status (connection, latency, last update)
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

function fmt(n, decimals = 0) {
  if (n == null) return '—';
  return n.toLocaleString('en-US', { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
}

function fmtPct(n) {
  if (n == null) return '—';
  return `${(n * 100).toFixed(1)}%`;
}

function fmtDelta(n) {
  if (n == null) return '—';
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${Math.abs(n).toFixed(0)}`;
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

// ── Direction Indicator ───────────────────────────────────────────────────────

function DirectionCard({ forecast }) {
  if (!forecast) {
    return (
      <Card>
        <SectionLabel>Forecast Direction</SectionLabel>
        <div style={{ textAlign: 'center', padding: '20px 0', color: T.textMut, fontSize: 13 }}>
          Loading…
        </div>
      </Card>
    );
  }

  const isUp = forecast.direction === 'UP';
  const color = isUp ? T.profit : T.loss;
  const confPct = (forecast.confidence * 100).toFixed(1);

  return (
    <Card glowColor={color}>
      <SectionLabel>TimesFM Forecast</SectionLabel>

      {/* Big direction */}
      <div style={{ textAlign: 'center', padding: '10px 0 14px' }}>
        <div style={{
          fontSize: 56,
          lineHeight: 1,
          color,
          textShadow: `0 0 30px ${color}66`,
          fontFamily: T.mono,
          fontWeight: 700,
          letterSpacing: '-2px',
          marginBottom: 4,
        }}>
          {isUp ? '↑' : '↓'} {forecast.direction}
        </div>
        <div style={{ fontSize: 13, color: T.textSec, fontFamily: "'Inter', sans-serif", marginBottom: 10 }}>
          Predicted window close direction
        </div>

        {/* Confidence bar */}
        <div style={{
          background: 'rgba(255,255,255,0.05)',
          borderRadius: 4,
          height: 6,
          overflow: 'hidden',
          marginBottom: 6,
        }}>
          <div style={{
            height: '100%',
            width: `${confPct}%`,
            background: `linear-gradient(90deg, ${color}88, ${color})`,
            borderRadius: 4,
            boxShadow: `0 0 8px ${color}55`,
            transition: 'width 600ms ease-out',
          }} />
        </div>
        <div style={{ fontSize: 20, fontFamily: T.mono, fontWeight: 700, color }}>
          {confPct}%
        </div>
        <div style={{ fontSize: 10, color: T.textMut, marginTop: 2, fontFamily: "'Inter', sans-serif" }}>
          model confidence
        </div>
      </div>
    </Card>
  );
}

// ── Price Card ────────────────────────────────────────────────────────────────

function PriceCard({ btcPrice, forecast }) {
  const delta = forecast ? forecast.predictedClose - forecast.windowOpenPrice : null;
  const isUp = delta != null ? delta >= 0 : null;
  const deltaColor = isUp === null ? T.text : isUp ? T.profit : T.loss;

  return (
    <Card>
      <SectionLabel>Price Data</SectionLabel>
      <DataRow label="BTC / USD" value={btcPrice ? `$${fmt(btcPrice)}` : '—'} color={T.text} />
      <DataRow
        label="Window Open"
        value={forecast?.windowOpenPrice ? `$${fmt(forecast.windowOpenPrice)}` : '—'}
        color={T.textSec}
      />
      <DataRow
        label="Predicted Close"
        value={forecast?.predictedClose ? `$${fmt(forecast.predictedClose)}` : '—'}
        color={forecast?.direction === 'UP' ? T.profit : T.loss}
      />
      <DataRow
        label="Δ Delta"
        value={fmtDelta(delta)}
        color={deltaColor}
      />
    </Card>
  );
}

// ── Quantile Meter ────────────────────────────────────────────────────────────

function QuantileBar({ label, value, windowOpenPrice, min, max, color }) {
  if (!value || !min || !max) return null;
  const pct = ((value - min) / (max - min)) * 100;
  const clampedPct = Math.max(0, Math.min(100, pct));

  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 10, color: T.textMut, fontFamily: T.mono }}>{label}</span>
        <span style={{ fontSize: 11, color, fontFamily: T.mono, fontWeight: 600 }}>
          ${fmt(value)}
        </span>
      </div>
      <div style={{ height: 3, background: 'rgba(255,255,255,0.06)', borderRadius: 2, position: 'relative' }}>
        <div style={{
          position: 'absolute',
          left: `${clampedPct}%`,
          top: '-2px',
          width: 7,
          height: 7,
          borderRadius: '50%',
          background: color,
          boxShadow: `0 0 6px ${color}`,
          transform: 'translateX(-50%)',
        }} />
        <div style={{
          height: '100%',
          width: `${clampedPct}%`,
          background: `linear-gradient(90deg, rgba(168,85,247,0.3), ${color}66)`,
          borderRadius: 2,
        }} />
      </div>
    </div>
  );
}

function ConfidenceMeter({ quantiles }) {
  if (!quantiles) return null;

  const min = quantiles.p10 - 50;
  const max = quantiles.p90 + 50;

  return (
    <Card>
      <SectionLabel>Confidence Quantiles</SectionLabel>
      <QuantileBar label="P90" value={quantiles.p90} min={min} max={max} color={T.profit} />
      <QuantileBar label="P75" value={quantiles.p75} min={min} max={max} color="#86efac" />
      <QuantileBar label="P50 (Median)" value={quantiles.p50} min={min} max={max} color={T.purple} />
      <QuantileBar label="P25" value={quantiles.p25} min={min} max={max} color="#fca5a5" />
      <QuantileBar label="P10" value={quantiles.p10} min={min} max={max} color={T.loss} />

      {/* Range summary */}
      <div style={{
        marginTop: 10,
        paddingTop: 8,
        borderTop: `1px solid ${T.border}`,
        display: 'flex',
        justifyContent: 'space-between',
        fontSize: 10,
        color: T.textMut,
        fontFamily: T.mono,
      }}>
        <span>Range: ${fmt(quantiles.p90 - quantiles.p10)}</span>
        <span>IQR: ${fmt(quantiles.p75 - quantiles.p25)}</span>
      </div>
    </Card>
  );
}

// ── Forecast History ──────────────────────────────────────────────────────────

function ForecastHistoryRow({ item, index }) {
  const isCorrect = item.correct;
  const dotColor = isCorrect ? T.profit : T.loss;

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
        background: dotColor,
        boxShadow: `0 0 4px ${dotColor}`,
        flexShrink: 0,
      }} />
      <div style={{ flex: 1, color: T.textSec, fontSize: 10 }}>
        {new Date(item.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}
      </div>
      <div style={{ color: item.predictedDirection === 'UP' ? T.profit : T.loss, width: 28, textAlign: 'center' }}>
        {item.predictedDirection === 'UP' ? '↑' : '↓'}
      </div>
      <div style={{ color: item.actualDirection === 'UP' ? T.profit : T.loss, width: 28, textAlign: 'center' }}>
        {item.actualDirection === 'UP' ? '↑' : '↓'}
      </div>
      <div style={{ color: isCorrect ? T.profit : T.loss, width: 40, textAlign: 'right' }}>
        {isCorrect ? '✓' : '✗'}
      </div>
    </div>
  );
}

function ForecastHistory({ history = [] }) {
  const accuracy = history.length > 0
    ? (history.filter(h => h.correct).length / history.length * 100).toFixed(0)
    : 0;

  return (
    <Card>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <SectionLabel>Recent Forecasts</SectionLabel>
        <span style={{ fontSize: 12, fontFamily: T.mono, color: T.profit, fontWeight: 700 }}>
          {accuracy}% acc
        </span>
      </div>

      {/* Column headers */}
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
        <div style={{ width: 28, textAlign: 'center' }}>Pred</div>
        <div style={{ width: 28, textAlign: 'center' }}>Actual</div>
        <div style={{ width: 40, textAlign: 'right' }}>Result</div>
      </div>

      <div style={{ maxHeight: 200, overflowY: 'auto' }}>
        {history.map((item, i) => (
          <ForecastHistoryRow key={item.id || i} item={item} index={i} />
        ))}
        {history.length === 0 && (
          <div style={{ color: T.textMut, fontSize: 12, padding: '12px 0', textAlign: 'center' }}>
            No history yet
          </div>
        )}
      </div>
    </Card>
  );
}

// ── Model Status ──────────────────────────────────────────────────────────────

function ModelStatus({ wsStatus, forecast }) {
  const statusMap = {
    CONNECTED: { label: 'LIVE', color: T.profit, dot: true },
    CONNECTING: { label: 'CONNECTING…', color: T.warning, dot: false },
    RECONNECTING: { label: 'RECONNECTING…', color: T.warning, dot: false },
    FAILED: { label: 'FAILED', color: T.loss, dot: false },
    DISCONNECTED: { label: 'MOCK DATA', color: T.purple, dot: false },
  };

  const s = statusMap[wsStatus] || statusMap.DISCONNECTED;
  const lastUpdated = forecast?.lastUpdated
    ? new Date(forecast.lastUpdated).toLocaleTimeString()
    : '—';

  return (
    <Card>
      <SectionLabel>Model Status</SectionLabel>
      <DataRow
        label="TimesFM Backend"
        value={
          <span style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
            {s.dot && (
              <span style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: s.color,
                boxShadow: `0 0 6px ${s.color}`,
                display: 'inline-block',
                animation: 'pulse 2s ease-in-out infinite',
              }} />
            )}
            <span style={{ color: s.color }}>{s.label}</span>
          </span>
        }
        color={s.color}
        mono={false}
      />
      <DataRow
        label="Model"
        value={forecast?.modelVersion || 'TimesFM-1.0-200m'}
        color={T.textSec}
      />
      <DataRow
        label="Latency"
        value={forecast?.inferenceLatencyMs ? `${forecast.inferenceLatencyMs}ms` : '—'}
        color={T.cyan}
      />
      <DataRow
        label="Last Update"
        value={lastUpdated}
        color={T.textSec}
      />
    </Card>
  );
}

// ── Main Panel ────────────────────────────────────────────────────────────────

export default function ForecastPanel({
  forecast = null,
  btcPrice = null,
  forecastHistory = [],
  wsStatus = 'DISCONNECTED',
}) {
  return (
    <div style={{ height: '100%', overflowY: 'auto', paddingRight: 2 }}>
      <DirectionCard forecast={forecast} />
      <PriceCard btcPrice={btcPrice} forecast={forecast} />
      <ConfidenceMeter quantiles={forecast?.quantiles} />
      <ForecastHistory history={forecastHistory} />
      <ModelStatus wsStatus={wsStatus} forecast={forecast} />

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
      `}</style>
    </div>
  );
}
