import React, { useEffect, useRef, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from '../../theme/tokens.js';

const REQUIRED = 'KILL';
const COUNTDOWN_S = 10;

/**
 * Kill-switch confirmation modal. Spec §10.
 *
 * Flow: user clicks EMERGENCY KILL in footer → modal opens → shows current
 * system status → requires typed "KILL" → 10s countdown *after* the text is
 * correct → only then the submit enables → POST /api/system/kill with
 * X-Confirm: KILL header. No retry on failure; error stays in modal.
 *
 * No keyboard shortcut. onClose is called only if the user explicitly cancels
 * or the post completes successfully.
 */
export default function KillConfirmModal({ isOpen, onClose, onKilled, systemStatus }) {
  const api = useApi();
  const [input, setInput] = useState('');
  const [remaining, setRemaining] = useState(COUNTDOWN_S);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const timerRef = useRef(null);
  const startedAtRef = useRef(null);

  // Reset when modal opens/closes
  useEffect(() => {
    if (!isOpen) {
      setInput('');
      setRemaining(COUNTDOWN_S);
      setError(null);
      setSubmitting(false);
      startedAtRef.current = null;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }
  }, [isOpen]);

  // Start countdown when input matches
  useEffect(() => {
    if (!isOpen) return;
    if (input === REQUIRED) {
      if (startedAtRef.current == null) {
        startedAtRef.current = Date.now();
        setRemaining(COUNTDOWN_S);
        if (timerRef.current) clearInterval(timerRef.current);
        timerRef.current = setInterval(() => {
          const elapsed = (Date.now() - startedAtRef.current) / 1000;
          const left = Math.max(0, COUNTDOWN_S - elapsed);
          setRemaining(left);
          if (left <= 0 && timerRef.current) {
            clearInterval(timerRef.current);
            timerRef.current = null;
          }
        }, 100);
      }
    } else {
      // Reset if they change the text
      startedAtRef.current = null;
      setRemaining(COUNTDOWN_S);
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    }
    return () => {};
  }, [input, isOpen]);

  // Cleanup on unmount
  useEffect(() => () => {
    if (timerRef.current) clearInterval(timerRef.current);
  }, []);

  const canSubmit = input === REQUIRED && remaining <= 0 && !submitting;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.post('/api/system/kill', {}, { headers: { 'X-Confirm': 'KILL' } });
      if (onKilled) onKilled();
      onClose();
    } catch (e) {
      setError(e?.response?.data?.detail ?? e?.message ?? 'kill failed');
      setSubmitting(false);
      // No auto-retry — user must re-confirm.
    }
  };

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      data-testid="kill-confirm-modal"
      style={{
        position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.75)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 1000,
      }}
    >
      <div style={{
        background: '#0b1220',
        border: '1px solid #ef4444',
        borderRadius: 4,
        padding: 24,
        width: 480, maxWidth: '90vw',
        fontFamily: T.font,
        color: T.text,
      }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: '#ef4444', letterSpacing: '0.1em', marginBottom: 14 }}>
          ⚠ EMERGENCY KILL SWITCH
        </div>

        <div style={{ fontSize: 11, color: T.label2, lineHeight: 1.5, marginBottom: 14 }}>
          This halts the engine immediately. No new trades. Open positions are NOT auto-closed.
          Current system status:
        </div>

        <pre style={{
          background: 'rgba(255,255,255,0.03)',
          border: `1px solid ${T.border}`,
          padding: '8px 10px', borderRadius: 2,
          fontSize: 10, color: T.label2, margin: 0, marginBottom: 14,
          maxHeight: 120, overflow: 'auto',
        }}>
          {systemStatus ? JSON.stringify(systemStatus, null, 2) : 'status unavailable'}
        </pre>

        <label style={{ display: 'block', fontSize: 10, color: T.label, marginBottom: 4 }}>
          Type <code style={{ color: '#ef4444' }}>KILL</code> to arm:
        </label>
        <input
          data-testid="kill-input"
          autoFocus
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          disabled={submitting}
          style={{
            width: '100%', padding: '8px 10px',
            background: '#07070c', border: `1px solid ${input === REQUIRED ? '#ef4444' : T.border}`,
            color: T.text, fontFamily: T.font, fontSize: 13,
            borderRadius: 2, marginBottom: 14,
          }}
        />

        {error ? (
          <div data-testid="kill-error" style={{
            padding: '8px 10px', marginBottom: 14,
            background: 'rgba(239,68,68,0.1)', border: '1px solid #ef4444',
            fontSize: 11, color: '#fca5a5', borderRadius: 2,
          }}>{error}</div>
        ) : null}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            data-testid="kill-cancel"
            style={{
              padding: '8px 16px',
              background: 'transparent', border: `1px solid ${T.border}`,
              color: T.label2, fontFamily: T.font, fontSize: 11,
              cursor: submitting ? 'not-allowed' : 'pointer', borderRadius: 2,
            }}
          >CANCEL</button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={!canSubmit}
            data-testid="kill-submit"
            style={{
              padding: '8px 16px',
              background: canSubmit ? '#ef4444' : 'rgba(239,68,68,0.15)',
              border: '1px solid #ef4444', color: canSubmit ? '#fff' : '#fca5a5',
              fontFamily: T.font, fontSize: 11, fontWeight: 700,
              cursor: canSubmit ? 'pointer' : 'not-allowed', borderRadius: 2,
              letterSpacing: '0.08em',
            }}
          >
            {submitting
              ? 'SENDING…'
              : input !== REQUIRED
                ? 'TYPE KILL'
                : remaining > 0
                  ? `CONFIRMING IN ${Math.ceil(remaining)}…`
                  : 'CONFIRM KILL'}
          </button>
        </div>
      </div>
    </div>
  );
}
