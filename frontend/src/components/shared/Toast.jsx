// Toast.jsx + ToastProvider + useToast()
//
// Minimal self-contained toast system.
//  - Top-right corner, fixed position.
//  - z-index 9999 — always above modal overlay (z-index 200).
//  - Auto-dismiss after 5s (configurable per toast).
//  - Variants: success (green), error (red), info (blue), pending (amber).
//
// Usage:
//   const { addToast } = useToast();
//   addToast({ variant: 'success', message: 'Trade filled!' });
//   addToast({ variant: 'error', message: 'Failed', duration: 8000 });

import React, {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
} from 'react';

// ─── Context ──────────────────────────────────────────────────────────────────

const ToastContext = createContext(null);

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}

// ─── Provider ─────────────────────────────────────────────────────────────────

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const counter = useRef(0);

  const addToast = useCallback(({ variant = 'info', message, duration = 5000 }) => {
    const id = ++counter.current;
    setToasts(prev => [...prev, { id, variant, message }]);
    if (duration > 0) {
      setTimeout(() => {
        setToasts(prev => prev.filter(t => t.id !== id));
      }, duration);
    }
    return id;
  }, []);

  const removeToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ addToast, removeToast }}>
      {children}
      <ToastContainer toasts={toasts} onDismiss={removeToast} />
    </ToastContext.Provider>
  );
}

// ─── Container ────────────────────────────────────────────────────────────────

const VARIANT_STYLES = {
  success: {
    border: '1px solid #4ade80',
    color: '#4ade80',
    background: 'rgba(74,222,128,0.08)',
    icon: '✓',
  },
  error: {
    border: '1px solid #f87171',
    color: '#f87171',
    background: 'rgba(248,113,113,0.08)',
    icon: '✕',
  },
  info: {
    border: '1px solid #38bdf8',
    color: '#38bdf8',
    background: 'rgba(56,189,248,0.08)',
    icon: 'ℹ',
  },
  pending: {
    border: '1px solid #f59e0b',
    color: '#f59e0b',
    background: 'rgba(245,158,11,0.08)',
    icon: '⋯',
  },
};

function ToastContainer({ toasts, onDismiss }) {
  if (toasts.length === 0) return null;
  return (
    <div
      aria-live="polite"
      aria-atomic="false"
      style={{
        position: 'fixed',
        top: 16,
        right: 16,
        zIndex: 9999,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        maxWidth: 360,
        fontFamily: "'IBM Plex Mono', monospace",
        pointerEvents: 'none',
      }}
    >
      {toasts.map(t => (
        <ToastItem key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastItem({ toast, onDismiss }) {
  const s = VARIANT_STYLES[toast.variant] || VARIANT_STYLES.info;
  return (
    <div
      role="alert"
      style={{
        ...s,
        display: 'flex',
        alignItems: 'flex-start',
        gap: 8,
        padding: '10px 14px',
        borderRadius: 3,
        fontSize: 12,
        letterSpacing: '0.03em',
        lineHeight: 1.5,
        backdropFilter: 'blur(4px)',
        pointerEvents: 'all',
        minWidth: 220,
        maxWidth: 360,
        wordBreak: 'break-word',
      }}
    >
      <span style={{ flex: '0 0 auto', fontWeight: 700 }}>{s.icon}</span>
      <span style={{ flex: 1 }}>{toast.message}</span>
      <button
        aria-label="Dismiss"
        onClick={() => onDismiss(toast.id)}
        style={{
          flex: '0 0 auto',
          background: 'none',
          border: 'none',
          color: s.color,
          cursor: 'pointer',
          fontSize: 14,
          lineHeight: 1,
          opacity: 0.6,
          padding: 0,
        }}
      >
        ×
      </button>
    </div>
  );
}
