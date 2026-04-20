import React, { useMemo, useState } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { useApiLoader } from '../../hooks/useApiLoader.js';
import PageHeader from '../../components/shared/PageHeader.jsx';
import Loading from '../../components/shared/Loading.jsx';
import EmptyState from '../../components/shared/EmptyState.jsx';
import { T } from '../../theme/tokens.js';
import { KNOBS, buildRow, renderValue, MISSING } from './knobs.js';

const GITHUB_YAML_BASE =
  'https://github.com/billybrichards/novakash/blob/develop/engine/strategies/configs';

// Default set of columns: all LIVE + GHOST with pre_gate_hook set (i.e.
// active v4/v5/v6-family strategies). User can toggle any off.
const DEFAULT_COLUMNS = ['v4_fusion', 'v5_ensemble', 'v5_fresh', 'v6_sniper'];

function modeColor(mode) {
  if (mode === 'LIVE') return T.profit;
  if (mode === 'GHOST') return T.label2;
  return T.label;
}

function cellStyle({ isModal, isMissing }) {
  if (isMissing) {
    return { color: T.label, fontStyle: 'italic' };
  }
  if (isModal) {
    return { color: T.text };
  }
  // Differs from modal → highlight purple.
  return { color: T.purple, fontWeight: 500 };
}

export default function Compare() {
  const api = useApi();
  const { data, error, loading } = useApiLoader(
    (signal) => api.get('/api/strategies', { signal }).catch(() => ({ data: {} })),
  );

  // `data` is the raw map from /api/strategies.
  const allIds = useMemo(() => {
    if (!data || typeof data !== 'object') return [];
    return Object.keys(data).sort();
  }, [data]);

  const [selected, setSelected] = useState(null); // null → use defaults once data loads
  const [diffsOnly, setDiffsOnly] = useState(false);

  // Resolve the active column set — fall back to defaults ∩ available.
  const activeIds = useMemo(() => {
    if (selected) return selected.filter(id => allIds.includes(id));
    const defaults = DEFAULT_COLUMNS.filter(id => allIds.includes(id));
    if (defaults.length) return defaults;
    return allIds.slice(0, 4);
  }, [selected, allIds]);

  const strategies = useMemo(
    () => activeIds.map(id => ({ id, ...(data?.[id] || {}) })),
    [activeIds, data],
  );

  // Pre-compute every row; filter when "diffs only" on.
  const rows = useMemo(() => {
    return KNOBS.map(knob => {
      const cells = buildRow(knob, strategies);
      const allSame = cells.every(c => c.key === cells[0].key);
      return { knob, cells, allSame };
    }).filter(r => !diffsOnly || !r.allSame);
  }, [strategies, diffsOnly]);

  const toggleColumn = (id) => {
    const current = activeIds;
    const next = current.includes(id)
      ? current.filter(x => x !== id)
      : [...current, id];
    setSelected(next.length ? next : [id]); // never leave zero cols
  };

  if (loading && !data) return <Loading label="Loading strategy registry…" />;
  if (error) {
    return (
      <EmptyState
        message="Could not load strategies from hub."
        hint={String(error)}
      />
    );
  }
  if (!allIds.length) {
    return (
      <EmptyState
        message="No strategies returned from /api/strategies."
        hint="Verify hub is running and registry endpoint is populated."
      />
    );
  }

  return (
    <div>
      <PageHeader
        tag="COMPARE · /compare"
        title="Strategy knobs"
        subtitle="Side-by-side YAML-backed diff for every active strategy."
      />

      {/* Column selector */}
      <div style={{ marginBottom: 12 }}>
        <div style={{
          fontSize: 10, color: T.label, letterSpacing: '0.12em',
          textTransform: 'uppercase', marginBottom: 6,
        }}>
          Strategies ({activeIds.length}/{allIds.length})
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {allIds.map(id => {
            const active = activeIds.includes(id);
            const mode = data?.[id]?.yaml?.mode;
            return (
              <button
                key={id}
                type="button"
                aria-pressed={active}
                onClick={() => toggleColumn(id)}
                style={{
                  fontSize: 10,
                  padding: '3px 10px',
                  borderRadius: 10,
                  border: `1px solid ${active ? T.purple : T.borderStrong}`,
                  background: active ? 'rgba(168,85,247,0.15)' : 'transparent',
                  color: active ? '#fff' : T.label2,
                  cursor: 'pointer',
                  fontFamily: T.font,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                }}
              >
                <span>{id}</span>
                <span style={{
                  fontSize: 8,
                  color: modeColor(mode),
                  letterSpacing: '0.1em',
                }}>{mode || '?'}</span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Diffs-only toggle */}
      <div style={{ marginBottom: 14 }}>
        <label style={{
          fontSize: 11, color: T.label2, display: 'inline-flex',
          alignItems: 'center', gap: 6, cursor: 'pointer',
        }}>
          <input
            type="checkbox"
            checked={diffsOnly}
            onChange={(e) => setDiffsOnly(e.target.checked)}
          />
          Show differences only
          <span style={{ color: T.label }}>
            ({rows.length} {rows.length === 1 ? 'row' : 'rows'} shown)
          </span>
        </label>
      </div>

      {/* Main table */}
      <div style={{
        overflow: 'auto',
        border: `1px solid ${T.border}`,
        borderRadius: 2,
        maxHeight: 'calc(100vh - 260px)',
      }}>
        <table style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 11.5,
          fontFamily: T.font,
        }}>
          <thead style={{ position: 'sticky', top: 0, zIndex: 2 }}>
            <tr style={{ background: T.bg }}>
              <th style={{
                ...headerCellStyle,
                position: 'sticky', left: 0, zIndex: 3, background: T.bg,
                borderRight: `1px solid ${T.border}`,
                minWidth: 220,
              }}>
                Knob
              </th>
              {strategies.map(s => {
                const mode = s.yaml?.mode;
                return (
                  <th key={s.id} style={{ ...headerCellStyle, minWidth: 180 }}>
                    <a
                      href={`${GITHUB_YAML_BASE}/${s.id}.yaml`}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: T.text, textDecoration: 'none' }}
                      title={`View ${s.id}.yaml on GitHub`}
                    >
                      {s.id}
                    </a>
                    <div style={{
                      fontSize: 9,
                      color: modeColor(mode),
                      letterSpacing: '0.12em',
                      marginTop: 2,
                    }}>{mode || '—'}</div>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td
                  colSpan={1 + strategies.length}
                  style={{ padding: 16, color: T.label2, textAlign: 'center' }}
                >
                  No knobs differ across the selected strategies.
                </td>
              </tr>
            ) : rows.map(({ knob, cells, allSame }) => (
              <tr
                key={knob.id}
                style={{ borderTop: `1px solid ${T.border}` }}
              >
                <td style={{
                  ...bodyCellStyle,
                  position: 'sticky', left: 0, zIndex: 1, background: T.bg,
                  borderRight: `1px solid ${T.border}`,
                  color: allSame ? T.label2 : T.text,
                }}>
                  <div style={{ fontWeight: 500 }}>{knob.label}</div>
                  {knob.desc ? (
                    <div style={{
                      fontSize: 10, color: T.label, marginTop: 2,
                      fontWeight: 400,
                    }}>{knob.desc}</div>
                  ) : null}
                </td>
                {cells.map((c, i) => (
                  <td
                    key={strategies[i].id}
                    style={{ ...bodyCellStyle, ...cellStyle(c) }}
                    title={c.isMissing ? 'Key not present in YAML' : undefined}
                  >
                    {renderValue(c.value)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Legend */}
      <div style={{
        marginTop: 16,
        padding: 12,
        border: `1px solid ${T.border}`,
        borderRadius: 2,
        fontSize: 11,
        color: T.label2,
      }}>
        <div style={{
          fontSize: 10, letterSpacing: '0.12em', color: T.label,
          textTransform: 'uppercase', marginBottom: 8,
        }}>
          Legend
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 18, marginBottom: 10 }}>
          <span><span style={{ color: T.text }}>●</span> modal (most-common) value</span>
          <span><span style={{ color: T.purple }}>●</span> differs from modal</span>
          <span><span style={{ color: T.label, fontStyle: 'italic' }}>—</span> key absent in YAML</span>
          <span><span style={{ color: T.text }}>null</span> key present, value is null</span>
        </div>
        <div style={{ fontSize: 10, color: T.label }}>
          Values pulled live from <code style={{ color: T.label2 }}>GET /api/strategies</code>.
          Click a column header to open the raw YAML on GitHub. Knob rows are curated
          in <code style={{ color: T.label2 }}>frontend/src/pages/compare/knobs.js</code>.
        </div>
      </div>
    </div>
  );
}

const headerCellStyle = {
  textAlign: 'left',
  padding: '10px 12px',
  fontSize: 11,
  fontWeight: 500,
  color: T.text,
  borderBottom: `1px solid ${T.borderStrong}`,
  verticalAlign: 'top',
};

const bodyCellStyle = {
  padding: '9px 12px',
  verticalAlign: 'top',
  lineHeight: 1.4,
};
