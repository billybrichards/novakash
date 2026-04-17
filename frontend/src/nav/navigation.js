// Single source of truth for the new navigation.
// Active entries show in AppShell sidebar. Archive entries only appear
// inside /archive. When a page is promoted/demoted, flip its entry between
// NAV_SECTIONS and ARCHIVED_PAGES — no route edits required elsewhere.

export const NAV_SECTIONS = [
  {
    title: 'TRADING',
    color: '#a855f7',
    items: [
      { path: '/',            label: 'Dashboard',      icon: '📊' },
      { path: '/trades',      label: 'Trades',         icon: '📋' },
      { path: '/wallet',      label: 'Wallet',         icon: '👛' },
      { path: '/pnl',         label: 'P&L',            icon: '💰' },
    ],
  },
  {
    title: 'ANALYSIS',
    color: '#06b6d4',
    items: [
      { path: '/signals',     label: 'Signal Explorer', icon: '📡' },
      { path: '/strategies',  label: 'Strategies',      icon: '🧬' },
    ],
  },
  {
    title: 'CONTROL',
    color: '#f59e0b',
    items: [
      { path: '/config',      label: 'Config',         icon: '⚙️' },
      { path: '/audit',       label: 'Audit Tasks',    icon: '🔔' },
      { path: '/system',      label: 'System',         icon: '🖥️' },
    ],
  },
  {
    title: 'ARCHIVE',
    color: '#64748b',
    items: [
      { path: '/archive',     label: 'Archive Center', icon: '📦' },
    ],
  },
];

// Every page preserved at /archive/<path>. Each entry renders a legacy page
// under ArchivedPageBanner. `replacedBy` is the operator-facing hint.
// Props-bearing routes (StrategyFloor with strategyId) handled separately
// in App.jsx because they take per-route props.
export const ARCHIVED_PAGES = [
  // Original lean-redesign legacy (pre-Tier-1)
  { path: '/archive/paper',             label: 'Paper Dashboard',       importName: 'PaperDashboard',       replacedBy: 'Dashboard (paper mode toggle)' },
  { path: '/archive/playwright',        label: 'Playwright Dashboard',  importName: 'PlaywrightDashboard',  replacedBy: 'Dashboard' },
  { path: '/archive/execution-hq',      label: 'Execution HQ',          importName: 'ExecutionHQ',          replacedBy: 'Dashboard' },
  { path: '/archive/live',              label: 'Live Trading',          importName: 'LiveTrading',          replacedBy: 'Dashboard + Trades' },
  { path: '/archive/factory',           label: 'Factory Floor',         importName: 'FactoryFloor',         replacedBy: 'Signal Explorer' },
  { path: '/archive/v58',               label: 'V58 Monitor',           importName: 'V58Monitor',           replacedBy: 'Signal Explorer' },
  { path: '/archive/windows',           label: 'Window Results',        importName: 'WindowResults',        replacedBy: 'Signal Explorer' },
  { path: '/archive/strategy',          label: 'Strategy Analysis',     importName: 'StrategyAnalysis',     replacedBy: 'Strategies' },
  { path: '/archive/timesfm',           label: 'TimesFM',               importName: 'TimesFM',              replacedBy: 'Signal Explorer (forecast)' },
  { path: '/archive/composite',         label: 'Composite Signals',     importName: 'CompositeSignals',     replacedBy: 'Signal Explorer' },
  { path: '/archive/margin',            label: 'Margin Engine',         importName: 'MarginEngine',         replacedBy: null, note: 'Separate subsystem — live, pending own redesign.' },
  { path: '/archive/positions',         label: 'Positions',             importName: 'Positions',            replacedBy: 'Dashboard (open positions pane)' },
  { path: '/archive/risk',              label: 'Risk',                  importName: 'Risk',                 replacedBy: 'Config + Dashboard' },
  { path: '/archive/signals-legacy',    label: 'Signals (legacy)',      importName: 'Signals',              replacedBy: 'Signal Explorer' },
  { path: '/archive/trades-legacy',     label: 'Trades (legacy)',       importName: 'Trades',               replacedBy: 'Trades' },
  { path: '/archive/dashboard-legacy',  label: 'Dashboard (legacy)',    importName: 'Dashboard',            replacedBy: 'Dashboard' },
  { path: '/archive/setup',             label: 'Setup',                 importName: 'Setup',                replacedBy: null, note: 'One-time bootstrap.' },
  { path: '/archive/trading-config',    label: 'Trading Config (raw)',  importName: 'TradingConfig',        replacedBy: 'Config' },
  { path: '/archive/legacy-config',     label: 'Legacy Config',         importName: 'LegacyConfig',         replacedBy: 'Config' },
  { path: '/archive/config-develop',    label: 'Config (develop)',      importName: 'Config',               replacedBy: 'Config' },
  { path: '/archive/audit-checklist',   label: 'Audit Checklist',       importName: 'AuditChecklist',       replacedBy: 'Audit Tasks' },
  { path: '/archive/margin-strategies', label: 'Margin Strategies',     importName: 'MarginStrategies',     replacedBy: null, note: 'Margin subsystem.' },
  { path: '/archive/deployments',       label: 'Deployments',           importName: 'Deployments',          replacedBy: null, note: 'CI/CD surface.' },
  { path: '/archive/notes',             label: 'Notes',                 importName: 'Notes',                replacedBy: null, note: 'Hub notes log.' },
  { path: '/archive/schema',            label: 'Schema',                importName: 'Schema',               replacedBy: null, note: 'DB schema inspector.' },
  { path: '/archive/signal-comparison', label: 'Signal Comparison',     importName: 'SignalComparison',     replacedBy: 'Signal Explorer' },
  { path: '/archive/ops',               label: 'Agent Ops',             importName: 'AgentOps',             replacedBy: 'Audit Tasks' },
  { path: '/archive/telegram',          label: 'Telegram',              importName: 'Telegram',             replacedBy: null, note: 'TG channel control.' },
  { path: '/archive/15min-monitor',     label: '15-Min Monitor',        importName: 'FifteenMinMonitor',    replacedBy: 'Signal Explorer (15m filter)' },
  // Polymarket subtree
  { path: '/archive/polymarket/overview',         label: 'Polymarket Overview',     importName: 'PolymarketOverview', replacedBy: 'Dashboard' },
  { path: '/archive/polymarket/monitor',          label: 'Polymarket Monitor',      importName: 'PolymarketMonitor',  replacedBy: 'Signal Explorer' },
  { path: '/archive/polymarket/floor',            label: 'Live Floor',              importName: 'LiveFloor',          replacedBy: 'Dashboard' },
  { path: '/archive/polymarket/evaluate',         label: 'Polymarket Evaluate',     importName: 'PolymarketEvaluate', replacedBy: 'Strategies' },
  { path: '/archive/polymarket/strategy-lab',     label: 'Strategy Lab',            importName: 'StrategyLab',        replacedBy: 'Strategies' },
  { path: '/archive/polymarket/strategy-history', label: 'Strategy History',        importName: 'StrategyHistory',    replacedBy: 'Trades' },
  { path: '/archive/polymarket/strategies',       label: 'Strategy Configs',        importName: 'StrategyConfigs',    replacedBy: 'Config' },
  { path: '/archive/polymarket/gate-monitor',     label: 'Gate Pipeline Monitor',   importName: 'GatePipelineMonitor', replacedBy: 'Signal Explorer' },
  { path: '/archive/polymarket/data-health',      label: 'Data Health',             importName: 'DataHealth',         replacedBy: 'System' },
  { path: '/archive/polymarket/command',          label: 'Strategy Command',        importName: 'StrategyCommand',    replacedBy: 'Config' },
  // Data surfaces
  { path: '/archive/data/v1',         label: 'Data Surface V1',       importName: 'V1Surface',            replacedBy: null, note: 'Raw data surface.' },
  { path: '/archive/data/v2',         label: 'Data Surface V2',       importName: 'V2Surface',            replacedBy: null, note: 'Raw data surface.' },
  { path: '/archive/data/v3',         label: 'Data Surface V3',       importName: 'V3Surface',            replacedBy: null, note: 'Raw data surface.' },
  { path: '/archive/data/v4',         label: 'Data Surface V4',       importName: 'V4Surface',            replacedBy: null, note: 'Raw data surface.' },
  { path: '/archive/data/assembler1', label: 'Assembler 1',           importName: 'Assembler1',           replacedBy: null, note: 'Pipeline assembler.' },
];

// Polymarket StrategyFloor renders with per-route strategyId prop.
// Can't use the generic ARCHIVED_PAGES map for these — App.jsx handles them
// as explicit <Route> entries but still lists them in the Archive Center table.
export const ARCHIVED_STRATEGY_FLOORS = [
  { path: '/archive/polymarket/down-only', label: 'DOWN Strategy Floor',     strategyId: 'v4_down_only', replacedBy: 'Strategies' },
  { path: '/archive/polymarket/up-asian',  label: 'UP Asian Strategy Floor', strategyId: 'v4_up_asian',  replacedBy: 'Strategies' },
];
