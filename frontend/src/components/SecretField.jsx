import { useState } from 'react';

/**
 * SecretField — Styled password input for sensitive API keys/secrets.
 *
 * Props:
 *   label       — field label
 *   value       — controlled value
 *   onChange    — change handler (value string)
 *   placeholder — input placeholder
 *   helpText    — optional help text below input
 *   helpLink    — { href, label } for a link in help text
 *   required    — show red dot when empty
 */
export default function SecretField({
  label,
  value = '',
  onChange,
  placeholder = 'Paste here…',
  helpText,
  helpLink,
  required = false,
}) {
  const [visible, setVisible] = useState(false);
  const filled = value && value.trim().length > 0;

  const handlePaste = async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (onChange) onChange(text.trim());
    } catch {
      // Clipboard API may be blocked; silently fail
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <label
          className="text-sm font-medium"
          style={{ color: 'rgba(255,255,255,0.7)' }}
        >
          {label}
        </label>
        {required && (
          <span
            className="w-1.5 h-1.5 rounded-full flex-shrink-0"
            style={{ background: filled ? '#4ade80' : '#f87171' }}
            title={filled ? 'Configured' : 'Required'}
          />
        )}
        {!required && filled && (
          <span
            className="w-1.5 h-1.5 rounded-full flex-shrink-0"
            style={{ background: '#4ade80' }}
            title="Configured"
          />
        )}
      </div>

      <div className="relative flex items-center">
        <input
          type={visible ? 'text' : 'password'}
          value={value}
          onChange={e => onChange && onChange(e.target.value)}
          placeholder={placeholder}
          className="w-full pr-20 pl-3 py-2 text-sm rounded-lg font-mono outline-none transition-all"
          style={{
            background: 'rgba(255,255,255,0.04)',
            border: `1px solid ${filled ? 'rgba(74,222,128,0.3)' : 'var(--border)'}`,
            color: 'rgba(255,255,255,0.85)',
          }}
          onFocus={e => {
            e.target.style.borderColor = 'var(--accent-purple)';
          }}
          onBlur={e => {
            e.target.style.borderColor = filled ? 'rgba(74,222,128,0.3)' : 'var(--border)';
          }}
        />

        {/* Buttons */}
        <div className="absolute right-2 flex items-center gap-1">
          {/* Paste */}
          <button
            type="button"
            onClick={handlePaste}
            className="px-2 py-0.5 text-xs rounded transition-colors"
            style={{
              color: 'rgba(255,255,255,0.4)',
              background: 'rgba(255,255,255,0.06)',
            }}
            title="Paste from clipboard"
          >
            Paste
          </button>

          {/* Show/hide */}
          <button
            type="button"
            onClick={() => setVisible(v => !v)}
            className="p-1 rounded transition-colors"
            style={{ color: 'rgba(255,255,255,0.3)' }}
            title={visible ? 'Hide' : 'Show'}
          >
            {visible ? (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            ) : (
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            )}
          </button>
        </div>
      </div>

      {/* Help text */}
      {(helpText || helpLink) && (
        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>
          {helpText}
          {helpLink && (
            <>
              {' '}
              <a
                href={helpLink.href}
                target="_blank"
                rel="noopener noreferrer"
                className="underline transition-colors"
                style={{ color: 'var(--accent-cyan)' }}
              >
                {helpLink.label}
              </a>
            </>
          )}
        </p>
      )}
    </div>
  );
}
