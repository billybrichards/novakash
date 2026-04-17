import React, { useState } from 'react';
import { useApi } from '../hooks/useApi.js';
import { useApiLoader } from '../hooks/useApiLoader.js';
import PageHeader from '../components/shared/PageHeader.jsx';
import DataTable from '../components/shared/DataTable.jsx';
import Loading from '../components/shared/Loading.jsx';
import FilterPills from '../components/shared/FilterPills.jsx';
import { T } from '../theme/tokens.js';

// Hub stores severity as LOW / MEDIUM / HIGH (not MED). Accept both.
const SEV_COLOR = { LOW: T.label2, MED: T.warn, MEDIUM: T.warn, HIGH: T.loss };
const STATUS_FILTERS = [
  { label: 'all', value: null },
  { label: 'open', value: 'OPEN' },
  { label: 'in progress', value: 'IN_PROGRESS' },
  { label: 'closed', value: 'CLOSED' },
];
const SEV_FILTERS = [
  { label: 'any', value: null },
  { label: 'HIGH', value: 'HIGH' },
  { label: 'MED', value: 'MEDIUM' },
  { label: 'LOW', value: 'LOW' },
];

const ageOf = iso => {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  const m = Math.floor(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
};

export default function AuditTasks() {
  const api = useApi();
  const [status, setStatus] = useState('OPEN');
  const [sev, setSev] = useState(null);
  const [mutateErr, setMutateErr] = useState(null);

  const { data: rows, error: err, loading, reload } = useApiLoader(
    (signal) => {
      const params = new URLSearchParams({ limit: '200' });
      if (status) params.set('status', status);
      if (sev) params.set('severity', sev);
      return api.get(`/api/audit-tasks?${params.toString()}`, { signal });
    },
    [status, sev]
  );

  const patchStatus = async (id, next) => {
    setMutateErr(null);
    try {
      await api.patch(`/api/audit-tasks/${id}`, { status: next });
      await reload();
    } catch (e) {
      setMutateErr(`Update failed for #${id}: ${e.message || e}`);
    }
  };

  const visible = rows ?? [];

  // Normalize MEDIUM → MED for the summary chip; the raw value stays visible in the table.
  const counts = visible.reduce((acc, r) => {
    acc.total += 1;
    const bucket = r.severity === 'MEDIUM' ? 'MED' : r.severity;
    acc[bucket] = (acc[bucket] ?? 0) + 1;
    return acc;
  }, { total: 0, HIGH: 0, MED: 0, LOW: 0 });

  return (
    <div>
      <PageHeader
        tag="AUDIT · /audit"
        title="Audit Tasks"
        subtitle="Anomaly inbox — engine-emitted tasks backed by /api/audit-tasks."
        right={<div style={{ fontSize: 11, color: T.label2 }}>{counts.total} shown · {counts.HIGH} HIGH · {counts.MED} MED · {counts.LOW} LOW</div>}
      />

      <div style={{ display: 'flex', gap: 18, marginBottom: 12, flexWrap: 'wrap' }}>
        <FilterPills label="status" options={STATUS_FILTERS} value={status} onChange={setStatus} />
        <FilterPills label="severity" options={SEV_FILTERS} value={sev} onChange={setSev} />
      </div>

      {err ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>Load error: {err}</div> : null}
      {mutateErr ? <div style={{ color: T.loss, fontSize: 12, marginBottom: 10 }}>{mutateErr}</div> : null}

      <div style={{ background: T.card, border: `1px solid ${T.border}`, padding: 14, borderRadius: 2 }}>
        {loading && visible.length === 0 ? (
          <Loading />
        ) : (
          <DataTable
            emptyText="No audit tasks match these filters."
            columns={[
              { key: 'id', label: '#', num: true, render: r => <span style={{ color: T.label2 }}>{r.id}</span> },
              { key: 'task_type', label: 'type' },
              { key: 'severity', label: 'severity', render: r => (
                <span style={{ color: SEV_COLOR[r.severity] || T.label2, fontSize: 10, letterSpacing: '0.1em' }}>
                  {r.severity}
                </span>
              )},
              { key: 'title', label: 'title', render: r => <span style={{ color: T.label2 }}>{r.title}</span> },
              { key: 'category', label: 'category' },
              { key: 'priority', label: 'pri', num: true },
              { key: 'age', label: 'age', render: r => <span style={{ color: T.label2 }}>{ageOf(r.created_at)}</span> },
              { key: 'status', label: 'status', render: r => <span style={{ fontSize: 10 }}>{r.status}</span> },
              { key: '_actions', label: '', render: r => (
                <div style={{ display: 'flex', gap: 6 }}>
                  {r.status === 'OPEN' && <button type="button" onClick={() => patchStatus(r.id, 'IN_PROGRESS')} style={btnStyle}>start</button>}
                  {r.status !== 'CLOSED' && <button type="button" onClick={() => patchStatus(r.id, 'CLOSED')} style={btnStyle}>close</button>}
                </div>
              )},
            ]}
            rows={visible.map(r => ({ ...r, _key: r.id }))}
          />
        )}
      </div>
    </div>
  );
}

const btnStyle = {
  fontSize: 10,
  padding: '2px 8px',
  background: 'transparent',
  border: `1px solid ${T.borderStrong}`,
  color: T.text,
  cursor: 'pointer',
  fontFamily: T.font,
  borderRadius: 2,
};
