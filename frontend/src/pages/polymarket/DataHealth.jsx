import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { useApi } from '../../hooks/useApi.js';
import { T } from './components/theme.js';
import { DATA_SOURCES, DATA_SOURCE_FIELDS } from '../../constants/strategies.js';

/**
 * Data Surface Health — Dashboard showing freshness of each data source
 * feeding the FullDataSurface.
 *
 * Per-source card: name, last update time, staleness indicator.
 * Fields covered per source.
 * Overall health score.
 *
 * Route: /polymarket/data-health
 *
 * TODO: Connect to GET /api/data-surface/health endpoint when it exists.
 * Currently uses static source definitions + /api/v58/hq for limited
 * live data (current_price timestamp, v4 snapshot age).
 */

const POLL_MS = 5000;

const S = {
  page: {
    minHeight: '100vh', background: T.bg, color: T.text,
    padding: '16px 20px', fontFamily: T.mono,
  },
  header: {
    marginBottom: 20,
  },
  title: { fontSize: 16, fontWeight: 800, color: T.text },
  subtitle: { fontSize: 10, color: T.textMuted, letterSpacing: '0.06em', marginTop: 4 },
  healthBar: {
    display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16,
    padding: '12px 16px', background: T.card, border: `1px solid ${T.cardBorder}`,
    borderRadius: 6,
  },
  healthScore: (score) => ({
    fontSize: 28, fontWeight: 800, fontFamily: T.mono,
    color: score >= 80 ? T.green : score >= 50 ? T.amber : T.red,
  }),
  healthLabel: {
    fontSize: 10, color: T.textMuted, letterSpacing: '0.06em',
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
    gap: 10,
  },
  card: (status) => ({
    background: T.card,
    border: `1px solid ${status === 'healthy' ? 'rgba(16,185,129,0.3)' : status === 'stale' ? 'rgba(245,158,11,0.3)' : 'rgba(239,68,68,0.3)'}`,
    borderRadius: 6, padding: 14,
    borderLeft: `3px solid ${status === 'healthy' ? T.green : status === 'stale' ? T.amber : T.red}`,
  }),
  cardHeader: {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    marginBottom: 8,
  },
  sourceName: {
    fontSize: 11, fontWeight: 700, color: T.text, fontFamily: T.mono,
  },
  statusDot: (status) => ({
    width: 8, height: 8, borderRadius: '50%',
    background: status === 'healthy' ? T.green : status === 'stale' ? T.amber : T.red,
    display: 'inline-block',
  }),
  metaRow: {
    display: 'flex', gap: 12, marginBottom: 6, flexWrap: 'wrap',
  },
  metaItem: {
    fontSize: 9, color: T.textMuted, fontFamily: T.mono,
  },
  metaValue: {
    fontSize: 9, color: T.text, fontFamily: T.mono, fontWeight: 600,
  },
  fieldList: {
    marginTop: 8, paddingTop: 8, borderTop: `1px solid ${T.cardBorder}`,
  },
  fieldLabel: {
    fontSize: 8, fontWeight: 600, color: T.cyan, letterSpacing: '0.06em',
    marginBottom: 4,
  },
  fieldChips: {
    display: 'flex', gap: 3, flexWrap: 'wrap',
  },
  fieldChip: {
    fontSize: 7, padding: '2px 5px', borderRadius: 2,
    background: 'rgba(6,182,212,0.08)', color: T.cyan,
    fontFamily: T.mono,
  },
  lastUpdate: (status) => ({
    fontSize: 9, fontWeight: 600, fontFamily: T.mono,
    color: status === 'healthy' ? T.green : status === 'stale' ? T.amber : T.red,
  }),
};

function getStatus(lastUpdateMs, staleAfterMs) {
  if (lastUpdateMs == null) return 'unknown';
  const age = Date.now() - lastUpdateMs;
  if (age <= staleAfterMs) return 'healthy';
  if (age <= staleAfterMs * 3) return 'stale';
  return 'dead';
}

function fmtAgo(ms) {
  if (ms == null) return 'unknown';
  const age = Date.now() - ms;
  if (age < 1000) return 'just now';
  if (age < 60000) return `${Math.floor(age / 1000)}s ago`;
  if (age < 3600000) return `${Math.floor(age / 60000)}m ago`;
  return `${Math.floor(age / 3600000)}h ago`;
}

function SourceCard({ sourceId, config, lastUpdate }) {
  const status = getStatus(lastUpdate, config.staleAfterMs);
  const fields = DATA_SOURCE_FIELDS[sourceId] || [];

  return (
    <div style={S.card(status)}>
      <div style={S.cardHeader}>
        <span style={S.sourceName}>{config.label}</span>
        <span style={S.statusDot(status)} title={status} />
      </div>

      <div style={S.metaRow}>
        <span style={S.metaItem}>
          Expected: <span style={S.metaValue}>{config.expectedHz}Hz</span>
        </span>
        <span style={S.metaItem}>
          Stale: <span style={S.metaValue}>{config.staleAfterMs / 1000}s</span>
        </span>
      </div>

      <div style={S.metaRow}>
        <span style={S.metaItem}>
          Last: <span style={S.lastUpdate(status)}>{fmtAgo(lastUpdate)}</span>
        </span>
      </div>

      {fields.length > 0 && (
        <div style={S.fieldList}>
          <div style={S.fieldLabel}>FIELDS</div>
          <div style={S.fieldChips}>
            {fields.map(f => (
              <span key={f} style={S.fieldChip}>{f}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default function DataHealth() {
  const api = useApi();
  const [lastUpdates, setLastUpdates] = useState({});
  const [loading, setLoading] = useState(true);

  const fetchHealth = useCallback(async () => {
    if (!api) return;

    // TODO: Replace with GET /api/data-surface/health when endpoint exists.
    // For now, derive partial staleness from /api/v58/hq response.
    try {
      const hq = await api.get('/api/v58/hq');
      const now = Date.now();
      const updates = {};

      // Binance WS — derive from current_price presence
      if (hq?.current_price) {
        updates.binance_ws = now; // We got data, so it's fresh
      }

      // V4 snapshot — derive from v4_snapshot
      if (hq?.v4_snapshot) {
        const snapAge = hq.v4_snapshot?.assembled_at
          ? hq.v4_snapshot.assembled_at * 1000
          : now - 3000; // assume 3s ago if present
        updates.v4_snapshot = snapAge;
      }

      // CLOB — from gate heartbeat
      if (hq?.gate_heartbeat?.[0]?.gate_results?.clob_up_bid != null) {
        updates.clob = now;
      }

      // VPIN — from vpin field
      if (hq?.vpin != null) {
        updates.vpin = now;
      }

      // CoinGlass — from coinglass snapshot
      if (hq?.coinglass || hq?.cg_snapshot) {
        updates.coinglass = now - 5000; // Assume slightly stale
      }

      // Tiingo and Chainlink — if deltas present
      if (hq?.delta_tiingo != null || hq?.v4_snapshot?.delta_tiingo != null) {
        updates.tiingo = now - 2000;
      }
      if (hq?.delta_chainlink != null || hq?.v4_snapshot?.delta_chainlink != null) {
        updates.chainlink = now - 5000;
      }

      // V3 composite — check if any v3 data present
      if (hq?.v4_snapshot?.v3_5m_composite != null) {
        updates.v3_composite = now - 5000;
      }

      setLastUpdates(prev => ({ ...prev, ...updates }));
    } catch { /* endpoint may not be available */ }

    setLoading(false);
  }, [api]);

  useEffect(() => {
    fetchHealth();
    const iv = setInterval(fetchHealth, POLL_MS);
    return () => clearInterval(iv);
  }, [fetchHealth]);

  // Calculate overall health score
  const healthScore = useMemo(() => {
    const sourceIds = Object.keys(DATA_SOURCES);
    if (sourceIds.length === 0) return 0;
    let healthy = 0;
    for (const id of sourceIds) {
      const status = getStatus(lastUpdates[id], DATA_SOURCES[id].staleAfterMs);
      if (status === 'healthy') healthy += 1;
      else if (status === 'stale') healthy += 0.5;
    }
    return Math.round((healthy / sourceIds.length) * 100);
  }, [lastUpdates]);

  const sourceEntries = Object.entries(DATA_SOURCES);

  return (
    <div style={S.page}>
      <div style={S.header}>
        <div style={S.title}>Data Surface Health</div>
        <div style={S.subtitle}>
          Freshness monitoring for {sourceEntries.length} data sources feeding the FullDataSurface
        </div>
      </div>

      {/* Overall health bar */}
      <div style={S.healthBar}>
        <div style={S.healthScore(healthScore)}>{healthScore}%</div>
        <div>
          <div style={{ fontSize: 11, fontWeight: 700, color: T.text }}>Overall Health</div>
          <div style={S.healthLabel}>
            {sourceEntries.filter(([id]) => getStatus(lastUpdates[id], DATA_SOURCES[id].staleAfterMs) === 'healthy').length} healthy
            {' \u00b7 '}
            {sourceEntries.filter(([id]) => getStatus(lastUpdates[id], DATA_SOURCES[id].staleAfterMs) === 'stale').length} stale
            {' \u00b7 '}
            {sourceEntries.filter(([id]) => {
              const s = getStatus(lastUpdates[id], DATA_SOURCES[id].staleAfterMs);
              return s === 'dead' || s === 'unknown';
            }).length} unknown/dead
          </div>
        </div>
      </div>

      {loading && (
        <div style={{ textAlign: 'center', padding: 40, color: T.textMuted }}>
          Loading health data...
        </div>
      )}

      {/* Source cards grid */}
      <div style={S.grid}>
        {sourceEntries.map(([id, config]) => (
          <SourceCard
            key={id}
            sourceId={id}
            config={config}
            lastUpdate={lastUpdates[id]}
          />
        ))}
      </div>

      {/* Architecture note */}
      <div style={{
        marginTop: 20, padding: 12, borderRadius: 4,
        background: 'rgba(6,182,212,0.06)', border: `1px solid rgba(6,182,212,0.15)`,
        fontSize: 9, color: T.textMuted, lineHeight: 1.5,
      }}>
        <strong style={{ color: T.cyan }}>Strategy Engine v2 (CA-07):</strong>{' '}
        When the DataSurfaceManager lands, this page will use a dedicated{' '}
        <code style={{ color: T.cyan }}>GET /api/data-surface/health</code>{' '}
        endpoint that reports per-source freshness with millisecond precision.
        Current data is estimated from /api/v58/hq response fields.
      </div>
    </div>
  );
}
