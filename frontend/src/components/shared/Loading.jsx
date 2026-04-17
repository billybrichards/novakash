import React from 'react';
import { T } from '../../theme/tokens.js';

export default function Loading({ label = 'Loading…' }) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-busy="true"
      style={{ color: T.label, fontSize: 12, padding: '12px 0' }}
    >
      {label}
    </div>
  );
}
