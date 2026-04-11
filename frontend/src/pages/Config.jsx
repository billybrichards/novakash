/**
 * Config.jsx — CFG-02/03/05 read-only DB-config browser.
 *
 * Surfaces all 142+ DB-managed config keys across services (engine,
 * margin_engine, hub, data-collector, macro-observer) in one
 * sidebar-tabbed page. Read-only in this PR; write access ships in CFG-04
 * (which adds the POST /api/v58/config/upsert endpoint and the
 * "edit mode" toggle).
 *
 * Backend: hub/api/config_v2.py
 *   GET /api/v58/config/services
 *   GET /api/v58/config?service=engine
 *   GET /api/v58/config/schema?service=engine
 *   GET /api/v58/config/history?service=engine&key=V10_6_ENABLED
 *
 * Sister pages:
 *   /trading-config        — legacy 25-key bundle editor (TradingConfig.jsx)
 *   /legacy-config         — old 13-key minimal page (LegacyConfig.jsx)
 *
 * The legacy /config-name nav entry now points HERE; bookmarks for the
 * 13-key page are still reachable at /legacy-config.
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
  white: '#fff',
  mono: "'JetBrains Mono', 'Fira Code', monospace",
};

const SERVICE_LABEL = {
  engine: 'Engine (Polymarket 5m)',
  margin_engine: 'Margin Engine (Hyperliquid)',
  hub: 'Hub (API gateway)',
  'data-collector': 'Data Collector',
  'macro-observer': 'Macro Observer',
  timesfm: 'TimesFM (read-only)',
};

const TYPE_COLOR = {
  bool: T.purple,
  int: T.cyan,
  float: T.cyan,
  string: T.green,
  enum: T.amber,
  csv: T.green,
};

// ─── Small primitives ─────────────────────────────────────────────────────

function Chip({ color, children, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 3,
        fontSize: 9,
        fontWeight: 700,
        padding: '2px 6px',
        borderRadius: 3,
        background: `${color}22`,
        color,
        border: `1px solid ${color}55`,
        fontFamily: T.mono,
        letterSpacing: '0.04em',
      }}
    >
      {children}
    </span>
  );
}

function TypeBadge({ type }) {
  const color = TYPE_COLOR[type] || T.textMuted;
  return <Chip color={color}>{(type || 'string').toUpperCase()}</Chip>;
}

function RestartChip() {
  return (
    <Chip color={T.amber} title="Service restart required for change to take effect (CFG-07b)">
      RESTART
    </Chip>
  );
}

function NotEditableChip() {
  return (
    <Chip color={T.textMuted} title="Surfaced read-only; not editable from the UI">
      READ-ONLY
    </Chip>
  );
}

function fmtCurrentValue(v, type) {
  if (v === null || v === undefined || v === '') return '—';
  if (type === 'bool') return v ? 'true' : 'false';
  return String(v);
}

// ─── Banner ───────────────────────────────────────────────────────────────

function PhaseBanner() {
  return (
    <div
      style={{
        background: 'rgba(168, 85, 247, 0.08)',
        border: '1px solid rgba(168, 85, 247, 0.35)',
        borderRadius: 6,
        padding: '10px 14px',
        marginBottom: 16,
        color: T.text,
        fontSize: 12,
        lineHeight: 1.5,
        fontFamily: T.mono,
      }}
    >
      <span style={{ color: T.purple, fontWeight: 700 }}>CFG-02 / CFG-03 / CFG-05</span>{' '}
      — read-only schema view. Write access ships in <strong>CFG-04</strong> (next PR).
      Every key shown here is a row in the new <code>config_keys</code> +{' '}
      <code>config_values</code> tables. Edits via this page will land once the write
      endpoints + admin claim are wired. See{' '}
      <code>docs/CONFIG_MIGRATION_PLAN.md §10</code> for the phasing.
    </div>
  );
}

// ─── Sidebar ──────────────────────────────────────────────────────────────

function ServiceSidebar({ services, activeService, onSelect, loading }) {
  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 8,
        padding: 10,
        position: 'sticky',
        top: 16,
      }}
    >
      <div
        style={{
          fontSize: 9,
          color: T.textMuted,
          fontWeight: 700,
          letterSpacing: '0.1em',
          padding: '4px 8px 8px',
          fontFamily: T.mono,
        }}
      >
        SERVICES
      </div>
      {loading && (
        <div style={{ color: T.textMuted, fontSize: 11, padding: 8 }}>loading…</div>
      )}
      {!loading && services.length === 0 && (
        <div style={{ color: T.textMuted, fontSize: 11, padding: 8 }}>
          no services found — has the hub seeded the config_keys table?
        </div>
      )}
      {services.map((svc) => {
        const isActive = svc.service === activeService;
        return (
          <button
            type="button"
            key={svc.service}
            onClick={() => onSelect(svc.service)}
            style={{
              display: 'flex',
              width: '100%',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '8px 10px',
              marginBottom: 2,
              background: isActive ? 'rgba(6, 182, 212, 0.1)' : 'transparent',
              border: `1px solid ${isActive ? T.cyan : 'transparent'}`,
              borderRadius: 4,
              color: isActive ? T.cyan : T.text,
              fontSize: 12,
              fontFamily: T.mono,
              cursor: 'pointer',
              textAlign: 'left',
            }}
          >
            <span>
              {SERVICE_LABEL[svc.service] || svc.service}
            </span>
            <span
              style={{
                fontSize: 10,
                color: T.textMuted,
                fontWeight: 700,
                marginLeft: 8,
              }}
            >
              {svc.key_count}
            </span>
          </button>
        );
      })}
    </div>
  );
}

// ─── Filters ──────────────────────────────────────────────────────────────

function FilterBar({
  searchText,
  setSearchText,
  categoryFilter,
  setCategoryFilter,
  categories,
  totalKeys,
  visibleKeys,
}) {
  return (
    <div
      style={{
        display: 'flex',
        gap: 10,
        alignItems: 'center',
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: '8px 12px',
        marginBottom: 12,
        flexWrap: 'wrap',
      }}
    >
      <input
        type="text"
        placeholder="filter by key name or description…"
        value={searchText}
        onChange={(e) => setSearchText(e.target.value)}
        style={{
          flex: '1 1 240px',
          minWidth: 200,
          background: 'rgba(255, 255, 255, 0.04)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 4,
          padding: '6px 10px',
          color: T.text,
          fontFamily: T.mono,
          fontSize: 11,
          outline: 'none',
        }}
      />
      <select
        value={categoryFilter}
        onChange={(e) => setCategoryFilter(e.target.value)}
        style={{
          background: 'rgba(255, 255, 255, 0.04)',
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 4,
          padding: '6px 10px',
          color: T.text,
          fontFamily: T.mono,
          fontSize: 11,
          outline: 'none',
        }}
      >
        <option value="">all categories</option>
        {categories.map((cat) => (
          <option key={cat} value={cat}>
            {cat}
          </option>
        ))}
      </select>
      <span
        style={{
          color: T.textMuted,
          fontSize: 10,
          fontFamily: T.mono,
          marginLeft: 'auto',
        }}
      >
        showing {visibleKeys}/{totalKeys} keys
      </span>
    </div>
  );
}

// ─── Per-key row ──────────────────────────────────────────────────────────

function KeyRow({ keyData }) {
  const [expanded, setExpanded] = useState(false);
  const isAtDefault = keyData.is_at_default;
  const currentDisplay = fmtCurrentValue(keyData.current_value, keyData.type);
  const defaultDisplay = fmtCurrentValue(keyData.default_value, keyData.type);

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderRadius: 6,
        padding: 10,
        marginBottom: 6,
      }}
    >
      <div
        onClick={() => setExpanded((v) => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          cursor: 'pointer',
          flexWrap: 'wrap',
        }}
      >
        <code
          style={{
            color: T.cyan,
            fontFamily: T.mono,
            fontSize: 11,
            fontWeight: 600,
            flex: '0 0 auto',
          }}
        >
          {keyData.key}
        </code>
        <TypeBadge type={keyData.type} />
        {keyData.restart_required && <RestartChip />}
        {!keyData.editable_via_ui && <NotEditableChip />}
        <span
          style={{
            marginLeft: 'auto',
            fontFamily: T.mono,
            fontSize: 11,
            color: isAtDefault ? T.textMuted : T.amber,
            fontWeight: 600,
          }}
          title={isAtDefault ? 'value is at default' : 'value differs from default'}
        >
          {currentDisplay}
        </span>
      </div>
      {expanded && (
        <div
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: `1px solid ${T.cardBorder}`,
            fontSize: 11,
            color: T.text,
            lineHeight: 1.5,
          }}
        >
          <div style={{ marginBottom: 6 }}>{keyData.description}</div>
          <div
            style={{
              display: 'flex',
              gap: 16,
              flexWrap: 'wrap',
              fontFamily: T.mono,
              fontSize: 10,
              color: T.textMuted,
            }}
          >
            <span>
              <span style={{ color: T.textDim }}>default:</span>{' '}
              <span style={{ color: T.text }}>{defaultDisplay}</span>
            </span>
            <span>
              <span style={{ color: T.textDim }}>category:</span>{' '}
              <span style={{ color: T.text }}>{keyData.category}</span>
            </span>
            {keyData.set_by && (
              <span>
                <span style={{ color: T.textDim }}>last set by:</span>{' '}
                <span style={{ color: T.text }}>{keyData.set_by}</span>
              </span>
            )}
            {keyData.set_at && (
              <span>
                <span style={{ color: T.textDim }}>at:</span>{' '}
                <span style={{ color: T.text }}>{keyData.set_at}</span>
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Category section ─────────────────────────────────────────────────────

function CategorySection({ category, keys }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div style={{ marginBottom: 18 }}>
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        style={{
          width: '100%',
          textAlign: 'left',
          background: T.headerBg,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          padding: '8px 12px',
          color: T.text,
          fontFamily: T.mono,
          fontSize: 11,
          fontWeight: 700,
          letterSpacing: '0.05em',
          textTransform: 'uppercase',
          marginBottom: 6,
          cursor: 'pointer',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
        }}
      >
        <span>{collapsed ? '▸' : '▾'} {category}</span>
        <span style={{ color: T.textMuted, fontWeight: 600 }}>{keys.length}</span>
      </button>
      {!collapsed && keys.map((k) => <KeyRow key={k.key} keyData={k} />)}
    </div>
  );
}

// ─── Main Config page ─────────────────────────────────────────────────────

export default function Config() {
  const api = useApi();

  const [services, setServices] = useState([]);
  const [servicesLoading, setServicesLoading] = useState(true);
  const [activeService, setActiveService] = useState(null);

  const [serviceData, setServiceData] = useState(null);
  const [dataLoading, setDataLoading] = useState(false);
  const [error, setError] = useState(null);

  const [searchText, setSearchText] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');

  // Fetch the list of services on mount
  useEffect(() => {
    let cancelled = false;
    setServicesLoading(true);
    api
      .get('/api/v58/config/services')
      .then((res) => {
        if (cancelled) return;
        const list = res.data?.services || [];
        setServices(list);
        if (list.length > 0 && !activeService) {
          setActiveService(list[0].service);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setError(
          err.response?.data?.detail ||
            'failed to load /api/v58/config/services'
        );
      })
      .finally(() => {
        if (!cancelled) setServicesLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // activeService intentionally omitted — only re-fetch services on first mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch service data whenever the active service changes
  useEffect(() => {
    if (!activeService) return;
    let cancelled = false;
    setDataLoading(true);
    setError(null);
    api
      .get(`/api/v58/config?service=${encodeURIComponent(activeService)}`)
      .then((res) => {
        if (cancelled) return;
        setServiceData(res.data);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(
          err.response?.data?.detail ||
            `failed to load config for service=${activeService}`
        );
      })
      .finally(() => {
        if (!cancelled) setDataLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [api, activeService]);

  // Compute filtered categories + keys
  const filteredCategories = useMemo(() => {
    if (!serviceData?.categories) return [];
    const q = searchText.trim().toLowerCase();
    return serviceData.categories
      .filter((c) => !categoryFilter || c.id === categoryFilter)
      .map((c) => ({
        ...c,
        keys: c.keys.filter((k) => {
          if (!q) return true;
          return (
            (k.key || '').toLowerCase().includes(q) ||
            (k.description || '').toLowerCase().includes(q)
          );
        }),
      }))
      .filter((c) => c.keys.length > 0);
  }, [serviceData, searchText, categoryFilter]);

  const totalKeys = serviceData?.key_count || 0;
  const visibleKeys = useMemo(
    () => filteredCategories.reduce((sum, c) => sum + c.keys.length, 0),
    [filteredCategories]
  );

  const allCategoryIds = useMemo(() => {
    if (!serviceData?.categories) return [];
    return serviceData.categories.map((c) => c.id).sort();
  }, [serviceData]);

  return (
    <div style={{ color: T.text, paddingBottom: 40 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          justifyContent: 'space-between',
          marginBottom: 16,
          flexWrap: 'wrap',
          gap: 8,
        }}
      >
        <div>
          <h1
            style={{
              fontSize: 22,
              fontWeight: 800,
              color: T.white,
              margin: 0,
              fontFamily: T.mono,
              letterSpacing: '0.02em',
            }}
          >
            DB Config Browser
          </h1>
          <div
            style={{
              color: T.textMuted,
              fontSize: 11,
              marginTop: 4,
              fontFamily: T.mono,
            }}
          >
            CFG-02/03/05 — DB-backed config schema, read-only frontend.
            Covers all services: Engine (Polymarket 5m), Margin Engine (Hyperliquid Perps), Hub, Data Collector, Macro Observer.
            <span style={{ color: T.textDim, marginLeft: 6 }}>Data: GET /api/v58/config (175 keys)</span>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Chip color={T.cyan}>READ-ONLY</Chip>
          <Chip color={T.purple}>v1</Chip>
          <Chip color={T.cyan} title="Config spans both Polymarket and Hyperliquid trading strategies">POLY + PERPS</Chip>
        </div>
      </div>

      <PhaseBanner />

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '240px 1fr',
          gap: 16,
        }}
      >
        <ServiceSidebar
          services={services}
          activeService={activeService}
          onSelect={setActiveService}
          loading={servicesLoading}
        />

        <div>
          {error && (
            <div
              style={{
                background: 'rgba(239, 68, 68, 0.08)',
                border: '1px solid rgba(239, 68, 68, 0.35)',
                color: T.red,
                padding: '8px 12px',
                borderRadius: 6,
                fontSize: 11,
                fontFamily: T.mono,
                marginBottom: 12,
              }}
            >
              {error}
            </div>
          )}

          {!error && (
            <FilterBar
              searchText={searchText}
              setSearchText={setSearchText}
              categoryFilter={categoryFilter}
              setCategoryFilter={setCategoryFilter}
              categories={allCategoryIds}
              totalKeys={totalKeys}
              visibleKeys={visibleKeys}
            />
          )}

          {dataLoading && (
            <div
              style={{
                color: T.textMuted,
                fontSize: 12,
                padding: 16,
                textAlign: 'center',
              }}
            >
              loading config keys for {activeService}…
            </div>
          )}

          {!dataLoading && !error && filteredCategories.length === 0 && (
            <div
              style={{
                color: T.textMuted,
                fontSize: 12,
                padding: 16,
                textAlign: 'center',
                fontFamily: T.mono,
              }}
            >
              no keys match the current filter
              {totalKeys === 0 && ' — service has no DB-managed keys yet'}
            </div>
          )}

          {!dataLoading &&
            filteredCategories.map((cat) => (
              <CategorySection key={cat.id} category={cat.id} keys={cat.keys} />
            ))}
        </div>
      </div>
    </div>
  );
}
