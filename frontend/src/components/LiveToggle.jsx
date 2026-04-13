import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

/**
 * LiveToggle — exclusive mode switch: 📄 PAPER or 💰 LIVE
 *
  * 📄 PAPER  — safe mode, instantly forces paper on and live off
  * 💰 LIVE   — requires confirmation modal, forces live on and paper off
 *
 * Mobile: stacks vertically, confirmation modal becomes full-screen.
 */
export default function LiveToggle({ compact = false }) {
  const api = useApi();
  const [status, setStatus] = useState({
    paper_enabled: true,
    live_enabled: false,
    can_go_live: false,
    live_has_approved_config: false,
    api_keys_configured: false,
    active_live_config: null,
    engine_paper_mode: true,
    engine_active_config: null,
    engine_kill_switch: false,
    wallet_balance_usdc: null,
  });
  const [showLiveModal, setShowLiveModal] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchStatus = useCallback(async () => {
    try {
      const res = await api('GET', '/trading-config/live-status');
      setStatus(res.data);
    } catch { /* silent */ }
  }, [api]);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 15000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Apply red glow to header bar when live is enabled
  useEffect(() => {
    const header = document.getElementById('live-toggle-header');
    if (!header) return;
    if (status.live_enabled) {
      header.style.borderBottom = '1px solid rgba(248,113,113,0.3)';
      header.style.boxShadow = '0 1px 20px rgba(248,113,113,0.08)';
    } else {
      header.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
      header.style.boxShadow = 'none';
    }
  }, [status.live_enabled]);

  const handlePaperToggle = async () => {
    try {
      const res = await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'paper', enabled: true },
      });
      setStatus(prev => ({
        ...prev,
        paper_enabled: res.data.paper_enabled ?? true,
        live_enabled: res.data.live_enabled ?? false,
      }));
    } catch (e) { console.error('Failed to toggle paper mode', e); }
  };

  const handleLiveClick = () => {
    if (status.live_enabled) {
      disableLive();
    } else {
      setConfirmText('');
      setError('');
      setShowLiveModal(true);
    }
  };

  const disableLive = async () => {
    try {
      const res = await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'live', enabled: false },
      });
      setStatus(prev => ({
        ...prev,
        paper_enabled: res.data.paper_enabled ?? true,
        live_enabled: res.data.live_enabled ?? false,
      }));
    } catch (e) { console.error('Failed to disable live mode', e); }
  };

  const confirmEnableLive = async () => {
    if (confirmText !== 'CONFIRM') {
      setError('Type CONFIRM exactly to proceed');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const res = await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'live', enabled: true, confirmation: 'CONFIRM' },
      });
      setStatus(prev => ({
        ...prev,
        paper_enabled: res.data.paper_enabled ?? false,
        live_enabled: res.data.live_enabled ?? true,
      }));
      setShowLiveModal(false);
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to enable live trading');
    } finally {
      setLoading(false);
    }
  };

  const checks = [
    {
      label: 'API keys configured',
      ok: status.api_keys_configured,
      hint: 'Add POLYMARKET_API_KEY or OPINION_API_KEY in Setup',
    },
    {
      label: 'Live config approved',
      ok: status.live_has_approved_config,
      hint: 'Go to Trading Config and approve a live config',
    },
    {
      label: 'Live config is active',
      ok: !!status.active_live_config,
      hint: 'Activate a live config in Trading Config',
    },
  ];

  const allChecksPass = checks.every(c => c.ok);

  const toggleBtn = (opts) => {
    const { label, icon, enabled, color, borderColor, bgColor, glowColor, onClick } = opts;
    return (
      <button
        onClick={onClick}
        title={`${label}: ${enabled ? 'ON' : 'OFF'}`}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: compact ? 4 : 6,
          padding: compact ? '4px 8px' : '5px 10px 5px 8px',
          borderRadius: 6,
          border: `1px solid ${enabled ? borderColor : 'rgba(255,255,255,0.08)'}`,
          background: enabled ? bgColor : 'rgba(255,255,255,0.03)',
          cursor: 'pointer',
          transition: 'all 200ms ease-out',
          boxShadow: enabled ? `0 0 14px ${glowColor}` : 'none',
          animation: enabled && label === 'LIVE' ? 'livePulse 2s ease-in-out infinite' : 'none',
          minHeight: 36,
        }}
      >
        <span style={{ fontSize: compact ? 11 : 13 }}>{icon}</span>
        {!compact && (
          <span style={{
            fontSize: 10,
            fontFamily: 'IBM Plex Mono, monospace',
            fontWeight: 600,
            letterSpacing: '0.08em',
            color: enabled ? color : 'rgba(255,255,255,0.3)',
            transition: 'color 200ms',
          }}>
            {label}
          </span>
        )}
        {/* Mini toggle pill */}
        <div style={{
          width: compact ? 24 : 28,
          height: compact ? 12 : 14,
          borderRadius: 7,
          background: enabled ? `${color}99` : 'rgba(255,255,255,0.1)',
          position: 'relative',
          transition: 'background 200ms',
          flexShrink: 0,
        }}>
          <span style={{
            position: 'absolute',
            top: compact ? 1 : 2,
            left: enabled ? (compact ? 13 : 15) : 2,
            width: compact ? 10 : 10,
            height: compact ? 10 : 10,
            borderRadius: '50%',
            background: '#fff',
            transition: 'left 200ms ease-out',
            boxShadow: enabled ? `0 0 4px ${glowColor}` : 'none',
          }} />
        </div>
      </button>
    );
  };

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} className="live-toggle-wrapper">
        {toggleBtn({
          label: 'PAPER',
          icon: '📄',
          enabled: status.paper_enabled,
          color: '#a855f7',
          borderColor: 'rgba(168,85,247,0.45)',
          bgColor: 'rgba(168,85,247,0.1)',
          glowColor: 'rgba(168,85,247,0.22)',
          onClick: handlePaperToggle,
        })}
        {toggleBtn({
          label: 'LIVE',
          icon: '💰',
          enabled: status.live_enabled,
          color: '#f87171',
          borderColor: 'rgba(248,113,113,0.5)',
          bgColor: 'rgba(248,113,113,0.1)',
          glowColor: 'rgba(248,113,113,0.3)',
          onClick: handleLiveClick,
        })}

        {/* Engine runtime indicator — shows what the engine is ACTUALLY running */}
        {!compact && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 4,
            padding: '4px 8px', borderRadius: 4,
            background: 'rgba(255,255,255,0.03)',
            border: '1px solid rgba(255,255,255,0.06)',
          }}>
            <span style={{
              width: 6, height: 6, borderRadius: '50%',
              background: status.engine_kill_switch ? '#f87171'
                : status.engine_paper_mode ? '#a855f7' : '#4ade80',
              boxShadow: status.engine_kill_switch
                ? '0 0 6px rgba(248,113,113,0.5)'
                : status.engine_paper_mode
                  ? '0 0 6px rgba(168,85,247,0.3)'
                  : '0 0 6px rgba(74,222,128,0.4)',
            }} />
            <span style={{
              fontSize: 9, fontFamily: "'IBM Plex Mono', monospace",
              fontWeight: 600, letterSpacing: '0.06em',
              color: status.engine_kill_switch ? '#f87171'
                : status.engine_paper_mode ? 'rgba(168,85,247,0.7)' : 'rgba(74,222,128,0.8)',
            }}>
              {status.engine_kill_switch ? 'KILLED'
                : status.engine_paper_mode ? 'ENGINE: PAPER' : 'ENGINE: LIVE'}
            </span>
            {status.wallet_balance_usdc != null && !status.engine_paper_mode && (
              <span style={{
                fontSize: 9, fontFamily: "'IBM Plex Mono', monospace",
                color: 'rgba(255,255,255,0.35)',
              }}>
                ${parseFloat(status.wallet_balance_usdc).toFixed(0)}
              </span>
            )}
          </div>
        )}
      </div>

      {/* ── Live Enable Modal ──────────────────────────────────────────────── */}
      {showLiveModal && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.85)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 9999,
            backdropFilter: 'blur(4px)',
            padding: '0 16px',
          }}
          onClick={e => e.target === e.currentTarget && setShowLiveModal(false)}
        >
          <div
            className="live-modal-inner"
            style={{
              background: '#0d0d16',
              border: '1px solid rgba(248,113,113,0.3)',
              borderRadius: 12,
              padding: 24,
              width: 420,
              maxWidth: '100%',
              boxShadow: '0 0 40px rgba(248,113,113,0.15)',
              animation: 'fadeSlideIn 200ms ease-out',
              maxHeight: '90vh',
              overflowY: 'auto',
            }}
          >
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, marginBottom: 20 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <span style={{ fontSize: 20 }}>⚠️</span>
                <div>
                  <div style={{
                    color: '#f87171',
                    fontFamily: 'IBM Plex Mono, monospace',
                    fontSize: 14,
                    fontWeight: 700,
                    letterSpacing: '0.05em',
                  }}>
                    ENABLE LIVE TRADING
                  </div>
                  <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 2 }}>
                    Real money. Real orders. No undo.
                  </div>
                </div>
              </div>
              <button
                onClick={() => setShowLiveModal(false)}
                style={{
                  background: 'none', border: 'none', color: 'rgba(255,255,255,0.3)',
                  fontSize: 18, cursor: 'pointer', padding: 4, lineHeight: 1,
                  flexShrink: 0,
                }}
              >
                ✕
              </button>
            </div>

            {/* Active live config info */}
            {status.active_live_config ? (
              <div style={{
                background: 'rgba(248,113,113,0.05)',
                border: '1px solid rgba(248,113,113,0.15)',
                borderRadius: 8,
                padding: '10px 14px',
                marginBottom: 16,
              }}>
                <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: 10, marginBottom: 4 }}>ACTIVE LIVE CONFIG</div>
                <div style={{ color: '#fff', fontSize: 13, fontWeight: 500 }}>
                  {status.active_live_config.name}
                </div>
                <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 2 }}>
                  v{status.active_live_config.version} ·{' '}
                  {status.active_live_config.is_approved
                    ? <span style={{ color: '#4ade80' }}>✓ approved</span>
                    : <span style={{ color: '#f87171' }}>✗ not approved</span>
                  }
                </div>
              </div>
            ) : (
              <div style={{
                background: 'rgba(248,113,113,0.05)',
                border: '1px solid rgba(248,113,113,0.2)',
                borderRadius: 8, padding: '10px 14px', marginBottom: 16,
                color: '#f87171', fontSize: 12,
              }}>
                ✗ No active live config found
              </div>
            )}

            {/* Checklist */}
            <div style={{ marginBottom: 20 }}>
              {checks.map((check, i) => (
                <div key={i} style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 10,
                  padding: '8px 0',
                  borderBottom: i < checks.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                }}>
                  <span style={{
                    fontSize: 13,
                    marginTop: 1,
                    color: check.ok ? '#4ade80' : 'rgba(248,113,113,0.7)',
                    flexShrink: 0,
                  }}>
                    {check.ok ? '✓' : '✗'}
                  </span>
                  <div>
                    <div style={{ color: check.ok ? 'rgba(255,255,255,0.7)' : 'rgba(255,255,255,0.4)', fontSize: 12 }}>
                      {check.label}
                    </div>
                    {!check.ok && (
                      <div style={{ color: 'rgba(248,113,113,0.6)', fontSize: 10, marginTop: 1 }}>
                        {check.hint}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>

            {/* Confirmation input */}
            {allChecksPass ? (
              <div style={{ marginBottom: 16 }}>
                <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: 11, marginBottom: 6 }}>
                  Type <span style={{ color: '#f87171', fontFamily: 'IBM Plex Mono, monospace' }}>CONFIRM</span> to enable live trading:
                </div>
                <input
                  type="text"
                  value={confirmText}
                  onChange={e => { setConfirmText(e.target.value); setError(''); }}
                  placeholder="CONFIRM"
                  autoFocus
                  style={{
                    width: '100%',
                    background: 'rgba(255,255,255,0.05)',
                    border: `1px solid ${confirmText === 'CONFIRM' ? 'rgba(248,113,113,0.5)' : 'rgba(255,255,255,0.1)'}`,
                    borderRadius: 6,
                    color: '#f87171',
                    fontFamily: 'IBM Plex Mono, monospace',
                    fontSize: 14,
                    fontWeight: 600,
                    letterSpacing: '0.1em',
                    padding: '10px 12px',
                    outline: 'none',
                    textTransform: 'uppercase',
                    boxSizing: 'border-box',
                    minHeight: 44,
                  }}
                  onKeyDown={e => e.key === 'Enter' && confirmEnableLive()}
                />
              </div>
            ) : (
              <div style={{
                background: 'rgba(248,113,113,0.05)',
                border: '1px solid rgba(248,113,113,0.15)',
                borderRadius: 6, padding: '10px 12px', marginBottom: 16,
                color: 'rgba(248,113,113,0.7)', fontSize: 12,
              }}>
                Complete all checklist items before enabling live trading.
              </div>
            )}

            {error && (
              <div style={{
                color: '#f87171', fontSize: 12, marginBottom: 12,
                padding: '6px 10px',
                background: 'rgba(248,113,113,0.08)',
                borderRadius: 4,
              }}>
                {error}
              </div>
            )}

            {/* Actions */}
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                onClick={() => setShowLiveModal(false)}
                style={{
                  flex: 1, padding: '11px', borderRadius: 6,
                  border: '1px solid rgba(255,255,255,0.1)',
                  background: 'transparent',
                  color: 'rgba(255,255,255,0.5)', fontSize: 12, cursor: 'pointer',
                  fontFamily: 'IBM Plex Mono, monospace',
                  minHeight: 44,
                }}
              >
                Cancel
              </button>
              <button
                onClick={confirmEnableLive}
                disabled={!allChecksPass || confirmText !== 'CONFIRM' || loading}
                style={{
                  flex: 2, padding: '11px', borderRadius: 6, border: 'none',
                  background: allChecksPass && confirmText === 'CONFIRM'
                    ? 'rgba(248,113,113,0.85)'
                    : 'rgba(248,113,113,0.2)',
                  color: allChecksPass && confirmText === 'CONFIRM' ? '#fff' : 'rgba(248,113,113,0.4)',
                  fontSize: 12, fontWeight: 700,
                  letterSpacing: '0.05em',
                  cursor: allChecksPass && confirmText === 'CONFIRM' && !loading ? 'pointer' : 'not-allowed',
                  fontFamily: 'IBM Plex Mono, monospace',
                  transition: 'all 150ms',
                  minHeight: 44,
                }}
              >
                {loading ? 'ENABLING...' : 'ENABLE LIVE TRADING'}
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`
        @keyframes livePulse {
          0%, 100% { box-shadow: 0 0 12px rgba(248,113,113,0.2), 0 0 4px rgba(248,113,113,0.1); }
          50% { box-shadow: 0 0 22px rgba(248,113,113,0.4), 0 0 8px rgba(248,113,113,0.15); }
        }
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(-8px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }
        @media (max-width: 768px) {
          .live-toggle-wrapper {
            flex-direction: row;
            gap: 6px;
          }
          .live-modal-inner {
            border-radius: 16px 16px 0 0 !important;
            position: fixed !important;
            bottom: 0 !important;
            left: 0 !important;
            right: 0 !important;
            width: 100% !important;
            max-width: 100% !important;
            max-height: 85vh !important;
            padding: 20px 16px 32px !important;
          }
        }
      `}</style>
    </>
  );
}
