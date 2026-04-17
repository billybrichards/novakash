import React from 'react';
import { T } from '../../theme/tokens.js';

/** Standard empty-state placeholder used across every list/table. */
export default function EmptyState({ message, hint }) {
  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        color: T.label2,
        fontSize: 12,
        padding: '16px 0',
        textAlign: 'center',
      }}
    >
      <p style={{ margin: 0 }}>{message}</p>
      {hint ? <p style={{ color: T.label, fontSize: 11, marginTop: 4, marginBottom: 0 }}>{hint}</p> : null}
    </div>
  );
}
