import { useState } from 'react';

/**
 * SetupSection — Reusable collapsible section for the Setup wizard.
 *
 * Props:
 *   title       — section title
 *   icon        — emoji or string icon
 *   status      — 'ready' | 'incomplete' | 'missing'
 *   children    — section body content
 *   defaultOpen — whether to start expanded
 */
export default function SetupSection({ title, icon, status = 'missing', children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);

  const badge = {
    ready:      { label: '✅ Ready',        bg: 'rgba(74,222,128,0.1)',  color: '#4ade80', border: 'rgba(74,222,128,0.25)' },
    incomplete: { label: '⚠️ Incomplete',  bg: 'rgba(251,191,36,0.1)', color: '#fbbf24', border: 'rgba(251,191,36,0.25)' },
    missing:    { label: '❌ Not configured', bg: 'rgba(248,113,113,0.1)', color: '#f87171', border: 'rgba(248,113,113,0.25)' },
  }[status] || badge?.missing;

  return (
    <div
      style={{
        background: 'var(--card)',
        border: '1px solid var(--border)',
        borderRadius: '12px',
        overflow: 'hidden',
        transition: 'border-color 200ms ease',
      }}
    >
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full text-left flex items-center gap-3 px-6 py-4"
        style={{
          background: open ? 'rgba(168,85,247,0.04)' : 'transparent',
          borderBottom: open ? '1px solid var(--border)' : '1px solid transparent',
          transition: 'background 200ms ease, border-color 200ms ease',
        }}
      >
        <span className="text-xl flex-shrink-0">{icon}</span>

        <span
          className="flex-1 text-base font-semibold"
          style={{ color: 'var(--text-primary)', letterSpacing: '-0.01em' }}
        >
          {title}
        </span>

        {/* Status badge */}
        <span
          className="text-xs font-medium px-2.5 py-1 rounded-full border flex-shrink-0"
          style={{
            background: badge.bg,
            color: badge.color,
            borderColor: badge.border,
          }}
        >
          {badge.label}
        </span>

        {/* Chevron */}
        <svg
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="none"
          className="flex-shrink-0 ml-1"
          style={{
            color: 'rgba(255,255,255,0.3)',
            transform: open ? 'rotate(180deg)' : 'rotate(0deg)',
            transition: 'transform 200ms ease-out',
          }}
        >
          <path
            d="M4 6l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {/* Collapsible body */}
      <div
        style={{
          maxHeight: open ? '2000px' : '0px',
          overflow: 'hidden',
          transition: 'max-height 300ms ease-out',
        }}
      >
        <div className="px-6 py-5">
          {children}
        </div>
      </div>
    </div>
  );
}
