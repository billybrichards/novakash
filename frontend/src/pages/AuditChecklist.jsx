/**
 * AuditChecklist.jsx — Big Audit Session tracking page.
 *
 * Hybrid mode: fetches live task state from /api/audit-tasks on mount
 * (status overrides static TASKS by dedupe_key=task.id). Falls back to
 * static TASKS if the API is unreachable. Agents can update task status
 * via PATCH /api/audit-tasks/{id} without a frontend deploy.
 *
 * Seed the DB with:  python scripts/seed_audit_tasks.py
 */

import { useMemo, useState, useEffect, useCallback } from 'react';
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

const SEVERITY_COLOR = {
  CRITICAL: T.red,
  HIGH: T.amber,
  MEDIUM: T.cyan,
  LOW: T.textMuted,
};

const STATUS_COLOR = {
  OPEN: T.red,
  IN_PROGRESS: T.amber,
  DONE: T.green,
  BLOCKED: T.purple,
  INFO: T.cyan,
};

// ─── Audit Data ───────────────────────────────────────────────────────────
// Edit this block as tasks progress. The page re-renders statically.

const SESSION_META = {
  title: 'Clean-Architect Audit · 2026-04-11',
  summary:
    'Deep audit of the Polymarket engine (engine/) against the margin_engine/ reference architecture, the v4 fusion surface on novakash-timesfm-repo, and PR #18 reconciler regressions. Covers data-quality, decision-surface gaps, production errors, v1-v4 observability surfaces, and engine CI/CD automation.',
  startedAt: '2026-04-11',
  progressLog: 'docs/AUDIT_PROGRESS.md',
  repos: [
    { name: 'novakash', branch: 'develop', head: '6816f86' },
    { name: 'novakash-timesfm-repo', branch: 'main', head: 'af51523' },
  ],
};

const CATEGORIES = [
  {
    id: 'data-quality',
    title: 'Data Quality — Price References',
    color: T.red,
    description:
      'Venue-specific price reference bugs. Polymarket engine (engine/) resolves against oracle spot and needs spot-aligned deltas. margin_engine trades Hyperliquid perps and needs perp/mark-aligned deltas. Mixing the two contaminates signals regardless of model quality. Tracked as two tasks: DQ-01 (Polymarket) and DQ-05 (margin_engine).',
  },
  {
    id: 'production-errors',
    title: 'Production Errors · Regressions',
    color: T.amber,
    description:
      'Active error streams in engine.log on Montreal. Includes pre-existing bugs and a regression from PR #18 (reconciler type deduction).',
  },
  {
    id: 'decision-surface',
    title: 'V10.6 Decision Surface',
    color: T.cyan,
    description:
      'The 865-outcome proposal commit c3a6cbd is documentation-only. Thresholds, offset bounds, UP penalty, confidence haircut, proportional sizing are NOT in engine code.',
  },
  {
    id: 'v4-adoption',
    title: 'V4 Fusion Surface · Polymarket Engine',
    color: T.purple,
    description:
      'margin_engine/ uses the 10-gate v4 stack (PR #16). The Polymarket engine (engine/) still does not call v4 at all — grep finds zero references.',
  },
  {
    id: 'clean-architect',
    title: 'Clean-Architect Migration',
    color: T.blue,
    description:
      '3096-line five_min_vpin.py is the single biggest source of architectural debt. margin_engine/ has ports/adapters/use-cases/value-objects — this is the reference to migrate toward.',
  },
  {
    id: 'frontend',
    title: 'Frontend & Observability',
    color: T.green,
    description:
      'V4Panel landed in PR #22 on the /margin page. This audit page ships next. Both observe paper-mode margin_engine; the Polymarket engine has no equivalent surface.',
  },
  {
    id: 'ci-cd',
    title: 'CI/CD · Montreal Automation',
    color: '#f97316',
    description:
      'docs/CI_CD.md (6816f86) explicitly flags engine/ as the only major service without a GitHub Actions deploy workflow. The deploy-macro-observer.yml ~200-line template is the canonical pattern to port. Engine currently relies on Railway git-watcher auto-deploy with no smoke test, no secrets check, no post-deploy health probe, no rollback, and has been observed CRASHED in recent history.',
  },
  {
    id: 'signal-optimization',
    title: 'Signal Optimization · CLOB + Direction Analysis',
    color: '#22d3ee',
    description:
      'Data-driven signal improvements from 897K-sample analysis (2026-04-12). Key finding: DOWN predictions have 76–99% WR; UP predictions have 1.5–53% WR. CLOB data required for full edge — fixed in PR #136. Gates SIG-03/SIG-04 to be implemented next.',
  },
  {
    id: 'config-migration',
    title: 'CFG · DB-backed config migration',
    color: T.cyan,
    description:
      'Full migration of runtime configuration from .env files to a DB-backed store with hot-reload, audit trail, and a /config UI. Tracked in docs/CONFIG_MIGRATION_PLAN.md (CFG-01). Phase 0/1 (CFG-02/03/05) ships read-only schema + read API + read UI. Phase 1 (CFG-04/06) adds writes + admin claim. Phase 2 (CFG-07/08/10) wires per-service loaders + flips SKIP_DB_CONFIG_SYNC. Phase 3 (CFG-11) cleans up legacy .env reads.',
  },
  {
    id: 'ml-training-data',
    title: 'ML Training Data Audit · 2026-04-13',
    color: '#f472b6',
    description:
      'Full inventory of every data asset, database table, prediction surface, signal, and Polymarket outcome available for ML training. Covers v1 (TimesFM), v2 (LightGBM), v3 (composite), v4 (decision surface), gate audit trail, and reconciled outcome labels. Target: 500+ labeled window-outcome pairs per Δ bucket for reliable model retraining. Explored via automated agent across novakash/develop + novakash-timesfm-repo/main.',
  },
  {
    id: 'btc-15m-expansion',
    title: 'BTC 15-Minute Trading Expansion · 2026-04-13',
    color: '#818cf8',
    description:
      'Expand the 5-strategy clean architecture to BTC 15-minute Polymarket markets. 5 critical hardcoded "5m" blockers to fix, 5 new YAML strategy configs (v15m_down/up_asian/up_basic/fusion/gate), timing gates scaled 3x. Most infrastructure already exists (15m feed, model slot, V4 snapshot). Plan: docs/BTC_15M_EXPANSION_PLAN.md. All new strategies start GHOST — promotion only with Billy approval.',
  },
];

// ─── Fallback Tasks (used when API is unreachable) ────────────────────────────
// All full task data is now fetched from /api/audit-tasks. This minimal
// fallback is shown only when the API fails.

const FALLBACK_TASKS = [
  {
    id: 'loading',
    title: 'Loading tasks from database...',
    status: 'OPEN',
    severity: 'INFO',
    category: 'Meta',
  },
];

// ─── Components ───────────────────────────────────────────────────────────

function SeverityChip({ severity }) {
  const color = SEVERITY_COLOR[severity] || T.textMuted;
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontSize: 8,
        fontWeight: 800,
        padding: '2px 6px',
        borderRadius: 3,
        background: `${color}26`,
        color,
        border: `1px solid ${color}55`,
        fontFamily: T.mono,
        letterSpacing: '0.05em',
      }}
    >
      {severity}
    </span>
  );
}

function StatusChip({ status }) {
  const color = STATUS_COLOR[status] || T.textMuted;
  const labels = {
    OPEN: '○ OPEN',
    IN_PROGRESS: '◐ IN PROGRESS',
    DONE: '● DONE',
    BLOCKED: '■ BLOCKED',
    INFO: '◇ INFO',
  };
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        fontSize: 8,
        fontWeight: 800,
        padding: '2px 6px',
        borderRadius: 3,
        background: `${color}26`,
        color,
        border: `1px solid ${color}55`,
        fontFamily: T.mono,
        letterSpacing: '0.05em',
      }}
    >
      {labels[status] || status}
    </span>
  );
}

function FileRef({ file }) {
  const text = file.line > 1 ? `${file.path}:${file.line}` : file.path;
  return (
    <span
      style={{
        display: 'inline-block',
        fontSize: 9,
        fontFamily: T.mono,
        color: T.cyan,
        background: 'rgba(6,182,212,0.08)',
        padding: '1px 5px',
        borderRadius: 3,
        marginRight: 4,
        marginBottom: 4,
      }}
      title={`${file.repo} · ${text}`}
    >
      {text}
    </span>
  );
}

function TaskCard({ task, categoryColor }) {
  const [expanded, setExpanded] = useState(task.status === 'IN_PROGRESS');

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderLeft: `3px solid ${categoryColor}`,
        borderRadius: 6,
        padding: 12,
        marginBottom: 8,
      }}
    >
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 12,
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 4, flexWrap: 'wrap' }}>
            <span
              style={{
                fontSize: 9,
                fontFamily: T.mono,
                color: T.textDim,
                fontWeight: 800,
                letterSpacing: '0.05em',
              }}
            >
              {task.id}
            </span>
            <SeverityChip severity={task.severity} />
            <StatusChip status={task.status} />
          </div>
          <div style={{ fontSize: 12, color: T.text, fontWeight: 600, lineHeight: 1.3 }}>
            {task.title}
          </div>
        </div>
        <span style={{ fontSize: 9, color: T.textDim, fontFamily: T.mono, flexShrink: 0 }}>
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      {expanded && (
        <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${T.cardBorder}` }}>
          {task.files.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div
                style={{
                  fontSize: 8,
                  color: T.textMuted,
                  fontWeight: 800,
                  letterSpacing: '0.08em',
                  marginBottom: 4,
                }}
              >
                FILES
              </div>
              <div>
                {task.files.map((f, i) => (
                  <FileRef key={i} file={f} />
                ))}
              </div>
            </div>
          )}

          <div style={{ marginBottom: 10 }}>
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 800,
                letterSpacing: '0.08em',
                marginBottom: 4,
              }}
            >
              EVIDENCE
            </div>
            <ul style={{ margin: 0, paddingLeft: 16 }}>
              {task.evidence.map((e, i) => (
                <li
                  key={i}
                  style={{
                    fontSize: 10,
                    color: T.text,
                    marginBottom: 2,
                    lineHeight: 1.4,
                  }}
                >
                  {e}
                </li>
              ))}
            </ul>
          </div>

          <div style={{ marginBottom: task.progressNotes?.length ? 10 : 0 }}>
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 800,
                letterSpacing: '0.08em',
                marginBottom: 4,
              }}
            >
              FIX
            </div>
            <div
              style={{
                fontSize: 10,
                color: T.text,
                padding: '6px 8px',
                background: 'rgba(16,185,129,0.05)',
                border: '1px solid rgba(16,185,129,0.15)',
                borderRadius: 4,
                lineHeight: 1.4,
              }}
            >
              {task.fix}
            </div>
          </div>

          {task.progressNotes && task.progressNotes.length > 0 && (
            <div>
              <div
                style={{
                  fontSize: 8,
                  color: T.textMuted,
                  fontWeight: 800,
                  letterSpacing: '0.08em',
                  marginBottom: 4,
                }}
              >
                PROGRESS LOG
              </div>
              <div
                style={{
                  padding: '6px 8px',
                  background: 'rgba(168,85,247,0.05)',
                  border: '1px solid rgba(168,85,247,0.15)',
                  borderRadius: 4,
                }}
              >
                {task.progressNotes.map((entry, i) => (
                  <div
                    key={i}
                    style={{
                      display: 'flex',
                      gap: 8,
                      alignItems: 'flex-start',
                      marginBottom:
                        i === task.progressNotes.length - 1 ? 0 : 6,
                    }}
                  >
                    <span
                      style={{
                        fontSize: 9,
                        color: T.purple,
                        fontFamily: T.mono,
                        fontWeight: 800,
                        whiteSpace: 'nowrap',
                        flexShrink: 0,
                      }}
                    >
                      {entry.date}
                    </span>
                    <span
                      style={{
                        fontSize: 10,
                        color: T.text,
                        lineHeight: 1.4,
                      }}
                    >
                      {entry.note}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ProgressBar({ done, total }) {
  const pct = total > 0 ? (done / total) * 100 : 0;
  return (
    <div style={{ width: '100%' }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: 9,
          color: T.textMuted,
          marginBottom: 3,
          fontFamily: T.mono,
        }}
      >
        <span>PROGRESS</span>
        <span>
          {done}/{total} · {pct.toFixed(0)}%
        </span>
      </div>
      <div
        style={{
          height: 6,
          background: 'rgba(15,23,42,0.6)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 3,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${pct}%`,
            background: T.green,
            transition: 'width 0.3s ease',
          }}
        />
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────

export default function AuditChecklist() {
  const api = useApi();
  const [severityFilter, setSeverityFilter] = useState('ALL');
  const [statusFilter, setStatusFilter] = useState('ALL');
  const [categoryFilter, setCategoryFilter] = useState('ALL');

  // DB-backed tasks — stores full task objects from API
  const [dbTasks, setDbTasks] = useState(null); // null = not yet fetched
  const [dbSource, setDbSource] = useState('static'); // 'static' | 'db'
  const [lastSyncTime, setLastSyncTime] = useState(null); // ISO timestamp

  const fetchDbTasks = useCallback(async () => {
    try {
      const res = await api.get('/audit-tasks?limit=500&task_type=audit_checklist');
      const rows = res.data?.rows ?? [];
      if (rows.length > 0) {
        setDbTasks(rows);
        setDbSource('db');
        setLastSyncTime(new Date().toISOString());
      }
    } catch (_err) {
      // DB unavailable — silently fall back to fallback data
    }
  }, [api]);

  useEffect(() => {
    fetchDbTasks();
  }, [fetchDbTasks]);

  // Use DB tasks if available, otherwise use fallback array
  const tasks = useMemo(() => {
    if (!dbTasks || dbTasks.length === 0) return FALLBACK_TASKS;
    return dbTasks;
  }, [dbTasks]);

  const filteredTasks = useMemo(() => {
    return tasks.filter((t) => {
      if (severityFilter !== 'ALL' && t.severity !== severityFilter) return false;
      if (statusFilter !== 'ALL' && t.status !== statusFilter) return false;
      if (categoryFilter !== 'ALL' && t.category !== categoryFilter) return false;
      return true;
    });
  }, [tasks, severityFilter, statusFilter, categoryFilter]);

  const stats = useMemo(() => {
    const total = tasks.length;
    const done = tasks.filter((t) => t.status === 'DONE').length;
    const open = tasks.filter((t) => t.status === 'OPEN').length;
    const inProgress = tasks.filter((t) => t.status === 'IN_PROGRESS').length;
    const critical = tasks.filter((t) => t.severity === 'CRITICAL').length;
    const high = tasks.filter((t) => t.severity === 'HIGH').length;
    return { total, done, open, inProgress, critical, high };
  }, [tasks]);

  const tasksByCategory = useMemo(() => {
    const map = {};
    for (const cat of CATEGORIES) {
      map[cat.id] = filteredTasks.filter((t) => t.category === cat.id);
    }
    return map;
  }, [filteredTasks]);

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1400, margin: '0 auto' }}>
      {/* Header */}
      <div style={{ marginBottom: 16 }}>
        <h1
          style={{
            fontSize: 16,
            fontWeight: 800,
            color: T.white,
            margin: 0,
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          {SESSION_META.title}
          <span
            style={{
              fontSize: 8,
              fontWeight: 700,
              padding: '2px 6px',
              borderRadius: 3,
              background: dbSource === 'db' ? 'rgba(16,185,129,0.15)' : 'rgba(168,85,247,0.15)',
              color: dbSource === 'db' ? T.green : T.purple,
              border: `1px solid ${dbSource === 'db' ? 'rgba(16,185,129,0.3)' : 'rgba(168,85,247,0.3)'}`,
              fontFamily: T.mono,
            }}
          >
            {dbSource === 'db' ? 'DB-LIVE' : 'STATIC'}
          </span>
          <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3, background: 'rgba(6,182,212,0.12)', color: T.cyan, border: '1px solid rgba(6,182,212,0.3)', fontFamily: T.mono, letterSpacing: '0.06em' }}>POLY + PERPS</span>
        </h1>
        <p style={{ fontSize: 10, color: T.textMuted, margin: '4px 0 0', maxWidth: 900, lineHeight: 1.5 }}>
          {SESSION_META.summary}
        </p>
        <div style={{ display: 'flex', gap: 12, marginTop: 6, flexWrap: 'wrap' }}>
          {SESSION_META.repos.map((r, i) => (
            <span
              key={i}
              style={{
                fontSize: 9,
                fontFamily: T.mono,
                color: T.textMuted,
              }}
            >
              <span style={{ color: T.textDim }}>{r.name}</span>
              <span style={{ color: T.cyan, margin: '0 4px' }}>/</span>
              <span style={{ color: T.text }}>{r.branch}</span>
              <span style={{ color: T.textDim, marginLeft: 4 }}>@ {r.head}</span>
            </span>
          ))}
        </div>
        <div
          style={{
            marginTop: 10,
            padding: '8px 10px',
            borderRadius: 6,
            border: `1px solid ${T.cardBorder}`,
            background:
              dbSource === 'db' ? 'rgba(16,185,129,0.08)' : 'rgba(168,85,247,0.08)',
            fontSize: 9,
            color: T.textMuted,
            lineHeight: 1.5,
          }}
        >
          {dbSource === 'db' ? (
            <>
              <span style={{ color: T.green, fontWeight: 700, letterSpacing: '0.08em' }}>
                🗄️ Tasks loaded from database
              </span>{' '}
              {' • '}
              <span style={{ color: T.text, fontFamily: T.mono }}>{stats.total} tasks</span>
              {' • '}
              <span style={{ color: T.textMuted }}>
                Last synced:{' '}
                <span style={{ color: T.text, fontFamily: T.mono }}>
                  {lastSyncTime
                    ? new Date(lastSyncTime).toLocaleTimeString()
                    : 'just now'}
                </span>
              </span>
            </>
          ) : (
            <>
              <span style={{ color: T.amber, fontWeight: 700, letterSpacing: '0.08em' }}>
                ⚠️ DB unreachable — using fallback
              </span>{' '}
              ({FALLBACK_TASKS.length} placeholder tasks). Seed DB with{' '}
              <span style={{ color: T.text, fontFamily: T.mono }}>python scripts/seed_audit_tasks.py</span>
            </>
          )}
        </div>
      </div>

      {/* Stats + Progress */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(100px, 1fr))',
          gap: 8,
          marginBottom: 12,
        }}
      >
        {[
          { label: 'TOTAL', value: stats.total, color: T.text },
          { label: 'DONE', value: stats.done, color: T.green },
          { label: 'IN PROGRESS', value: stats.inProgress, color: T.amber },
          { label: 'OPEN', value: stats.open, color: T.red },
          { label: 'CRITICAL', value: stats.critical, color: T.red },
          { label: 'HIGH', value: stats.high, color: T.amber },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            style={{
              background: T.card,
              border: `1px solid ${T.cardBorder}`,
              borderRadius: 6,
              padding: '8px 10px',
            }}
          >
            <div
              style={{
                fontSize: 8,
                color: T.textMuted,
                fontWeight: 700,
                letterSpacing: '0.08em',
                marginBottom: 3,
              }}
            >
              {label}
            </div>
            <div style={{ fontSize: 18, fontWeight: 900, fontFamily: T.mono, color }}>{value}</div>
          </div>
        ))}
      </div>

      <div
        style={{
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          padding: '10px 12px',
          marginBottom: 16,
        }}
      >
        <ProgressBar done={stats.done} total={stats.total} />
      </div>

      {/* Filter bar */}
      <div
        style={{
          display: 'flex',
          gap: 12,
          marginBottom: 14,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        <FilterGroup
          label="SEVERITY"
          value={severityFilter}
          onChange={setSeverityFilter}
          options={['ALL', 'CRITICAL', 'HIGH', 'MEDIUM', 'LOW']}
        />
        <FilterGroup
          label="STATUS"
          value={statusFilter}
          onChange={setStatusFilter}
          options={['ALL', 'OPEN', 'IN_PROGRESS', 'DONE', 'INFO']}
        />
        <FilterGroup
          label="CATEGORY"
          value={categoryFilter}
          onChange={setCategoryFilter}
          options={['ALL', ...CATEGORIES.map((c) => c.id)]}
        />
      </div>

      {/* Categories */}
      {CATEGORIES.map((cat) => {
        const tasks = tasksByCategory[cat.id];
        if (!tasks || tasks.length === 0) return null;
        return (
          <div key={cat.id} style={{ marginBottom: 20 }}>
            <div
              style={{
                padding: '8px 12px',
                marginBottom: 8,
                borderRadius: 6,
                background: `${cat.color}0d`,
                border: `1px solid ${cat.color}33`,
                borderLeft: `3px solid ${cat.color}`,
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  marginBottom: 2,
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 800,
                    color: cat.color,
                    letterSpacing: '0.05em',
                    textTransform: 'uppercase',
                  }}
                >
                  {cat.title}
                </span>
                <span
                  style={{
                    fontSize: 9,
                    color: T.textMuted,
                    fontFamily: T.mono,
                  }}
                >
                  {tasks.filter((t) => t.status === 'DONE').length}/{tasks.length} done
                </span>
              </div>
              <div style={{ fontSize: 9, color: T.textMuted, lineHeight: 1.4 }}>
                {cat.description}
              </div>
            </div>
            {tasks.map((task) => (
              <TaskCard key={task.id} task={task} categoryColor={cat.color} />
            ))}
          </div>
        );
      })}

      {filteredTasks.length === 0 && (
        <div
          style={{
            textAlign: 'center',
            padding: 30,
            color: T.textMuted,
            fontSize: 11,
            background: T.card,
            border: `1px solid ${T.cardBorder}`,
            borderRadius: 6,
          }}
        >
          No tasks match the current filters.
        </div>
      )}
    </div>
  );
}

function FilterGroup({ label, value, onChange, options }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <span
        style={{
          fontSize: 8,
          color: T.textMuted,
          fontWeight: 800,
          letterSpacing: '0.08em',
          marginRight: 4,
        }}
      >
        {label}
      </span>
      {options.map((opt) => (
        <button
          key={opt}
          onClick={() => onChange(opt)}
          style={{
            padding: '4px 8px',
            borderRadius: 3,
            fontSize: 9,
            fontWeight: 700,
            fontFamily: T.mono,
            background: value === opt ? 'rgba(6,182,212,0.15)' : 'transparent',
            color: value === opt ? T.cyan : T.textMuted,
            border: `1px solid ${value === opt ? 'rgba(6,182,212,0.3)' : T.cardBorder}`,
            cursor: 'pointer',
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
          }}
        >
          {opt}
        </button>
      ))}
    </div>
  );
}
