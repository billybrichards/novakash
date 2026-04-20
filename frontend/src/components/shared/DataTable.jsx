import React from 'react';
import { T } from '../../theme/tokens.js';

export default function DataTable({ columns, rows, emptyText = 'No data.', rowStyle }) {
  if (!rows || rows.length === 0) {
    return <div style={{ color: T.label, fontSize: 12, padding: '12px 0' }}>{emptyText}</div>;
  }
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11.5 }}>
      <thead>
        <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
          {columns.map(c => (
            <th key={c.key} style={{
              textAlign: c.num ? 'right' : 'left',
              padding: '7px 10px',
              textTransform: 'uppercase',
              fontWeight: 500,
            }}>{c.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={r._key ?? i} style={{ borderTop: `1px solid ${T.border}`, ...(rowStyle ? rowStyle(r) : {}) }}>
            {columns.map(c => (
              <td key={c.key} style={{
                padding: '7px 10px',
                textAlign: c.num ? 'right' : 'left',
                fontVariantNumeric: c.num ? 'tabular-nums' : undefined,
              }}>
                {c.render ? c.render(r) : r[c.key] ?? <span style={{ color: T.label }}>—</span>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
