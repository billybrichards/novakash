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
      { path: '/windows',     label: 'Windows',        icon: '🪟' },
      { path: '/wallet',      label: 'Wallet',         icon: '👛' },
      { path: '/pnl',         label: 'P&L',            icon: '💰' },
    ],
  },
  {
    title: 'ANALYSIS',
    color: '#06b6d4',
    items: [
      { path: '/signals',       label: 'Signal Explorer', icon: '📡' },
      { path: '/gate-traces',   label: 'Gate Traces',     icon: '🔬' },
      { path: '/gate-matrix',   label: 'Gate Matrix',     icon: '🧩' },
      { path: '/analysis',      label: 'Analysis · 30d',  icon: '📈' },
      { path: '/strategies',    label: 'Strategies',      icon: '🧬' },
    ],
  },
  {
    title: 'CONTROL',
    color: '#f59e0b',
    items: [
      { path: '/config',      label: 'Config',         icon: '⚙️' },
      { path: '/audit',       label: 'Audit Tasks',    icon: '🔔' },
      { path: '/notes',       label: 'Notes',          icon: '📝' },
      { path: '/schema',      label: 'Schema',         icon: '🗄️' },
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
// `category` is one of: trading | polymarket | data | ops
// Props-bearing routes (StrategyFloor with strategyId) handled separately
// in App.jsx because they take per-route props.
export const ARCHIVED_PAGES = [
  // Trading — execution monitors, signal views, position tracking
  { path: '/archive/paper',             label: 'Paper Dashboard',       importName: 'PaperDashboard',       category: 'trading', replacedBy: 'Dashboard (paper mode toggle)' },
  { path: '/archive/playwright',        label: 'Playwright Dashboard',  importName: 'PlaywrightDashboard',  category: 'trading', replacedBy: 'Dashboard' },
  { path: '/archive/execution-hq',      label: 'Execution HQ',          importName: 'ExecutionHQ',          category: 'trading', replacedBy: 'Dashboard' },
  { path: '/archive/live',              label: 'Live Trading',          importName: 'LiveTrading',          category: 'trading', replacedBy: 'Dashboard + Trades' },
  { path: '/archive/factory',           label: 'Factory Floor',         importName: 'FactoryFloor',         category: 'trading', replacedBy: 'Signal Explorer' },
  { path: '/archive/v58',               label: 'V58 Monitor',           importName: 'V58Monitor',           category: 'trading', replacedBy: 'Signal Explorer' },
  // WindowResults promoted to /windows (active nav under TRADING).
  // StrategyAnalysis promoted to /analysis (nav entry "Analysis · 30d").
  { path: '/archive/timesfm',           label: 'TimesFM',               importName: 'TimesFM',              category: 'trading', replacedBy: 'Signal Explorer (forecast)' },
  { path: '/archive/composite',         label: 'Composite Signals',     importName: 'CompositeSignals',     category: 'trading', replacedBy: 'Signal Explorer' },
  { path: '/archive/positions',         label: 'Positions',             importName: 'Positions',            category: 'trading', replacedBy: 'Dashboard (open positions pane)' },
  { path: '/archive/signals-legacy',    label: 'Signals (legacy)',      importName: 'Signals',              category: 'trading', replacedBy: 'Signal Explorer' },
  { path: '/archive/trades-legacy',     label: 'Trades (legacy)',       importName: 'Trades',               category: 'trading', replacedBy: 'Trades' },
  { path: '/archive/dashboard-legacy',  label: 'Dashboard (legacy)',    importName: 'Dashboard',            category: 'trading', replacedBy: 'Dashboard' },
  { path: '/archive/signal-comparison', label: 'Signal Comparison',     importName: 'SignalComparison',     category: 'trading', replacedBy: 'Signal Explorer' },
  { path: '/archive/15min-monitor',     label: '15-Min Monitor',        importName: 'FifteenMinMonitor',    category: 'trading', replacedBy: 'Signal Explorer (15m filter)' },
  // Ops — config, risk, audit, system tools, and separate subsystems
  { path: '/archive/margin',            label: 'Margin Engine',         importName: 'MarginEngine',         category: 'ops',     replacedBy: null, note: 'Separate subsystem — live, pending own redesign.' },
  { path: '/archive/risk',              label: 'Risk',                  importName: 'Risk',                 category: 'ops',     replacedBy: 'Config + Dashboard' },
  { path: '/archive/trading-config',    label: 'Trading Config (raw)',  importName: 'TradingConfig',        category: 'ops',     replacedBy: 'Config' },
  { path: '/archive/legacy-config',     label: 'Legacy Config',         importName: 'LegacyConfig',         category: 'ops',     replacedBy: 'Config' },
  { path: '/archive/config-develop',    label: 'Config (develop)',      importName: 'Config',               category: 'ops',     replacedBy: 'Config' },
  { path: '/archive/audit-checklist',   label: 'Audit Checklist',       importName: 'AuditChecklist',       category: 'ops',     replacedBy: 'Audit Tasks' },
  { path: '/archive/margin-strategies', label: 'Margin Strategies',     importName: 'MarginStrategies',     category: 'ops',     replacedBy: 'Strategies', note: 'Margin subsystem.' },
  { path: '/archive/deployments',       label: 'Deployments',           importName: 'Deployments',          category: 'ops',     replacedBy: 'System', note: 'CI/CD surface.' },
  // Notes promoted to /notes (active nav under CONTROL).
  // Schema promoted to /schema (active nav under CONTROL).
  { path: '/archive/ops',               label: 'Agent Ops',             importName: 'AgentOps',             category: 'ops',     replacedBy: 'Audit Tasks' },
  { path: '/archive/telegram',          label: 'Telegram',              importName: 'Telegram',             category: 'ops',     replacedBy: null, note: 'TG channel control — no equivalent in new UI.' },
  // Polymarket subtree
  { path: '/archive/polymarket/overview',         label: 'Polymarket Overview',      importName: 'PolymarketOverview',   category: 'polymarket', replacedBy: 'Dashboard' },
  { path: '/archive/polymarket/monitor',          label: 'Polymarket Monitor',       importName: 'PolymarketMonitor',    category: 'polymarket', replacedBy: 'Signal Explorer' },
  { path: '/archive/polymarket/floor',            label: 'Live Floor',               importName: 'LiveFloor',            category: 'polymarket', replacedBy: 'Dashboard' },
  { path: '/archive/polymarket/evaluate',         label: 'Polymarket Evaluate',      importName: 'PolymarketEvaluate',   category: 'polymarket', replacedBy: 'Strategies' },
  { path: '/archive/polymarket/strategy-lab',     label: 'Strategy Lab',             importName: 'StrategyLab',          category: 'polymarket', replacedBy: 'Strategies' },
  { path: '/archive/polymarket/strategy-history', label: 'Strategy History',         importName: 'StrategyHistory',      category: 'polymarket', replacedBy: 'Trades' },
  { path: '/archive/polymarket/strategies',       label: 'Strategy Configs',         importName: 'StrategyConfigs',      category: 'polymarket', replacedBy: 'Config' },
  // GatePipelineMonitor promoted to /gate-matrix (nav entry "Gate Matrix").
  { path: '/archive/polymarket/data-health',      label: 'Data Health',              importName: 'DataHealth',           category: 'polymarket', replacedBy: 'System' },
  { path: '/archive/polymarket/command',          label: 'Strategy Command',         importName: 'StrategyCommand',      category: 'polymarket', replacedBy: 'Config' },
  // Data surfaces — raw feed inspection pages (each version superseded by next)
  { path: '/archive/data/v1',         label: 'Data Surface V1',       importName: 'V1Surface',            category: 'data',    replacedBy: 'Data Surface V2' },
  { path: '/archive/data/v2',         label: 'Data Surface V2',       importName: 'V2Surface',            category: 'data',    replacedBy: 'Data Surface V3' },
  { path: '/archive/data/v3',         label: 'Data Surface V3',       importName: 'V3Surface',            category: 'data',    replacedBy: 'Data Surface V4' },
  { path: '/archive/data/v4',         label: 'Data Surface V4',       importName: 'V4Surface',            category: 'data',    replacedBy: null, note: 'Latest raw data surface — no successor yet.' },
  { path: '/archive/data/assembler1', label: 'Assembler 1',           importName: 'Assembler1',           category: 'data',    replacedBy: null, note: 'Pipeline assembler — no successor in new UI.' },
];

// Polymarket StrategyFloor renders with per-route strategyId prop.
// Can't use the generic ARCHIVED_PAGES map for these — App.jsx handles them
// as explicit <Route> entries but still lists them in the Archive Center table.
export const ARCHIVED_STRATEGY_FLOORS = [
  { path: '/archive/polymarket/down-only', label: 'DOWN Strategy Floor',     strategyId: 'v4_down_only', category: 'polymarket', replacedBy: 'Strategies' },
  { path: '/archive/polymarket/up-asian',  label: 'UP Asian Strategy Floor', strategyId: 'v4_up_asian',  category: 'polymarket', replacedBy: 'Strategies' },
];
