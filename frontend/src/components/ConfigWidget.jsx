import React, { useRef, useEffect, useCallback, useState } from 'react';

/**
 * ConfigWidget — A single config variable with label, input, canvas viz, and impact text.
 *
 * Props:
 *   def       — variable definition from /trading-config/defaults
 *   value     — current value
 *   onChange  — (key, value) => void
 *   vpinData  — array of VPIN history values (for vpin widgets)
 *   disabled  — grey out and disable inputs
 */
export default function ConfigWidget({ def: varDef, value, onChange, vpinData = [], disabled = false }) {
  const canvasRef = useRef(null);
  const [localValue, setLocalValue] = useState(value);

  useEffect(() => {
    setLocalValue(value);
  }, [value]);

  const handleChange = useCallback((newVal) => {
    setLocalValue(newVal);
    onChange(varDef.key, newVal);
  }, [varDef.key, onChange]);

  // ── Canvas setup (HiDPI) ────────────────────────────────────────────────
  const setupCanvas = (canvas, w, h) => {
    if (!canvas) return null;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    return ctx;
  };

  // ── Draw equity curve ────────────────────────────────────────────────────
  const drawEquityCurve = useCallback((canvas, bankroll, winRate = 0.55, numTrades = 50) => {
    const ctx = setupCanvas(canvas, 260, 60);
    if (!ctx) return;

    ctx.clearRect(0, 0, 260, 60);

    const bg = '#08080e';
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, 260, 60);

    // Simulate equity path
    const fraction = varDef.key === 'bet_fraction' ? parseFloat(localValue) : 0.05;
    const br = parseFloat(bankroll) || 25;
    const equity = [br];
    for (let i = 1; i < numTrades; i++) {
      const prev = equity[i - 1];
      const win = Math.random() < winRate;
      const change = win ? prev * fraction * 1.9 : -prev * fraction;
      equity.push(Math.max(0.01, prev + change));
    }

    const minE = Math.min(...equity);
    const maxE = Math.max(...equity);
    const range = maxE - minE || 1;

    const toY = (v) => 55 - ((v - minE) / range) * 50;
    const toX = (i) => (i / (equity.length - 1)) * 255;

    // Fill gradient
    const grad = ctx.createLinearGradient(0, 0, 0, 60);
    grad.addColorStop(0, 'rgba(168,85,247,0.25)');
    grad.addColorStop(1, 'rgba(168,85,247,0)');
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    equity.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.lineTo(toX(equity.length - 1), 60);
    ctx.lineTo(0, 60);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    equity.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.strokeStyle = '#a855f7';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }, [localValue, varDef.key]);

  // ── Draw drawdown kill-switch overlay ────────────────────────────────────
  const drawDrawdownCurve = useCallback((canvas, drawdownPct) => {
    const ctx = setupCanvas(canvas, 260, 70);
    if (!ctx) return;
    ctx.clearRect(0, 0, 260, 70);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, 260, 70);

    const startBr = 100;
    const equity = [startBr];
    for (let i = 1; i < 60; i++) {
      const prev = equity[i - 1];
      const win = Math.random() < 0.52;
      equity.push(Math.max(0.01, prev + (win ? prev * 0.04 : -prev * 0.06)));
    }

    const maxE = Math.max(...equity);
    const minE = Math.min(0, ...equity) - 5;
    const range = maxE - minE || 1;
    const toY = (v) => 65 - ((v - minE) / range) * 60;
    const toX = (i) => (i / 59) * 255;

    // Fill
    const grad = ctx.createLinearGradient(0, 0, 0, 70);
    grad.addColorStop(0, 'rgba(74,222,128,0.2)');
    grad.addColorStop(1, 'rgba(74,222,128,0)');
    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    equity.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.lineTo(toX(59), 70);
    ctx.lineTo(0, 70);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(toX(0), toY(equity[0]));
    equity.forEach((v, i) => ctx.lineTo(toX(i), toY(v)));
    ctx.strokeStyle = '#4ade80';
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Kill-switch line at drawdownPct below peak
    const killLevel = maxE * (1 - parseFloat(drawdownPct));
    const killY = toY(killLevel);

    ctx.beginPath();
    ctx.moveTo(0, killY);
    ctx.lineTo(260, killY);
    ctx.strokeStyle = 'rgba(248,113,113,0.9)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Label
    ctx.fillStyle = '#f87171';
    ctx.font = '9px IBM Plex Mono';
    ctx.fillText(`KILL ${(parseFloat(drawdownPct) * 100).toFixed(0)}%`, 4, killY - 3);
  }, []);

  // ── Draw VPIN histogram ──────────────────────────────────────────────────
  const drawVpinHistogram = useCallback((canvas, threshold, color, label) => {
    const ctx = setupCanvas(canvas, 260, 70);
    if (!ctx) return;
    ctx.clearRect(0, 0, 260, 70);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, 260, 70);

    const data = vpinData.length > 0 ? vpinData : Array.from({ length: 200 }, () => Math.random() * 0.8 + 0.2);

    // Histogram: 20 bins from 0 to 1
    const bins = 20;
    const counts = new Array(bins).fill(0);
    data.forEach(v => {
      const bin = Math.min(bins - 1, Math.floor(v * bins));
      counts[bin]++;
    });

    const maxCount = Math.max(...counts);
    const barW = 260 / bins;

    counts.forEach((count, i) => {
      const x = i * barW;
      const h = (count / maxCount) * 55;
      const binMid = (i + 0.5) / bins;
      const isAbove = binMid >= parseFloat(threshold);

      ctx.fillStyle = isAbove
        ? `${color}55`
        : 'rgba(255,255,255,0.07)';
      ctx.fillRect(x + 1, 58 - h, barW - 2, h);
    });

    // Base line
    ctx.fillStyle = 'rgba(255,255,255,0.15)';
    ctx.fillRect(0, 59, 260, 1);

    // Threshold line
    const lineX = parseFloat(threshold) * 260;
    ctx.beginPath();
    ctx.moveTo(lineX, 0);
    ctx.lineTo(lineX, 62);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // Stats
    const abovePct = (data.filter(v => v >= parseFloat(threshold)).length / data.length * 100).toFixed(0);
    ctx.fillStyle = color;
    ctx.font = '9px IBM Plex Mono';
    ctx.fillText(`${abovePct}% above threshold`, lineX + 4, 10);
  }, [vpinData]);

  // ── Draw spread distribution ─────────────────────────────────────────────
  const drawSpreadDist = useCallback((canvas, threshold) => {
    const ctx = setupCanvas(canvas, 260, 60);
    if (!ctx) return;
    ctx.clearRect(0, 0, 260, 60);
    ctx.fillStyle = '#08080e';
    ctx.fillRect(0, 0, 260, 60);

    // Simulated spread distribution (log-normal)
    const data = Array.from({ length: 200 }, () => {
      const u1 = Math.random(), u2 = Math.random();
      return Math.abs(Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2)) * 0.015 + 0.005;
    });

    const maxSpread = 0.06;
    const bins = 20;
    const counts = new Array(bins).fill(0);
    data.forEach(v => {
      const bin = Math.min(bins - 1, Math.floor((v / maxSpread) * bins));
      counts[bin]++;
    });

    const maxCount = Math.max(...counts);
    const barW = 260 / bins;
    const tVal = parseFloat(threshold);

    counts.forEach((count, i) => {
      const x = i * barW;
      const h = (count / maxCount) * 50;
      const binMid = (i + 0.5) / bins * maxSpread;
      ctx.fillStyle = binMid >= tVal ? 'rgba(6,182,212,0.5)' : 'rgba(255,255,255,0.07)';
      ctx.fillRect(x + 1, 53 - h, barW - 2, h);
    });

    ctx.fillStyle = 'rgba(255,255,255,0.15)';
    ctx.fillRect(0, 54, 260, 1);

    const lineX = (tVal / maxSpread) * 260;
    ctx.beginPath();
    ctx.moveTo(lineX, 0);
    ctx.lineTo(lineX, 56);
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 2;
    ctx.stroke();

    const aboveCount = data.filter(v => v >= tVal).length;
    ctx.fillStyle = '#06b6d4';
    ctx.font = '9px IBM Plex Mono';
    ctx.fillText(`${aboveCount} arbs detected`, lineX + 4, 10);
  }, []);

  // ── Trigger canvas redraws ───────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    switch (varDef.key) {
      case 'starting_bankroll':
      case 'bet_fraction':
        drawEquityCurve(canvas, localValue);
        break;
      case 'max_drawdown_pct':
        drawDrawdownCurve(canvas, localValue);
        break;
      case 'vpin_informed_threshold':
        drawVpinHistogram(canvas, localValue, '#f59e0b', 'informed');
        break;
      case 'vpin_cascade_threshold':
        drawVpinHistogram(canvas, localValue, '#f87171', 'cascade');
        break;
      case 'arb_min_spread':
        drawSpreadDist(canvas, localValue);
        break;
      default:
        break;
    }
  }, [localValue, varDef.key, drawEquityCurve, drawDrawdownCurve, drawVpinHistogram, drawSpreadDist]);

  // ── Derived display values ────────────────────────────────────────────────
  const getImpactText = () => {
    const v = parseFloat(localValue);
    switch (varDef.key) {
      case 'bet_fraction': {
        const bankrollEl = document.querySelector('[data-key="starting_bankroll"]');
        const br = parseFloat(bankrollEl?.dataset?.value || 25);
        return `Per trade: $${(br * v).toFixed(2)} USD`;
      }
      case 'max_position_usd': {
        const bankrollEl = document.querySelector('[data-key="starting_bankroll"]');
        const br = parseFloat(bankrollEl?.dataset?.value || 25);
        return `${((v / br) * 100).toFixed(0)}% of bankroll`;
      }
      case 'vpin_bucket_size_usd':
        return `Approx 1 bucket = ${Math.round(v / 2000)}s at $2M/hr volume`;
      case 'vpin_lookback_buckets': {
        const bucketEl = document.querySelector('[data-key="vpin_bucket_size_usd"]');
        const bucketSize = parseFloat(bucketEl?.dataset?.value || 50000);
        return `Total window: $${((v * bucketSize) / 1000000).toFixed(1)}M volume`;
      }
      case 'arb_max_execution_ms':
        return `Latency budget: ${v}ms — ${v < 200 ? '⚡ tight' : v < 500 ? '✓ reasonable' : '⚠️ may miss closes'}`;
      case 'cascade_cooldown_seconds':
        return `${Math.floor(v / 60)}min cooldown — max ${Math.floor(3600 / v)} cascade bets/hr`;
      case 'cascade_min_liq_usd':
        return `Ignores liquidations < $${(v / 1000000).toFixed(1)}M`;
      default:
        return varDef.impact;
    }
  };

  const hasCanvas = ['starting_bankroll', 'bet_fraction', 'max_drawdown_pct',
    'vpin_informed_threshold', 'vpin_cascade_threshold', 'arb_min_spread'].includes(varDef.key);

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div
      data-key={varDef.key}
      data-value={localValue}
      style={{
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: 8,
        padding: '14px 16px',
        opacity: disabled ? 0.5 : 1,
        transition: 'opacity 200ms',
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 8 }}>
        <div>
          <div style={{ color: 'rgba(255,255,255,0.9)', fontSize: 13, fontWeight: 500, letterSpacing: '0.02em' }}>
            {varDef.label}
          </div>
          <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>
            {varDef.description}
          </div>
        </div>

        {/* Toggle widget */}
        {varDef.widget === 'toggle' && (
          <button
            onClick={() => !disabled && handleChange(!localValue)}
            disabled={disabled}
            style={{
              width: 48,
              height: 26,
              borderRadius: 13,
              border: 'none',
              cursor: disabled ? 'default' : 'pointer',
              background: localValue
                ? 'rgba(168,85,247,0.8)'
                : 'rgba(255,255,255,0.1)',
              position: 'relative',
              transition: 'background 200ms',
              flexShrink: 0,
              marginLeft: 12,
            }}
          >
            <span style={{
              position: 'absolute',
              top: 3,
              left: localValue ? 25 : 3,
              width: 20,
              height: 20,
              borderRadius: '50%',
              background: '#fff',
              transition: 'left 200ms ease-out',
              boxShadow: localValue ? '0 0 6px rgba(168,85,247,0.6)' : 'none',
            }} />
          </button>
        )}

        {/* Venue select */}
        {varDef.widget === 'venue_select' && (
          <div style={{ display: 'flex', gap: 6, marginLeft: 12 }}>
            {['opinion', 'polymarket'].map(v => (
              <button
                key={v}
                onClick={() => !disabled && handleChange(v)}
                disabled={disabled}
                style={{
                  padding: '4px 10px',
                  borderRadius: 4,
                  border: `1px solid ${localValue === v ? 'rgba(168,85,247,0.5)' : 'rgba(255,255,255,0.1)'}`,
                  background: localValue === v ? 'rgba(168,85,247,0.15)' : 'transparent',
                  color: localValue === v ? '#a855f7' : 'rgba(255,255,255,0.4)',
                  fontSize: 11,
                  cursor: disabled ? 'default' : 'pointer',
                  transition: 'all 150ms',
                  fontFamily: 'IBM Plex Mono, monospace',
                  textTransform: 'uppercase',
                }}
              >
                {v}
              </button>
            ))}
          </div>
        )}

        {/* Readonly */}
        {varDef.widget === 'readonly' && (
          <div style={{
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 13,
            color: 'rgba(255,255,255,0.6)',
            background: 'rgba(255,255,255,0.05)',
            padding: '3px 8px',
            borderRadius: 4,
            marginLeft: 12,
          }}>
            {(parseFloat(localValue) * 100).toFixed(1)}%
          </div>
        )}
      </div>

      {/* Slider + Number input row */}
      {(varDef.widget === 'slider' || varDef.widget === 'number') && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: hasCanvas ? 10 : 0 }}>
          {varDef.widget === 'slider' && (
            <div style={{ flex: 1, position: 'relative' }}>
              <input
                type="range"
                min={varDef.min}
                max={varDef.max}
                step={varDef.step}
                value={localValue}
                disabled={disabled}
                onChange={e => handleChange(varDef.type === 'number' ? parseFloat(e.target.value) : e.target.value)}
                style={{
                  width: '100%',
                  accentColor: '#a855f7',
                  cursor: disabled ? 'default' : 'pointer',
                }}
              />
              <div style={{
                display: 'flex',
                justifyContent: 'space-between',
                marginTop: 2,
              }}>
                <span style={{ color: 'rgba(255,255,255,0.25)', fontSize: 10 }}>
                  {varDef.unit === 'USD' ? `$${varDef.min?.toLocaleString()}` :
                   varDef.unit === '%' ? `${(varDef.min * 100).toFixed(0)}%` :
                   varDef.min}
                </span>
                <span style={{ color: 'rgba(255,255,255,0.25)', fontSize: 10 }}>
                  {varDef.unit === 'USD' ? `$${varDef.max?.toLocaleString()}` :
                   varDef.unit === '%' ? `${(varDef.max * 100).toFixed(0)}%` :
                   varDef.max}
                </span>
              </div>
            </div>
          )}

          {/* Numeric input */}
          <input
            type="number"
            min={varDef.min}
            max={varDef.max}
            step={varDef.step}
            value={localValue}
            disabled={disabled}
            onChange={e => {
              const v = parseFloat(e.target.value);
              if (!isNaN(v)) handleChange(v);
            }}
            style={{
              width: varDef.widget === 'number' ? '100%' : 80,
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              borderRadius: 4,
              color: '#a855f7',
              fontFamily: 'IBM Plex Mono, monospace',
              fontSize: 13,
              padding: '4px 8px',
              textAlign: 'right',
              outline: 'none',
            }}
          />
          {varDef.unit && (
            <span style={{ color: 'rgba(255,255,255,0.3)', fontSize: 11, minWidth: 24 }}>
              {varDef.unit === '%'
                ? (parseFloat(localValue) * 100).toFixed(1) + '%'
                : varDef.unit}
            </span>
          )}
        </div>
      )}

      {/* Canvas visualization */}
      {hasCanvas && (
        <canvas
          ref={canvasRef}
          style={{
            borderRadius: 4,
            display: 'block',
            marginBottom: 8,
          }}
        />
      )}

      {/* Impact text */}
      <div style={{
        color: 'rgba(255,255,255,0.35)',
        fontSize: 11,
        fontFamily: 'IBM Plex Mono, monospace',
        marginTop: 4,
        borderLeft: '2px solid rgba(168,85,247,0.3)',
        paddingLeft: 8,
      }}>
        {getImpactText()}
      </div>

      {/* Venue comparison (fees page only) */}
      {varDef.widget === 'venue_select' && (
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          {[
            { name: 'Opinion', fee: 0.04, color: '#a855f7' },
            { name: 'Polymarket', fee: 0.072, color: '#06b6d4' },
          ].map(v => (
            <div
              key={v.name}
              style={{
                flex: 1,
                background: localValue === v.name.toLowerCase()
                  ? `${v.color}15`
                  : 'rgba(255,255,255,0.03)',
                border: `1px solid ${localValue === v.name.toLowerCase() ? v.color + '40' : 'rgba(255,255,255,0.06)'}`,
                borderRadius: 6,
                padding: '8px 10px',
                transition: 'all 200ms',
              }}
            >
              <div style={{ color: v.color, fontSize: 12, fontWeight: 600 }}>{v.name}</div>
              <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: 11, marginTop: 2 }}>
                {(v.fee * 100).toFixed(1)}% round-trip
              </div>
              <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, marginTop: 2 }}>
                $10 trade → ${(10 * v.fee).toFixed(2)} fee
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
