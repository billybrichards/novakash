/**
 * Deployments.jsx — AWS services overview.
 *
 * Single source-of-truth page listing every service, where it runs,
 * how it deploys, and its live health. Static data (duplicated from
 * docs/CI_CD.md) with a handful of live probes layered on top via
 * existing hub endpoints.
 *
 * Render at /deployments. Link added to the sidebar under SYSTEM.
 *
 * The live-probe layer is minimal by design: we only poll endpoints
 * that already exist through the hub proxy (/v3/health, /v4/snapshot)
 * so this page adds zero new backend surface. For services that
 * don't have a reachable health endpoint (engine, macro-observer,
 * data-collector), the card shows "static-only" and points at the
 * relevant GitHub Actions workflow for the authoritative state.
 *
 * Once CI-01 lands and the engine error-signature gate is live, a
 * future iteration of this page can display the most recent deploy
 * run's error-signature counts pulled from the GitHub Actions API.
 */

import { useEffect, useRef, useState } from 'react';
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

// ─── Service Registry ─────────────────────────────────────────────────────
// Mirrors docs/CI_CD.md. Update in lockstep when services move.

const SERVICES = [
  {
    id: 'timesfm',
    name: 'timesfm-service',
    icon: '🔮',
    role: 'v2/v3/v4 model inference + fusion snapshot assembler',
    repo: 'novakash-timesfm-repo',
    branch: 'main',
    host: '16.52.14.182',
    hostLabel: 'EC2 ca-central-1 — dedicated c6a.2xlarge (16GB) with Elastic IP',
    workflow: '.github/workflows/ci.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to main',
    healthProbePath: '/v4/snapshot?asset=BTC&timescales=5m',
    healthProbeVia: 'api',
    secretsNeeded: ['DEPLOY_HOST', 'DEPLOY_SSH_KEY', 'COINGLASS_API_KEY', 'TIINGO_API_KEY', 'POLYGON_RPC_URL'],
    notes: 'Primary model service. v2 (LightGBM + quantiles), v3 (composite multiscale), v4 (fusion surface). Port 8080. Dedicated 16GB box (split from old shared 3.98.114.0 on 2026-04-14). All endpoints proxied through hub/api/margin.py.',
  },
  {
    id: 'macro-observer',
    name: 'macro-observer',
    icon: '✦',
    role: 'Qwen 3.5 122B macro bias classifier (per-timescale)',
    repo: 'novakash',
    branch: 'develop',
    host: '16.54.141.121',
    hostLabel: 'EC2 ca-central-1 — dedicated t3.medium (4GB) with Elastic IP, shared with hub + data-collector',
    workflow: '.github/workflows/deploy-macro-observer.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to develop, path macro-observer/**',
    healthProbePath: null,
    secretsNeeded: ['HUB_HOST', 'HUB_SSH_KEY', 'DATABASE_URL', 'QWEN_API_KEY', 'QWEN_BASE_URL', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'],
    notes: 'Runs in Docker on Hub box. Writes to macro_signals table every 60s. Qwen 3.5 swapped in via PR #15 (previously Anthropic). Per-timescale bias added via PR #17. Consumed by /v4/macro in timesfm-service.',
  },
  {
    id: 'data-collector',
    name: 'data-collector',
    icon: '📥',
    role: 'Polymarket market-data snapshot writer',
    repo: 'novakash',
    branch: 'develop',
    host: '16.54.141.121',
    hostLabel: 'EC2 ca-central-1 — dedicated t3.medium (4GB) with Elastic IP, shared with hub + macro-observer',
    workflow: '.github/workflows/deploy-data-collector.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to develop, path data-collector/**',
    healthProbePath: null,
    secretsNeeded: ['HUB_HOST', 'HUB_SSH_KEY', 'DATABASE_URL'],
    notes: 'Writes to market_data + market_snapshots tables (~2.3/sec, 8 writes per ~3.5s Gamma-API-bound cycle). Migrated from Railway to AWS Montreal in PR #24, then to dedicated Hub box in PR #166. Docker container with healthcheck on /tmp/collector.alive (30s window).',
  },
  {
    id: 'margin-engine',
    name: 'margin-engine',
    icon: '🏦',
    role: 'Clean-architecture Hyperliquid/Binance perp trader',
    repo: 'novakash',
    branch: 'develop',
    host: 'eu-west-2 (MARGIN_ENGINE_HOST)',
    hostLabel: 'EC2 London (separate from Montreal)',
    workflow: '.github/workflows/deploy-margin-engine.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to develop, path margin_engine/**',
    healthProbePath: '/margin/status',
    healthProbeVia: 'api',
    secretsNeeded: ['MARGIN_ENGINE_HOST', 'MARGIN_ENGINE_SSH_KEY', 'DATABASE_URL'],
    notes: 'PAPER mode by default (MARGIN_PAPER_MODE=true). v4 fusion gates active (MARGIN_ENGINE_USE_V4_ACTIONS=true since PR #20). systemd service margin-engine.service. 10-gate v4 stack from PR #16.',
  },
  {
    id: 'hub',
    name: 'hub (API)',
    icon: '🌐',
    role: 'FastAPI backend for the frontend dashboard',
    repo: 'novakash',
    branch: 'develop',
    host: '16.54.141.121',
    hostLabel: 'EC2 ca-central-1 — dedicated t3.medium (4GB) with Elastic IP, shared with data-collector + macro-observer',
    workflow: '.github/workflows/deploy-hub.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to develop, path hub/**',
    healthProbePath: '/api/system/status',
    healthProbeVia: 'api',
    secretsNeeded: ['HUB_HOST', 'HUB_SSH_KEY', 'DATABASE_URL', 'JWT_SECRET', 'TIMESFM_HOST', 'MARGIN_ENGINE_HOST'],
    notes: 'Proxies to timesfm + margin-engine + engine via httpx. Handles auth (JWT) and all REST+WS endpoints the frontend talks to. Migrated from Railway to AWS in PR #166. Port 8091.',
  },
  {
    id: 'frontend',
    name: 'frontend (web)',
    icon: '🖥',
    role: 'React dashboard served via nginx',
    repo: 'novakash',
    branch: 'develop',
    host: 'AWS_FRONTEND_HOST',
    hostLabel: 'EC2 + nginx (/var/www/frontend)',
    workflow: '.github/workflows/deploy-frontend.yml',
    workflowStatus: 'active',
    deployTriggers: 'push to develop, path frontend/**',
    healthProbePath: '/',
    healthProbeVia: 'direct',
    secretsNeeded: ['AWS_FRONTEND_HOST', 'AWS_FRONTEND_SSH_KEY'],
    notes: 'Vite + React 18. Deploys via npm ci + npm run build + tar + scp + systemctl restart nginx. Proxies /api/* and /auth/* to Hub at 16.54.141.121:8091.',
  },
  {
    id: 'engine',
    name: 'engine (Polymarket)',
    icon: '⚙',
    role: 'Legacy Polymarket binary-options trading engine (god class)',
    repo: 'novakash',
    branch: 'develop',
    host: '15.223.247.178',
    hostLabel: 'EC2 Montreal novakash-montreal-vnc (i-0785ed930423ae9fd)',
    workflow: '.github/workflows/deploy-engine.yml',
    workflowStatus: 'drafted',
    deployTriggers: 'push to develop, path engine/**|scripts/restart_engine.sh',
    healthProbePath: null,
    secretsNeeded: ['ENGINE_HOST', 'ENGINE_SSH_KEY', 'DATABASE_URL', 'COINGLASS_API_KEY', 'BINANCE_API_KEY', 'BINANCE_API_SECRET', 'POLY_API_KEY', 'POLY_API_SECRET', 'POLY_API_PASSPHRASE', 'POLY_PRIVATE_KEY', 'POLY_FUNDER_ADDRESS', 'POLY_SIGNATURE_TYPE', 'POLY_BTC_TOKEN_IDS', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'],
    notes: 'Runs as raw python3 process under novakash user via scripts/restart_engine.sh (NOT systemd, NOT Docker). VNC box, shared with noVNC desktop on 6080. CI-01 workflow drafted in PR #27 — waiting for ENGINE_HOST + ENGINE_SSH_KEY secrets + first workflow_dispatch run before flipping CI-01 status to DONE. Last investigated crash: 2026-04-11 ~11:05 UTC, host-level DNS outage recovered by reboot at 12:11 UTC.',
  },
];

// ─── Helpers ─────────────────────────────────────────────────────────────

const STATUS_COLORS = {
  active: T.green,      // has GH Actions workflow deploying cleanly
  drafted: T.amber,     // workflow written but not yet firing / needs secrets
  legacy: T.orange,     // on Railway still, no GH Actions workflow
};

const STATUS_LABELS = {
  active: 'CI/CD ACTIVE',
  drafted: 'CI/CD DRAFTED',
  legacy: 'LEGACY (Railway)',
};

function Chip({ color, bg, border, label, value, title }) {
  return (
    <span
      title={title}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        fontSize: 9,
        fontWeight: 800,
        padding: '3px 7px',
        borderRadius: 3,
        background: bg,
        color,
        border: `1px solid ${border}`,
        fontFamily: T.mono,
        letterSpacing: '0.04em',
        textTransform: 'uppercase',
        whiteSpace: 'nowrap',
      }}
    >
      {label && <span style={{ opacity: 0.65 }}>{label}</span>}
      <span>{value}</span>
    </span>
  );
}

function HealthDot({ state, title }) {
  const color =
    state === 'ok' ? T.green
    : state === 'warn' ? T.amber
    : state === 'error' ? T.red
    : T.textDim;
  return (
    <span
      title={title}
      style={{
        display: 'inline-block',
        width: 8,
        height: 8,
        borderRadius: '50%',
        background: color,
        boxShadow: state === 'ok' ? `0 0 6px ${color}88` : 'none',
        animation: state === 'pending' ? 'pulse 2s infinite' : 'none',
      }}
    />
  );
}

// ─── Service Card ────────────────────────────────────────────────────────

function ServiceCard({ service, liveHealth }) {
  const statusColor = STATUS_COLORS[service.workflowStatus] || T.textMuted;
  const statusLabel = STATUS_LABELS[service.workflowStatus] || service.workflowStatus;
  const [expanded, setExpanded] = useState(false);

  const health = liveHealth[service.id];
  const healthState = !service.healthProbePath
    ? 'unknown'
    : health?.loading
      ? 'pending'
      : health?.error
        ? 'error'
        : health?.ok
          ? 'ok'
          : 'warn';

  return (
    <div
      style={{
        background: T.card,
        border: `1px solid ${T.cardBorder}`,
        borderLeft: `3px solid ${statusColor}`,
        borderRadius: 8,
        overflow: 'hidden',
      }}
    >
      {/* Header */}
      <div
        onClick={() => setExpanded(!expanded)}
        style={{
          cursor: 'pointer',
          padding: '12px 14px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 12,
          borderBottom: expanded ? `1px solid ${T.cardBorder}` : 'none',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 18 }}>{service.icon}</span>
            <span
              style={{
                fontSize: 13,
                fontWeight: 800,
                color: T.white,
                fontFamily: T.mono,
              }}
            >
              {service.name}
            </span>
            <Chip
              color={statusColor}
              bg={`${statusColor}1a`}
              border={`${statusColor}55`}
              value={statusLabel}
              title={`workflow status: ${service.workflowStatus}`}
            />
            {service.healthProbePath && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                <HealthDot
                  state={healthState}
                  title={
                    healthState === 'ok'
                      ? `healthy · last probe ${health?.latencyMs}ms`
                      : healthState === 'error'
                        ? `error: ${health?.error}`
                        : healthState === 'pending'
                          ? 'probing...'
                          : 'warn'
                  }
                />
                <span
                  style={{
                    fontSize: 9,
                    fontFamily: T.mono,
                    color: T.textMuted,
                  }}
                >
                  {healthState === 'ok'
                    ? `${health?.latencyMs}ms`
                    : healthState === 'error'
                      ? 'ERROR'
                      : healthState === 'pending'
                        ? '...'
                        : ''}
                </span>
              </span>
            )}
          </div>
          <div style={{ fontSize: 10, color: T.textMuted, lineHeight: 1.4 }}>
            {service.role}
          </div>
          <div
            style={{
              display: 'flex',
              gap: 12,
              marginTop: 6,
              fontSize: 9,
              color: T.textDim,
              fontFamily: T.mono,
              flexWrap: 'wrap',
            }}
          >
            <span>
              <span style={{ color: T.textMuted }}>repo</span>{' '}
              <span style={{ color: T.text }}>{service.repo}</span>{' '}
              <span style={{ color: T.cyan }}>/ {service.branch}</span>
            </span>
            <span>
              <span style={{ color: T.textMuted }}>host</span>{' '}
              <span style={{ color: T.text }}>{service.host}</span>
            </span>
          </div>
        </div>
        <span
          style={{
            fontSize: 10,
            color: T.textDim,
            fontFamily: T.mono,
            flexShrink: 0,
            paddingTop: 4,
          }}
        >
          {expanded ? '▲' : '▼'}
        </span>
      </div>

      {/* Expanded body */}
      {expanded && (
        <div style={{ padding: '14px 14px', fontSize: 10, color: T.text, lineHeight: 1.5 }}>
          <div style={{ marginBottom: 10 }}>
            <span
              style={{
                fontSize: 8,
                fontWeight: 800,
                color: T.textMuted,
                letterSpacing: '0.08em',
              }}
            >
              HOST
            </span>
            <div style={{ marginTop: 2 }}>{service.hostLabel}</div>
          </div>

          <div style={{ marginBottom: 10 }}>
            <span
              style={{
                fontSize: 8,
                fontWeight: 800,
                color: T.textMuted,
                letterSpacing: '0.08em',
              }}
            >
              DEPLOY WORKFLOW
            </span>
            <div style={{ marginTop: 2 }}>
              <code
                style={{
                  background: 'rgba(15,23,42,0.6)',
                  padding: '2px 6px',
                  borderRadius: 3,
                  fontSize: 9,
                  color: T.cyan,
                  fontFamily: T.mono,
                }}
              >
                {service.workflow}
              </code>
            </div>
            <div style={{ marginTop: 4, fontSize: 9, color: T.textMuted, fontFamily: T.mono }}>
              trigger: {service.deployTriggers}
            </div>
          </div>

          {service.healthProbePath && (
            <div style={{ marginBottom: 10 }}>
              <span
                style={{
                  fontSize: 8,
                  fontWeight: 800,
                  color: T.textMuted,
                  letterSpacing: '0.08em',
                }}
              >
                HEALTH PROBE
              </span>
              <div style={{ marginTop: 2, fontFamily: T.mono, fontSize: 9, color: T.cyan }}>
                {service.healthProbeVia === 'direct' ? 'GET' : 'GET /api'}
                {service.healthProbePath}
              </div>
              {health && health.detail && (
                <div style={{ marginTop: 4, fontSize: 9, color: T.textMuted }}>
                  {health.detail}
                </div>
              )}
            </div>
          )}

          <div style={{ marginBottom: 10 }}>
            <span
              style={{
                fontSize: 8,
                fontWeight: 800,
                color: T.textMuted,
                letterSpacing: '0.08em',
              }}
            >
              REQUIRED SECRETS
            </span>
            <div
              style={{
                marginTop: 4,
                display: 'flex',
                flexWrap: 'wrap',
                gap: 4,
              }}
            >
              {service.secretsNeeded.map((s) => (
                <span
                  key={s}
                  style={{
                    fontSize: 8,
                    fontFamily: T.mono,
                    color: T.textMuted,
                    background: 'rgba(15,23,42,0.6)',
                    padding: '2px 6px',
                    borderRadius: 3,
                    border: `1px solid ${T.cardBorder}`,
                  }}
                >
                  {s}
                </span>
              ))}
            </div>
          </div>

          <div>
            <span
              style={{
                fontSize: 8,
                fontWeight: 800,
                color: T.textMuted,
                letterSpacing: '0.08em',
              }}
            >
              NOTES
            </span>
            <div style={{ marginTop: 2, color: T.text }}>{service.notes}</div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────

export default function Deployments() {
  const api = useApi();
  const [liveHealth, setLiveHealth] = useState({});
  const [lastRefresh, setLastRefresh] = useState(null);

  const probeService = async (service) => {
    if (!service.healthProbePath) return;
    const start = Date.now();
    try {
      const path = service.healthProbeVia === 'direct'
        ? service.healthProbePath
        : service.healthProbePath;
      // For api-via probes, useApi handles the /api prefix.
      // For direct probes, fetch the relative path straight off nginx.
      if (service.healthProbeVia === 'direct') {
        const resp = await fetch(path, { method: 'GET' });
        setLiveHealth((h) => ({
          ...h,
          [service.id]: {
            ok: resp.ok,
            latencyMs: Date.now() - start,
            detail: `HTTP ${resp.status}`,
          },
        }));
      } else {
        const res = await api('GET', service.healthProbePath);
        setLiveHealth((h) => ({
          ...h,
          [service.id]: {
            ok: true,
            latencyMs: Date.now() - start,
            detail: typeof res?.data === 'object' ? 'JSON response received' : String(res?.data || ''),
          },
        }));
      }
    } catch (err) {
      setLiveHealth((h) => ({
        ...h,
        [service.id]: {
          ok: false,
          error: err?.response?.status ? `HTTP ${err.response.status}` : err.message || 'unknown',
          latencyMs: Date.now() - start,
        },
      }));
    }
  };

  const probeAll = async () => {
    const withProbe = SERVICES.filter((s) => s.healthProbePath);
    // Mark all as pending first
    setLiveHealth((h) => {
      const next = { ...h };
      for (const s of withProbe) next[s.id] = { ...next[s.id], loading: true };
      return next;
    });
    await Promise.all(withProbe.map(probeService));
    setLastRefresh(new Date());
  };

  useEffect(() => { probeAll(); }, [api]);

  useEffect(() => {
    const interval = setInterval(probeAll, 15000);
    return () => clearInterval(interval);
  }, [api]);

  const activeCount = SERVICES.filter((s) => s.workflowStatus === 'active').length;
  const draftedCount = SERVICES.filter((s) => s.workflowStatus === 'drafted').length;
  const legacyCount = SERVICES.filter((s) => s.workflowStatus === 'legacy').length;

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1200, margin: '0 auto' }}>
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
          Deployments
          <Chip
            color={T.cyan}
            bg="rgba(6,182,212,0.12)"
            border="rgba(6,182,212,0.3)"
            value="AWS services"
          />
          <span style={{ fontSize: 8, fontWeight: 700, padding: '2px 6px', borderRadius: 3, background: 'rgba(100,116,139,0.1)', color: '#64748b', border: '1px solid rgba(100,116,139,0.3)', fontFamily: T.mono, letterSpacing: '0.06em' }}>SYSTEM</span>
        </h1>
        <p
          style={{
            fontSize: 10,
            color: T.textMuted,
            margin: '4px 0 0',
            maxWidth: 900,
            lineHeight: 1.5,
          }}
        >
          Live overview of every service in the novakash system, matching{' '}
          <code
            style={{
              fontFamily: T.mono,
              color: T.cyan,
              background: 'rgba(6,182,212,0.08)',
              padding: '1px 4px',
              borderRadius: 2,
            }}
          >
            docs/CI_CD.md
          </code>
          . Services with a reachable health endpoint through the hub proxy are
          probed every 15s. Services without a probe path (engine,
          macro-observer, data-collector) show their deploy workflow state
          only — the authoritative truth lives in GitHub Actions.
        </p>
      </div>

      {/* Status summary */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
          gap: 8,
          marginBottom: 14,
        }}
      >
        {[
          { label: 'TOTAL', value: SERVICES.length, color: T.text },
          { label: 'CI/CD ACTIVE', value: activeCount, color: T.green },
          { label: 'CI/CD DRAFTED', value: draftedCount, color: T.amber },
          { label: 'LEGACY (Railway)', value: legacyCount, color: T.orange },
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
            <div
              style={{
                fontSize: 18,
                fontWeight: 900,
                fontFamily: T.mono,
                color,
              }}
            >
              {value}
            </div>
          </div>
        ))}
      </div>

      {/* Refresh strip */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '6px 10px',
          marginBottom: 12,
          background: T.card,
          border: `1px solid ${T.cardBorder}`,
          borderRadius: 6,
          fontSize: 9,
          color: T.textMuted,
          fontFamily: T.mono,
        }}
      >
        <span>
          live probes refresh every 15s ·{' '}
          {lastRefresh
            ? `last ${lastRefresh.toISOString().slice(11, 19)}Z`
            : 'waiting for first probe'}
        </span>
        <button
          onClick={probeAll}
          style={{
            background: 'rgba(6,182,212,0.15)',
            border: '1px solid rgba(6,182,212,0.3)',
            color: T.cyan,
            padding: '4px 10px',
            borderRadius: 3,
            fontSize: 9,
            fontWeight: 700,
            fontFamily: T.mono,
            cursor: 'pointer',
            letterSpacing: '0.05em',
            textTransform: 'uppercase',
          }}
        >
          refresh now
        </button>
      </div>

      {/* Service cards */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {SERVICES.map((s) => (
          <ServiceCard key={s.id} service={s} liveHealth={liveHealth} />
        ))}
      </div>

      {/* Footer reference */}
      <div
        style={{
          marginTop: 20,
          padding: '12px 14px',
          background: 'rgba(168,85,247,0.05)',
          border: '1px solid rgba(168,85,247,0.15)',
          borderRadius: 6,
          fontSize: 9,
          color: T.textMuted,
          lineHeight: 1.5,
        }}
      >
        <div
          style={{
            fontSize: 8,
            fontWeight: 800,
            color: T.purple,
            letterSpacing: '0.08em',
            marginBottom: 4,
          }}
        >
          REFERENCES
        </div>
        <div>
          · <code style={{ color: T.cyan, fontFamily: T.mono }}>docs/CI_CD.md</code>{' '}
          is the authoritative spec — keep this page in sync with every PR that
          changes deploy topology.
        </div>
        <div>
          · <code style={{ color: T.cyan, fontFamily: T.mono }}>/audit</code> tracks the
          ongoing clean-architect audit tasks including CI-01 (Montreal CI/CD for engine).
        </div>
        <div>
          · Once CI-01 lands, a future iteration of this page will pull the
          most recent GitHub Actions workflow run status + error-signature
          counts for each service.
        </div>
      </div>
    </div>
  );
}
