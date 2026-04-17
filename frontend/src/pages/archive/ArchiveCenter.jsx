import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ARCHIVED_PAGES, ARCHIVED_STRATEGY_FLOORS } from '../../nav/navigation.js';
import PageHeader from '../../components/shared/PageHeader.jsx';
import FilterPills from '../../components/shared/FilterPills.jsx';
import { T } from '../../theme/tokens.js';

// Category is declared on each entry in navigation.js (trading | polymarket | data | ops).
const CATEGORY_COLORS = {
  trading:   '#a855f7',
  polymarket: '#06b6d4',
  data:      '#4ade80',
  ops:       '#f59e0b',
};

export default function ArchiveCenter() {
  const [filter, setFilter] = useState(null);
  const [query, setQuery] = useState('');

  // Merge ARCHIVED_PAGES + ARCHIVED_STRATEGY_FLOORS into one unified list so
  // operators see every archive route regardless of whether it's
  // generic-component or props-bearing (StrategyFloor).
  const allRows = useMemo(() => {
    const a = ARCHIVED_PAGES.map(p => ({ ...p, kind: 'page' }));
    const b = ARCHIVED_STRATEGY_FLOORS.map(p => ({
      ...p,
      kind: 'floor',
      note: `StrategyFloor with strategyId="${p.strategyId}"`,
    }));
    return [...a, ...b].sort((x, y) => x.path.localeCompare(y.path));
  }, []);

  const categories = useMemo(() => {
    const counts = {};
    for (const r of allRows) counts[r.category] = (counts[r.category] ?? 0) + 1;
    return [
      { label: `all (${allRows.length})`, value: null },
      ...Object.entries(counts)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([c, n]) => ({ label: `${c} (${n})`, value: c })),
    ];
  }, [allRows]);

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return allRows.filter(r => {
      if (filter && r.category !== filter) return false;
      if (q) {
        const hay = `${r.label} ${r.path} ${r.replacedBy ?? ''} ${r.note ?? ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [allRows, filter, query]);

  return (
    <div>
      <PageHeader
        tag="ARCHIVE · /archive"
        title="Archive Center"
        subtitle={`${allRows.length} legacy + develop-added pages preserved. Routable but kept off the main nav. Click a route to view the archived page (wrapped in a yellow banner with replacement pointer).`}
        right={<div style={{ fontSize: 11, color: T.label2 }}>
          {rows.length} shown
          {filter ? <span style={{ color: T.label, marginLeft: 6 }}>· filter: {filter}</span> : null}
        </div>}
      />

      <div style={{ display: 'flex', gap: 14, marginBottom: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <FilterPills label="category" options={categories} value={filter} onChange={setFilter} />
        <div style={{ marginLeft: 'auto' }}>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search label / path / replaced-by…"
            style={{
              background: T.card,
              border: `1px solid ${T.border}`,
              color: T.text,
              padding: '5px 10px',
              fontSize: 11,
              fontFamily: T.font,
              borderRadius: 2,
              width: 280,
            }}
          />
        </div>
      </div>

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
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Category</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Replaced by</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Note</th>
              <th style={{ textAlign: 'left', padding: '8px 10px', textTransform: 'uppercase' }}>Route</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={5} style={{ padding: 24, color: T.label, textAlign: 'center', fontSize: 11 }}>
                  No archived pages match.
                </td>
              </tr>
            ) : rows.map(p => (
              <tr key={p.path} style={{ borderTop: `1px solid ${T.border}` }}>
                <td style={{ padding: '8px 10px' }}>
                  {p.label}
                  {p.kind === 'floor' ? (
                    <span style={{ marginLeft: 6, fontSize: 9, color: T.warn }}>(props)</span>
                  ) : null}
                </td>
                <td style={{ padding: '8px 10px' }}>
                  <span style={{
                    fontSize: 10,
                    color: CATEGORY_COLORS[p.category] || T.label2,
                    textTransform: 'uppercase',
                    letterSpacing: '0.1em',
                  }}>{p.category}</span>
                </td>
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
        These files remain in <code>src/pages/</code> — nothing deleted. To promote a page back into the main nav,
        add it to <code>NAV_SECTIONS</code> in <code>src/nav/navigation.js</code> and remove the matching entry
        from <code>ARCHIVED_PAGES</code>. All routes lazy-load (chunked via <code>React.lazy()</code>) so archive
        pages cost nothing until visited.
      </p>
    </div>
  );
}
