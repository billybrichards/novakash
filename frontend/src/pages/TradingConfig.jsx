import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useApi } from '../hooks/useApi.js';
import ConfigWidget from '../components/ConfigWidget.jsx';

// ── Constants ─────────────────────────────────────────────────────────────────

const MODE_STYLES = {
  paper: {
    color: '#a855f7',
    bg: 'rgba(168,85,247,0.1)',
    border: 'rgba(168,85,247,0.3)',
    label: 'PAPER',
    icon: '📄',
  },
  live: {
    color: '#f87171',
    bg: 'rgba(248,113,113,0.1)',
    border: 'rgba(248,113,113,0.3)',
    label: 'LIVE',
    icon: '💰',
  },
};

const CATEGORY_META = {
  risk:     { label: 'Risk Management',   icon: '🛡️',  color: '#4ade80' },
  vpin:     { label: 'VPIN Signals',      icon: '📡',  color: '#f59e0b' },
  arb:      { label: 'Arbitrage',         icon: '⚡',  color: '#06b6d4' },
  cascade:  { label: 'Cascade Strategy',  icon: '🌊',  color: '#a855f7' },
  fees:     { label: 'Fees & Venues',     icon: '💸',  color: 'rgba(255,255,255,0.5)' },
};

// ── Sub-components ────────────────────────────────────────────────────────────

function ModeBadge({ mode, size = 'sm' }) {
  const s = MODE_STYLES[mode] || MODE_STYLES.paper;
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      padding: size === 'sm' ? '2px 7px' : '4px 10px',
      borderRadius: 4,
      border: `1px solid ${s.border}`,
      background: s.bg,
      color: s.color,
      fontSize: size === 'sm' ? 10 : 12,
      fontFamily: 'IBM Plex Mono, monospace',
      fontWeight: 600,
      letterSpacing: '0.06em',
    }}>
      {s.icon} {s.label}
    </span>
  );
}

function StatusBadge({ config }) {
  if (!config) return null;
  if (config.is_active && config.is_approved) {
    return (
      <span style={{ color: '#4ade80', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>
        ● ACTIVE · APPROVED
      </span>
    );
  }
  if (config.is_active) {
    return (
      <span style={{ color: '#a855f7', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>
        ● ACTIVE
      </span>
    );
  }
  if (config.is_approved) {
    return (
      <span style={{ color: '#06b6d4', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>
        ✓ APPROVED
      </span>
    );
  }
  return (
    <span style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, fontFamily: 'IBM Plex Mono, monospace' }}>
      ○ DRAFT
    </span>
  );
}

function CollapsibleSection({ category, children, defaultOpen = true }) {
  const [open, setOpen] = useState(defaultOpen);
  const meta = CATEGORY_META[category] || { label: category, icon: '⚙️', color: '#fff' };

  return (
    <div style={{
      border: '1px solid rgba(255,255,255,0.06)',
      borderRadius: 10,
      overflow: 'hidden',
      marginBottom: 12,
    }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 16px',
          background: 'rgba(255,255,255,0.025)',
          border: 'none',
          cursor: 'pointer',
          borderBottom: open ? '1px solid rgba(255,255,255,0.06)' : 'none',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 16 }}>{meta.icon}</span>
          <span style={{
            color: meta.color,
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 12,
            fontWeight: 700,
            letterSpacing: '0.06em',
          }}>
            {meta.label.toUpperCase()}
          </span>
        </div>
        <span style={{
          color: 'rgba(255,255,255,0.3)',
          fontSize: 12,
          transition: 'transform 200ms ease-out',
          transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
        }}>
          ›
        </span>
      </button>

      {open && (
        <div style={{ padding: 12, display: 'flex', flexDirection: 'column', gap: 10 }}>
          {children}
        </div>
      )}
    </div>
  );
}

function ConfigCard({ config, isLoaded, onLoad, onClone, onDelete, onApprove, onActivate }) {
  const modeStyle = MODE_STYLES[config.mode] || MODE_STYLES.paper;

  return (
    <div style={{
      background: isLoaded ? 'rgba(168,85,247,0.06)' : 'rgba(255,255,255,0.02)',
      border: `1px solid ${isLoaded ? 'rgba(168,85,247,0.25)' : 'rgba(255,255,255,0.06)'}`,
      borderRadius: 8,
      padding: '12px 14px',
      transition: 'all 200ms ease-out',
    }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{
            color: isLoaded ? '#a855f7' : 'rgba(255,255,255,0.85)',
            fontSize: 13,
            fontWeight: 600,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}>
            {config.name}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 4 }}>
            <ModeBadge mode={config.mode} />
            <span style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10 }}>v{config.version}</span>
            <StatusBadge config={config} />
          </div>
        </div>
      </div>

      {config.description && (
        <div style={{ color: 'rgba(255,255,255,0.35)', fontSize: 11, marginBottom: 8, lineHeight: 1.4 }}>
          {config.description}
        </div>
      )}

      <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
        <ActionBtn label="Load" color="#a855f7" onClick={() => onLoad(config)} />
        <ActionBtn label="Clone →" color="#06b6d4" onClick={() => onClone(config)} />
        {!config.is_active && (
          <ActionBtn label="Activate" color="#4ade80" onClick={() => onActivate(config)} />
        )}
        {config.mode === 'live' && !config.is_approved && (
          <ActionBtn label="Approve" color="#f59e0b" onClick={() => onApprove(config)} />
        )}
        <ActionBtn label="Delete" color="#f87171" onClick={() => onDelete(config)} />
      </div>
    </div>
  );
}

function ActionBtn({ label, color, onClick }) {
  const [hover, setHover] = useState(false);
  return (
    <button
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onClick}
      style={{
        padding: '3px 8px',
        borderRadius: 4,
        border: `1px solid ${hover ? color + '60' : color + '25'}`,
        background: hover ? color + '15' : 'transparent',
        color: hover ? color : color + '80',
        fontSize: 10,
        cursor: 'pointer',
        fontFamily: 'IBM Plex Mono, monospace',
        transition: 'all 150ms ease-out',
      }}
    >
      {label}
    </button>
  );
}

function ApproveModal({ config, onClose, onConfirm }) {
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    setLoading(true);
    setError('');
    try {
      await onConfirm(config.id, password);
      onClose();
    } catch (e) {
      setError(e.message || 'Approval failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'rgba(0,0,0,0.8)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 9998, backdropFilter: 'blur(4px)',
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{
        background: '#0d0d16',
        border: '1px solid rgba(245,158,11,0.3)',
        borderRadius: 12,
        padding: 24,
        width: 380,
        maxWidth: '90vw',
        boxShadow: '0 0 40px rgba(245,158,11,0.1)',
        animation: 'fadeSlideIn 200ms ease-out',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 }}>
          <span style={{ fontSize: 18 }}>🔐</span>
          <div>
            <div style={{ color: '#f59e0b', fontFamily: 'IBM Plex Mono, monospace', fontSize: 13, fontWeight: 700 }}>
              APPROVE FOR LIVE TRADING
            </div>
            <div style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, marginTop: 2 }}>
              {config.name} · v{config.version}
            </div>
          </div>
        </div>

        <div style={{ color: 'rgba(255,255,255,0.5)', fontSize: 12, marginBottom: 14, lineHeight: 1.5 }}>
          Approving this config authorises the engine to use it for live trading.
          Enter your approval password to confirm.
        </div>

        <input
          type="password"
          value={password}
          onChange={e => { setPassword(e.target.value); setError(''); }}
          placeholder="Approval password"
          autoFocus
          onKeyDown={e => e.key === 'Enter' && submit()}
          style={{
            width: '100%',
            background: 'rgba(255,255,255,0.05)',
            border: '1px solid rgba(255,255,255,0.1)',
            borderRadius: 6,
            color: '#fff',
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 13,
            padding: '8px 12px',
            outline: 'none',
            marginBottom: error ? 8 : 14,
            boxSizing: 'border-box',
          }}
        />

        {error && (
          <div style={{ color: '#f87171', fontSize: 11, marginBottom: 12 }}>{error}</div>
        )}

        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={onClose} style={{
            flex: 1, padding: '8px', borderRadius: 6,
            border: '1px solid rgba(255,255,255,0.1)', background: 'transparent',
            color: 'rgba(255,255,255,0.4)', fontSize: 12, cursor: 'pointer',
          }}>
            Cancel
          </button>
          <button onClick={submit} disabled={loading || !password} style={{
            flex: 2, padding: '8px', borderRadius: 6, border: 'none',
            background: password ? 'rgba(245,158,11,0.8)' : 'rgba(245,158,11,0.2)',
            color: password ? '#fff' : 'rgba(245,158,11,0.4)',
            fontSize: 12, fontWeight: 700, cursor: password && !loading ? 'pointer' : 'not-allowed',
            fontFamily: 'IBM Plex Mono, monospace',
          }}>
            {loading ? 'APPROVING...' : 'APPROVE'}
          </button>
        </div>
      </div>
    </div>
  );
}

function NewConfigModal({ onClose, onSave, defaults }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [mode, setMode] = useState('paper');

  const submit = () => {
    if (!name.trim()) return;
    onSave({ name: name.trim(), description, mode });
    onClose();
  };

  return (
    <div style={{
      position: 'fixed', inset: 0,
      background: 'rgba(0,0,0,0.8)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      zIndex: 9998, backdropFilter: 'blur(4px)',
    }} onClick={e => e.target === e.currentTarget && onClose()}>
      <div style={{
        background: '#0d0d16',
        border: '1px solid rgba(168,85,247,0.25)',
        borderRadius: 12,
        padding: 24,
        width: 380,
        maxWidth: '90vw',
        animation: 'fadeSlideIn 200ms ease-out',
      }}>
        <div style={{ color: '#a855f7', fontFamily: 'IBM Plex Mono, monospace', fontSize: 13, fontWeight: 700, marginBottom: 16 }}>
          NEW CONFIG
        </div>

        <div style={{ marginBottom: 12 }}>
          <label style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, display: 'block', marginBottom: 4 }}>NAME</label>
          <input
            type="text"
            value={name}
            onChange={e => setName(e.target.value)}
            placeholder="e.g. Conservative Paper v1"
            autoFocus
            style={{
              width: '100%', background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)', borderRadius: 6,
              color: '#fff', fontFamily: 'IBM Plex Mono, monospace',
              fontSize: 13, padding: '8px 12px', outline: 'none', boxSizing: 'border-box',
            }}
          />
        </div>

        <div style={{ marginBottom: 12 }}>
          <label style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, display: 'block', marginBottom: 4 }}>DESCRIPTION (optional)</label>
          <textarea
            value={description}
            onChange={e => setDescription(e.target.value)}
            placeholder="What is this config for?"
            rows={2}
            style={{
              width: '100%', background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)', borderRadius: 6,
              color: '#fff', fontFamily: 'IBM Plex Mono, monospace',
              fontSize: 12, padding: '8px 12px', outline: 'none',
              resize: 'vertical', boxSizing: 'border-box',
            }}
          />
        </div>

        <div style={{ marginBottom: 18 }}>
          <label style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11, display: 'block', marginBottom: 6 }}>MODE</label>
          <div style={{ display: 'flex', gap: 8 }}>
            {['paper', 'live'].map(m => (
              <button
                key={m}
                onClick={() => setMode(m)}
                style={{
                  flex: 1, padding: '8px',
                  borderRadius: 6,
                  border: `1px solid ${mode === m ? MODE_STYLES[m].border : 'rgba(255,255,255,0.08)'}`,
                  background: mode === m ? MODE_STYLES[m].bg : 'transparent',
                  color: mode === m ? MODE_STYLES[m].color : 'rgba(255,255,255,0.3)',
                  fontSize: 12, cursor: 'pointer',
                  fontFamily: 'IBM Plex Mono, monospace',
                  fontWeight: mode === m ? 700 : 400,
                  transition: 'all 150ms',
                }}
              >
                {MODE_STYLES[m].icon} {m.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={onClose} style={{
            flex: 1, padding: '9px', borderRadius: 6,
            border: '1px solid rgba(255,255,255,0.1)', background: 'transparent',
            color: 'rgba(255,255,255,0.4)', fontSize: 12, cursor: 'pointer',
          }}>
            Cancel
          </button>
          <button onClick={submit} disabled={!name.trim()} style={{
            flex: 2, padding: '9px', borderRadius: 6, border: 'none',
            background: name.trim() ? 'rgba(168,85,247,0.8)' : 'rgba(168,85,247,0.2)',
            color: name.trim() ? '#fff' : 'rgba(168,85,247,0.4)',
            fontSize: 12, fontWeight: 700, cursor: name.trim() ? 'pointer' : 'not-allowed',
            fontFamily: 'IBM Plex Mono, monospace',
          }}>
            CREATE CONFIG
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function TradingConfig() {
  const api = useApi();

  // State
  const [defaults, setDefaults] = useState([]);
  const [configs, setConfigs] = useState([]);
  const [liveStatus, setLiveStatus] = useState({});
  const [vpinData, setVpinData] = useState([]);

  // Editor state
  const [workingConfig, setWorkingConfig] = useState({}); // current edits
  const [workingName, setWorkingName] = useState('');
  const [workingDescription, setWorkingDescription] = useState('');
  const [workingMode, setWorkingMode] = useState('paper');
  const [loadedConfigId, setLoadedConfigId] = useState(null);
  const [isDirty, setIsDirty] = useState(false);

  // UI state
  const [filterMode, setFilterMode] = useState('all');
  const [showNewModal, setShowNewModal] = useState(false);
  const [approveTarget, setApproveTarget] = useState(null);
  const [saveStatus, setSaveStatus] = useState(''); // 'saving' | 'saved' | 'error' | ''
  const [compareMode, setCompareMode] = useState(false);
  const [compareIds, setCompareIds] = useState([]);

  // Load data
  const loadData = useCallback(async () => {
    try {
      const [defsRes, listRes, statusRes] = await Promise.all([
        api('GET', '/trading-config/defaults'),
        api('GET', '/trading-config/list'),
        api('GET', '/trading-config/live-status'),
      ]);
      setDefaults(defsRes.data.defaults || []);
      setConfigs(listRes.data.configs || []);
      setLiveStatus(statusRes.data || {});
    } catch (e) {
      console.error('Failed to load trading config data', e);
    }
  }, [api]);

  const loadVpinData = useCallback(async () => {
    try {
      const res = await api('GET', '/vpin-history');
      const values = (res.data.history || []).map(h => h.vpin_value || h.value || 0);
      if (values.length > 0) setVpinData(values);
    } catch {
      // VPIN data optional — widgets fall back to simulated data
    }
  }, [api]);

  useEffect(() => {
    loadData();
    loadVpinData();
  }, [loadData, loadVpinData]);

  // ── Initialise working config from defaults ────────────────────────────────
  useEffect(() => {
    if (defaults.length > 0 && Object.keys(workingConfig).length === 0) {
      const initial = {};
      defaults.forEach(d => { initial[d.key] = d.default; });
      setWorkingConfig(initial);
    }
  }, [defaults]);

  // ── Config editor handlers ────────────────────────────────────────────────
  const handleWidgetChange = useCallback((key, value) => {
    setWorkingConfig(prev => ({ ...prev, [key]: value }));
    setIsDirty(true);
  }, []);

  const handleLoadConfig = useCallback((config) => {
    setWorkingConfig(config.config || {});
    setWorkingName(config.name);
    setWorkingDescription(config.description || '');
    setWorkingMode(config.mode);
    setLoadedConfigId(config.id);
    setIsDirty(false);
  }, []);

  // ── Save / Create ─────────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!workingName.trim()) {
      setSaveStatus('error');
      return;
    }
    setSaveStatus('saving');
    try {
      if (loadedConfigId) {
        await api('PUT', `/trading-config/${loadedConfigId}`, {
          data: {
            name: workingName,
            description: workingDescription,
            config: workingConfig,
          },
        });
      } else {
        const res = await api('POST', '/trading-config', {
          data: {
            name: workingName,
            description: workingDescription,
            config: workingConfig,
            mode: workingMode,
          },
        });
        setLoadedConfigId(res.data.config.id);
      }
      setSaveStatus('saved');
      setIsDirty(false);
      await loadData();
      setTimeout(() => setSaveStatus(''), 2000);
    } catch (e) {
      setSaveStatus('error');
      setTimeout(() => setSaveStatus(''), 3000);
    }
  }, [api, loadedConfigId, workingName, workingDescription, workingConfig, workingMode, loadData]);

  const handleNewConfig = useCallback(async ({ name, description, mode }) => {
    const initial = {};
    defaults.forEach(d => { initial[d.key] = d.default; });
    try {
      const res = await api('POST', '/trading-config', {
        data: { name, description, config: initial, mode },
      });
      const newConfig = res.data.config;
      setWorkingConfig(newConfig.config || initial);
      setWorkingName(newConfig.name);
      setWorkingDescription(newConfig.description || '');
      setWorkingMode(newConfig.mode);
      setLoadedConfigId(newConfig.id);
      setIsDirty(false);
      await loadData();
    } catch (e) {
      console.error('Failed to create config', e);
    }
  }, [api, defaults, loadData]);

  const handleClone = useCallback(async (config) => {
    try {
      await api('POST', `/trading-config/${config.id}/clone`);
      await loadData();
    } catch (e) {
      console.error('Failed to clone config', e);
    }
  }, [api, loadData]);

  const handleDelete = useCallback(async (config) => {
    if (!window.confirm(`Delete "${config.name}"? This cannot be undone.`)) return;
    try {
      await api('DELETE', `/trading-config/${config.id}`);
      if (loadedConfigId === config.id) {
        setLoadedConfigId(null);
        setIsDirty(false);
      }
      await loadData();
    } catch (e) {
      console.error('Failed to delete config', e);
    }
  }, [api, loadData, loadedConfigId]);

  const handleActivate = useCallback(async (config) => {
    try {
      await api('POST', `/trading-config/${config.id}/activate`);
      await loadData();
    } catch (e) {
      alert(e.response?.data?.detail || 'Failed to activate config');
    }
  }, [api, loadData]);

  const handleApprove = useCallback(async (configId, password) => {
    await api('POST', `/trading-config/${configId}/approve`, {
      data: { password },
    });
    await loadData();
  }, [api, loadData]);

  const handleLoadDefaults = useCallback(() => {
    const initial = {};
    defaults.forEach(d => { initial[d.key] = d.default; });
    setWorkingConfig(initial);
    setIsDirty(true);
  }, [defaults]);

  // ── Grouped defaults by category ─────────────────────────────────────────
  const grouped = defaults.reduce((acc, def) => {
    const cat = def.category || 'other';
    if (!acc[cat]) acc[cat] = [];
    acc[cat].push(def);
    return acc;
  }, {});

  const filteredConfigs = filterMode === 'all'
    ? configs
    : configs.filter(c => c.mode === filterMode);

  // ── Active configs summary ─────────────────────────────────────────────────
  const activePaper = configs.find(c => c.mode === 'paper' && c.is_active);
  const activeLive = configs.find(c => c.mode === 'live' && c.is_active);

  return (
    <div style={{ padding: '28px 24px', maxWidth: 1200, margin: '0 auto' }}>

      {/* ── Page Header ──────────────────────────────────────────────────── */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 24, flexWrap: 'wrap', gap: 12 }}>
        <div>
          <h1 style={{
            color: 'rgba(255,255,255,0.9)',
            fontFamily: 'IBM Plex Mono, monospace',
            fontSize: 20,
            fontWeight: 700,
            letterSpacing: '-0.02em',
            margin: 0,
          }}>
            ⚙️ Trading Config
          </h1>
          <div style={{ color: 'rgba(255,255,255,0.35)', fontSize: 12, marginTop: 4 }}>
            Configure risk, signals, and strategy parameters for paper and live engines
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
          {/* Mode badge for currently loaded config */}
          {loadedConfigId && (
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              padding: '6px 12px',
              borderRadius: 6,
              background: 'rgba(255,255,255,0.03)',
              border: '1px solid rgba(255,255,255,0.06)',
            }}>
              <span style={{ color: 'rgba(255,255,255,0.4)', fontSize: 11 }}>loaded:</span>
              <span style={{ color: 'rgba(255,255,255,0.8)', fontSize: 12, fontFamily: 'IBM Plex Mono, monospace' }}>
                {workingName}
              </span>
              <ModeBadge mode={workingMode} />
              {isDirty && (
                <span style={{ color: '#f59e0b', fontSize: 10 }}>● unsaved</span>
              )}
            </div>
          )}

          <button
            onClick={handleLoadDefaults}
            style={{
              padding: '7px 14px',
              borderRadius: 6,
              border: '1px solid rgba(255,255,255,0.1)',
              background: 'transparent',
              color: 'rgba(255,255,255,0.5)',
              fontSize: 12,
              cursor: 'pointer',
              fontFamily: 'IBM Plex Mono, monospace',
            }}
          >
            Reset Defaults
          </button>

          <button
            onClick={() => setShowNewModal(true)}
            style={{
              padding: '7px 14px',
              borderRadius: 6,
              border: '1px solid rgba(6,182,212,0.3)',
              background: 'rgba(6,182,212,0.08)',
              color: '#06b6d4',
              fontSize: 12,
              cursor: 'pointer',
              fontFamily: 'IBM Plex Mono, monospace',
              fontWeight: 600,
            }}
          >
            + New Config
          </button>

          <button
            onClick={handleSave}
            disabled={!workingName.trim() || saveStatus === 'saving'}
            style={{
              padding: '7px 16px',
              borderRadius: 6,
              border: 'none',
              background: saveStatus === 'saved'
                ? 'rgba(74,222,128,0.7)'
                : saveStatus === 'error'
                  ? 'rgba(248,113,113,0.7)'
                  : isDirty
                    ? 'rgba(168,85,247,0.8)'
                    : 'rgba(168,85,247,0.3)',
              color: '#fff',
              fontSize: 12,
              fontWeight: 700,
              cursor: workingName.trim() ? 'pointer' : 'not-allowed',
              fontFamily: 'IBM Plex Mono, monospace',
              letterSpacing: '0.05em',
              transition: 'all 200ms',
            }}
          >
            {saveStatus === 'saving' ? 'SAVING...'
              : saveStatus === 'saved' ? '✓ SAVED'
              : saveStatus === 'error' ? '✗ ERROR'
              : 'SAVE CONFIG'}
          </button>
        </div>
      </div>

      {/* ── Active Config Status Bar ──────────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 1fr',
        gap: 12,
        marginBottom: 24,
      }}>
        {[
          { mode: 'paper', label: 'Paper Engine', enabled: liveStatus.paper_enabled, config: activePaper },
          { mode: 'live', label: 'Live Engine', enabled: liveStatus.live_enabled, config: activeLive },
        ].map(({ mode, label, enabled, config }) => {
          const ms = MODE_STYLES[mode];
          return (
            <div
              key={mode}
              style={{
                background: enabled ? ms.bg : 'rgba(255,255,255,0.02)',
                border: `1px solid ${enabled ? ms.border : 'rgba(255,255,255,0.06)'}`,
                borderRadius: 8,
                padding: '14px 16px',
                transition: 'all 300ms ease-out',
                boxShadow: enabled && mode === 'live' ? '0 0 20px rgba(248,113,113,0.1)' : 'none',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontSize: 14 }}>{ms.icon}</span>
                  <span style={{
                    color: enabled ? ms.color : 'rgba(255,255,255,0.3)',
                    fontFamily: 'IBM Plex Mono, monospace',
                    fontSize: 11,
                    fontWeight: 700,
                    letterSpacing: '0.08em',
                  }}>
                    {label.toUpperCase()}
                  </span>
                </div>
                <span style={{
                  fontSize: 10,
                  fontFamily: 'IBM Plex Mono, monospace',
                  color: enabled ? ms.color : 'rgba(255,255,255,0.2)',
                  padding: '2px 6px',
                  borderRadius: 3,
                  border: `1px solid ${enabled ? ms.border : 'rgba(255,255,255,0.06)'}`,
                  background: enabled ? ms.bg : 'transparent',
                }}>
                  {enabled ? '● RUNNING' : '○ STOPPED'}
                </span>
              </div>

              {config ? (
                <div>
                  <div style={{ color: 'rgba(255,255,255,0.7)', fontSize: 12, fontWeight: 500 }}>
                    {config.name}
                  </div>
                  <div style={{ color: 'rgba(255,255,255,0.3)', fontSize: 10, marginTop: 2 }}>
                    v{config.version} · <StatusBadge config={config} />
                  </div>
                  <button
                    onClick={() => handleLoadConfig(config)}
                    style={{
                      marginTop: 8,
                      padding: '3px 8px',
                      borderRadius: 4,
                      border: `1px solid ${ms.border}`,
                      background: 'transparent',
                      color: ms.color,
                      fontSize: 10,
                      cursor: 'pointer',
                      fontFamily: 'IBM Plex Mono, monospace',
                    }}
                  >
                    Load into editor →
                  </button>
                </div>
              ) : (
                <div style={{ color: 'rgba(255,255,255,0.25)', fontSize: 12 }}>
                  No active config — create one below
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Main layout: Editor + Library ────────────────────────────────── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: '1fr 340px',
        gap: 20,
        alignItems: 'start',
      }}>

        {/* ── Config Editor ──────────────────────────────────────────────── */}
        <div>
          {/* Config name/description bar */}
          <div style={{
            background: 'rgba(255,255,255,0.02)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 8,
            padding: '12px 14px',
            marginBottom: 16,
          }}>
            <div style={{ display: 'flex', gap: 10, marginBottom: 8 }}>
              <input
                type="text"
                value={workingName}
                onChange={e => { setWorkingName(e.target.value); setIsDirty(true); }}
                placeholder="Config name (required to save)"
                style={{
                  flex: 1,
                  background: 'rgba(255,255,255,0.05)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: 6,
                  color: '#fff',
                  fontFamily: 'IBM Plex Mono, monospace',
                  fontSize: 13,
                  padding: '7px 12px',
                  outline: 'none',
                }}
              />
              {/* Mode selector for new configs */}
              {!loadedConfigId && (
                <div style={{ display: 'flex', gap: 6 }}>
                  {['paper', 'live'].map(m => (
                    <button
                      key={m}
                      onClick={() => setWorkingMode(m)}
                      style={{
                        padding: '6px 12px',
                        borderRadius: 6,
                        border: `1px solid ${workingMode === m ? MODE_STYLES[m].border : 'rgba(255,255,255,0.08)'}`,
                        background: workingMode === m ? MODE_STYLES[m].bg : 'transparent',
                        color: workingMode === m ? MODE_STYLES[m].color : 'rgba(255,255,255,0.3)',
                        fontSize: 11,
                        cursor: 'pointer',
                        fontFamily: 'IBM Plex Mono, monospace',
                        fontWeight: 700,
                      }}
                    >
                      {MODE_STYLES[m].icon} {m.toUpperCase()}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <input
              type="text"
              value={workingDescription}
              onChange={e => { setWorkingDescription(e.target.value); setIsDirty(true); }}
              placeholder="Description (optional)"
              style={{
                width: '100%',
                background: 'rgba(255,255,255,0.03)',
                border: '1px solid rgba(255,255,255,0.07)',
                borderRadius: 6,
                color: 'rgba(255,255,255,0.5)',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 12,
                padding: '6px 12px',
                outline: 'none',
                boxSizing: 'border-box',
              }}
            />
          </div>

          {/* Category sections */}
          {Object.entries(grouped).map(([category, vars]) => (
            <CollapsibleSection key={category} category={category}>
              {vars.map(varDef => (
                <ConfigWidget
                  key={varDef.key}
                  def={varDef}
                  value={workingConfig[varDef.key] ?? varDef.default}
                  onChange={handleWidgetChange}
                  vpinData={vpinData}
                />
              ))}
            </CollapsibleSection>
          ))}
        </div>

        {/* ── Config Library ─────────────────────────────────────────────── */}
        <div style={{ position: 'sticky', top: 24 }}>
          <div style={{
            background: 'rgba(255,255,255,0.015)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10,
            overflow: 'hidden',
          }}>
            {/* Library header */}
            <div style={{
              padding: '12px 14px',
              borderBottom: '1px solid rgba(255,255,255,0.06)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}>
              <span style={{
                color: 'rgba(255,255,255,0.7)',
                fontFamily: 'IBM Plex Mono, monospace',
                fontSize: 11,
                fontWeight: 700,
                letterSpacing: '0.06em',
              }}>
                CONFIG LIBRARY
              </span>
              <span style={{
                color: 'rgba(255,255,255,0.25)',
                fontSize: 11,
              }}>
                {filteredConfigs.length} configs
              </span>
            </div>

            {/* Mode filter */}
            <div style={{
              display: 'flex',
              padding: '8px 14px',
              gap: 6,
              borderBottom: '1px solid rgba(255,255,255,0.04)',
            }}>
              {['all', 'paper', 'live'].map(f => (
                <button
                  key={f}
                  onClick={() => setFilterMode(f)}
                  style={{
                    flex: 1,
                    padding: '4px',
                    borderRadius: 4,
                    border: `1px solid ${filterMode === f ? 'rgba(168,85,247,0.3)' : 'rgba(255,255,255,0.06)'}`,
                    background: filterMode === f ? 'rgba(168,85,247,0.1)' : 'transparent',
                    color: filterMode === f ? '#a855f7' : 'rgba(255,255,255,0.3)',
                    fontSize: 10,
                    cursor: 'pointer',
                    fontFamily: 'IBM Plex Mono, monospace',
                    fontWeight: 600,
                    transition: 'all 150ms',
                  }}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>

            {/* Config list */}
            <div style={{ padding: 10, maxHeight: 520, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
              {filteredConfigs.length === 0 ? (
                <div style={{ color: 'rgba(255,255,255,0.25)', fontSize: 12, padding: '20px 4px', textAlign: 'center' }}>
                  No configs yet.<br />
                  <button
                    onClick={() => setShowNewModal(true)}
                    style={{
                      marginTop: 8,
                      background: 'none',
                      border: 'none',
                      color: '#a855f7',
                      fontSize: 12,
                      cursor: 'pointer',
                    }}
                  >
                    + Create your first config
                  </button>
                </div>
              ) : (
                filteredConfigs.map(config => (
                  <ConfigCard
                    key={config.id}
                    config={config}
                    isLoaded={loadedConfigId === config.id}
                    onLoad={handleLoadConfig}
                    onClone={handleClone}
                    onDelete={handleDelete}
                    onApprove={c => setApproveTarget(c)}
                    onActivate={handleActivate}
                  />
                ))
              )}
            </div>
          </div>

          {/* Live status summary */}
          <div style={{
            marginTop: 12,
            background: 'rgba(255,255,255,0.015)',
            border: '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10,
            padding: '12px 14px',
          }}>
            <div style={{
              color: 'rgba(255,255,255,0.4)',
              fontFamily: 'IBM Plex Mono, monospace',
              fontSize: 10,
              fontWeight: 700,
              letterSpacing: '0.06em',
              marginBottom: 10,
            }}>
              GO-LIVE CHECKLIST
            </div>

            {[
              { label: 'API keys configured', ok: liveStatus.api_keys_configured },
              { label: 'Live config approved', ok: liveStatus.live_has_approved_config },
              { label: 'Live config active', ok: !!liveStatus.active_live_config },
            ].map((item, i) => (
              <div key={i} style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                padding: '5px 0',
                borderBottom: i < 2 ? '1px solid rgba(255,255,255,0.04)' : 'none',
              }}>
                <span style={{ fontSize: 12, color: item.ok ? '#4ade80' : 'rgba(248,113,113,0.6)' }}>
                  {item.ok ? '✓' : '✗'}
                </span>
                <span style={{
                  color: item.ok ? 'rgba(255,255,255,0.6)' : 'rgba(255,255,255,0.3)',
                  fontSize: 11,
                }}>
                  {item.label}
                </span>
              </div>
            ))}

            <div style={{
              marginTop: 10,
              padding: '8px 10px',
              borderRadius: 6,
              background: liveStatus.can_go_live
                ? 'rgba(74,222,128,0.08)'
                : 'rgba(248,113,113,0.05)',
              border: `1px solid ${liveStatus.can_go_live ? 'rgba(74,222,128,0.2)' : 'rgba(248,113,113,0.15)'}`,
              color: liveStatus.can_go_live ? '#4ade80' : '#f87171',
              fontSize: 11,
              fontFamily: 'IBM Plex Mono, monospace',
              textAlign: 'center',
            }}>
              {liveStatus.can_go_live
                ? '✓ Ready for live trading'
                : '✗ Not ready for live trading'}
            </div>
          </div>
        </div>
      </div>

      {/* ── Modals ───────────────────────────────────────────────────────── */}
      {showNewModal && (
        <NewConfigModal
          onClose={() => setShowNewModal(false)}
          onSave={handleNewConfig}
          defaults={defaults}
        />
      )}

      {approveTarget && (
        <ApproveModal
          config={approveTarget}
          onClose={() => setApproveTarget(null)}
          onConfirm={handleApprove}
        />
      )}

      <style>{`
        @keyframes fadeSlideIn {
          from { opacity: 0; transform: translateY(-10px) scale(0.97); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }
        input[type=range] {
          -webkit-appearance: none;
          height: 4px;
          background: rgba(255,255,255,0.1);
          border-radius: 2px;
        }
        input[type=range]::-webkit-slider-thumb {
          -webkit-appearance: none;
          width: 14px;
          height: 14px;
          border-radius: 50%;
          background: #a855f7;
          cursor: pointer;
          box-shadow: 0 0 6px rgba(168,85,247,0.5);
          transition: box-shadow 150ms;
        }
        input[type=range]:hover::-webkit-slider-thumb {
          box-shadow: 0 0 10px rgba(168,85,247,0.8);
        }
        input[type=range]:disabled::-webkit-slider-thumb {
          background: rgba(255,255,255,0.2);
          cursor: default;
        }
        input[type=number]::-webkit-inner-spin-button { opacity: 0.3; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
      `}</style>
    </div>
  );
}
