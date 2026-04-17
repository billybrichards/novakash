import React from 'react';
import { Link } from 'react-router-dom';
import { ARCHIVED_PAGES } from '../../nav/navigation.js';
import PageHeader from '../../components/shared/PageHeader.jsx';
import { T } from '../../theme/tokens.js';

export default function ArchiveCenter() {
  return (
    <div>
      <PageHeader
        tag="ARCHIVE · /archive"
        title="Archive Center"
        subtitle="Legacy pages from pre-redesign. Preserved in the codebase and routable; not shown in the main nav."
      />

      <div style={{
        background: T.card,
        border: `1px solid ${T.border}`,
        padding: 16,
        borderRadius: 2,
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ color: T.label, fontSize: 10, letterSpacing: '0.12em' }}>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Legacy page</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Replaced by</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Note</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Route</th>
            </tr>
          </thead>
          <tbody>
            {ARCHIVED_PAGES.map(p => (
              <tr key={p.path} style={{ borderTop: `1px solid ${T.border}` }}>
                <td style={{ padding: '8px 10px' }}>{p.label}</td>
                <td style={{ padding: '8px 10px', color: p.replacedBy ? T.profit : T.label2 }}>
                  {p.replacedBy ?? '—'}
                </td>
                <td style={{ padding: '8px 10px', color: T.label2 }}>{p.note ?? ''}</td>
                <td style={{ padding: '8px 10px' }}>
                  <Link to={p.path} style={{ color: T.cyan, fontSize: 11 }}>{p.path}</Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p style={{ color: T.label2, fontSize: 11, marginTop: 14 }}>
        These files remain in <code>src/pages/</code> — nothing deleted. To promote a page back into the main
        nav, add it to <code>NAV_SECTIONS</code> in <code>src/nav/navigation.js</code> and remove the
        matching entry from <code>ARCHIVED_PAGES</code>.
      </p>
    </div>
  );
}
