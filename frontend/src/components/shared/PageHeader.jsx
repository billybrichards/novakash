import React from 'react';
import { T } from '../../theme/tokens.js';

const PAGETAG_STYLE = {
  display: 'inline-block',
  fontSize: 10,
  letterSpacing: '0.15em',
  color: T.purple,
  border: `1px solid ${T.purple}`,
  padding: '2px 8px',
  borderRadius: 2,
  marginBottom: 6,
};

export default function PageHeader({ tag, title, subtitle, right }) {
  return (
    <div style={{
      display: 'flex',
      justifyContent: 'space-between',
      alignItems: 'flex-end',
      padding: '20px 0 16px',
      borderBottom: `1px solid ${T.border}`,
      marginBottom: 16,
    }}>
      <div>
        {tag ? <div style={PAGETAG_STYLE}>{tag}</div> : null}
        <h1 style={{ margin: 0, fontSize: 20, fontWeight: 500, letterSpacing: '0.02em' }}>{title}</h1>
        {subtitle ? (
          <div style={{ color: T.label, fontSize: 12, marginTop: 4 }}>
            {subtitle}
          </div>
        ) : null}
      </div>
      <div>{right}</div>
    </div>
  );
}
