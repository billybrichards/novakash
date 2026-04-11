/**
 * Schema.jsx — DB table inventory (SCHEMA-01).
 *
 * Renders the curated SCHEMA_CATALOG joined with live runtime stats
 * (row counts, last write times) from the /api/v58/schema endpoints.
 *
 * Operators use this page to answer:
 *   - How many tables does the system have and what's their status?
 *   - Which service writes which table?
 *   - Is this table active, legacy, or deprecated?
 *   - When was the last write? How many rows? Which column schema?
 *   - Where's the design doc?
 *
 * Data flow:
 *   GET /api/v58/schema/summary          → header counts
 *   GET /api/v58/schema/tables           → card grid with runtime stats
 *   GET /api/v58/schema/tables/{name}    → expanded detail with columns
 *
 * Backend: hub/api/schema.py + hub/db/schema_catalog.py
 * Route:   /schema (registered in App.jsx, nav entry at bottom of SYSTEM)
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme ────────────────────────────────────────────────────────────────
const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.8)',
  cardBorder: 'rgba(51, 65, 85, 1)',
  headerBg: 'rgba(30, 41, 59, 1)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  blue: '#3b82f6',
  orange: '#f97316',
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

// ─── Service colours ───────────────────────────────────────────────────────
// Keep in sync with Layout.jsx sidebar colour conventions where possible.
const SERVICE_COLORS = {
  engine: T.purple,               // POLYMARKET
  'data-collector': T.blue,       // raw-data collector
  margin_engine: T.amber,         // BINANCE MARGIN
  'macro-observer': T.cyan,       // ANALYSIS / macro
  hub: T.textMuted,               // SYSTEM (hub)
  'timesfm-service': T.green,     // external model service
  unknown: T.textDim,
};

// ─── Category labels ──────────────────────────────────────────────────────
const CATEGORY_LABELS = {
  polymarket: 'POLYMARKET',
  margin: 'MARGIN',
  macro: 'MACRO',
  data: 'DATA FEEDS',
  hub: 'HUB',
  exec: 'EXECUTION',
  external: 'EXTERNAL',
  uncategorised: 'UNCATEGORISED',
};

const STATUS_META = {
  active: { color: T.green, bg: 'rgba(16,185,129,0.12)', border: 'rgba(16,185,129,0.3)', label: 'ACTIVE' },
  legacy: { color: T.amber, bg: 'rgba(245,158,11,0.12)', border: 'rgba(245,158,11,0.3)', label: 'LEGACY' },
  deprecated: { color: T.red, bg: 'rgba(239,68,68,0.12)', border: 'rgba(239,68,68,0.3)', label: 'DEPRECATED' },
};

// ─── SOT class styling ────────────────────────────────────────────────────
// Source-of-Truth classification from the data architecture audit.
const SOT_CLASS_META = {
  SOT:         { color: '#22d3ee', bg: 'rgba(34,211,238,0.12)', border: 'rgba(34,211,238,0.35)', label: 'SOT' },
  DERIVED:     { color: '#a78bfa', bg: 'rgba(167,139,250,0.12)', border: 'rgba(167,139,250,0.35)', label: 'DERIVED' },
  CACHE:       { color: '#60a5fa', bg: 'rgba(96,165,250,0.12)', border: 'rgba(96,165,250,0.35)', label: 'CACHE' },
  LEGACY:      { color: '#fbbf24', bg: 'rgba(251,191,36,0.12)', border: 'rgba(251,191,36,0.35)', label: 'LEGACY' },
  OPERATIONAL: { color: '#94a3b8', bg: 'rgba(148,163,184,0.10)', border: 'rgba(148,163,184,0.30)', label: 'OPS' },
};

// ─── Formatters ───────────────────────────────────────────────────────────

function fmtWhen(iso) {
  if (!iso) return null;
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    const now = new Date();
    const diffMs = now - d;
    const diffSec = Math.round(diffMs / 1000);
    if (diffSec < 10) return 'just now';
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.round(diffHr / 24);
    if (diffDay < 30) return `${diffDay}d ago`;
    return d.toISOString().slice(0, 10);
  } catch {
    return null;
  }
}

function fmtCount(n) {
  if (n === null || n === undefined) return null;
  if (typeof n !== 'number') return String(n);
  if (n === 0) return '0';
  if (n < 1_000) return n.toLocaleString();
  if (n < 1_000_000) return `${(n / 1_000).toFixed(n < 10_000 ? 1 : 0)}k`;
  if (n < 1_000_000_000) return `${(n / 1_000_000).toFixed(n < 10_000_000 ? 1 : 0)}M`;
  return `${(n / 1_000_000_000).toFixed(1)}B`;
}

function abbrPath(path) {
  if (!path) return '';
  // Take the last two segments for compact display, keep parentheses intact
  const parenIdx = path.indexOf(' (');
  const core = parenIdx >= 0 ? path.slice(0, parenIdx) : path;
  const parens = parenIdx >= 0 ? path.slice(parenIdx) : '';
  const parts = core.split('/');
  const last = parts.length > 2 ? parts.slice(-2).join('/') : core;
  return last + parens;
}

// ─── Small components ────────────────────────────────────────────────────

function Chip({ color, bg, border, children, title, size = 'sm' }) {
  const padY = size === 'md' ? 4 : 2;
  const padX = size === 'md' ? 9 : 7;
  const fs = size === 'md' ? 10 : 9;
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: fs,
        fontWeight: 700,
        padding: `${padY}px ${padX}px`,
        borderRadius: 3,
        background: bg,
        color,
        border: `1px solid ${border}`,
        fontFamily: T.mono,
        letterSpacing: '0.04em',
        whiteSpace: 'nowrap',
      }}
    >
      {children}
    </span>
  );
}

function StatusChip({ status }) {
  const meta = STATUS_META[status] || STATUS_META.active;
  return (
    <Chip color={meta.color} bg={meta.bg} border={meta.border} title={`status: ${status}`}>
      {meta.label}
    </Chip>
  );
}

function ServiceChip({ service }) {
  const color = SERVICE_COLORS[service] || SERVICE_COLORS.unknown;
  return (
    <Chip
      color={color}
      bg={`${color}14`}
      border={`${color}55`}
      title={`service: ${service}`}
    >
      {service || 'unknown'}
    </Chip>
  );
}

function CategoryChip({ category }) {
  const label = CATEGORY_LABELS[category] || category?.toUpperCase() || '—';
  return (
    <Chip
      color={T.textMuted}
      bg="rgba(100,116,139,0.08)"
      border="rgba(100,116,139,0.25)"
      title={`category: ${category}`}
    >
      {label}
    </Chip>
  );
}

function SotClassChip({ sotClass }) {
  if (!sotClass) return null;
  const meta = SOT_CLASS_META[sotClass] || SOT_CLASS_META.OPERATIONAL;
  return (
    <Chip color={meta.color} bg={meta.bg} border={meta.border} title={`data class: ${sotClass}`}>
      {meta.label}
    </Chip>
  );
}

function KV({ label, value, mono = true, color = T.text }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, padding: '3px 0' }}>
      <span style={{ color: T.textMuted, fontSize: 10, fontFamily: T.mono }}>{label}</span>
      <span
        style={{
          color,
          fontSize: mono ? 11 : 12,
          fontFamily: mono ? T.mono : undefined,
          textAlign: 'right',
          maxWidth: '70%',
          wordBreak: 'break-word',
        }}
      >
        {value == null || value === '' ? '—' : value}
      </span>
    </div>
  );
}

function FilterButton({ active, onClick, children, title }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      style={{
        fontFamily: T.mono,
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: '0.05em',
        textTransform: 'uppercase',
        padding: '6px 12px',
        borderRadius: 4,
        background: active ? 'rgba(168,85,247,0.18)' : 'rgba(255,255,255,0.04)',
        border: `1px solid ${active ? 'rgba(168,85,247,0.55)' : 'rgba(255,255,255,0.1)'}`,
        color: active ? T.purple : T.textMuted,
        cursor: 'pointer',
        transition: 'all 120ms ease-out',
      }}
    >
      {children}
    </button>
  );
}

function TextInput({ value, onChange, placeholder }) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        background: 'rgba(15,23,42,0.6)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 4,
        padding: '7px 10px',
        color: T.text,
        fontFamily: T.mono,
        fontSize: 11,
        outline: 'none',
        width: 220,
      }}
    />
  );
}

// ─── Header summary bar ──────────────────────────────────────────────────

function HeaderSummary({ summary, generatedAt, onRefresh, refreshing }) {
  const when = fmtWhen(generatedAt);
  const total = summary?.total_tables ?? '—';
  const active = summary?.active ?? '—';
  const legacy = summary?.legacy ?? '—';
  const deprecated = summary?.deprecated ?? '—';

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: 16,
        marginBottom: 14,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <div style={{ fontSize: 10, color: T.purple, fontFamily: T.mono, letterSpacing: '0.1em' }}>
            DATABASE SCHEMA INVENTORY (SCHEMA-01)
          </div>
          <div style={{ fontSize: 14, color: T.white, fontWeight: 600, marginTop: 4 }}>
            Every tracked DB table — purpose, writers, readers, active/legacy status
          </div>
          <div style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono, marginTop: 6, maxWidth: 760, lineHeight: 1.5 }}>
            Source: <span style={{ color: T.text }}>hub/db/schema_catalog.py</span> joined with live runtime stats from the hub DB (row counts, last write timestamps).
            Tables that are in the catalog but missing from the current DB render as "external / planned".
          </div>
        </div>
        <div style={{ display: 'flex', gap: 18, alignItems: 'center', flexWrap: 'wrap' }}>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>TOTAL</div>
            <div style={{ fontSize: 22, color: T.white, fontFamily: T.mono, fontWeight: 700 }}>{total}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 9, color: T.green, fontFamily: T.mono }}>ACTIVE</div>
            <div style={{ fontSize: 22, color: T.green, fontFamily: T.mono, fontWeight: 700 }}>{active}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 9, color: T.amber, fontFamily: T.mono }}>LEGACY</div>
            <div style={{ fontSize: 22, color: T.amber, fontFamily: T.mono, fontWeight: 700 }}>{legacy}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 9, color: T.red, fontFamily: T.mono }}>DEPRECATED</div>
            <div style={{ fontSize: 22, color: T.red, fontFamily: T.mono, fontWeight: 700 }}>{deprecated}</div>
          </div>
          <div style={{ marginLeft: 10, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
            <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
              {when ? `synced ${when}` : '—'}
            </div>
            <button
              type="button"
              onClick={onRefresh}
              disabled={refreshing}
              style={{
                fontFamily: T.mono,
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: '0.05em',
                textTransform: 'uppercase',
                padding: '5px 10px',
                borderRadius: 4,
                background: refreshing ? 'rgba(255,255,255,0.03)' : 'rgba(168,85,247,0.14)',
                border: `1px solid ${refreshing ? 'rgba(255,255,255,0.06)' : 'rgba(168,85,247,0.35)'}`,
                color: refreshing ? T.textDim : T.purple,
                cursor: refreshing ? 'wait' : 'pointer',
              }}
            >
              {refreshing ? 'refreshing…' : 'refresh'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Table card ──────────────────────────────────────────────────────────

function TableCard({ entry, expanded, onToggle, detail, detailLoading, detailError }) {
  const rowLabel = (() => {
    if (entry.row_count === null || entry.row_count === undefined) {
      if (entry.exists === false) return 'not in DB';
      return '—';
    }
    const base = fmtCount(entry.row_count);
    return entry.row_count_is_estimate ? `~${base}` : base;
  })();

  const lastWriteLabel = fmtWhen(entry.last_write) || '—';

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${expanded ? 'rgba(168,85,247,0.5)' : T.cardBorder}`,
        borderRadius: 6,
        marginBottom: 10,
        transition: 'border-color 140ms ease-out',
      }}
    >
      {/* ─── Header row ─── */}
      <button
        type="button"
        onClick={onToggle}
        style={{
          display: 'block',
          width: '100%',
          textAlign: 'left',
          background: 'transparent',
          border: 'none',
          padding: 14,
          cursor: 'pointer',
          color: 'inherit',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 14, flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 260px', minWidth: 260 }}>
            <div
              style={{
                fontFamily: T.mono,
                fontSize: 14,
                fontWeight: 700,
                color: entry.exists === false ? T.textMuted : T.white,
                marginBottom: 6,
              }}
            >
              {entry.name}
            </div>
            <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
              <StatusChip status={entry.status} />
              <SotClassChip sotClass={entry.sot_class} />
              <ServiceChip service={entry.service} />
              <CategoryChip category={entry.category} />
              {entry.exists === false && (
                <Chip color={T.textDim} bg="rgba(100,116,139,0.08)" border="rgba(100,116,139,0.25)" title="not present in the hub DB">
                  NOT IN DB
                </Chip>
              )}
              {entry.large && (
                <Chip color={T.textDim} bg="rgba(100,116,139,0.06)" border="rgba(100,116,139,0.2)" title="large table — row count is pg_class estimate">
                  LARGE
                </Chip>
              )}
            </div>
            <div style={{ fontSize: 11, color: T.textMuted, lineHeight: 1.5, marginBottom: 6 }}>
              {entry.purpose}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 14, alignItems: 'center', flexWrap: 'wrap' }}>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>ROWS</div>
              <div style={{ fontSize: 14, color: T.text, fontFamily: T.mono, fontWeight: 700 }}>{rowLabel}</div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>LAST WRITE</div>
              <div style={{ fontSize: 11, color: T.text, fontFamily: T.mono }}>{lastWriteLabel}</div>
            </div>
            <div
              style={{
                fontSize: 16,
                color: T.textMuted,
                transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)',
                transition: 'transform 140ms ease-out',
              }}
            >
              ▸
            </div>
          </div>
        </div>
      </button>

      {/* ─── Expanded detail ─── */}
      {expanded && (
        <div style={{ borderTop: `1px solid ${T.cardBorder}`, padding: 14 }}>
          {detailLoading && (
            <div style={{ color: T.textMuted, fontFamily: T.mono, fontSize: 11 }}>loading detail…</div>
          )}
          {detailError && (
            <div
              style={{
                padding: '8px 10px',
                background: 'rgba(239,68,68,0.08)',
                border: '1px solid rgba(239,68,68,0.3)',
                borderRadius: 4,
                color: T.red,
                fontFamily: T.mono,
                fontSize: 10,
              }}
            >
              {String(detailError)}
            </div>
          )}
          {!detailLoading && !detailError && detail && (
            <TableDetail entry={entry} detail={detail} />
          )}
        </div>
      )}
    </div>
  );
}

function TableDetail({ entry, detail }) {
  return (
    <div style={{ display: 'grid', gap: 18, gridTemplateColumns: '1fr', padding: '4px 0' }}>
      {/* ─── Writers + readers ─── */}
      <div style={{ display: 'grid', gap: 14, gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
        <div>
          <div style={{ fontSize: 10, color: T.amber, fontFamily: T.mono, letterSpacing: '0.08em', marginBottom: 6 }}>
            WRITERS
          </div>
          {(entry.writers && entry.writers.length > 0) ? (
            <ul style={{ margin: 0, paddingLeft: 14 }}>
              {entry.writers.map((w, i) => (
                <li
                  key={i}
                  style={{
                    fontSize: 10,
                    color: T.text,
                    fontFamily: T.mono,
                    marginBottom: 4,
                    lineHeight: 1.5,
                    wordBreak: 'break-word',
                  }}
                >
                  {w}
                </li>
              ))}
            </ul>
          ) : (
            <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono }}>— none —</div>
          )}
        </div>
        <div>
          <div style={{ fontSize: 10, color: T.cyan, fontFamily: T.mono, letterSpacing: '0.08em', marginBottom: 6 }}>
            READERS
          </div>
          {(entry.readers && entry.readers.length > 0) ? (
            <ul style={{ margin: 0, paddingLeft: 14 }}>
              {entry.readers.map((r, i) => (
                <li
                  key={i}
                  style={{
                    fontSize: 10,
                    color: T.text,
                    fontFamily: T.mono,
                    marginBottom: 4,
                    lineHeight: 1.5,
                    wordBreak: 'break-word',
                  }}
                >
                  {r}
                </li>
              ))}
            </ul>
          ) : (
            <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono }}>— none —</div>
          )}
        </div>
      </div>

      {/* ─── Columns ─── */}
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
          <div style={{ fontSize: 10, color: T.blue, fontFamily: T.mono, letterSpacing: '0.08em' }}>
            COLUMNS
          </div>
          <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
            {detail.column_count != null ? `${detail.column_count} columns` : '—'}
            {detail.row_count != null && (
              <>
                {' · '}
                {detail.row_count_is_estimate ? '~' : ''}
                {fmtCount(detail.row_count)} rows
                {detail.row_count_is_estimate ? ' (estimated)' : ''}
              </>
            )}
          </div>
        </div>
        {detail.exists === false && (
          <div
            style={{
              padding: '8px 10px',
              background: 'rgba(245,158,11,0.08)',
              border: '1px solid rgba(245,158,11,0.25)',
              borderRadius: 4,
              color: T.amber,
              fontFamily: T.mono,
              fontSize: 10,
              marginBottom: 8,
            }}
          >
            Table is not present in the hub's DB. Likely a planned table or one that lives in a different service's DB. See the catalog notes below.
          </div>
        )}
        {detail.columns && detail.columns.length > 0 ? (
          <div
            style={{
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 4,
              overflow: 'auto',
              maxHeight: 320,
            }}
          >
            <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: T.mono, fontSize: 10 }}>
              <thead>
                <tr style={{ background: T.headerBg, color: T.textMuted }}>
                  <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>#</th>
                  <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>name</th>
                  <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>type</th>
                  <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>null?</th>
                  <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>default</th>
                </tr>
              </thead>
              <tbody>
                {detail.columns.map((c, i) => (
                  <tr key={`${c.name}-${i}`} style={{ borderBottom: `1px solid ${T.cardBorder}` }}>
                    <td style={{ padding: '5px 10px', color: T.textDim }}>{i + 1}</td>
                    <td style={{ padding: '5px 10px', color: T.white }}>{c.name}</td>
                    <td style={{ padding: '5px 10px', color: T.cyan }}>
                      {c.type}
                      {c.max_length ? `(${c.max_length})` : ''}
                      {(!c.max_length && c.numeric_precision != null)
                        ? `(${c.numeric_precision}${c.numeric_scale != null ? `,${c.numeric_scale}` : ''})`
                        : ''}
                    </td>
                    <td style={{ padding: '5px 10px', color: c.nullable ? T.textMuted : T.amber }}>
                      {c.nullable ? 'null' : 'NOT NULL'}
                    </td>
                    <td style={{ padding: '5px 10px', color: T.textDim, maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.default || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono }}>
            — no columns loaded —
          </div>
        )}
      </div>

      {/* ─── Metadata KV ─── */}
      <div
        style={{
          display: 'grid',
          gap: 14,
          gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
        }}
      >
        <div>
          <div style={{ fontSize: 10, color: T.purple, fontFamily: T.mono, letterSpacing: '0.08em', marginBottom: 6 }}>
            METADATA
          </div>
          <KV label="table" value={entry.name} />
          <KV label="service" value={entry.service} />
          <KV label="category" value={entry.category} />
          <KV label="status" value={entry.status} />
          <KV label="data class" value={entry.sot_class || '(unclassified)'} />
          <KV label="data flow" value={entry.data_flow || '(not mapped)'} mono={false} />
          <KV
            label="recency column"
            value={entry.recency_column || '(none)'}
          />
          <KV label="last write" value={fmtWhen(detail.last_write) || detail.last_write || '—'} />
          <KV
            label="row count"
            value={
              detail.row_count != null
                ? `${detail.row_count_is_estimate ? '~' : ''}${fmtCount(detail.row_count)}${detail.row_count_is_estimate ? ' (est)' : ''}`
                : '—'
            }
          />
        </div>
        <div>
          <div style={{ fontSize: 10, color: T.green, fontFamily: T.mono, letterSpacing: '0.08em', marginBottom: 6 }}>
            DOCS
          </div>
          {(entry.docs && entry.docs.length > 0) ? (
            <ul style={{ margin: 0, paddingLeft: 14 }}>
              {entry.docs.map((d, i) => (
                <li
                  key={i}
                  style={{
                    fontSize: 10,
                    color: T.cyan,
                    fontFamily: T.mono,
                    marginBottom: 4,
                    wordBreak: 'break-word',
                  }}
                >
                  {d}
                </li>
              ))}
            </ul>
          ) : (
            <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono }}>— no docs linked —</div>
          )}
          {entry.notes && (
            <>
              <div style={{ fontSize: 10, color: T.amber, fontFamily: T.mono, letterSpacing: '0.08em', marginTop: 12, marginBottom: 6 }}>
                NOTES
              </div>
              <div style={{ fontSize: 10, color: T.textMuted, lineHeight: 1.6 }}>
                {entry.notes}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Dependency map (simple static view) ─────────────────────────────────

function DependencyMap({ tables }) {
  // Group by service → show which tables each service writes / reads.
  const byService = useMemo(() => {
    const map = new Map();
    for (const t of tables) {
      if (!map.has(t.service)) {
        map.set(t.service, { writes: [], reads: new Set() });
      }
      map.get(t.service).writes.push(t.name);
      for (const r of t.readers || []) {
        // Extract the service directory prefix from the reader path
        // e.g. "hub/api/v58_monitor.py (foo)" → "hub"
        const m = r.match(/^([a-z][a-z_-]*)\//);
        if (m) {
          if (!map.has(m[1])) map.set(m[1], { writes: [], reads: new Set() });
          map.get(m[1]).reads.add(t.name);
        }
      }
    }
    return map;
  }, [tables]);

  const rows = Array.from(byService.entries())
    .map(([svc, { writes, reads }]) => ({
      service: svc,
      writes,
      reads: Array.from(reads).filter((t) => !writes.includes(t)),
    }))
    .sort((a, b) => a.service.localeCompare(b.service));

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: 14,
        marginBottom: 14,
      }}
    >
      <div style={{ fontSize: 10, color: T.purple, fontFamily: T.mono, letterSpacing: '0.08em', marginBottom: 10 }}>
        WRITE / READ DEPENDENCY MAP
      </div>
      <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono, marginBottom: 10, lineHeight: 1.5 }}>
        Derived from the catalog's writers + readers lists. A service "writes" the tables it owns, and "reads" any tables whose reader list includes a file path from its directory.
      </div>
      <div
        style={{
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 4,
          overflow: 'auto',
          maxHeight: 400,
        }}
      >
        <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily: T.mono, fontSize: 10 }}>
          <thead>
            <tr style={{ background: T.headerBg, color: T.textMuted }}>
              <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>service</th>
              <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>writes</th>
              <th style={{ textAlign: 'left', padding: '6px 10px', borderBottom: `1px solid ${T.cardBorder}`, fontWeight: 700 }}>reads (other services' tables)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.service} style={{ borderBottom: `1px solid ${T.cardBorder}`, verticalAlign: 'top' }}>
                <td style={{ padding: '8px 10px' }}>
                  <ServiceChip service={r.service} />
                </td>
                <td style={{ padding: '8px 10px', color: T.amber }}>
                  {r.writes.length > 0 ? r.writes.join(', ') : '—'}
                </td>
                <td style={{ padding: '8px 10px', color: T.cyan }}>
                  {r.reads.length > 0 ? r.reads.join(', ') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── GatesView (NAV-01, 2026-04-11) ───────────────────────────────────────
//
// Renders the hand-curated GATES_CATALOG from /api/v58/schema/gates.
// The user asked for a single place answering "which gates consume
// which tables" — this tab is the answer. One accordion row per gate,
// click to expand and see inputs, outputs, env flags, fail reasons,
// tables_read / tables_written, and the cross-reference of
// "other gates that share a table with this one".

function GatesView({ payload, loading, error, expandedGate, setExpandedGate }) {
  if (loading) {
    return (
      <div
        style={{
          padding: 30,
          textAlign: 'center',
          color: T.textMuted,
          fontSize: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
        }}
      >
        Loading gates catalog…
      </div>
    );
  }
  if (error) {
    return (
      <div
        style={{
          padding: 14,
          background: 'rgba(239,68,68,0.08)',
          border: `1px solid ${T.red}`,
          borderRadius: 6,
          color: T.red,
          fontSize: 12,
        }}
      >
        {error}
      </div>
    );
  }
  if (!payload || !payload.items || payload.items.length === 0) {
    return (
      <div
        style={{
          padding: 14,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          color: T.textMuted,
          fontSize: 12,
        }}
      >
        No gates in the catalog. Check <span style={{ color: T.text }}>hub/db/schema_catalog.py</span>.
      </div>
    );
  }

  const engineColor = (eng) => {
    if (eng === 'polymarket') return T.purple;
    if (eng === 'margin_engine') return T.amber;
    return T.textMuted;
  };
  const statusMeta = STATUS_META;

  const byEngine = payload.by_engine || {};
  const engines = payload.engines || Object.keys(byEngine);

  return (
    <div>
      {/* Summary strip */}
      <div
        style={{
          display: 'flex',
          gap: 12,
          marginBottom: 14,
          flexWrap: 'wrap',
        }}
      >
        {engines.map((eng) => (
          <div
            key={eng}
            style={{
              background: T.card,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              padding: '10px 14px',
              display: 'flex',
              gap: 10,
              alignItems: 'center',
              minWidth: 160,
            }}
          >
            <div
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: engineColor(eng),
              }}
            />
            <div>
              <div style={{ fontSize: 10, color: T.textDim, fontFamily: T.mono, letterSpacing: '0.1em' }}>
                {eng.toUpperCase()}
              </div>
              <div style={{ fontSize: 16, color: T.white, fontWeight: 700, fontFamily: T.mono }}>
                {(byEngine[eng] || []).length} gates
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Gates grid — one accordion row per gate */}
      <div
        style={{
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          overflow: 'hidden',
        }}
      >
        {payload.items.map((item, idx) => {
          const expanded = expandedGate === item.key;
          const meta = statusMeta[item.status] || statusMeta.active;
          const engColor = engineColor(item.engine);
          return (
            <div
              key={item.key}
              style={{
                borderBottom: idx < payload.items.length - 1 ? `1px solid ${T.cardBorder}` : 'none',
              }}
            >
              <button
                onClick={() => setExpandedGate(expanded ? null : item.key)}
                aria-expanded={expanded}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  width: '100%',
                  padding: '12px 16px',
                  background: expanded ? 'rgba(6,182,212,0.04)' : 'transparent',
                  border: 'none',
                  cursor: 'pointer',
                  color: T.text,
                  textAlign: 'left',
                  fontFamily: 'inherit',
                }}
              >
                {/* Engine + pipeline position chip */}
                <div
                  style={{
                    background: `${engColor}18`,
                    border: `1px solid ${engColor}55`,
                    color: engColor,
                    padding: '3px 8px',
                    borderRadius: 4,
                    fontSize: 10,
                    fontFamily: T.mono,
                    fontWeight: 600,
                    minWidth: 42,
                    textAlign: 'center',
                  }}
                >
                  {item.pipeline_position}
                </div>
                {/* Gate name + class name */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      fontFamily: T.mono,
                      color: T.white,
                    }}
                  >
                    {item.key}
                  </div>
                  <div
                    style={{
                      fontSize: 10,
                      color: T.textDim,
                      fontFamily: T.mono,
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {item.class_name} · {item.file}
                  </div>
                </div>
                {/* Tables read count */}
                <div style={{ fontSize: 10, color: T.textMuted, fontFamily: T.mono }}>
                  {(item.tables_read || []).length} read
                </div>
                {/* Status chip */}
                <div
                  style={{
                    background: meta.bg,
                    border: `1px solid ${meta.border}`,
                    color: meta.color,
                    padding: '2px 8px',
                    borderRadius: 3,
                    fontSize: 9,
                    fontFamily: T.mono,
                    letterSpacing: '0.1em',
                  }}
                >
                  {meta.label}
                </div>
                {/* Caret */}
                <div
                  style={{
                    fontSize: 10,
                    color: T.textDim,
                    transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)',
                    transition: 'transform 150ms',
                  }}
                >
                  ▶
                </div>
              </button>
              {expanded && (
                <div
                  style={{
                    padding: '14px 16px 20px 62px',
                    background: 'rgba(6,182,212,0.02)',
                    borderTop: `1px solid ${T.cardBorder}`,
                  }}
                >
                  <div style={{ fontSize: 12, color: T.text, lineHeight: 1.6, marginBottom: 12 }}>
                    {item.purpose}
                  </div>
                  <GatesGrid item={item} />
                  {item.notes && (
                    <div
                      style={{
                        marginTop: 12,
                        padding: '8px 12px',
                        background: 'rgba(245,158,11,0.06)',
                        border: '1px solid rgba(245,158,11,0.2)',
                        borderRadius: 4,
                        fontSize: 11,
                        color: T.amber,
                        lineHeight: 1.5,
                      }}
                    >
                      <span style={{ fontWeight: 600, marginRight: 6 }}>NOTE:</span>
                      {item.notes}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer */}
      <div
        style={{
          marginTop: 14,
          padding: 14,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          fontSize: 11,
          color: T.textMuted,
          lineHeight: 1.6,
        }}
      >
        Source: <span style={{ color: T.text, fontFamily: T.mono }}>GATES_CATALOG</span> in{' '}
        <span style={{ color: T.text, fontFamily: T.mono }}>hub/db/schema_catalog.py</span>.
        Hand-curated by PR review. Adding a new gate? Append an entry to the dict and pair it with the
        code change in the same PR. The cross-reference
        "which gates share a table" is computed at endpoint time.
      </div>
    </div>
  );
}

function GatesGrid({ item }) {
  const sections = [
    { key: 'inputs', label: 'Inputs', color: T.cyan },
    { key: 'outputs', label: 'Outputs', color: T.green },
    { key: 'env_flags', label: 'Env flags', color: T.purple },
    { key: 'fail_reasons', label: 'Fail reasons', color: T.red },
    { key: 'tables_read', label: 'Tables read', color: T.amber },
    { key: 'tables_written', label: 'Tables written', color: T.orange },
  ];
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        gap: 12,
      }}
    >
      {sections.map((sec) => {
        const list = item[sec.key] || [];
        return (
          <div
            key={sec.key}
            style={{
              background: T.bg,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 4,
              padding: '10px 12px',
            }}
          >
            <div
              style={{
                fontSize: 9,
                fontFamily: T.mono,
                letterSpacing: '0.1em',
                color: sec.color,
                marginBottom: 6,
              }}
            >
              {sec.label.toUpperCase()} · {list.length}
            </div>
            {list.length === 0 ? (
              <div style={{ fontSize: 11, color: T.textDim, fontStyle: 'italic' }}>(none)</div>
            ) : (
              list.map((entry, i) => (
                <div
                  key={i}
                  style={{
                    fontSize: 11,
                    fontFamily: T.mono,
                    color: T.text,
                    lineHeight: 1.5,
                    wordBreak: 'break-word',
                  }}
                >
                  · {entry}
                </div>
              ))
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────

export default function Schema() {
  const api = useApi();
  const [summary, setSummary] = useState(null);
  const [listPayload, setListPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [err, setErr] = useState(null);

  // NAV-01: top-level tab between Tables (SCHEMA-01) and Gates & Signals
  // (this session's addition). Default to 'tables' to preserve the
  // pre-existing page as the landing view.
  const [activeTab, setActiveTab] = useState('tables');

  const [serviceFilter, setServiceFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [search, setSearch] = useState('');

  const [expandedTable, setExpandedTable] = useState(null);
  const [detailByName, setDetailByName] = useState({});
  const [detailLoadingByName, setDetailLoadingByName] = useState({});
  const [detailErrorByName, setDetailErrorByName] = useState({});

  // Gates tab state — loaded lazily on first tab click
  const [gatesPayload, setGatesPayload] = useState(null);
  const [gatesLoading, setGatesLoading] = useState(false);
  const [gatesError, setGatesError] = useState(null);
  const [expandedGate, setExpandedGate] = useState(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [sumRes, listRes] = await Promise.all([
        api('GET', '/v58/schema/summary'),
        api('GET', '/v58/schema/tables?include_runtime=true'),
      ]);
      setSummary(sumRes.data);
      setListPayload(listRes.data);
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || 'Failed to load schema inventory');
    }
  }, [api]);

  const loadGates = useCallback(async () => {
    if (gatesPayload) return; // cached
    setGatesLoading(true);
    setGatesError(null);
    try {
      const res = await api('GET', '/v58/schema/gates');
      setGatesPayload(res.data);
    } catch (e) {
      setGatesError(e?.response?.data?.detail || e?.message || 'Failed to load gates catalog');
    } finally {
      setGatesLoading(false);
    }
  }, [api, gatesPayload]);

  useEffect(() => {
    (async () => {
      setLoading(true);
      await load();
      setLoading(false);
    })();
  }, [load]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    // Also blow away cached detail so expanded rows re-fetch
    setDetailByName({});
    setDetailErrorByName({});
    await load();
    setRefreshing(false);
  }, [load]);

  const allTables = listPayload?.tables || [];
  const services = summary?.services || [];

  // Filter the cards
  const filteredTables = useMemo(() => {
    const q = search.trim().toLowerCase();
    return allTables.filter((t) => {
      if (serviceFilter !== 'all' && t.service !== serviceFilter) return false;
      if (statusFilter !== 'all' && t.status !== statusFilter) return false;
      if (q) {
        const hay = [
          t.name,
          t.purpose,
          t.service,
          t.category,
          t.sot_class || '',
          t.data_flow || '',
          ...(t.writers || []),
          ...(t.readers || []),
        ]
          .join(' ')
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [allTables, serviceFilter, statusFilter, search]);

  const handleToggleTable = useCallback(
    async (name) => {
      if (expandedTable === name) {
        setExpandedTable(null);
        return;
      }
      setExpandedTable(name);
      if (detailByName[name]) return; // already loaded
      setDetailLoadingByName((m) => ({ ...m, [name]: true }));
      setDetailErrorByName((m) => ({ ...m, [name]: null }));
      try {
        const res = await api('GET', `/v58/schema/tables/${encodeURIComponent(name)}`);
        setDetailByName((m) => ({ ...m, [name]: res.data }));
      } catch (e) {
        setDetailErrorByName((m) => ({
          ...m,
          [name]: e?.response?.data?.detail || e?.message || 'Failed to load detail',
        }));
      } finally {
        setDetailLoadingByName((m) => ({ ...m, [name]: false }));
      }
    },
    [api, expandedTable, detailByName]
  );

  return (
    <div
      style={{
        background: T.bg,
        minHeight: '100vh',
        padding: 20,
        color: T.text,
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
      }}
    >
      <div style={{ maxWidth: 1320, margin: '0 auto' }}>
        <HeaderSummary
          summary={summary}
          generatedAt={listPayload?.generated_at}
          onRefresh={handleRefresh}
          refreshing={refreshing}
        />

        {/* ─── Tab bar (NAV-01 Gates & Signals addition) ─── */}
        <div
          style={{
            display: 'flex',
            gap: 8,
            marginBottom: 14,
            borderBottom: `1px solid ${T.cardBorder}`,
            paddingBottom: 0,
          }}
        >
          {[
            { id: 'tables', label: 'DB Tables', sub: `${summary?.total || 0} tracked` },
            { id: 'gates', label: 'Gates & Signals', sub: 'V10.6 + v4 pipelines' },
          ].map((tab) => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  if (tab.id === 'gates') loadGates();
                }}
                style={{
                  background: active ? T.card : 'transparent',
                  border: `1px solid ${active ? T.cyan : 'transparent'}`,
                  borderBottom: active ? `1px solid ${T.bg}` : 'none',
                  borderRadius: '6px 6px 0 0',
                  color: active ? T.cyan : T.textMuted,
                  padding: '9px 18px',
                  cursor: 'pointer',
                  fontSize: 12,
                  fontFamily: 'inherit',
                  marginBottom: -1,
                  transition: 'all 150ms ease-out',
                }}
              >
                <div style={{ fontWeight: 600 }}>{tab.label}</div>
                <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, marginTop: 2 }}>
                  {tab.sub}
                </div>
              </button>
            );
          })}
        </div>

        {activeTab === 'gates' ? (
          <GatesView
            payload={gatesPayload}
            loading={gatesLoading}
            error={gatesError}
            expandedGate={expandedGate}
            setExpandedGate={setExpandedGate}
          />
        ) : (
        <>
        {/* ─── Filter bar ─── */}
        <div
          style={{
            background: T.card,
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
            padding: 14,
            marginBottom: 14,
            display: 'flex',
            flexWrap: 'wrap',
            gap: 14,
            alignItems: 'center',
          }}
        >
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, letterSpacing: '0.1em', marginRight: 4 }}>
              SERVICE
            </span>
            <FilterButton active={serviceFilter === 'all'} onClick={() => setServiceFilter('all')}>
              all
            </FilterButton>
            {services.map((s) => (
              <FilterButton
                key={s}
                active={serviceFilter === s}
                onClick={() => setServiceFilter(s)}
              >
                {s}
              </FilterButton>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
            <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, letterSpacing: '0.1em', marginRight: 4 }}>
              STATUS
            </span>
            <FilterButton active={statusFilter === 'all'} onClick={() => setStatusFilter('all')}>
              all
            </FilterButton>
            <FilterButton active={statusFilter === 'active'} onClick={() => setStatusFilter('active')}>
              active
            </FilterButton>
            <FilterButton active={statusFilter === 'legacy'} onClick={() => setStatusFilter('legacy')}>
              legacy
            </FilterButton>
            <FilterButton active={statusFilter === 'deprecated'} onClick={() => setStatusFilter('deprecated')}>
              deprecated
            </FilterButton>
          </div>
          <div style={{ marginLeft: 'auto' }}>
            <TextInput
              value={search}
              onChange={setSearch}
              placeholder="Search name, purpose, files…"
            />
          </div>
        </div>

        {/* ─── Content ─── */}
        {err && (
          <div
            style={{
              padding: '10px 14px',
              background: 'rgba(239,68,68,0.08)',
              border: '1px solid rgba(239,68,68,0.3)',
              borderRadius: 4,
              color: T.red,
              fontFamily: T.mono,
              fontSize: 11,
              marginBottom: 14,
            }}
          >
            {String(err)}
          </div>
        )}

        {loading && !listPayload ? (
          <div
            style={{
              padding: 30,
              textAlign: 'center',
              color: T.textMuted,
              fontFamily: T.mono,
              fontSize: 11,
            }}
          >
            loading schema inventory…
          </div>
        ) : (
          <>
            <DependencyMap tables={allTables} />

            <div style={{ marginBottom: 8, fontSize: 10, color: T.textDim, fontFamily: T.mono }}>
              showing {filteredTables.length} of {allTables.length} tables
            </div>

            {filteredTables.length === 0 && (
              <div
                style={{
                  padding: 30,
                  textAlign: 'center',
                  color: T.textMuted,
                  fontFamily: T.mono,
                  fontSize: 11,
                  background: T.card,
                  border: `1px solid ${T.cardBorder}`,
                  borderRadius: 6,
                }}
              >
                No tables match the current filters.
              </div>
            )}

            {filteredTables.map((t) => (
              <TableCard
                key={t.name}
                entry={t}
                expanded={expandedTable === t.name}
                onToggle={() => handleToggleTable(t.name)}
                detail={detailByName[t.name]}
                detailLoading={!!detailLoadingByName[t.name]}
                detailError={detailErrorByName[t.name]}
              />
            ))}

            <div
              style={{
                marginTop: 20,
                padding: 12,
                background: T.card,
                border: `1px solid ${T.cardBorder}`,
                borderRadius: 6,
                fontSize: 10,
                color: T.textDim,
                fontFamily: T.mono,
                lineHeight: 1.6,
              }}
            >
              <div style={{ color: T.textMuted, marginBottom: 4 }}>
                The schema catalog is maintained by hand in{' '}
                <span style={{ color: T.text }}>hub/db/schema_catalog.py</span>.
              </div>
              To add a new table: append a SchemaEntry to the dict and PR. The hub endpoint will pick it up on the next deploy.
              Row counts on large tables use the pg_class estimate for speed — add{' '}
              <span style={{ color: T.text }}>{'{"large": True}'}</span> to the entry if needed.
            </div>
          </>
        )}
        </>
        )}
      </div>
    </div>
  );
}
