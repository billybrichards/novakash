/**
 * AgentOps.jsx — Claude Agent SDK background task runner UI.
 *
 * Button grid to spawn agents, live task feed with auto-refresh.
 * Running tasks show elapsed time. Done tasks expand to show the report.
 * Failed tasks show error + retry button.
 *
 * Backend: hub/api/agent_ops.py
 * Route:   /ops (registered in App.jsx, nav entry in SYSTEM section of Layout.jsx)
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useApi } from '../hooks/useApi.js';

// ─── Theme ────────────────────────────────────────────────────────────────────
const T = {
  bg: '#050914',
  card: 'rgba(15, 23, 42, 0.85)',
  cardBorder: 'rgba(51, 65, 85, 0.8)',
  headerBg: 'rgba(30, 41, 59, 0.9)',
  text: 'rgba(203, 213, 225, 1)',
  textMuted: 'rgba(100, 116, 139, 1)',
  textDim: 'rgba(71, 85, 105, 1)',
  cyan: '#06b6d4',
  green: '#10b981',
  red: '#ef4444',
  amber: '#f59e0b',
  purple: '#a855f7',
  blue: '#3b82f6',
  indigo: '#6366f1',
  mono: "'JetBrains Mono', 'Fira Code', 'IBM Plex Mono', monospace",
};

// ─── Agent type metadata ──────────────────────────────────────────────────────
const AGENT_META = {
  sitrep:         { label: 'SITREP',          icon: '⚡', color: T.amber,   desc: 'Full system sitrep' },
  health:         { label: 'HEALTH',          icon: '💚', color: T.green,   desc: 'System health check' },
  trade_analysis: { label: 'TRADE ANALYSIS',  icon: '📊', color: T.cyan,    desc: 'Recent trade outcomes' },
  signal_quality: { label: 'SIGNAL QUALITY',  icon: '📡', color: T.blue,    desc: 'VPIN + signal audit' },
  clean_arch:     { label: 'CLEAN ARCH',      icon: '🏗', color: T.purple,  desc: 'Architecture audit' },
  error_analyzer: { label: 'ERROR ANALYZER',  icon: '🔍', color: T.red,     desc: 'Scan for errors' },
  data_audit:     { label: 'DATA AUDIT',      icon: '🗄', color: T.indigo,  desc: 'DB + feed coverage' },
  frontend_fix:   { label: 'FRONTEND FIX',    icon: '🖥', color: '#f97316', desc: 'Frontend bug scan' },
};

const POLL_MS = 5_000;   // poll while any task is running
const IDLE_MS = 30_000;  // poll interval when all done

// ─── Utilities ────────────────────────────────────────────────────────────────

function fmtElapsed(iso) {
  if (!iso) return '';
  try {
    const diffMs = Date.now() - new Date(iso).getTime();
    const s = Math.round(diffMs / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m}m ago`;
    return `${Math.round(m / 60)}h ago`;
  } catch { return ''; }
}

function fmtDuration(startIso, endIso) {
  if (!startIso) return '';
  try {
    const end = endIso ? new Date(endIso).getTime() : Date.now();
    const ms = end - new Date(startIso).getTime();
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    return `${Math.round(s / 60)}m ${s % 60}s`;
  } catch { return ''; }
}

// ─── Spinner ─────────────────────────────────────────────────────────────────

function Spinner({ size = 14 }) {
  return (
    <span
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        border: `2px solid rgba(255,255,255,0.15)`,
        borderTop: `2px solid ${T.cyan}`,
        borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
        flexShrink: 0,
      }}
    />
  );
}

// ─── Agent button ─────────────────────────────────────────────────────────────

function AgentButton({ type, meta, onRun, running }) {
  const isRunning = running === type;
  return (
    <button
      onClick={() => !isRunning && onRun(type)}
      title={meta.desc}
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        padding: '14px 10px',
        borderRadius: 10,
        border: `1px solid ${isRunning ? meta.color + '60' : 'rgba(255,255,255,0.07)'}`,
        background: isRunning ? meta.color + '12' : 'rgba(15,23,42,0.6)',
        cursor: isRunning ? 'default' : 'pointer',
        transition: 'all 200ms ease-out',
        minHeight: 90,
        opacity: isRunning ? 0.85 : 1,
      }}
      onMouseEnter={e => {
        if (!isRunning) e.currentTarget.style.borderColor = meta.color + '50';
      }}
      onMouseLeave={e => {
        if (!isRunning) e.currentTarget.style.borderColor = 'rgba(255,255,255,0.07)';
      }}
    >
      <span style={{ fontSize: 22, lineHeight: 1 }}>
        {isRunning ? <Spinner size={22} /> : meta.icon}
      </span>
      <span style={{
        fontFamily: T.mono,
        fontSize: 9,
        fontWeight: 700,
        letterSpacing: '0.08em',
        color: isRunning ? meta.color : T.text,
        textAlign: 'center',
        lineHeight: 1.3,
      }}>
        {meta.label}
      </span>
      {isRunning && (
        <span style={{ fontSize: 9, color: T.textMuted }}>running…</span>
      )}
    </button>
  );
}

// ─── Task row ─────────────────────────────────────────────────────────────────

function TaskRow({ task, onRetry, onDelete }) {
  const [expanded, setExpanded] = useState(false);
  const meta = AGENT_META[task.agent_type] || { label: task.agent_type, icon: '?', color: T.textMuted };

  const isRunning = task.status === 'running';
  const isDone = task.status === 'done';
  const isFailed = task.status === 'failed';

  const statusDot = isRunning ? (
    <Spinner size={12} />
  ) : isDone ? (
    <span style={{ color: T.green, fontSize: 14, lineHeight: 1 }}>✓</span>
  ) : (
    <span style={{ color: T.red, fontSize: 14, lineHeight: 1 }}>✗</span>
  );

  const elapsed = isRunning
    ? fmtDuration(task.started_at, null)
    : fmtElapsed(task.completed_at || task.started_at);

  return (
    <div style={{
      borderRadius: 8,
      border: `1px solid ${isRunning ? meta.color + '40' : 'rgba(51,65,85,0.6)'}`,
      background: isRunning ? meta.color + '06' : T.card,
      overflow: 'hidden',
      transition: 'border-color 300ms',
    }}>
      {/* Header row */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '10px 14px',
      }}>
        {/* Status indicator */}
        <div style={{ width: 16, display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0 }}>
          {statusDot}
        </div>

        {/* Agent type badge */}
        <span style={{
          fontFamily: T.mono,
          fontSize: 10,
          fontWeight: 700,
          color: meta.color,
          letterSpacing: '0.06em',
          flexShrink: 0,
        }}>
          {meta.label}
        </span>

        {/* Status text */}
        <span style={{ fontSize: 12, color: T.textMuted, flexShrink: 0 }}>
          {isRunning ? `running (${elapsed})` : `${isDone ? 'done' : 'failed'} (${elapsed})`}
        </span>

        {/* Task ID (truncated) */}
        <span style={{
          fontSize: 9,
          color: T.textDim,
          fontFamily: T.mono,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          minWidth: 0,
          flex: 1,
        }}>
          {task.id?.slice(0, 8)}
        </span>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          {isDone && (
            <button
              onClick={() => setExpanded(e => !e)}
              style={{
                padding: '4px 10px',
                borderRadius: 5,
                border: `1px solid rgba(16,185,129,0.3)`,
                background: expanded ? 'rgba(16,185,129,0.15)' : 'rgba(16,185,129,0.06)',
                color: T.green,
                fontSize: 10,
                fontFamily: T.mono,
                fontWeight: 700,
                cursor: 'pointer',
                letterSpacing: '0.05em',
              }}
            >
              {expanded ? 'HIDE ▲' : 'VIEW REPORT ▼'}
            </button>
          )}
          {isFailed && (
            <button
              onClick={() => onRetry(task.agent_type)}
              style={{
                padding: '4px 10px',
                borderRadius: 5,
                border: `1px solid rgba(239,68,68,0.3)`,
                background: 'rgba(239,68,68,0.06)',
                color: T.red,
                fontSize: 10,
                fontFamily: T.mono,
                fontWeight: 700,
                cursor: 'pointer',
                letterSpacing: '0.05em',
              }}
            >
              RETRY
            </button>
          )}
          <button
            onClick={() => onDelete(task.id)}
            title="Delete task"
            style={{
              padding: '4px 8px',
              borderRadius: 5,
              border: '1px solid rgba(255,255,255,0.07)',
              background: 'transparent',
              color: T.textDim,
              fontSize: 10,
              cursor: 'pointer',
            }}
          >
            ✕
          </button>
        </div>
      </div>

      {/* Error message */}
      {isFailed && task.error && (
        <div style={{
          padding: '0 14px 10px 40px',
          fontSize: 11,
          color: T.red,
          fontFamily: T.mono,
          opacity: 0.8,
        }}>
          {task.error}
        </div>
      )}

      {/* Expanded result */}
      {isDone && expanded && task.result && (
        <div style={{
          borderTop: '1px solid rgba(51,65,85,0.5)',
          padding: '14px 16px',
          maxHeight: 600,
          overflowY: 'auto',
        }}>
          <pre style={{
            margin: 0,
            fontSize: 12,
            color: T.text,
            fontFamily: T.mono,
            lineHeight: 1.65,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}>
            {task.result}
          </pre>
        </div>
      )}
    </div>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function AgentOps() {
  const api = useApi();
  const [tasks, setTasks] = useState([]);
  const [activeRunning, setActiveRunning] = useState(null); // type currently being spawned
  const [error, setError] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const pollRef = useRef(null);

  const hasRunning = tasks.some(t => t.status === 'running');

  const loadTasks = useCallback(async () => {
    try {
      const res = await api.get('/agent-ops/tasks');
      setTasks(res.data.tasks || []);
      setLastRefresh(new Date());
      setError(null);
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || 'Failed to load tasks');
    }
  }, [api]);

  // Auto-refresh: faster when running
  useEffect(() => {
    loadTasks();

    const schedule = () => {
      pollRef.current = setTimeout(async () => {
        await loadTasks();
        schedule();
      }, hasRunning ? POLL_MS : IDLE_MS);
    };
    schedule();

    return () => clearTimeout(pollRef.current);
  }, [loadTasks, hasRunning]);

  const handleRun = useCallback(async (agentType) => {
    try {
      setActiveRunning(agentType);
      await api.post(`/agent-ops/run/${agentType}`);
      await loadTasks();
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || 'Failed to spawn agent');
    } finally {
      setActiveRunning(null);
    }
  }, [api, loadTasks]);

  const handleRetry = useCallback((agentType) => {
    handleRun(agentType);
  }, [handleRun]);

  const handleDelete = useCallback(async (taskId) => {
    try {
      await api.delete(`/agent-ops/tasks/${taskId}`);
      setTasks(prev => prev.filter(t => t.id !== taskId));
    } catch (err) {
      setError(err?.response?.data?.detail || err.message || 'Failed to delete task');
    }
  }, [api]);

  return (
    <div style={{
      minHeight: '100vh',
      background: T.bg,
      padding: '24px 20px',
      fontFamily: 'Inter, system-ui, sans-serif',
      color: T.text,
    }}>
      <style>{`
        @keyframes spin {
          from { transform: rotate(0deg); }
          to { transform: rotate(360deg); }
        }
      `}</style>

      {/* Header */}
      <div style={{ marginBottom: 28 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
          <h1 style={{
            margin: 0,
            fontSize: 22,
            fontWeight: 700,
            color: T.text,
            letterSpacing: '-0.02em',
          }}>
            Agent Ops
          </h1>
          <span style={{
            fontFamily: T.mono,
            fontSize: 10,
            color: T.purple,
            background: 'rgba(168,85,247,0.1)',
            border: '1px solid rgba(168,85,247,0.2)',
            borderRadius: 4,
            padding: '2px 7px',
            fontWeight: 700,
            letterSpacing: '0.05em',
          }}>
            CLAUDE SDK
          </span>
        </div>
        <p style={{ margin: 0, fontSize: 13, color: T.textMuted }}>
          Spawn Claude agents to analyse the codebase, audit signals, and generate reports.
          {lastRefresh && (
            <span style={{ marginLeft: 10, fontSize: 11, color: T.textDim }}>
              Updated {fmtElapsed(lastRefresh.toISOString())}
            </span>
          )}
          {hasRunning && (
            <span style={{ marginLeft: 8, fontSize: 11, color: T.cyan }}>
              · auto-refreshing every 5s
            </span>
          )}
        </p>
      </div>

      {/* Error banner */}
      {error && (
        <div style={{
          marginBottom: 20,
          padding: '10px 14px',
          borderRadius: 8,
          border: '1px solid rgba(239,68,68,0.3)',
          background: 'rgba(239,68,68,0.06)',
          color: T.red,
          fontSize: 13,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}>
          <span>{error}</span>
          <button
            onClick={() => setError(null)}
            style={{ background: 'none', border: 'none', color: T.red, cursor: 'pointer', fontSize: 16, padding: 0 }}
          >✕</button>
        </div>
      )}

      {/* Button grid */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fill, minmax(130px, 1fr))',
        gap: 10,
        marginBottom: 28,
      }}>
        {Object.entries(AGENT_META).map(([type, meta]) => (
          <AgentButton
            key={type}
            type={type}
            meta={meta}
            onRun={handleRun}
            running={activeRunning}
          />
        ))}
      </div>

      {/* Recent tasks */}
      <div>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 12,
        }}>
          <h2 style={{
            margin: 0,
            fontSize: 13,
            fontWeight: 700,
            color: T.textMuted,
            fontFamily: T.mono,
            letterSpacing: '0.08em',
          }}>
            RECENT TASKS
          </h2>
          {tasks.length > 0 && (
            <span style={{ fontSize: 11, color: T.textDim, fontFamily: T.mono }}>
              {tasks.length} task{tasks.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>

        {tasks.length === 0 ? (
          <div style={{
            padding: '40px 20px',
            textAlign: 'center',
            color: T.textDim,
            fontSize: 13,
            border: '1px dashed rgba(51,65,85,0.5)',
            borderRadius: 10,
          }}>
            No tasks yet. Click a button above to spawn an agent.
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {tasks.map(task => (
              <TaskRow
                key={task.id}
                task={task}
                onRetry={handleRetry}
                onDelete={handleDelete}
              />
            ))}
          </div>
        )}
      </div>

      {/* Agent type reference */}
      <div style={{ marginTop: 32 }}>
        <h2 style={{
          margin: '0 0 12px',
          fontSize: 13,
          fontWeight: 700,
          color: T.textDim,
          fontFamily: T.mono,
          letterSpacing: '0.08em',
        }}>
          AGENT REFERENCE
        </h2>
        <div style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
          gap: 8,
        }}>
          {Object.entries(AGENT_META).map(([type, meta]) => (
            <div
              key={type}
              style={{
                padding: '10px 12px',
                borderRadius: 7,
                border: '1px solid rgba(51,65,85,0.4)',
                background: 'rgba(15,23,42,0.4)',
                display: 'flex',
                alignItems: 'flex-start',
                gap: 10,
              }}
            >
              <span style={{ fontSize: 16, flexShrink: 0, marginTop: 1 }}>{meta.icon}</span>
              <div>
                <div style={{
                  fontFamily: T.mono,
                  fontSize: 10,
                  fontWeight: 700,
                  color: meta.color,
                  letterSpacing: '0.06em',
                  marginBottom: 3,
                }}>
                  {meta.label}
                </div>
                <div style={{ fontSize: 11, color: T.textMuted, lineHeight: 1.4 }}>
                  {meta.desc}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
