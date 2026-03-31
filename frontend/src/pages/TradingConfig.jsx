import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';
import ConfigWidget from '../components/ConfigWidget.jsx';

// ── Theme helpers ─────────────────────────────────────────────────────────────

const MODE_STYLES = {
  paper: { color: '#a855f7', bg: 'rgba(168,85,247,0.1)', border: 'rgba(168,85,247,0.3)', label: 'PAPER', icon: '📄' },
  live:  { color: '#f87171', bg: 'rgba(248,113,113,0.1)', border: 'rgba(248,113,113,0.3)', label: 'LIVE',  icon: '💰' },
};

const CATEGORY_META = {
  risk:    { label: '§ Risk Management',  icon: '🛡️', color: '#4ade80' },
  vpin:    { label: '§ VPIN Thresholds',  icon: '📡', color: '#f59e0b' },
  arb:     { label: '§ Arb Strategy',     icon: '⚡', color: '#06b6d4' },
  cascade: { label: '§ Cascade Strategy', icon: '🌊', color: '#a855f7' },
  fees:    { label: '§ Fees & Venue',     icon: '💸', color: 'rgba(255,255,255,0.5)' },
};

// ── Sub-components ────────────────────────────────────────────────────────────

function ModeBadge({ mode, size = 'sm' }) {
  const s = MODE_STYLES[mode] || MODE_STYLES.paper;
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 4,
      padding: size === 'sm' ? '2px 7px' : '4px 10px',
      borderRadius: 4,
      border: `1px solid ${s.border}`,
      background: s.bg,
      color: s.color,
      fontSize: size === 'sm' ? 10 : 12,
      fontFamily: 'IBM Plex Mono, monospace',
      fontWeight: 600,
      letterSpacing: '0.06em',
    }}>
      {s.icon} {s.label}
    </span>
  );
}

function StatusBadge({ config }) {
  if (!config) return null;
  if (config.is_active && config.is_approved) return (
    <span style={{ color: '#4ade80', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>● ACTIVE · APPROVED</span>
  );
  if (config.is_active) return (
    <span style={{ color: '#a855f7', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>● ACTIVE</span>
  );
  if (config.is_approved) return (
    <span style={{ color: '#06b6d4', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>✓ APPROVED</span>
  );
  return <span style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>○ DRAFT</span>;
}

function CollapsibleSection({ category, defaultOpen = true, children }) {
  const [open, setOpen] = useState(defaultOpen);
  const meta = CATEGORY_META[category] || { label: category, icon: '⚙️', color: '#fff' };

  return (
    <div style={{
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 10,
      overflow: 'hidden',
      marginBottom: 10,
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 16px',
          background: 'rgba(255,255,255,0.025)',
          border: 'none', cursor: 'pointer',
          borderBottom: open ? '1px solid rgba(255,255,255,0.05)' : 'none',
          minHeight: 48,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 16 }}>{meta.icon}</span>
          <span style={{
            color: meta.color,
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 12, fontWeight: 700, letterSpacing: '0.06em',
          }}>
            {meta.label.toUpperCase()}
          </span>
        </div>
        <span style={{
          color: 'rgba(255,255,255,0.3)', fontSize: 14,
          transform: open ? 'rotate(90deg)' : 'rotate(0)',
          transition: 'transform 200ms ease-out',
          display: 'inline-block',
        }}>›</span>
      </button>
      {open && (
        <div style={{ padding: '12px 12px', display: 'flex', flexDirection: 'column', gap: 10 }}>
          {children}
        </div>
      )}
    </div>
  );
}

// ── Config variable widget (full interactive) ────────────────────────────────

function ConfigVar({ def: varDef, value, onChange, config, vpinHistory }) {
  const [localValue, setLocalValue] = useState(value);

  useEffect(() => { setLocalValue(value); }, [value]);

  const handleChange = (newVal) => {
    setLocalValue(newVal);
    onChange(varDef.key, newVal);
  };

  // Canvas widget type for this variable
  const canvasType = {
    starting_bankroll: 'equity_projection',
    bet_fraction: 'bet_size_bars',
    max_drawdown_pct: 'drawdown_line',
    vpin_informed_threshold: 'vpin_histogram',
    vpin_cascade_threshold: 'vpin_histogram',
    arb_min_spread: 'spread_scale',
    preferred_venue: 'fee_comparison',
  }[varDef.key];

  const canvasColor = {
    vpin_informed_threshold: '#f59e0b',
    vpin_cascade_threshold: '#f87171',
  }[varDef.key];

  // Format display value
  const displayValue = () => {
    const v = parseFloat(localValue);
    if (varDef.unit === '%') return `${(v * 100).toFixed(1)}%`;
    if (varDef.unit === 'USD') return `$${v.toLocaleString()}`;
    if (varDef.key === 'cascade_cooldown_seconds') {
      const m = Math.floor(v / 60); const s = Math.floor(v % 60);
      return `${m}m ${s}s`;
    }
    if (varDef.key === 'cascade_min_liq_usd' || varDef.key === 'vpin_bucket_size_usd') {
      if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
      if (v >= 1_000) return `$${(v / 1_000).toFixed(0)}K`;
      return `$${v}`;
    }
    return v;
  };

  // Impact/help text
  const impactText = () => {
    const v = parseFloat(localValue);
    const br = parseFloat(config.starting_bankroll) || 25;
    const bf = parseFloat(config.bet_fraction) || 0.025;

    switch (varDef.key) {
      case 'starting_bankroll':
        return `Per trade: $${(br * bf).toFixed(2)} (at ${(bf * 100).toFixed(1)}% bet fraction)`;
      case 'bet_fraction':
        return `Per trade: $${(br * v).toFixed(2)} | Max loss/trade: $${(br * v).toFixed(2)}`;
      case 'max_drawdown_pct':
        return `Kill switch at $${(br * (1 - v)).toFixed(2)} equity (from $${br} start)`;
      case 'daily_loss_limit':
        const lossPerTrade = br * bf;
        return `≈ ${Math.floor(v / lossPerTrade)} losing trades before daily halt`;
      case 'max_position_usd':
        return `${((v / br) * 100).toFixed(0)}% of bankroll exposed per position`;
      case 'vpin_informed_threshold':
        return `Signals above ${(v * 100).toFixed(0)}% VPIN trigger informed trade signal`;
      case 'vpin_cascade_threshold':
        return v <= (parseFloat(config.vpin_informed_threshold) || 0.55)
          ? '⚠️ Must be greater than informed threshold'
          : `Cascade triggers above ${(v * 100).toFixed(0)}% VPIN (above informed level)`;
      case 'vpin_bucket_size_usd':
        return `Bucket fills in ~${Math.round(v / 2000)}s at $2M/hr BTC volume`;
      case 'vpin_lookback_buckets':
        const bsz = parseFloat(config.vpin_bucket_size_usd) || 50000;
        return `Total window: ${v} × $${(bsz / 1000).toFixed(0)}K = $${((v * bsz) / 1_000_000).toFixed(1)}M volume`;
      case 'arb_min_spread':
        const feePct = config.preferred_venue === 'opinion' ? 0.02 : 0.036;
        const netProfit = v - feePct;
        return netProfit > 0
          ? `Net profit per $1 arb: $${netProfit.toFixed(3)} after fees`
          : `⚠️ Below breakeven at current fees (${(feePct * 100).toFixed(1)}% round-trip)`;
      case 'arb_max_execution_ms':
        return `Latency budget: ${v}ms — ${v < 200 ? '⚡ tight' : v < 500 ? '✓ reasonable' : '⚠️ may miss closes'}`;
      case 'cascade_cooldown_seconds':
        const maxPerHr = Math.floor(3600 / v);
        return `Min gap between cascade bets — max ${maxPerHr} cascade bets/hr`;
      case 'cascade_min_liq_usd':
        const mStr = v >= 1_000_000 ? `$${(v / 1_000_000).toFixed(1)}M` : `$${(v / 1000).toFixed(0)}K`;
        return `Ignores liquidation events below ${mStr}`;
      default:
        return varDef.impact || '';
    }
  };

  const isWarning = varDef.key === 'vpin_cascade_threshold' &&
    parseFloat(localValue) <= parseFloat(config.vpin_informed_threshold || 0.55);

  return (
    <div
      data-key={varDef.key}
      data-value={localValue}
      style={{
        background: 'rgba(255,255,255,0.02)',
        border: `1px solid ${isWarning ? 'rgba(248,113,113,0.3)' : 'rgba(255,255,255,0.06)'}`,
        borderRadius: 8,
        padding: '12px 14px',
      }}
    >
      {/* Header row */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ flex: 1, minWidth: 0, marginRight: 10 }}>
          <div style={{ color: 'rgba(255,255,255,0.9)', fontSize: 13, fontWeight: 500 }}>
            {varDef.label}
          </div>
          <div style={{ color: 'rgba(255,255,255,0.35)', fontSize: 11, marginTop: 2, lineHeight: 1.4 }}>
            {varDef.description}
          </div>
        </div>

        {/* Toggle widget */}
        {varDef.widget === 'toggle' && (
          <button
            onClick={() => handleChange(!localValue)}
            style={{
              width: 52,
              height: 28,
              borderRadius: 14,
              border: 'none',
              cursor: 'pointer',
              background: localValue ? 'rgba(168,85,247,0.75)' : 'rgba(255,255,255,0.1)',
              position: 'relative',
              transition: 'background 200ms',
              flexShrink: 0,
              minHeight: 44,
              minWidth: 52,
            }}
          >
            <span style={{
              position: 'absolute',
              top: 4,
              left: localValue ? 28 : 4,
              width: 20,
              height: 20,
              borderRadius: '50%',
              background: '#fff',
              transition: 'left 200ms ease-out',
              boxShadow: localValue ? '0 0 8px rgba(168,85,247,0.7)' : 'none',
            }} />
          </button>
        )}

        {/* Venue select */}
        {varDef.widget === 'venue_select' && (
          <div style={{ display: 'flex', gap: 5, flexShrink: 0 }}>
            {['opinion', 'polymarket'].map(v => (
              <button
                key={v}
                onClick={() => handleChange(v)}
                style={{
                  padding: '5px 10px',
                  borderRadius: 5,
                  border: `1px solid ${localValue === v ? 'rgba(168,85,247,0.5)' : 'rgba(255,255,255,0.1)'}`,
                  background: localValue === v ? 'rgba(168,85,247,0.15)' : 'transparent',
                  color: localValue === v ? '#a855f7' : 'rgba(255,255,255,0.35)',
                  fontSize: 11, cursor: 'pointer', transition: 'all 150ms',
                  fontFamily: 'IBM Plex Mono, monospace', textTransform: 'capitalize',
                  minHeight: 36,
                }}
              >
                {v}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Slider + number input */}
      {(varDef.widget === 'slider' || varDef.widget === 'number') && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
          {varDef.widget === 'slider' && (
            <div style={{ flex: 1 }}>
              <input
                type="range"
                min={varDef.min}
                max={varDef.max}
                step={varDef.step}
                value={localValue}
                onChange={e => handleChange(varDef.type === 'integer' ? parseInt(e.target.value) : parseFloat(e.target.value))}
                style={{ width: '100%', accentColor: isWarning ? '#f87171' : '#a855f7' }}
              />
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 1 }}>
                <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 9 }}>
                  {varDef.unit === 'USD' ? `$${Number(varDef.min).toLocaleString()}` :
                   varDef.unit === '%' ? `${(varDef.min * 100).toFixed(0)}%` : varDef.min}
                </span>
                <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 9 }}>
                  {varDef.unit === 'USD' ? `$${Number(varDef.max).toLocaleString()}` :
                   varDef.unit === '%' ? `${(varDef.max * 100).toFixed(0)}%` : varDef.max}
                </span>
              </div>
            </div>
          )}

          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
            <input
              type="number"
              min={varDef.min}
              max={varDef.max}
              step={varDef.step}
              value={localValue}
              onChange={e => {
                const v = varDef.type === 'integer' ? parseInt(e.target.value) : parseFloat(e.target.value);
                if (!isNaN(v)) handleChange(v);
              }}
              style={{
                width: varDef.widget === 'number' ? '100%' : 72,
                background: 'rgba(255,255,255,0.05)',
                border: `1px solid ${isWarning ? 'rgba(248,113,113,0.4)' : 'rgba(255,255,255,0.1)'}`,
                borderRadius: 4,
                color: isWarning ? '#f87171' : '#a855f7',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 12, padding: '5px 8px',
                textAlign: 'right', outline: 'none',
                minHeight: 36,
              }}
            />
            {varDef.unit === '%' && (
              <span style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, minWidth: 30 }}>
                {(parseFloat(localValue) * 100).toFixed(1)}%
              </span>
            )}
          </div>
        </div>
      )}

      {/* Canvas widget */}
      {canvasType && (
        <div style={{ marginBottom: 8 }}>
          <ConfigWidget
            type={canvasType}
            value={varDef.widget === 'venue_select' ? localValue : parseFloat(localValue)}
            onChange={varDef.widget === 'slider' && (canvasType === 'vpin_histogram' || canvasType === 'spread_scale' || canvasType === 'drawdown_line')
              ? (v) => handleChange(v)
              : undefined}
            config={config}
            vpinHistory={vpinHistory}
            color={canvasColor}
          />
        </div>
      )}

      {/* Impact text */}
      <div style={{
        color: isWarning ? '#f87171' : 'rgba(255,255,255,0.3)',
        fontSize: 11,
        fontFamily: 'IBM Plex Mono, monospace',
        lineHeight: 1.4,
        borderLeft: `2px solid ${isWarning ? 'rgba(248,113,113,0.4)' : 'rgba(168,85,247,0.2)'}`,
        paddingLeft: 8,
      }}>
        {impactText()}
      </div>

      {/* Venue fee table */}
      {varDef.widget === 'venue_select' && (
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }} className="venue-fee-grid">
          {[
            { name: 'Opinion', key: 'opinion', perLeg: 0.010, color: '#a855f7' },
            { name: 'Polymarket', key: 'polymarket', perLeg: 0.018, color: '#06b6d4' },
          ].map(v => {
            const roundTrip = v.perLeg * 2;
            const selected = localValue === v.key;
            return (
              <div
                key={v.key}
                onClick={() => handleChange(v.key)}
                style={{
                  flex: 1,
                  background: selected ? `${v.color}12` : 'rgba(255,255,255,0.02)',
                  border: `1px solid ${selected ? v.color + '40' : 'rgba(255,255,255,0.06)'}`,
                  borderRadius: 6,
                  padding: '8px 10px',
                  cursor: 'pointer',
                  transition: 'all 200ms',
                }}
              >
                <div style={{ color: v.color, fontSize: 12, fontWeight: 600, marginBottom: 4 }}>{v.name}</div>
                <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' }}>
                  {(v.perLeg * 100).toFixed(1)}% per leg
                </div>
                <div style={{ color: 'rgba(255,255,255,0.35)', fontSize: 11 }}>
                  → {(roundTrip * 100).toFixed(1)}% round trip
                </div>
                <div style={{ color: v.color, fontSize: 11, marginTop: 2 }}>
                  → ${(10 * roundTrip).toFixed(2)} on $10
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Config library card ──────────────────────────────────────────────────────

function ConfigCard({ config, isLoaded, onLoad, onClone, onDelete, onApprove, onActivate }) {
  const ms = MODE_STYLES[config.mode] || MODE_STYLES.paper;
  return (
    <div style={{
      background: isLoaded ? 'rgba(168,85,247,0.06)' : 'rgba(255,255,255,0.02)',
      border: `1px solid ${isLoaded ? 'rgba(168,85,247,0.25)' : 'rgba(255,255,255,0.06)'}`,
      borderRadius: 8,
      padding: '10px 12px',
      transition: 'all 200ms ease-out',
    }}>
      <div style={{ marginBottom: 6 }}>
        <div style={{
          color: isLoaded ? '#a855f7' : 'rgba(255,255,255,0.85)',
          fontSize: 12, fontWeight: 600,
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}>
          {config.name}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 3, flexWrap: 'wrap' }}>
          <ModeBadge mode={config.mode} />
          <span style={{ color: 'rgba(255,255,255,0.25)', fontSize: 10 }}>v{config.version}</span>
          <StatusBadge config={config} />
        </div>
        {config.created_at && (
          <div style={{ color: 'rgba(255,255,255,0.2)', fontSize: 10, marginTop: 2 }}>
            {new Date(config.created_at).toLocaleDateString()}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
        {[
          { label: 'Load', color: '#a855f7', action: () => onLoad(config) },
          { label: 'Clone', color: '#06b6d4', action: () => onClone(config) },
          ...(!config.is_active ? [{ label: 'Activate', color: '#4ade80', action: () => onActivate(config) }] : []),
          ...(config.mode === 'live' && !config.is_approved ? [{ label: 'Approve', color: '#f59e0b', action: () => onApprove(config) }] : []),
          { label: 'Delete', color: '#f87171', action: () => onDelete(config) },
        ].map(btn => (
          <button
            key={btn.label}
            onClick={btn.action}
            style={{
              padding: '3px 8px',
              borderRadius: 4,
              border: `1px solid ${btn.color}25`,
              background: 'transparent',
              color: `${btn.color}80`,
              fontSize: 10, cursor: 'pointer',
              fontFamily: 'IBM Plex Mono, monospace',
              transition: 'all 150ms',
              minHeight: 28,
            }}
            onMouseEnter={e => {
              e.target.style.borderColor = btn.color + '60';
              e.target.style.background = btn.color + '15';
              e.target.style.color = btn.color;
            }}
            onMouseLeave={e => {
              e.target.style.borderColor = btn.color + '25';
              e.target.style.background = 'transparent';
              e.target.style.color = btn.color + '80';
            }}
          >
            {btn.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Modals ────────────────────────────────────────────────────────────────────

function ApproveModal({ config, onClose, onConfirm }) {
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setLoading(true); setError('');
    try { await onConfirm(config.id, password); onClose(); }
    catch (e) { setError(e.message || 'Approval failed'); }
    finally { setLoading(false); }
  };

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'rgba(0,0,0,0.85)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 9999, backdropFilter: 'blur(4px)', padding: '0 16px',
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div className="modal-inner" style={{
        background: '#0d0d16',
        border: '1px solid rgba(245,158,11,0.3)',
        borderRadius: 12, padding: 24,
        width: 380, maxWidth: '100%',
        boxShadow: '0 0 40px rgba(245,158,11,0.1)',
        animation: 'fadeSlideIn 200ms ease-out',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
          <span style={{ fontSize: 18 }}>🔐</span>
          <div>
            <div style={{ color: '#f59e0b', fontFamily: 'IBM Plex Mono, monospace', fontSize: 13, fontWeight: 700 }}>
              APPROVE FOR LIVE TRADING
            </div>
            <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 2 }}>
              {config.name} · v{config.version}
            </div>
          </div>
        </div>
        <input
          type="password"
          value={password}
          onChange={e => { setPassword(e.target.value); setError(''); }}
          placeholder="Approval password"
          autoFocus
          onKeyDown={e => e.key === 'Enter' && submit()}
          style={{
            width: '100%', background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.1)', borderRadius: 6,
            color: '#fff', fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 13, padding: '10px 12px', outline: 'none',
            marginBottom: error ? 8 : 16, boxSizing: 'border-box', minHeight: 44,
          }}
        />
        {error && <div style={{ color: '#f87171', fontSize: 11, marginBottom: 12 }}>{error}</div>}
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={onClose} style={{
            flex: 1, padding: '10px', borderRadius: 6,
            border: '1px solid rgba(255,255,255,0.1)', background: 'transparent',
            color: 'rgba(255,255,255,0.4)', fontSize: 12, cursor: 'pointer', minHeight: 44,
          }}>Cancel</button>
          <button onClick={submit} disabled={loading || !password} style={{
            flex: 2, padding: '10px', borderRadius: 6, border: 'none',
            background: password ? 'rgba(245,158,11,0.8)' : 'rgba(245,158,11,0.2)',
            color: password ? '#fff' : 'rgba(245,158,11,0.4)',
            fontSize: 12, fontWeight: 700, cursor: password && !loading ? 'pointer' : 'not-allowed',
            fontFamily: 'IBM Plex Mono, monospace', minHeight: 44,
          }}>
            {loading ? 'APPROVING...' : 'APPROVE'}
          </button>
        </div>
      </div>
    </div>
  );
}

function CompareModal({ configs, ids, onClose }) {
  const [a, b] = ids.map(id => configs.find(c => c.id === id)).filter(Boolean);
  if (!a || !b) return null;

  const allKeys = [...new Set([
    ...Object.keys(a.config || {}),
    ...Object.keys(b.config || {}),
  ])].sort();

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'rgba(0,0,0,0.9)',
      display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
      zIndex: 9999, backdropFilter: 'blur(4px)',
      padding: '40px 16px', overflowY: 'auto',
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{
        background: '#0d0d16',
        border: '1px solid rgba(255,255,255,0.08)',
        borderRadius: 12, padding: 20,
        width: 700, maxWidth: '100%',
        animation: 'fadeSlideIn 200ms ease-out',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 16 }}>
          <div style={{ color: '#a855f7', fontFamily: 'IBM Plex Mono, monospace', fontSize: 13, fontWeight: 700 }}>
            CONFIG DIFF
          </div>
          <button onClick={onClose} style={{
            background: 'none', border: 'none', color: 'rgba(255,255,255,0.3)',
            fontSize: 16, cursor: 'pointer', padding: 4,
          }}>✕</button>
        </div>

        {/* Column headers */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 8, marginBottom: 8 }}>
          <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>KEY</div>
          <div style={{ color: '#a855f7', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace', fontWeight: 600 }}>{a.name}</div>
          <div style={{ color: '#06b6d4', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace', fontWeight: 600 }}>{b.name}</div>
        </div>

        <div style={{ maxHeight: 400, overflowY: 'auto' }}>
          {allKeys.map(key => {
            const va = a.config?.[key];
            const vb = b.config?.[key];
            const diff = JSON.stringify(va) !== JSON.stringify(vb);
            return (
              <div key={key} style={{
                display: 'grid',
                gridTemplateColumns: '1fr 1fr 1fr',
                gap: 8,
                padding: '5px 0',
                borderBottom: '1px solid rgba(255,255,255,0.04)',
                background: diff ? 'rgba(245,158,11,0.03)' : 'transparent',
              }}>
                <div style={{ color: diff ? '#f59e0b' : 'rgba(255,255,255,0.35)', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>
                  {diff ? '● ' : ''}{key}
                </div>
                <div style={{ color: diff ? '#a855f7' : 'rgba(255,255,255,0.4)', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' }}>
                  {va !== undefined ? String(va) : '—'}
                </div>
                <div style={{ color: diff ? '#06b6d4' : 'rgba(255,255,255,0.4)', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' }}>
                  {vb !== undefined ? String(vb) : '—'}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ── Main TradingConfig page ───────────────────────────────────────────────────

export default function TradingConfig() {
  const api = useApi();

  const [defaults, setDefaults] = useState([]);
  const [configs, setConfigs] = useState([]);
  const [liveStatus, setLiveStatus] = useState({});
  const [vpinHistory, setVpinHistory] = useState([]);

  const [workingConfig, setWorkingConfig] = useState({});
  const [workingName, setWorkingName] = useState('');
  const [workingDescription, setWorkingDescription] = useState('');
  const [workingMode, setWorkingMode] = useState('paper');
  const [loadedConfigId, setLoadedConfigId] = useState(null);
  const [isDirty, setIsDirty] = useState(false);

  const [filterMode, setFilterMode] = useState('all');
  const [approveTarget, setApproveTarget] = useState(null);
  const [compareIds, setCompareIds] = useState([]);
  const [showCompare, setShowCompare] = useState(false);
  const [saveStatus, setSaveStatus] = useState(''); // '' | 'saving' | 'saved' | 'error'
  const [libraryOpen, setLibraryOpen] = useState(true);

  // Load
  const loadData = useCallback(async () => {
    try {
      const [defsRes, listRes, statusRes] = await Promise.all([
        api('GET', '/trading-config/defaults'),
        api('GET', '/trading-config/list'),
        api('GET', '/trading-config/live-status'),
      ]);
      setDefaults(defsRes.data.defaults || []);
      setConfigs(listRes.data.configs || []);
      setLiveStatus(statusRes.data || {});
    } catch (e) { console.error('Failed to load trading config data', e); }
  }, [api]);

  const loadVpinHistory = useCallback(async () => {
    try {
      const res = await api('GET', '/dashboard/vpin-history');
      const vals = (res.data.history || []).map(h => h.vpin_value ?? h.value ?? 0).filter(v => v > 0);
      if (vals.length > 0) setVpinHistory(vals);
    } catch { /* optional */ }
  }, [api]);

  useEffect(() => {
    loadData();
    loadVpinHistory();
  }, [loadData, loadVpinHistory]);

  // Init working config from defaults
  useEffect(() => {
    if (defaults.length > 0 && Object.keys(workingConfig).length === 0) {
      const init = {};
      defaults.forEach(d => { init[d.key] = d.default; });
      setWorkingConfig(init);
    }
  }, [defaults]);

  // Handler helpers
  const handleChange = useCallback((key, value) => {
    setWorkingConfig(prev => ({ ...prev, [key]: value }));
    setIsDirty(true);
  }, []);

  const handleLoadConfig = useCallback((config) => {
    setWorkingConfig(config.config || {});
    setWorkingName(config.name);
    setWorkingDescription(config.description || '');
    setWorkingMode(config.mode);
    setLoadedConfigId(config.id);
    setIsDirty(false);
    // Scroll to top of editor
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }, []);

  const handleSave = useCallback(async (activate = false) => {
    if (!workingName.trim()) { setSaveStatus('error'); return; }
    setSaveStatus('saving');
    try {
      let cfgId = loadedConfigId;
      if (loadedConfigId) {
        await api('PUT', `/trading-config/${loadedConfigId}`, {
          data: { name: workingName, description: workingDescription, config: workingConfig },
        });
      } else {
        const res = await api('POST', '/trading-config', {
          data: { name: workingName, description: workingDescription, config: workingConfig, mode: workingMode },
        });
        cfgId = res.data.config.id;
        setLoadedConfigId(cfgId);
      }
      if (activate && cfgId) {
        await api('POST', `/trading-config/${cfgId}/activate`);
      }
      setSaveStatus('saved');
      setIsDirty(false);
      await loadData();
      setTimeout(() => setSaveStatus(''), 2500);
    } catch {
      setSaveStatus('error');
      setTimeout(() => setSaveStatus(''), 3000);
    }
  }, [api, loadedConfigId, workingName, workingDescription, workingConfig, workingMode, loadData]);

  const handlePromoteToLive = useCallback(async () => {
    if (!loadedConfigId) return;
    if (!window.confirm(`Clone "${workingName}" as a LIVE config? This will need approval before activation.`)) return;
    try {
      await api('POST', `/trading-config/${loadedConfigId}/clone`, { data: { mode: 'live' } });
      await loadData();
    } catch (e) { alert(e.response?.data?.detail || 'Failed to clone config'); }
  }, [api, loadedConfigId, workingName, loadData]);

  const handleClone = useCallback(async (config) => {
    try { await api('POST', `/trading-config/${config.id}/clone`); await loadData(); }
    catch (e) { console.error(e); }
  }, [api, loadData]);

  const handleDelete = useCallback(async (config) => {
    if (!window.confirm(`Delete "${config.name}"?`)) return;
    try {
      await api('DELETE', `/trading-config/${config.id}`);
      if (loadedConfigId === config.id) { setLoadedConfigId(null); setIsDirty(false); }
      await loadData();
    } catch { }
  }, [api, loadData, loadedConfigId]);

  const handleActivate = useCallback(async (config) => {
    try { await api('POST', `/trading-config/${config.id}/activate`); await loadData(); }
    catch (e) { alert(e.response?.data?.detail || 'Failed to activate'); }
  }, [api, loadData]);

  const handleApprove = useCallback(async (configId, password) => {
    await api('POST', `/trading-config/${configId}/approve`, { data: { password } });
    await loadData();
  }, [api, loadData]);

  const handleLoadDefaults = () => {
    const init = {};
    defaults.forEach(d => { init[d.key] = d.default; });
    setWorkingConfig(init);
    setIsDirty(true);
  };

  // Grouped defaults
  const grouped = defaults.reduce((acc, def) => {
    const cat = def.category || 'other';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(def);
    return acc;
  }, {});

  const filteredConfigs = filterMode === 'all' ? configs : configs.filter(c => c.mode === filterMode);
  const activePaper = configs.find(c => c.mode === 'paper' && c.is_active);
  const activeLive = configs.find(c => c.mode === 'live' && c.is_active);

  const saveLabel = saveStatus === 'saving' ? 'SAVING...'
    : saveStatus === 'saved' ? '✓ SAVED'
    : saveStatus === 'error' ? '✗ ERROR'
    : isDirty ? 'SAVE DRAFT' : 'SAVE DRAFT';

  return (
    <div className="trading-config-page" style={{ padding: '20px 16px', maxWidth: 1280, margin: '0 auto' }}>

      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div style={{ marginBottom: 20 }}>
        <h1 style={{
          color: 'rgba(255,255,255,0.9)',
          fontFamily: 'IBM Plex Mono, monospace',
          fontSize: 18, fontWeight: 700, letterSpacing: '-0.01em',
          margin: 0, marginBottom: 4,
        }}>
          ⚙️ Trading Config
        </h1>
        <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 12 }}>
          Configure risk, signals, and strategy parameters
        </div>
      </div>

      {/* ── Active engine status ─────────────────────────────────────────────── */}
      <div className="engine-status-grid" style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 10,
        marginBottom: 20,
      }}>
        {[
          { mode: 'paper', label: 'Paper Engine', enabled: liveStatus.paper_enabled, config: activePaper },
          { mode: 'live',  label: 'Live Engine',  enabled: liveStatus.live_enabled,  config: activeLive  },
        ].map(({ mode, label, enabled, config }) => {
          const ms = MODE_STYLES[mode];
          return (
            <div key={mode} style={{
              background: enabled ? ms.bg : 'rgba(255,255,255,0.02)',
              border: `1px solid ${enabled ? ms.border : 'rgba(255,255,255,0.06)'}`,
              borderRadius: 8, padding: '12px 14px',
              transition: 'all 300ms ease-out',
              boxShadow: enabled && mode === 'live' ? '0 0 20px rgba(248,113,113,0.1)' : 'none',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span>{ms.icon}</span>
                  <span style={{
                    color: enabled ? ms.color : 'rgba(255,255,255,0.3)',
                    fontFamily: 'IBM Plex Mono, monospace',
                    fontSize: 11, fontWeight: 700, letterSpacing: '0.06em',
                  }}>
                    {label.toUpperCase()}
                  </span>
                </div>
                <span style={{
                  fontSize: 9, fontFamily: 'IBM Plex Mono, monospace',
                  color: enabled ? ms.color : 'rgba(255,255,255,0.2)',
                  padding: '2px 6px', borderRadius: 3,
                  border: `1px solid ${enabled ? ms.border : 'rgba(255,255,255,0.06)'}`,
                  background: enabled ? ms.bg : 'transparent',
                }}>
                  {enabled ? '● RUNNING' : '○ STOPPED'}
                </span>
              </div>
              {config ? (
                <div>
                  <div style={{ color: 'rgba(255,255,255,0.7)', fontSize: 12, fontWeight: 500 }}>{config.name}</div>
                  <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, marginTop: 1 }}>
                    v{config.version} · <StatusBadge config={config} />
                  </div>
                  <button
                    onClick={() => handleLoadConfig(config)}
                    style={{
                      marginTop: 6, padding: '3px 8px', borderRadius: 4,
                      border: `1px solid ${ms.border}`, background: 'transparent',
                      color: ms.color, fontSize: 10, cursor: 'pointer',
                      fontFamily: 'IBM Plex Mono, monospace',
                    }}
                  >
                    Load into editor →
                  </button>
                </div>
              ) : (
                <div style={{ color: 'rgba(255,255,255,0.25)', fontSize: 12 }}>
                  No active config
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Main two-column layout ───────────────────────────────────────────── */}
      <div className="config-layout" style={{
        display: 'grid',
        gridTemplateColumns: '1fr 300px',
        gap: 16,
        alignItems: 'start',
      }}>

        {/* ── Left: Config Editor ──────────────────────────────────────────── */}
        <div>
          {/* Config name / mode bar */}
          <div style={{
            background: 'rgba(255,255,255,0.02)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 8, padding: '10px 12px',
            marginBottom: 12,
          }}>
            <div style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
              <input
                type="text"
                value={workingName}
                onChange={e => { setWorkingName(e.target.value); setIsDirty(true); }}
                placeholder="Config name (required to save)"
                style={{
                  flex: 1, minWidth: 140,
                  background: 'rgba(255,255,255,0.05)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: 6, color: '#fff',
                  fontFamily: 'IBM Plex Mono, monospace',
                  fontSize: 12, padding: '8px 10px',
                  outline: 'none', minHeight: 36,
                }}
              />
              {!loadedConfigId && (
                <div style={{ display: 'flex', gap: 5 }}>
                  {['paper', 'live'].map(m => (
                    <button
                      key={m}
                      onClick={() => setWorkingMode(m)}
                      style={{
                        padding: '6px 10px', borderRadius: 5,
                        border: `1px solid ${workingMode === m ? MODE_STYLES[m].border : 'rgba(255,255,255,0.08)'}`,
                        background: workingMode === m ? MODE_STYLES[m].bg : 'transparent',
                        color: workingMode === m ? MODE_STYLES[m].color : 'rgba(255,255,255,0.3)',
                        fontSize: 11, cursor: 'pointer',
                        fontFamily: 'IBM Plex Mono, monospace', fontWeight: 700,
                        minHeight: 36,
                      }}
                    >
                      {MODE_STYLES[m].icon} {m.toUpperCase()}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <input
              type="text"
              value={workingDescription}
              onChange={e => { setWorkingDescription(e.target.value); setIsDirty(true); }}
              placeholder="Description (optional)"
              style={{
                width: '100%',
                background: 'rgba(255,255,255,0.03)',
                border: '1px solid rgba(255,255,255,0.07)',
                borderRadius: 5, color: 'rgba(255,255,255,0.45)',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 11, padding: '6px 10px',
                outline: 'none', boxSizing: 'border-box',
              }}
            />
            {isDirty && (
              <div style={{ color: '#f59e0b', fontSize: 10, marginTop: 5 }}>● Unsaved changes</div>
            )}
            <button onClick={handleLoadDefaults} style={{
              marginTop: 6,
              background: 'none', border: 'none',
              color: 'rgba(255,255,255,0.25)', fontSize: 10, cursor: 'pointer',
              padding: '2px 0', fontFamily: 'IBM Plex Mono, monospace',
            }}>
              ↺ Reset to defaults
            </button>
          </div>

          {/* Category sections */}
          {Object.entries(grouped).map(([category, vars]) => (
            <CollapsibleSection key={category} category={category}>
              {vars.map(varDef => (
                <ConfigVar
                  key={varDef.key}
                  def={varDef}
                  value={workingConfig[varDef.key] ?? varDef.default}
                  onChange={handleChange}
                  config={workingConfig}
                  vpinHistory={vpinHistory}
                />
              ))}
            </CollapsibleSection>
          ))}

          {/* Config Library (collapsible, at bottom of editor) */}
          <div style={{
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10, overflow: 'hidden', marginTop: 12,
          }}>
            <button
              onClick={() => setLibraryOpen(o => !o)}
              style={{
                width: '100%', display: 'flex', alignItems: 'center',
                justifyContent: 'space-between',
                padding: '12px 16px',
                background: 'rgba(255,255,255,0.025)',
                border: 'none', cursor: 'pointer',
                borderBottom: libraryOpen ? '1px solid rgba(255,255,255,0.05)' : 'none',
                minHeight: 48,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 14 }}>📚</span>
                <span style={{
                  color: 'rgba(255,255,255,0.6)',
                  fontFamily: 'IBM Plex Mono, monospace',
                  fontSize: 11, fontWeight: 700, letterSpacing: '0.06em',
                }}>CONFIG LIBRARY ({configs.length})</span>
              </div>
              <span style={{
                color: 'rgba(255,255,255,0.3)', fontSize: 14,
                transform: libraryOpen ? 'rotate(90deg)' : 'rotate(0)',
                transition: 'transform 200ms ease-out', display: 'inline-block',
              }}>›</span>
            </button>

            {libraryOpen && (
              <div style={{ padding: 12 }}>
                {/* Filter tabs */}
                <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
                  {['all', 'paper', 'live'].map(f => (
                    <button
                      key={f}
                      onClick={() => setFilterMode(f)}
                      style={{
                        padding: '4px 10px', borderRadius: 4,
                        border: `1px solid ${filterMode === f ? 'rgba(168,85,247,0.3)' : 'rgba(255,255,255,0.06)'}`,
                        background: filterMode === f ? 'rgba(168,85,247,0.1)' : 'transparent',
                        color: filterMode === f ? '#a855f7' : 'rgba(255,255,255,0.3)',
                        fontSize: 10, cursor: 'pointer',
                        fontFamily: 'IBM Plex Mono, monospace', fontWeight: 600,
                        minHeight: 30,
                      }}
                    >
                      {f.toUpperCase()}
                    </button>
                  ))}

                  {compareIds.length === 2 && (
                    <button
                      onClick={() => setShowCompare(true)}
                      style={{
                        marginLeft: 'auto', padding: '4px 10px', borderRadius: 4,
                        border: '1px solid rgba(6,182,212,0.3)',
                        background: 'rgba(6,182,212,0.08)',
                        color: '#06b6d4', fontSize: 10, cursor: 'pointer',
                        fontFamily: 'IBM Plex Mono, monospace',
                      }}
                    >
                      Compare ({compareIds.length})
                    </button>
                  )}
                </div>

                {filteredConfigs.length === 0 ? (
                  <div style={{ color: 'rgba(255,255,255,0.25)', fontSize: 12, padding: '16px 0', textAlign: 'center' }}>
                    No configs yet.
                  </div>
                ) : (
                  <div style={{ display: 'grid', gap: 8 }} className="config-card-grid">
                    {filteredConfigs.map(config => (
                      <ConfigCard
                        key={config.id}
                        config={config}
                        isLoaded={loadedConfigId === config.id}
                        onLoad={handleLoadConfig}
                        onClone={handleClone}
                        onDelete={handleDelete}
                        onApprove={c => setApproveTarget(c)}
                        onActivate={handleActivate}
                      />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Right: Library sidebar (desktop) + Go-live checklist ─────────── */}
        <div className="config-sidebar" style={{ position: 'sticky', top: 70 }}>

          {/* Go-live checklist */}
          <div style={{
            background: 'rgba(255,255,255,0.015)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10, padding: '12px 14px',
            marginBottom: 12,
          }}>
            <div style={{
              color: 'rgba(255,255,255,0.4)',
              fontFamily: 'IBM Plex Mono, monospace',
              fontSize: 10, fontWeight: 700, letterSpacing: '0.06em',
              marginBottom: 10,
            }}>
              GO-LIVE CHECKLIST
            </div>

            {[
              { label: 'API keys configured', ok: liveStatus.api_keys_configured },
              { label: 'Live config approved', ok: liveStatus.live_has_approved_config },
              { label: 'Live config active', ok: !!liveStatus.active_live_config },
            ].map((item, i) => (
              <div key={i} style={{
                display: 'flex', alignItems: 'center', gap: 8,
                padding: '5px 0',
                borderBottom: i < 2 ? '1px solid rgba(255,255,255,0.04)' : 'none',
              }}>
                <span style={{ fontSize: 12, color: item.ok ? '#4ade80' : 'rgba(248,113,113,0.6)' }}>
                  {item.ok ? '✓' : '✗'}
                </span>
                <span style={{ color: item.ok ? 'rgba(255,255,255,0.6)' : 'rgba(255,255,255,0.3)', fontSize: 11 }}>
                  {item.label}
                </span>
              </div>
            ))}

            <div style={{
              marginTop: 10, padding: '7px 10px', borderRadius: 5,
              background: liveStatus.can_go_live ? 'rgba(74,222,128,0.08)' : 'rgba(248,113,113,0.05)',
              border: `1px solid ${liveStatus.can_go_live ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.15)'}`,
              color: liveStatus.can_go_live ? '#4ade80' : '#f87171',
              fontSize: 11, fontFamily: 'IBM Plex Mono, monospace', textAlign: 'center',
            }}>
              {liveStatus.can_go_live ? '✓ Ready for live' : '✗ Not ready for live'}
            </div>
          </div>

          {/* Library (desktop) */}
          <div style={{
            background: 'rgba(255,255,255,0.015)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10, overflow: 'hidden',
          }}>
            <div style={{
              padding: '10px 14px',
              borderBottom: '1px solid rgba(255,255,255,0.05)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              background: 'rgba(255,255,255,0.02)',
            }}>
              <span style={{
                color: 'rgba(255,255,255,0.6)',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 10, fontWeight: 700, letterSpacing: '0.06em',
              }}>
                SAVED CONFIGS
              </span>
              <span style={{ color: 'rgba(255,255,255,0.2)', fontSize: 10 }}>{configs.length}</span>
            </div>

            {/* Filter */}
            <div style={{ display: 'flex', padding: '7px 10px', gap: 5, borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
              {['all', 'paper', 'live'].map(f => (
                <button
                  key={f}
                  onClick={() => setFilterMode(f)}
                  style={{
                    flex: 1, padding: '3px 0', borderRadius: 4,
                    border: `1px solid ${filterMode === f ? 'rgba(168,85,247,0.3)' : 'rgba(255,255,255,0.06)'}`,
                    background: filterMode === f ? 'rgba(168,85,247,0.1)' : 'transparent',
                    color: filterMode === f ? '#a855f7' : 'rgba(255,255,255,0.25)',
                    fontSize: 9, cursor: 'pointer',
                    fontFamily: 'IBM Plex Mono, monospace', fontWeight: 600,
                  }}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>

            <div style={{ padding: 8, maxHeight: 400, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6 }}>
              {filteredConfigs.length === 0 ? (
                <div style={{ color: 'rgba(255,255,255,0.2)', fontSize: 11, padding: '12px 4px', textAlign: 'center' }}>
                  No configs yet
                </div>
              ) : filteredConfigs.map(config => (
                <ConfigCard
                  key={config.id}
                  config={config}
                  isLoaded={loadedConfigId === config.id}
                  onLoad={handleLoadConfig}
                  onClone={handleClone}
                  onDelete={handleDelete}
                  onApprove={c => setApproveTarget(c)}
                  onActivate={handleActivate}
                />
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ── Sticky Bottom Action Bar ─────────────────────────────────────────── */}
      <div
        className="action-bar"
        style={{
          position: 'sticky',
          bottom: 0,
          left: 0,
          right: 0,
          background: 'rgba(7,7,12,0.97)',
          borderTop: '1px solid rgba(255,255,255,0.07)',
          padding: '10px 16px',
          display: 'flex',
          gap: 8,
          alignItems: 'center',
          zIndex: 50,
          backdropFilter: 'blur(8px)',
          flexWrap: 'wrap',
        }}
      >
        <div style={{ flex: 1, minWidth: 120 }}>
          {loadedConfigId ? (
            <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, fontFamily: 'IBM Plex Mono, monospace' }}>
              Editing: <span style={{ color: '#a855f7' }}>{workingName}</span>
              {isDirty && <span style={{ color: '#f59e0b' }}> ●</span>}
            </div>
          ) : (
            <div style={{ color: 'rgba(255,255,255,0.2)', fontSize: 11 }}>No config loaded</div>
          )}
        </div>

        <button
          onClick={handleLoadDefaults}
          style={{
            padding: '8px 12px', borderRadius: 6,
            border: '1px solid rgba(255,255,255,0.1)', background: 'transparent',
            color: 'rgba(255,255,255,0.35)', fontSize: 11, cursor: 'pointer',
            fontFamily: 'IBM Plex Mono, monospace', minHeight: 40,
          }}
        >
          ↺ Defaults
        </button>

        <button
          onClick={() => handleSave(false)}
          disabled={!workingName.trim() || saveStatus === 'saving'}
          style={{
            padding: '8px 14px', borderRadius: 6,
            border: `1px solid ${saveStatus === 'error' ? 'rgba(248,113,113,0.3)' : 'rgba(168,85,247,0.3)'}`,
            background: saveStatus === 'saved' ? 'rgba(74,222,128,0.15)'
              : saveStatus === 'error' ? 'rgba(248,113,113,0.1)'
              : 'rgba(168,85,247,0.1)',
            color: saveStatus === 'saved' ? '#4ade80'
              : saveStatus === 'error' ? '#f87171'
              : '#a855f7',
            fontSize: 11, fontWeight: 700, cursor: 'pointer',
            fontFamily: 'IBM Plex Mono, monospace', letterSpacing: '0.04em',
            transition: 'all 200ms', minHeight: 40,
          }}
        >
          {saveLabel}
        </button>

        <button
          onClick={() => handleSave(true)}
          disabled={!workingName.trim() || saveStatus === 'saving'}
          style={{
            padding: '8px 14px', borderRadius: 6, border: 'none',
            background: workingName.trim() ? 'rgba(168,85,247,0.8)' : 'rgba(168,85,247,0.2)',
            color: workingName.trim() ? '#fff' : 'rgba(168,85,247,0.4)',
            fontSize: 11, fontWeight: 700,
            cursor: workingName.trim() ? 'pointer' : 'not-allowed',
            fontFamily: 'IBM Plex Mono, monospace', letterSpacing: '0.04em',
            transition: 'all 200ms', minHeight: 40,
          }}
        >
          Save & Activate
        </button>

        <button
          onClick={handlePromoteToLive}
          disabled={!loadedConfigId || workingMode === 'live'}
          style={{
            padding: '8px 14px', borderRadius: 6,
            border: '1px solid rgba(248,113,113,0.3)',
            background: 'rgba(248,113,113,0.08)',
            color: loadedConfigId && workingMode !== 'live' ? '#f87171' : 'rgba(248,113,113,0.3)',
            fontSize: 11, fontWeight: 600,
            cursor: loadedConfigId && workingMode !== 'live' ? 'pointer' : 'not-allowed',
            fontFamily: 'IBM Plex Mono, monospace',
            transition: 'all 200ms', minHeight: 40,
          }}
          title={workingMode === 'live' ? 'Already a live config' : 'Clone as live config'}
        >
          Promote to Live →
        </button>
      </div>

      {/* ── Modals ──────────────────────────────────────────────────────────── */}
      {approveTarget && (
        <ApproveModal
          config={approveTarget}
          onClose={() => setApproveTarget(null)}
          onConfirm={handleApprove}
        />
      )}

      {showCompare && compareIds.length === 2 && (
        <CompareModal
          configs={configs}
          ids={compareIds}
          onClose={() => { setShowCompare(false); setCompareIds([]); }}
        />
      )}

      {/* ── Styles ──────────────────────────────────────────────────────────── */}
      <style>{`
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(-10px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }

        input[type=range] {
          -webkit-appearance: none;
          height: 4px;
          background: rgba(255,255,255,0.1);
          border-radius: 2px;
          cursor: pointer;
        }
        input[type=range]::-webkit-slider-thumb {
          -webkit-appearance: none;
          width: 16px; height: 16px;
          border-radius: 50%;
          background: #a855f7;
          cursor: pointer;
          box-shadow: 0 0 6px rgba(168,85,247,0.5);
          transition: box-shadow 150ms;
        }
        input[type=range]:hover::-webkit-slider-thumb {
          box-shadow: 0 0 12px rgba(168,85,247,0.85);
        }
        input[type=number]::-webkit-inner-spin-button { opacity: 0.3; }

        @media (max-width: 768px) {
          .trading-config-page { padding: 12px 10px !important; }
          .config-layout {
            grid-template-columns: 1fr !important;
          }
          .config-sidebar {
            position: static !important;
            order: -1;
          }
          .engine-status-grid {
            grid-template-columns: 1fr !important;
          }
          .venue-fee-grid {
            flex-direction: column !important;
          }
          .config-card-grid {
            grid-template-columns: 1fr 1fr !important;
          }
          .action-bar {
            padding: 8px 10px !important;
            bottom: 60px !important; /* above mobile tab bar */
          }
          .action-bar button {
            font-size: 10px !important;
            padding: 7px 9px !important;
          }
        }

        @media (max-width: 480px) {
          .config-card-grid {
            grid-template-columns: 1fr !important;
          }
          .action-bar {
            flex-wrap: wrap;
          }
        }

        ::-webkit-scrollbar { width: 3px; height: 3px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
      `}</style>
    </div>
  );
}
