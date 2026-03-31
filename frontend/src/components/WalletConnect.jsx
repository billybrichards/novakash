import { useState } from 'react';

const NETWORKS = {
  polygon: { chainId: '0x89', label: 'Polygon', color: '#8247e5', symbol: 'MATIC' },
  bnb:     { chainId: '0x38', label: 'BNB Chain', color: '#f0b90b', symbol: 'BNB' },
};

/**
 * WalletConnect — MetaMask connection card with network switching.
 *
 * Props:
 *   walletAddress   — connected address (or '')
 *   network         — 'polygon' | 'bnb' | null
 *   balance         — balance string (e.g. '12.45 USDC')
 *   onConnect       — () => void — triggered on MetaMask connect
 *   onNetworkSwitch — (network) => void — triggered on tab switch
 *   error           — error message string
 */
export default function WalletConnect({
  walletAddress = '',
  network = null,
  balance = null,
  onConnect,
  onNetworkSwitch,
  error = '',
}) {
  const [activeTab, setActiveTab] = useState('metamask');

  const truncate = (addr) =>
    addr ? `${addr.slice(0, 6)}…${addr.slice(-4)}` : '';

  const connected = Boolean(walletAddress);
  const net = NETWORKS[network];

  return (
    <div className="space-y-4">
      {/* Tabs: MetaMask / WalletConnect */}
      <div
        className="flex rounded-lg p-0.5 gap-0.5"
        style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border)' }}
      >
        {['metamask', 'walletconnect'].map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className="flex-1 py-1.5 text-sm font-medium rounded-md transition-all"
            style={{
              background: activeTab === tab ? 'rgba(168,85,247,0.15)' : 'transparent',
              color: activeTab === tab ? 'var(--accent-purple)' : 'rgba(255,255,255,0.4)',
              border: activeTab === tab ? '1px solid rgba(168,85,247,0.3)' : '1px solid transparent',
            }}
          >
            {tab === 'metamask' ? '🦊 MetaMask' : '🔗 WalletConnect'}
          </button>
        ))}
      </div>

      {activeTab === 'metamask' ? (
        <div className="space-y-3">
          {!connected ? (
            /* Connect button */
            <button
              onClick={onConnect}
              className="w-full py-3 rounded-lg text-sm font-semibold transition-all"
              style={{
                background: 'rgba(168,85,247,0.15)',
                border: '1px solid rgba(168,85,247,0.4)',
                color: 'var(--accent-purple)',
              }}
            >
              🦊 Connect MetaMask
            </button>
          ) : (
            /* Connected state */
            <div
              className="rounded-lg p-4 space-y-3"
              style={{
                background: 'rgba(74,222,128,0.05)',
                border: '1px solid rgba(74,222,128,0.2)',
              }}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium" style={{ color: '#4ade80' }}>
                  ✅ Connected
                </span>
                {net && (
                  <span
                    className="text-xs px-2 py-0.5 rounded-full font-medium"
                    style={{
                      background: `${net.color}22`,
                      color: net.color,
                      border: `1px solid ${net.color}44`,
                    }}
                  >
                    {net.label}
                  </span>
                )}
              </div>

              <div className="font-mono text-sm" style={{ color: 'rgba(255,255,255,0.7)' }}>
                {truncate(walletAddress)}
              </div>

              {balance && (
                <div className="text-sm" style={{ color: 'rgba(255,255,255,0.5)' }}>
                  Balance: <span style={{ color: 'var(--profit)' }}>{balance}</span>
                </div>
              )}
            </div>
          )}

          {/* Network switch tabs (Polygon / BNB) */}
          {connected && (
            <div>
              <p className="text-xs mb-2" style={{ color: 'rgba(255,255,255,0.4)' }}>
                Switch network:
              </p>
              <div className="flex gap-2">
                {Object.entries(NETWORKS).map(([key, n]) => (
                  <button
                    key={key}
                    onClick={() => onNetworkSwitch && onNetworkSwitch(key)}
                    className="flex-1 py-1.5 text-xs font-medium rounded-lg transition-all"
                    style={{
                      background: network === key ? `${n.color}22` : 'rgba(255,255,255,0.04)',
                      border: `1px solid ${network === key ? n.color + '55' : 'var(--border)'}`,
                      color: network === key ? n.color : 'rgba(255,255,255,0.4)',
                    }}
                  >
                    {n.label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {error && (
            <p className="text-xs px-3 py-2 rounded-lg" style={{ background: 'rgba(248,113,113,0.1)', color: '#f87171', border: '1px solid rgba(248,113,113,0.2)' }}>
              {error}
            </p>
          )}
        </div>
      ) : (
        /* WalletConnect placeholder */
        <div
          className="flex flex-col items-center gap-3 py-8 rounded-lg"
          style={{ border: '1px dashed var(--border)', background: 'rgba(255,255,255,0.02)' }}
        >
          <div
            className="w-24 h-24 rounded-xl flex items-center justify-center"
            style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid var(--border)' }}
          >
            <span className="text-3xl opacity-30">📱</span>
          </div>
          <div className="text-center space-y-1">
            <p className="text-sm" style={{ color: 'rgba(255,255,255,0.5)' }}>
              WalletConnect QR Code
            </p>
            <span
              className="inline-block text-xs px-2 py-0.5 rounded-full font-medium"
              style={{
                background: 'rgba(251,191,36,0.1)',
                color: '#fbbf24',
                border: '1px solid rgba(251,191,36,0.25)',
              }}
            >
              Coming Soon
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
