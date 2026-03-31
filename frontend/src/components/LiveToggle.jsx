import React, { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi.js';

/**
 * LiveToggle — Two independent mode toggles in the nav header.
 *
 * 📄 PAPER  — purple glow, always safe to toggle
 * 💰 LIVE   — red glow, requires confirmation + approved config
 *
 * Both can be ON simultaneously. They are completely independent.
 */
export default function LiveToggle() {
  const api = useApi();
  const [status, setStatus] = useState({
    paper_enabled: true,
    live_enabled: false,
    can_go_live: false,
    live_has_approved_config: false,
    api_keys_configured: false,
    active_live_config: null,
  });
  const [showLiveModal, setShowLiveModal] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const fetchStatus = useCallback(async () => {
    try {
      const res = await api('GET', '/trading-config/live-status');
      setStatus(res.data);
    } catch (e) {
      // Silently ignore — don't spam errors in nav
    }
  }, [api]);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 15000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Apply red glow to the header bar when live is enabled
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

  // ── Paper toggle — always safe ───────────────────────────────────────────
  const handlePaperToggle = async () => {
    const newVal = !status.paper_enabled;
    try {
      await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'paper', enabled: newVal },
      });
      setStatus(prev => ({ ...prev, paper_enabled: newVal }));
    } catch (e) {
      console.error('Failed to toggle paper mode', e);
    }
  };

  // ── Live toggle — open modal if enabling ────────────────────────────────
  const handleLiveClick = () => {
    if (status.live_enabled) {
      // Disabling live is always allowed
      disableLive();
    } else {
      setConfirmText('');
      setError('');
      setShowLiveModal(true);
    }
  };

  const disableLive = async () => {
    try {
      await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'live', enabled: false },
      });
      setStatus(prev => ({ ...prev, live_enabled: false }));
    } catch (e) {
      console.error('Failed to disable live mode', e);
    }
  };

  const confirmEnableLive = async () => {
    if (confirmText !== 'CONFIRM') {
      setError('Type CONFIRM exactly to proceed');
      return;
    }
    setLoading(true);
    setError('');
    try {
      await api('POST', '/trading-config/toggle-mode', {
        data: { mode: 'live', enabled: true, confirmation: 'CONFIRM' },
      });
      setStatus(prev => ({ ...prev, live_enabled: true }));
      setShowLiveModal(false);
    } catch (e) {
      setError(e.response?.data?.detail || 'Failed to enable live trading');
    } finally {
      setLoading(false);
    }
  };

  // ── Check items for the live modal ──────────────────────────────────────
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

  return (
    <>
      {/* ── Two toggle switches ─────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>

        {/* Paper toggle */}
        <button
          onClick={handlePaperToggle}
          title={`Paper trading: ${status.paper_enabled ? 'ON' : 'OFF'}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px 4px 8px',
            borderRadius: 6,
            border: `1px solid ${status.paper_enabled ? 'rgba(168,85,247,0.4)' : 'rgba(255,255,255,0.08)'}`,
            background: status.paper_enabled ? 'rgba(168,85,247,0.1)' : 'rgba(255,255,255,0.03)',
            cursor: 'pointer',
            transition: 'all 200ms ease-out',
            boxShadow: status.paper_enabled ? '0 0 12px rgba(168,85,247,0.2)' : 'none',
          }}
        >
          <span style={{ fontSize: 12 }}>📄</span>
          <span style={{
            fontSize: 10,
            fontFamily: 'IBM Plex Mono, monospace',
            fontWeight: 600,
            letterSpacing: '0.08em',
            color: status.paper_enabled ? '#a855f7' : 'rgba(255,255,255,0.3)',
            transition: 'color 200ms',
          }}>
            PAPER
          </span>
          {/* Mini toggle pill */}
          <div style={{
            width: 28,
            height: 14,
            borderRadius: 7,
            background: status.paper_enabled ? 'rgba(168,85,247,0.6)' : 'rgba(255,255,255,0.1)',
            position: 'relative',
            transition: 'background 200ms',
            flexShrink: 0,
          }}>
            <span style={{
              position: 'absolute',
              top: 2,
              left: status.paper_enabled ? 15 : 2,
              width: 10,
              height: 10,
              borderRadius: '50%',
              background: '#fff',
              transition: 'left 200ms ease-out',
            }} />
          </div>
        </button>

        {/* Live toggle */}
        <button
          onClick={handleLiveClick}
          title={`Live trading: ${status.live_enabled ? 'ON' : 'OFF'}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 6,
            padding: '4px 10px 4px 8px',
            borderRadius: 6,
            border: `1px solid ${status.live_enabled ? 'rgba(248,113,113,0.5)' : 'rgba(255,255,255,0.08)'}`,
            background: status.live_enabled ? 'rgba(248,113,113,0.1)' : 'rgba(255,255,255,0.03)',
            cursor: 'pointer',
            transition: 'all 200ms ease-out',
            boxShadow: status.live_enabled ? '0 0 16px rgba(248,113,113,0.25), 0 0 4px rgba(248,113,113,0.1)' : 'none',
            animation: status.live_enabled ? 'livePulse 2s ease-in-out infinite' : 'none',
          }}
        >
          <span style={{ fontSize: 12 }}>💰</span>
          <span style={{
            fontSize: 10,
            fontFamily: 'IBM Plex Mono, monospace',
            fontWeight: 600,
            letterSpacing: '0.08em',
            color: status.live_enabled ? '#f87171' : 'rgba(255,255,255,0.3)',
            transition: 'color 200ms',
          }}>
            LIVE
          </span>
          <div style={{
            width: 28,
            height: 14,
            borderRadius: 7,
            background: status.live_enabled ? 'rgba(248,113,113,0.7)' : 'rgba(255,255,255,0.1)',
            position: 'relative',
            transition: 'background 200ms',
            flexShrink: 0,
          }}>
            <span style={{
              position: 'absolute',
              top: 2,
              left: status.live_enabled ? 15 : 2,
              width: 10,
              height: 10,
              borderRadius: '50%',
              background: '#fff',
              transition: 'left 200ms ease-out',
              boxShadow: status.live_enabled ? '0 0 4px rgba(248,113,113,0.8)' : 'none',
            }} />
          </div>
        </button>
      </div>

      {/* ── Live Enable Modal ───────────────────────────────────────────────── */}
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
          }}
          onClick={e => e.target === e.currentTarget && setShowLiveModal(false)}
        >
          <div
            style={{
              background: '#0d0d16',
              border: '1px solid rgba(248,113,113,0.3)',
              borderRadius: 12,
              padding: 28,
              width: 420,
              maxWidth: '90vw',
              boxShadow: '0 0 40px rgba(248,113,113,0.15)',
              animation: 'fadeSlideIn 200ms ease-out',
            }}
          >
            {/* Header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 20 }}>
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
                  {status.active_live_config.is_approved ? (
                    <span style={{ color: '#4ade80' }}>✓ approved</span>
                  ) : (
                    <span style={{ color: '#f87171' }}>✗ not approved</span>
                  )}
                </div>
              </div>
            ) : (
              <div style={{
                background: 'rgba(248,113,113,0.05)',
                border: '1px solid rgba(248,113,113,0.2)',
                borderRadius: 8,
                padding: '10px 14px',
                marginBottom: 16,
                color: '#f87171',
                fontSize: 12,
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
                  padding: '7px 0',
                  borderBottom: i < checks.length - 1 ? '1px solid rgba(255,255,255,0.04)' : 'none',
                }}>
                  <span style={{
                    fontSize: 13,
                    marginTop: 1,
                    color: check.ok ? '#4ade80' : 'rgba(248,113,113,0.7)',
                  }}>
                    {check.ok ? '✓' : '✗'}
                  </span>
                  <div>
                    <div style={{
                      color: check.ok ? 'rgba(255,255,255,0.7)' : 'rgba(255,255,255,0.4)',
                      fontSize: 12,
                    }}>
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
                  onChange={e => {
                    setConfirmText(e.target.value);
                    setError('');
                  }}
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
                    padding: '8px 12px',
                    outline: 'none',
                    textTransform: 'uppercase',
                    boxSizing: 'border-box',
                  }}
                  onKeyDown={e => e.key === 'Enter' && confirmEnableLive()}
                />
              </div>
            ) : (
              <div style={{
                background: 'rgba(248,113,113,0.05)',
                border: '1px solid rgba(248,113,113,0.15)',
                borderRadius: 6,
                padding: '10px 12px',
                marginBottom: 16,
                color: 'rgba(248,113,113,0.7)',
                fontSize: 12,
              }}>
                Complete all checklist items before enabling live trading.
              </div>
            )}

            {error && (
              <div style={{
                color: '#f87171',
                fontSize: 12,
                marginBottom: 12,
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
                  flex: 1,
                  padding: '9px',
                  borderRadius: 6,
                  border: '1px solid rgba(255,255,255,0.1)',
                  background: 'transparent',
                  color: 'rgba(255,255,255,0.5)',
                  fontSize: 12,
                  cursor: 'pointer',
                  fontFamily: 'IBM Plex Mono, monospace',
                }}
              >
                Cancel
              </button>
              <button
                onClick={confirmEnableLive}
                disabled={!allChecksPass || confirmText !== 'CONFIRM' || loading}
                style={{
                  flex: 2,
                  padding: '9px',
                  borderRadius: 6,
                  border: 'none',
                  background: allChecksPass && confirmText === 'CONFIRM'
                    ? 'rgba(248,113,113,0.85)'
                    : 'rgba(248,113,113,0.2)',
                  color: allChecksPass && confirmText === 'CONFIRM'
                    ? '#fff'
                    : 'rgba(248,113,113,0.4)',
                  fontSize: 12,
                  fontWeight: 700,
                  letterSpacing: '0.05em',
                  cursor: allChecksPass && confirmText === 'CONFIRM' && !loading ? 'pointer' : 'not-allowed',
                  fontFamily: 'IBM Plex Mono, monospace',
                  transition: 'all 150ms',
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
          50% { box-shadow: 0 0 20px rgba(248,113,113,0.35), 0 0 8px rgba(248,113,113,0.15); }
        }
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(-8px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }
      `}</style>
    </>
  );
}
