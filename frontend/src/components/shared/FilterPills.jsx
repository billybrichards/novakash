import React from 'react';
import { T } from '../../theme/tokens.js';

export default function FilterPills({ options, value, onChange, label }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      {label ? <span style={{ fontSize: 10, color: T.label, letterSpacing: '0.12em', textTransform: 'uppercase', marginRight: 4 }}>{label}</span> : null}
      {options.map(opt => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value ?? 'all'}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(opt.value)}
            style={{
              fontSize: 10,
              padding: '2px 10px',
              borderRadius: 10,
              border: `1px solid ${active ? T.purple : T.borderStrong}`,
              background: active ? 'rgba(168,85,247,0.15)' : 'transparent',
              color: active ? '#fff' : T.label2,
              cursor: 'pointer',
              fontFamily: T.font,
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
