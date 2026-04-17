import React from 'react';
import { Link } from 'react-router-dom';
import { T } from '../../theme/tokens.js';

export default function ArchivedPageBanner({ replacedBy, note, children }) {
  return (
    <div>
      <div style={{
        background: 'rgba(245,158,11,0.1)',
        border: '1px solid rgba(245,158,11,0.35)',
        borderRadius: 2,
        padding: '10px 14px',
        margin: '12px 0',
        fontSize: 12,
        color: T.warn,
        fontFamily: T.font,
      }}>
        <strong>ARCHIVED PAGE.</strong>{' '}
        {replacedBy
          ? <>Replaced by <em style={{ color: '#fff' }}>{replacedBy}</em>. This route is preserved for reference only.</>
          : note || 'Preserved for reference only.'}
        {' · '}
        <Link to="/archive" style={{ color: T.cyan }}>Back to Archive Center</Link>
      </div>
      {children}
    </div>
  );
}
