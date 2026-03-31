import { useState, useEffect, useCallback } from 'react';
import { useApi } from '../hooks/useApi';
import SetupSection from '../components/SetupSection';
import SecretField from '../components/SecretField';
import WalletConnect from '../components/WalletConnect';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function generateJwtSecret() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  return Array.from({ length: 64 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
}

function InputField({ label, value, onChange, placeholder, type = 'text', helpText, helpLink, note }) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium" style={{ color: 'rgba(255,255,255,0.7)' }}>
        {label}
      </label>
      <input
        type={type}
        value={value || ''}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full px-3 py-2 text-sm rounded-lg outline-none transition-all"
        style={{
          background: 'rgba(255,255,255,0.04)',
          border: '1px solid var(--border)',
          color: 'rgba(255,255,255,0.85)',
        }}
        onFocus={e => { e.target.style.borderColor = 'var(--accent-purple)'; }}
        onBlur={e => { e.target.style.borderColor = 'var(--border)'; }}
      />
      {note && (
        <p className="text-xs" style={{ color: 'var(--accent-cyan)' }}>{note}</p>
      )}
      {(helpText || helpLink) && (
        <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>
          {helpText}
          {helpLink && (
            <> <a href={helpLink.href} target="_blank" rel="noopener noreferrer" className="underline" style={{ color: 'var(--accent-cyan)' }}>{helpLink.label}</a></>
          )}
        </p>
      )}
    </div>
  );
}

function SaveButton({ onClick, saving, saved }) {
  return (
    <button
      onClick={onClick}
      disabled={saving}
      className="px-5 py-2 rounded-lg text-sm font-semibold transition-all"
      style={{
        background: saved ? 'rgba(74,222,128,0.15)' : 'rgba(168,85,247,0.15)',
        border: `1px solid ${saved ? 'rgba(74,222,128,0.4)' : 'rgba(168,85,247,0.4)'}`,
        color: saved ? '#4ade80' : 'var(--accent-purple)',
        opacity: saving ? 0.6 : 1,
      }}
    >
      {saving ? 'Saving…' : saved ? '✓ Saved' : 'Save'}
    </button>
  );
}

function Divider({ label }) {
  return (
    <div className="flex items-center gap-3 my-4">
      <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
      {label && <span className="text-xs font-medium uppercase tracking-widest" style={{ color: 'rgba(255,255,255,0.25)' }}>{label}</span>}
      <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />
    </div>
  );
}

// ─── Main Setup Page ──────────────────────────────────────────────────────────

export default function Setup() {
  const api = useApi();

  // ── Wallet state ──
  const [walletAddress, setWalletAddress]   = useState('');
  const [walletNetwork, setWalletNetwork]   = useState(null); // 'polygon' | 'bnb'
  const [walletBalance, setWalletBalance]   = useState(null);
  const [walletError, setWalletError]       = useState('');
  const [funderAddress, setFunderAddress]   = useState('');

  // ── Exchange API keys ──
  const [polyPrivateKey, setPolyPrivateKey]     = useState('');
  const [polyApiKey, setPolyApiKey]             = useState('');
  const [polyApiSecret, setPolyApiSecret]       = useState('');
  const [polyApiPassphrase, setPolyApiPassphrase] = useState('');
  const [polyFunderAddress, setPolyFunderAddress] = useState('');
  const [opinionApiKey, setOpinionApiKey]       = useState('');

  // ── Data feeds ──
  const [binanceApiKey, setBinanceApiKey]       = useState('');
  const [binanceApiSecret, setBinanceApiSecret] = useState('');
  const [coinglassApiKey, setCoinglassApiKey]   = useState('');
  const [polygonRpcUrl, setPolygonRpcUrl]       = useState('');

  // ── Alerts ──
  const [telegramToken, setTelegramToken] = useState('');
  const [telegramChatId, setTelegramChatId] = useState('');
  const [alertTestState, setAlertTestState] = useState('idle'); // idle | sending | ok | err

  // ── System ──
  const [adminUsername, setAdminUsername]   = useState('');
  const [adminPassword, setAdminPassword]   = useState('');
  const [jwtSecret, setJwtSecret]           = useState(() => generateJwtSecret());
  const [jwtVisible, setJwtVisible]         = useState(false);
  const [paperMode, setPaperMode]           = useState(true);
  const [bankroll, setBankroll]             = useState('1000');
  const [dbStatus, setDbStatus]             = useState(null); // null | 'ok' | 'error'

  // ── Deployment ──
  const [domain, setDomain] = useState('');
  const [vpsStatus, setVpsStatus] = useState('Not deployed');

  // ── Save state per section ──
  const [saving, setSaving]   = useState({});
  const [saved, setSaved]     = useState({});
  const [saveErr, setSaveErr] = useState({});

  // ─── Fetch existing config on mount ──────────────────────────────────────
  useEffect(() => {
    api.get('/api/config/setup')
      .then(res => {
        const d = res.data || {};
        if (d.poly_api_key)        setPolyApiKey(d.poly_api_key);
        if (d.poly_funder_address) { setPolyFunderAddress(d.poly_funder_address); setFunderAddress(d.poly_funder_address); }
        if (d.opinion_api_key)     setOpinionApiKey(d.opinion_api_key);
        if (d.binance_api_key)     setBinanceApiKey(d.binance_api_key);
        if (d.coinglass_api_key)   setCoinglassApiKey(d.coinglass_api_key);
        if (d.polygon_rpc_url)     setPolygonRpcUrl(d.polygon_rpc_url);
        if (d.telegram_bot_token)  setTelegramToken(d.telegram_bot_token);
        if (d.telegram_chat_id)    setTelegramChatId(d.telegram_chat_id);
        if (d.domain)              setDomain(d.domain);
        if (d.starting_bankroll)   setBankroll(String(d.starting_bankroll));
        if (d.paper_mode !== undefined) setPaperMode(d.paper_mode);
      })
      .catch(() => {}); // Backend may not exist yet; silent fail

    api.get('/api/system/status')
      .then(() => setDbStatus('ok'))
      .catch(() => setDbStatus('error'));
  }, []);

  // ─── MetaMask connection ─────────────────────────────────────────────────
  const connectMetaMask = useCallback(async () => {
    setWalletError('');
    if (!window.ethereum) {
      setWalletError('MetaMask not detected. Install it from metamask.io');
      return;
    }
    try {
      const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
      setWalletAddress(accounts[0]);

      const chainId = await window.ethereum.request({ method: 'eth_chainId' });
      const isPolygon = chainId === '0x89';
      setWalletNetwork(isPolygon ? 'polygon' : null);

      if (!isPolygon) {
        try {
          await window.ethereum.request({
            method: 'wallet_switchEthereumChain',
            params: [{ chainId: '0x89' }],
          });
          setWalletNetwork('polygon');
        } catch {
          setWalletError('Please switch to Polygon network in MetaMask.');
        }
      }
    } catch (err) {
      setWalletError(err.message);
    }
  }, []);

  const switchNetwork = useCallback(async (net) => {
    if (!window.ethereum) return;
    const chainId = net === 'polygon' ? '0x89' : '0x38';
    try {
      await window.ethereum.request({
        method: 'wallet_switchEthereumChain',
        params: [{ chainId }],
      });
      setWalletNetwork(net);
    } catch (err) {
      setWalletError(err.message);
    }
  }, []);

  // ─── Save section ────────────────────────────────────────────────────────
  const saveSection = useCallback(async (sectionId, payload) => {
    setSaving(s => ({ ...s, [sectionId]: true }));
    setSaveErr(s => ({ ...s, [sectionId]: '' }));
    try {
      await api.put('/api/config/setup', payload);
      setSaved(s => ({ ...s, [sectionId]: true }));
      setTimeout(() => setSaved(s => ({ ...s, [sectionId]: false })), 3000);
    } catch (err) {
      setSaveErr(s => ({ ...s, [sectionId]: err?.response?.data?.detail || 'Failed to save' }));
    } finally {
      setSaving(s => ({ ...s, [sectionId]: false }));
    }
  }, [api]);

  // ─── Test Telegram alert ─────────────────────────────────────────────────
  const testTelegramAlert = useCallback(async () => {
    setAlertTestState('sending');
    try {
      await api.post('/api/config/setup/test-telegram', {
        telegram_bot_token: telegramToken,
        telegram_chat_id: telegramChatId,
      });
      setAlertTestState('ok');
      setTimeout(() => setAlertTestState('idle'), 4000);
    } catch {
      setAlertTestState('err');
      setTimeout(() => setAlertTestState('idle'), 4000);
    }
  }, [api, telegramToken, telegramChatId]);

  // ─── Section status helpers ──────────────────────────────────────────────
  const walletStatus   = walletAddress ? 'ready' : 'missing';
  const exchangeStatus = (polyApiKey && polyApiSecret && polyApiPassphrase && opinionApiKey) ? 'ready'
    : (polyApiKey || opinionApiKey) ? 'incomplete' : 'missing';
  const feedsStatus    = (binanceApiKey && coinglassApiKey && polygonRpcUrl) ? 'ready'
    : (binanceApiKey || coinglassApiKey || polygonRpcUrl) ? 'incomplete' : 'missing';
  const alertsStatus   = (telegramToken && telegramChatId) ? 'ready'
    : (telegramToken || telegramChatId) ? 'incomplete' : 'missing';
  const systemStatus   = (adminUsername && adminPassword) ? 'ready'
    : (adminUsername || adminPassword) ? 'incomplete' : 'missing';
  const deployStatus   = domain ? 'ready' : 'missing';

  const statuses = [walletStatus, exchangeStatus, feedsStatus, alertsStatus, systemStatus, deployStatus];
  const readyCount = statuses.filter(s => s === 'ready').length;
  const progress = Math.round((readyCount / statuses.length) * 100);

  // ─── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">

      {/* Page header */}
      <div className="space-y-1">
        <h1
          className="text-2xl font-bold tracking-tight"
          style={{ color: 'var(--text-primary)' }}
        >
          ⚙️ Setup & Onboarding
        </h1>
        <p className="text-sm" style={{ color: 'rgba(255,255,255,0.4)' }}>
          Configure your trading bot step by step. All secrets are stored server-side.
        </p>
      </div>

      {/* Progress bar */}
      <div
        className="rounded-xl p-5"
        style={{ background: 'var(--card)', border: '1px solid var(--border)' }}
      >
        <div className="flex items-center justify-between mb-3">
          <span className="text-sm font-medium" style={{ color: 'rgba(255,255,255,0.7)' }}>
            Overall Progress
          </span>
          <span className="text-sm font-semibold" style={{ color: 'var(--accent-purple)' }}>
            {readyCount}/{statuses.length} sections complete
          </span>
        </div>
        <div
          className="h-2 rounded-full overflow-hidden"
          style={{ background: 'rgba(255,255,255,0.06)' }}
        >
          <div
            className="h-full rounded-full transition-all duration-500"
            style={{
              width: `${progress}%`,
              background: progress === 100
                ? '#4ade80'
                : 'linear-gradient(90deg, var(--accent-purple), var(--accent-cyan))',
            }}
          />
        </div>
        {readyCount === statuses.length && (
          <p className="mt-2 text-sm font-medium" style={{ color: '#4ade80' }}>
            🎉 All sections configured — you're ready to trade!
          </p>
        )}
      </div>

      {/* ─── Section 1: Wallet Connection ──────────────────────────────────── */}
      <SetupSection
        icon="🦊"
        title="Wallet Connection"
        status={walletStatus}
        defaultOpen={true}
      >
        <WalletConnect
          walletAddress={walletAddress}
          network={walletNetwork}
          balance={walletBalance}
          onConnect={connectMetaMask}
          onNetworkSwitch={switchNetwork}
          error={walletError}
        />

        {walletAddress && (
          <>
            <Divider label="Funder Address" />

            {/* First Trade Warning */}
            <div
              className="rounded-lg p-3 mb-4"
              style={{ background: 'rgba(251,191,36,0.08)', border: '1px solid rgba(251,191,36,0.2)' }}
            >
              <p className="text-xs font-medium mb-1" style={{ color: '#fbbf24' }}>
                ⚠️ First Trade Required — Polymarket Proxy Wallet
              </p>
              <p className="text-xs" style={{ color: 'rgba(255,255,255,0.5)' }}>
                Polymarket requires you to place at least one trade manually via their UI to initialise a proxy wallet.
                After your first trade, your funder address will be shown in Polymarket → Settings → Export.
              </p>
            </div>

            <InputField
              label="Funder Address"
              value={funderAddress}
              onChange={setFunderAddress}
              placeholder="0x…"
              helpText="Your Polymarket funder address (from Settings → Export). Auto-detected after first trade."
            />
          </>
        )}

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('wallet', {
              poly_funder_address: funderAddress || polyFunderAddress,
            })}
            saving={saving.wallet}
            saved={saved.wallet}
          />
          {saveErr.wallet && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.wallet}</span>
          )}
        </div>
      </SetupSection>

      {/* ─── Section 2: Exchange API Keys ──────────────────────────────────── */}
      <SetupSection
        icon="🔑"
        title="Exchange API Keys"
        status={exchangeStatus}
      >
        {/* Polymarket */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-semibold" style={{ color: 'var(--accent-purple)' }}>
              Polymarket
            </span>
            <span
              className="text-xs px-2 py-0.5 rounded-full"
              style={{
                background: (polyApiKey && polyApiSecret && polyApiPassphrase) ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
                color: (polyApiKey && polyApiSecret && polyApiPassphrase) ? '#4ade80' : '#f87171',
              }}
            >
              {(polyApiKey && polyApiSecret && polyApiPassphrase) ? '✅ Configured' : '❌ Missing'}
            </span>
          </div>

          <SecretField
            label="Private Key"
            value={polyPrivateKey}
            onChange={setPolyPrivateKey}
            placeholder="0x…"
            helpText="Export from MetaMask → Account Details → Show Private Key. Never share this."
            required
          />

          {/* Run Setup Script info box */}
          <div
            className="rounded-lg p-3"
            style={{ background: 'rgba(6,182,212,0.06)', border: '1px solid rgba(6,182,212,0.2)' }}
          >
            <p className="text-xs font-medium mb-1" style={{ color: 'var(--accent-cyan)' }}>
              📜 Run Setup Script to generate API keys
            </p>
            <p className="text-xs mb-2" style={{ color: 'rgba(255,255,255,0.45)' }}>
              Polymarket API keys are derived from your private key using their CLOB authentication.
              Run the included setup script to generate them automatically:
            </p>
            <code
              className="block text-xs p-2 rounded font-mono"
              style={{ background: 'rgba(0,0,0,0.3)', color: 'rgba(255,255,255,0.6)' }}
            >
              cd scripts && python setup_polymarket_keys.py
            </code>
          </div>

          <SecretField
            label="API Key"
            value={polyApiKey}
            onChange={setPolyApiKey}
            placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            required
          />
          <SecretField
            label="API Secret"
            value={polyApiSecret}
            onChange={setPolyApiSecret}
            placeholder="Paste your API secret"
            required
          />
          <SecretField
            label="API Passphrase"
            value={polyApiPassphrase}
            onChange={setPolyApiPassphrase}
            placeholder="Paste your passphrase"
            required
          />
          <InputField
            label="Funder Address"
            value={polyFunderAddress}
            onChange={setPolyFunderAddress}
            placeholder="0x…"
            helpText="Your Polymarket funder address (same as wallet section)"
          />
        </div>

        <Divider label="Opinion Markets" />

        {/* Opinion */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-semibold" style={{ color: 'var(--accent-cyan)' }}>
              Opinion Markets
            </span>
            <span
              className="text-xs px-2 py-0.5 rounded-full"
              style={{
                background: opinionApiKey ? 'rgba(74,222,128,0.1)' : 'rgba(248,113,113,0.1)',
                color: opinionApiKey ? '#4ade80' : '#f87171',
              }}
            >
              {opinionApiKey ? '✅ Configured' : '❌ Missing'}
            </span>
          </div>
          <SecretField
            label="API Key"
            value={opinionApiKey}
            onChange={setOpinionApiKey}
            placeholder="Paste Opinion Markets API key"
            helpLink={{ href: 'https://opinion.markets', label: 'Get API key →' }}
            required
          />
        </div>

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('exchange', {
              poly_private_key: polyPrivateKey,
              poly_api_key: polyApiKey,
              poly_api_secret: polyApiSecret,
              poly_api_passphrase: polyApiPassphrase,
              poly_funder_address: polyFunderAddress,
              opinion_api_key: opinionApiKey,
            })}
            saving={saving.exchange}
            saved={saved.exchange}
          />
          {saveErr.exchange && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.exchange}</span>
          )}
        </div>
      </SetupSection>

      {/* ─── Section 3: Data Feeds ──────────────────────────────────────────── */}
      <SetupSection
        icon="📡"
        title="Data Feed Keys"
        status={feedsStatus}
      >
        <div className="space-y-5">
          {/* Binance */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-sm font-semibold" style={{ color: '#f0b90b' }}>Binance</span>
              <span className="text-xs px-2 py-0.5 rounded" style={{ background: 'rgba(6,182,212,0.1)', color: 'var(--accent-cyan)' }}>
                Data only — no trading
              </span>
            </div>
            <div className="space-y-3">
              <SecretField
                label="API Key"
                value={binanceApiKey}
                onChange={setBinanceApiKey}
                placeholder="Paste Binance API key"
                helpLink={{ href: 'https://www.binance.com/en/my/settings/api-management', label: 'Create read-only key →' }}
              />
              <SecretField
                label="API Secret"
                value={binanceApiSecret}
                onChange={setBinanceApiSecret}
                placeholder="Paste Binance API secret"
              />
            </div>
          </div>

          <Divider />

          {/* CoinGlass */}
          <div>
            <span className="text-sm font-semibold block mb-3" style={{ color: 'rgba(255,255,255,0.7)' }}>
              CoinGlass
            </span>
            <SecretField
              label="API Key"
              value={coinglassApiKey}
              onChange={setCoinglassApiKey}
              placeholder="Paste CoinGlass API key"
              helpLink={{ href: 'https://coinglass.com/pricing', label: 'Get a key →' }}
            />
          </div>

          <Divider />

          {/* Alchemy */}
          <div>
            <span className="text-sm font-semibold block mb-3" style={{ color: 'rgba(255,255,255,0.7)' }}>
              Alchemy — Polygon RPC
            </span>
            <InputField
              label="RPC URL"
              value={polygonRpcUrl}
              onChange={setPolygonRpcUrl}
              placeholder="https://polygon-mainnet.g.alchemy.com/v2/your-key"
              helpLink={{ href: 'https://www.alchemy.com/', label: 'Create free app →' }}
            />
          </div>
        </div>

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('feeds', {
              binance_api_key: binanceApiKey,
              binance_api_secret: binanceApiSecret,
              coinglass_api_key: coinglassApiKey,
              polygon_rpc_url: polygonRpcUrl,
            })}
            saving={saving.feeds}
            saved={saved.feeds}
          />
          {saveErr.feeds && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.feeds}</span>
          )}
        </div>
      </SetupSection>

      {/* ─── Section 4: Alerts ─────────────────────────────────────────────── */}
      <SetupSection
        icon="🔔"
        title="Alerts"
        status={alertsStatus}
      >
        <div className="space-y-4">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-sm font-semibold" style={{ color: 'rgba(255,255,255,0.7)' }}>Telegram</span>
          </div>
          <SecretField
            label="Bot Token"
            value={telegramToken}
            onChange={setTelegramToken}
            placeholder="1234567890:AABBccDDeeFFggHH…"
            helpText="Create a bot via @BotFather on Telegram, then copy the token."
            helpLink={{ href: 'https://t.me/BotFather', label: 'Open BotFather →' }}
            required
          />
          <InputField
            label="Chat ID"
            value={telegramChatId}
            onChange={setTelegramChatId}
            placeholder="-1001234567890 or 123456789"
            helpText="Your chat or group ID. Use @userinfobot to find it."
            helpLink={{ href: 'https://t.me/userinfobot', label: 'Get your Chat ID →' }}
          />

          {/* Test alert button */}
          <div className="flex items-center gap-3 pt-1">
            <button
              onClick={testTelegramAlert}
              disabled={!telegramToken || !telegramChatId || alertTestState === 'sending'}
              className="px-4 py-2 rounded-lg text-sm font-medium transition-all"
              style={{
                background: alertTestState === 'ok' ? 'rgba(74,222,128,0.1)'
                  : alertTestState === 'err' ? 'rgba(248,113,113,0.1)'
                  : 'rgba(255,255,255,0.06)',
                border: `1px solid ${alertTestState === 'ok' ? 'rgba(74,222,128,0.3)'
                  : alertTestState === 'err' ? 'rgba(248,113,113,0.3)'
                  : 'var(--border)'}`,
                color: alertTestState === 'ok' ? '#4ade80'
                  : alertTestState === 'err' ? '#f87171'
                  : 'rgba(255,255,255,0.6)',
                opacity: (!telegramToken || !telegramChatId) ? 0.4 : 1,
              }}
            >
              {alertTestState === 'sending' ? '⏳ Sending…'
                : alertTestState === 'ok' ? '✅ Alert sent!'
                : alertTestState === 'err' ? '❌ Failed'
                : '📨 Test Alert'}
            </button>
          </div>
        </div>

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('alerts', {
              telegram_bot_token: telegramToken,
              telegram_chat_id: telegramChatId,
            })}
            saving={saving.alerts}
            saved={saved.alerts}
          />
          {saveErr.alerts && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.alerts}</span>
          )}
        </div>
      </SetupSection>

      {/* ─── Section 5: System ─────────────────────────────────────────────── */}
      <SetupSection
        icon="🖥️"
        title="System"
        status={systemStatus}
      >
        <div className="space-y-5">
          {/* DB status */}
          <div
            className="flex items-center gap-3 px-4 py-3 rounded-lg"
            style={{
              background: dbStatus === 'ok' ? 'rgba(74,222,128,0.06)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${dbStatus === 'ok' ? 'rgba(74,222,128,0.2)' : 'var(--border)'}`,
            }}
          >
            <span>{dbStatus === 'ok' ? '✅' : dbStatus === 'error' ? '❌' : '⏳'}</span>
            <div>
              <p className="text-sm font-medium" style={{ color: 'rgba(255,255,255,0.7)' }}>
                Database
              </p>
              <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>
                {dbStatus === 'ok' ? 'PostgreSQL connected — auto-configured'
                  : dbStatus === 'error' ? 'Connection error — check docker-compose'
                  : 'Checking…'}
              </p>
            </div>
          </div>

          <Divider label="Admin Credentials" />

          <InputField
            label="Admin Username"
            value={adminUsername}
            onChange={setAdminUsername}
            placeholder="admin"
          />
          <SecretField
            label="Admin Password"
            value={adminPassword}
            onChange={setAdminPassword}
            placeholder="Choose a strong password"
            required
          />

          <Divider label="JWT Secret" />

          {/* JWT Secret */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium" style={{ color: 'rgba(255,255,255,0.7)' }}>
                JWT Secret
              </label>
              <div className="flex gap-2">
                <button
                  onClick={() => setJwtVisible(v => !v)}
                  className="text-xs px-2 py-0.5 rounded transition-colors"
                  style={{ color: 'rgba(255,255,255,0.4)', background: 'rgba(255,255,255,0.06)' }}
                >
                  {jwtVisible ? 'Hide' : 'Show'}
                </button>
                <button
                  onClick={() => setJwtSecret(generateJwtSecret())}
                  className="text-xs px-2 py-0.5 rounded transition-colors"
                  style={{ color: 'var(--accent-cyan)', background: 'rgba(6,182,212,0.1)' }}
                >
                  Regenerate
                </button>
              </div>
            </div>
            <div
              className="px-3 py-2 rounded-lg font-mono text-xs break-all"
              style={{
                background: 'rgba(255,255,255,0.04)',
                border: '1px solid var(--border)',
                color: 'rgba(255,255,255,0.6)',
              }}
            >
              {jwtVisible ? jwtSecret : '•'.repeat(Math.min(jwtSecret.length, 48))}
            </div>
            <p className="text-xs" style={{ color: 'rgba(255,255,255,0.3)' }}>
              Auto-generated 64-char secret for signing JWT tokens. Changing this invalidates all sessions.
            </p>
          </div>

          <Divider label="Trading Mode" />

          {/* Paper Mode Toggle — big and prominent */}
          <div
            className="flex items-center justify-between px-5 py-4 rounded-xl"
            style={{
              background: paperMode ? 'rgba(168,85,247,0.08)' : 'rgba(248,113,113,0.08)',
              border: `1px solid ${paperMode ? 'rgba(168,85,247,0.3)' : 'rgba(248,113,113,0.3)'}`,
            }}
          >
            <div>
              <p className="text-base font-semibold" style={{ color: paperMode ? 'var(--accent-purple)' : '#f87171' }}>
                {paperMode ? '🔵 Paper Mode' : '🔴 Live Trading'}
              </p>
              <p className="text-xs mt-0.5" style={{ color: 'rgba(255,255,255,0.4)' }}>
                {paperMode
                  ? 'Simulated trades — no real money at risk'
                  : '⚠️ Real money will be traded. Be careful.'}
              </p>
            </div>
            <button
              onClick={() => setPaperMode(v => !v)}
              className="relative flex-shrink-0"
              style={{ width: '52px', height: '28px' }}
            >
              <div
                className="w-full h-full rounded-full transition-colors duration-200"
                style={{ background: paperMode ? 'var(--accent-purple)' : '#f87171' }}
              />
              <div
                className="absolute top-1 w-5 h-5 rounded-full bg-white shadow transition-transform duration-200"
                style={{ left: '4px', transform: paperMode ? 'translateX(0)' : 'translateX(24px)' }}
              />
            </button>
          </div>

          {/* Starting Bankroll */}
          <InputField
            label="Starting Bankroll (USDC)"
            value={bankroll}
            onChange={setBankroll}
            type="number"
            placeholder="1000"
            helpText="Initial capital for risk sizing calculations."
          />
        </div>

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('system', {
              starting_bankroll: parseFloat(bankroll) || 1000,
              paper_mode: paperMode,
            })}
            saving={saving.system}
            saved={saved.system}
          />
          {saveErr.system && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.system}</span>
          )}
        </div>
      </SetupSection>

      {/* ─── Section 6: Deployment ─────────────────────────────────────────── */}
      <SetupSection
        icon="🚀"
        title="Deployment"
        status={deployStatus}
      >
        <div className="space-y-4">
          {/* VPS status */}
          <div
            className="flex items-center gap-3 px-4 py-3 rounded-lg"
            style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid var(--border)' }}
          >
            <span>🖥️</span>
            <div>
              <p className="text-sm font-medium" style={{ color: 'rgba(255,255,255,0.7)' }}>VPS Status</p>
              <p className="text-xs" style={{ color: 'rgba(255,255,255,0.35)' }}>{vpsStatus}</p>
            </div>
          </div>

          <InputField
            label="Domain"
            value={domain}
            onChange={setDomain}
            placeholder="trader.yourdomain.com"
            helpText="Domain for Caddy HTTPS reverse proxy. Point your A record to the VPS IP first."
          />

          <div
            className="rounded-lg p-3"
            style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid var(--border)' }}
          >
            <p className="text-xs font-medium mb-2" style={{ color: 'rgba(255,255,255,0.5)' }}>
              📦 Deploy with Docker Compose
            </p>
            <code
              className="block text-xs p-2 rounded font-mono"
              style={{ background: 'rgba(0,0,0,0.3)', color: 'rgba(255,255,255,0.55)' }}
            >
              docker compose up -d --build
            </code>
            <p className="text-xs mt-2" style={{ color: 'rgba(255,255,255,0.3)' }}>
              Caddy will auto-provision an HTTPS certificate via Let's Encrypt for your domain.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3 mt-5">
          <SaveButton
            onClick={() => saveSection('deploy', { domain })}
            saving={saving.deploy}
            saved={saved.deploy}
          />
          {saveErr.deploy && (
            <span className="text-xs" style={{ color: 'var(--loss)' }}>{saveErr.deploy}</span>
          )}
        </div>
      </SetupSection>

    </div>
  );
}
