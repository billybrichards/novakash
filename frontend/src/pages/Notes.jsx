/**
 * Notes.jsx — DB-backed audit journal (NT-01).
 *
 * Persistent list of observations / TODOs / working notes that survives
 * frontend redeploys. Polls /api/notes every 30s so notes added from other
 * sessions show up automatically. Cmd+Enter in the compose textarea submits.
 *
 * Backend: hub/api/notes.py (GET/POST/PATCH/DELETE /api/notes)
 * Route:   /notes (registered in App.jsx, nav entry at top of POLYMARKET section)
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const POLL_MS = 30_000;

// ─── Utilities ────────────────────────────────────────────────────────────

function fmtWhen(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffSec = Math.round(diffMs / 1000);
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.round(diffHr / 24);
    if (diffDay < 30) return `${diffDay}d ago`;
    return d.toISOString().slice(0, 10);
  } catch {
    return String(iso).slice(0, 19);
  }
}

function parseTags(csv) {
  if (!csv) return [];
  return csv
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean);
}

function firstLine(s) {
  if (!s) return '';
  const idx = s.indexOf('\n');
  return idx === -1 ? s : s.slice(0, idx);
}

// ─── Small components ────────────────────────────────────────────────────

function Chip({ color, bg, border, children, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 9,
        fontWeight: 700,
        padding: '2px 7px',
        borderRadius: 3,
        background: bg,
        color,
        border: `1px solid ${border}`,
        fontFamily: T.mono,
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </span>
  );
}

function StatusPill({ status }) {
  const isOpen = status === 'open';
  return (
    <Chip
      color={isOpen ? T.green : T.textMuted}
      bg={isOpen ? 'rgba(16,185,129,0.12)' : 'rgba(100,116,139,0.12)'}
      border={isOpen ? 'rgba(16,185,129,0.3)' : 'rgba(100,116,139,0.3)'}
      title={`status: ${status}`}
    >
      {(status || 'open').toUpperCase()}
    </Chip>
  );
}

function TagChip({ tag }) {
  return (
    <Chip
      color={T.cyan}
      bg="rgba(6,182,212,0.08)"
      border="rgba(6,182,212,0.25)"
    >
      #{tag}
    </Chip>
  );
}

function Button({ onClick, variant = 'default', children, type = 'button', disabled = false, title }) {
  const palette = {
    default: {
      color: T.text,
      bg: 'rgba(255,255,255,0.05)',
      border: 'rgba(255,255,255,0.12)',
    },
    primary: {
      color: T.purple,
      bg: 'rgba(168,85,247,0.12)',
      border: 'rgba(168,85,247,0.35)',
    },
    cyan: {
      color: T.cyan,
      bg: 'rgba(6,182,212,0.12)',
      border: 'rgba(6,182,212,0.35)',
    },
    danger: {
      color: T.red,
      bg: 'rgba(239,68,68,0.12)',
      border: 'rgba(239,68,68,0.35)',
    },
    ghost: {
      color: T.textMuted,
      bg: 'transparent',
      border: 'rgba(255,255,255,0.1)',
    },
  };
  const p = palette[variant] || palette.default;
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      style={{
        fontFamily: T.mono,
        fontSize: 10,
        fontWeight: 700,
        letterSpacing: '0.05em',
        textTransform: 'uppercase',
        padding: '6px 12px',
        borderRadius: 4,
        background: disabled ? 'rgba(255,255,255,0.03)' : p.bg,
        border: `1px solid ${disabled ? 'rgba(255,255,255,0.06)' : p.border}`,
        color: disabled ? T.textDim : p.color,
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'all 140ms ease-out',
      }}
    >
      {children}
    </button>
  );
}

function TextInput({ value, onChange, placeholder, onKeyDown, title }) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      onKeyDown={onKeyDown}
      title={title}
      style={{
        background: 'rgba(15,23,42,0.6)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 4,
        padding: '7px 10px',
        color: T.text,
        fontFamily: T.mono,
        fontSize: 11,
        outline: 'none',
        width: '100%',
        boxSizing: 'border-box',
      }}
    />
  );
}

function TextArea({ value, onChange, placeholder, onKeyDown, inputRef, minHeight = 120 }) {
  return (
    <textarea
      ref={inputRef}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      onKeyDown={onKeyDown}
      style={{
        background: 'rgba(15,23,42,0.6)',
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 4,
        padding: '10px 12px',
        color: T.text,
        fontFamily: T.mono,
        fontSize: 12,
        outline: 'none',
        width: '100%',
        minHeight,
        resize: 'vertical',
        lineHeight: 1.5,
        boxSizing: 'border-box',
      }}
    />
  );
}

// ─── Compose area ────────────────────────────────────────────────────────

function ComposeArea({ onSave, onCancel, initial, autoFocus }) {
  const [title, setTitle] = useState(initial?.title || '');
  const [tags, setTags] = useState(initial?.tags || '');
  const [body, setBody] = useState(initial?.body || '');
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const bodyRef = useRef(null);

  useEffect(() => {
    if (autoFocus && bodyRef.current) {
      bodyRef.current.focus();
    }
  }, [autoFocus]);

  const canSave = body.trim().length > 0 && !saving;

  const handleSave = useCallback(async () => {
    if (!canSave) return;
    setSaving(true);
    setErr(null);
    try {
      await onSave({ title: title.trim(), body: body.trim(), tags: tags.trim() });
      setTitle('');
      setTags('');
      setBody('');
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || 'Save failed');
    } finally {
      setSaving(false);
    }
  }, [canSave, onSave, title, body, tags]);

  const handleBodyKey = (e) => {
    // Cmd+Enter (mac) or Ctrl+Enter (others) submits
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
      e.preventDefault();
      handleSave();
    }
  };

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
      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        <div style={{ flex: 1 }}>
          <TextInput
            value={title}
            onChange={setTitle}
            placeholder="Title (optional)"
          />
        </div>
        <div style={{ flex: 1 }}>
          <TextInput
            value={tags}
            onChange={setTags}
            placeholder="Tags (CSV, e.g. dq-06,audit)"
            title="Comma-separated tags"
          />
        </div>
      </div>
      <TextArea
        value={body}
        onChange={setBody}
        placeholder="Write your observation... (Cmd+Enter to save)"
        onKeyDown={handleBodyKey}
        inputRef={bodyRef}
        minHeight={140}
      />
      {err && (
        <div
          style={{
            marginTop: 8,
            padding: '6px 10px',
            background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: 4,
            color: T.red,
            fontFamily: T.mono,
            fontSize: 10,
          }}
        >
          {err}
        </div>
      )}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginTop: 10,
        }}
      >
        <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono }}>
          Cmd+Enter to save · plain text, newlines preserved
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {onCancel && (
            <Button variant="ghost" onClick={onCancel}>
              Cancel
            </Button>
          )}
          <Button variant="primary" onClick={handleSave} disabled={!canSave}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </div>
  );
}

// ─── Note card ───────────────────────────────────────────────────────────

function NoteCard({ note, onUpdate, onDelete }) {
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const handleSaveEdit = async ({ title, body, tags }) => {
    await onUpdate(note.id, { title, body, tags });
    setEditing(false);
  };

  const handleDelete = async () => {
    if (!window.confirm('Delete this note? This cannot be undone.')) return;
    setDeleting(true);
    try {
      await onDelete(note.id);
    } catch {
      setDeleting(false);
    }
  };

  const handleToggleArchive = async () => {
    setArchiving(true);
    try {
      await onUpdate(note.id, {
        status: note.status === 'open' ? 'archived' : 'open',
      });
    } finally {
      setArchiving(false);
    }
  };

  if (editing) {
    return (
      <div
        style={{
          background: T.card,
          border: `1px solid ${T.purple}`,
          borderRadius: 6,
          padding: 14,
          marginBottom: 10,
        }}
      >
        <div style={{ fontSize: 9, color: T.purple, fontFamily: T.mono, marginBottom: 8 }}>
          EDITING · id {note.id}
        </div>
        <ComposeArea
          initial={{ title: note.title, tags: note.tags, body: note.body }}
          onSave={handleSaveEdit}
          onCancel={() => setEditing(false)}
          autoFocus
        />
      </div>
    );
  }

  const tags = parseTags(note.tags);
  const displayTitle = note.title?.trim() || firstLine(note.body) || '(untitled)';

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: 14,
        marginBottom: 10,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 10,
          marginBottom: 8,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: 800,
              color: T.white,
              marginBottom: 4,
              wordBreak: 'break-word',
            }}
          >
            {displayTitle}
          </div>
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 6,
              alignItems: 'center',
              fontSize: 9,
              color: T.textMuted,
              fontFamily: T.mono,
            }}
          >
            <StatusPill status={note.status} />
            <span style={{ color: T.textDim }}>·</span>
            <span title={note.created_at}>created {fmtWhen(note.created_at)}</span>
            {note.updated_at && note.updated_at !== note.created_at && (
              <>
                <span style={{ color: T.textDim }}>·</span>
                <span title={note.updated_at}>updated {fmtWhen(note.updated_at)}</span>
              </>
            )}
            <span style={{ color: T.textDim }}>·</span>
            <span>by {note.author || 'unknown'}</span>
            <span style={{ color: T.textDim }}>·</span>
            <span style={{ color: T.textDim }}>#{note.id}</span>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          <Button variant="cyan" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button
            variant={note.status === 'open' ? 'default' : 'primary'}
            onClick={handleToggleArchive}
            disabled={archiving}
            title={note.status === 'open' ? 'Archive this note' : 'Reopen this note'}
          >
            {note.status === 'open' ? 'Archive' : 'Reopen'}
          </Button>
          <Button variant="danger" onClick={handleDelete} disabled={deleting}>
            {deleting ? '…' : 'Delete'}
          </Button>
        </div>
      </div>

      <pre
        style={{
          margin: '6px 0 8px',
          padding: '10px 12px',
          background: 'rgba(15,23,42,0.5)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 4,
          color: T.text,
          fontFamily: T.mono,
          fontSize: 11,
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          maxHeight: 400,
          overflowY: 'auto',
        }}
      >
        {note.body}
      </pre>

      {tags.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 6 }}>
          {tags.map((t) => (
            <TagChip key={t} tag={t} />
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Filter strip ─────────────────────────────────────────────────────────

function FilterStrip({ statusFilter, setStatusFilter, tagFilter, setTagFilter, search, setSearch, total, visible }) {
  const statuses = [
    { value: 'open', label: 'Open', color: T.green },
    { value: 'all', label: 'All', color: T.cyan },
    { value: 'archived', label: 'Archived', color: T.textMuted },
  ];

  return (
    <div
      style={{
        display: 'flex',
        flexWrap: 'wrap',
        gap: 8,
        alignItems: 'center',
        padding: '8px 10px',
        marginBottom: 12,
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        fontSize: 10,
        fontFamily: T.mono,
        color: T.textMuted,
      }}
    >
      <div style={{ display: 'flex', gap: 4 }}>
        {statuses.map((s) => {
          const active = statusFilter === s.value;
          return (
            <button
              key={s.value}
              onClick={() => setStatusFilter(s.value)}
              style={{
                fontFamily: T.mono,
                fontSize: 10,
                fontWeight: 700,
                letterSpacing: '0.04em',
                textTransform: 'uppercase',
                padding: '5px 10px',
                borderRadius: 3,
                background: active ? `${s.color}22` : 'rgba(255,255,255,0.03)',
                border: `1px solid ${active ? s.color : 'rgba(255,255,255,0.1)'}`,
                color: active ? s.color : T.textMuted,
                cursor: 'pointer',
                transition: 'all 140ms',
              }}
            >
              {s.label}
            </button>
          );
        })}
      </div>

      <div style={{ width: 140, minWidth: 120 }}>
        <TextInput
          value={tagFilter}
          onChange={setTagFilter}
          placeholder="tag filter"
        />
      </div>
      <div style={{ flex: 1, minWidth: 140 }}>
        <TextInput
          value={search}
          onChange={setSearch}
          placeholder="search body/title…"
        />
      </div>

      <div style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, whiteSpace: 'nowrap' }}>
        {visible === total ? `${total} total` : `${visible}/${total} shown`}
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────

// Default seed note for critical findings (shown if no notes exist)
const SEED_NOTS = {
  title: 'CRITICAL: DOWN-ONLY Trading Strategy — 99% Win Rate',
  body: '=== CRITICAL FINDING: DOWN-ONLY TRADING ===\n\nDate: 2026-04-12\nData: 897,503 signal evaluations (T-90-150, conf>=0.12)\n\nWIN RATES BY DIRECTION:\n\nDOWN predictions:\n  CLOB ask >0.75 (contrarian): 99.0% WR (175,261 trades)\n  CLOB ask 0.55-0.75: 97.8% WR (112,371 trades)\n  CLOB ask 0.35-0.55: 92.1% WR (86,821 trades)\n  CLOB ask <=0.35: 76.2% WR (177,435 trades)\n\nUP predictions:\n  CLOB ask >0.75: 53.8% WR (74,107 trades)\n  CLOB ask 0.55-0.75: 23.8% WR (124,835 trades)\n  CLOB ask 0.35-0.55: 1.8% WR (75,177 trades)\n  CLOB ask <=0.35: 1.5% WR (71,496 trades)\n\nWHY: Retail traders have strong UP bias on 5-min BTC windows\n\nRECOMMENDATION:\n  - Trade DOWN ONLY\n  - Skip ALL UP predictions\n  - Size: 2.0x for contrarian (clob_ask >=0.75)\n  - Expected WR: 76-99%\n\nSee: docs/analysis/DOWN_ONLY_STRATEGY_2026-04-12.md',
  tags: 'critical,down-only,strategy',
};

export default function Notes() {
  const api = useApi();
  const [notes, setNotes] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [composing, setComposing] = useState(false);

  const [statusFilter, setStatusFilter] = useState('open');
  const [tagFilter, setTagFilter] = useState('');
  const [search, setSearch] = useState('');

  const fetchNotes = useCallback(
    async ({ silent = false } = {}) => {
      if (!silent) setLoading(true);
      try {
        const res = await api('GET', '/notes', {
          params: {
            status: statusFilter,
            limit: 100,
            offset: 0,
            ...(tagFilter ? { tag: tagFilter } : {}),
          },
        });
        const data = res?.data || {};
        setNotes(Array.isArray(data.rows) ? data.rows : []);
        setTotal(typeof data.total === 'number' ? data.total : 0);
        setErr(null);
      } catch (e) {
        setErr(e?.response?.data?.detail || e?.message || 'Failed to load notes');
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [api, statusFilter, tagFilter]
  );

  // Initial + on-filter-change fetch
  useEffect(() => {
    fetchNotes();
  }, [fetchNotes]);

  // Poll every 30s (silent — no flicker)
  useEffect(() => {
    const id = setInterval(() => fetchNotes({ silent: true }), POLL_MS);
    return () => clearInterval(id);
  }, [fetchNotes]);

  const handleCreate = useCallback(
    async ({ title, body, tags }) => {
      // Optimistic add is skipped: just POST then refetch so we pick up the
      // server-assigned id + timestamps in one round-trip.
      await api('POST', '/notes', {
        data: { title, body, tags, status: 'open', author: 'claude' },
      });
      setComposing(false);
      await fetchNotes();
    },
    [api, fetchNotes]
  );

  const handleUpdate = useCallback(
    async (id, patch) => {
      // Optimistic update
      const prev = notes;
      const next = notes.map((n) => (n.id === id ? { ...n, ...patch, updated_at: new Date().toISOString() } : n));
      setNotes(next);
      try {
        await api('PATCH', `/notes/${id}`, { data: patch });
        // Refetch silently to reconcile with server
        fetchNotes({ silent: true });
      } catch (e) {
        setNotes(prev);
        throw e;
      }
    },
    [api, notes, fetchNotes]
  );

  const handleDelete = useCallback(
    async (id) => {
      const prev = notes;
      setNotes((ns) => ns.filter((n) => n.id !== id));
      try {
        await api('DELETE', `/notes/${id}`);
        // Server is the source of truth — refetch silently
        fetchNotes({ silent: true });
      } catch (e) {
        setNotes(prev);
        setErr(e?.response?.data?.detail || e?.message || 'Delete failed');
        throw e;
      }
    },
    [api, notes, fetchNotes]
  );

  // Client-side search filter (server-side search is out of scope for v1)
  const visibleNotes = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return notes;
    return notes.filter((n) => {
      const hay = `${n.title || ''} ${n.body || ''} ${n.tags || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [notes, search]);

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1100, margin: '0 auto' }}>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 10,
          marginBottom: 14,
        }}
      >
        <div>
          <h1
            style={{
              fontSize: 18,
              fontWeight: 900,
              color: T.white,
              margin: 0,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}
          >
            <span style={{ fontSize: 18 }}>📝</span>
            Notes
            <Chip color={T.cyan} bg="rgba(6,182,212,0.12)" border="rgba(6,182,212,0.3)">
              NT-01
            </Chip>
            <Chip color={T.textMuted} bg="rgba(100,116,139,0.1)" border="rgba(100,116,139,0.3)">SYSTEM</Chip>
          </h1>
          <p
            style={{
              fontSize: 10,
              color: T.textMuted,
              margin: '4px 0 0',
              lineHeight: 1.5,
              maxWidth: 760,
            }}
          >
            Persistent journal for audit observations, to-do items, and working
            notes. Backs /audit with room for quick drops that don't warrant a
            task. Survives frontend redeploys. Polls every 30s.
          </p>
        </div>
        <Button variant="primary" onClick={() => setComposing((c) => !c)}>
          {composing ? '× Close' : '+ New note'}
        </Button>
      </div>

      {/* Filter strip */}
      <FilterStrip
        statusFilter={statusFilter}
        setStatusFilter={setStatusFilter}
        tagFilter={tagFilter}
        setTagFilter={setTagFilter}
        search={search}
        setSearch={setSearch}
        total={total}
        visible={visibleNotes.length}
      />

      {/* Error banner */}
      {err && (
        <div
          style={{
            marginBottom: 12,
            padding: '10px 12px',
            background: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: 4,
            color: T.red,
            fontFamily: T.mono,
            fontSize: 11,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            gap: 10,
          }}
        >
          <span>{err}</span>
          <Button variant="danger" onClick={() => fetchNotes()}>
            Retry
          </Button>
        </div>
      )}

      {/* Compose area */}
      {composing && (
        <ComposeArea
          onSave={handleCreate}
          onCancel={() => setComposing(false)}
          autoFocus
        />
      )}

      {/* List */}
      {loading && notes.length === 0 ? (
        <div
          style={{
            padding: '28px 16px',
            textAlign: 'center',
            color: T.textMuted,
            fontFamily: T.mono,
            fontSize: 11,
            background: T.card,
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
          }}
        >
          loading notes…
        </div>
      ) : visibleNotes.length === 0 ? (
        <div
          style={{
            padding: '28px 16px',
            textAlign: 'center',
            color: T.textMuted,
            fontFamily: T.mono,
            fontSize: 11,
            background: T.card,
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
            lineHeight: 1.6,
          }}
        >
          No notes yet. Add your first observation with the button above.
        </div>
      ) : (
        <div>
          {visibleNotes.map((n) => (
            <NoteCard
              key={n.id}
              note={n}
              onUpdate={handleUpdate}
              onDelete={handleDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}
