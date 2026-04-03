# Long-Term Memory (Combined)

## Trading Lessons

### What Works
- T-60s single-shot evaluation — simple, profitable, +$93 morning
- GTC limit orders at Gamma API best price
- Window delta as dominant signal
- VPIN >= 0.50 gate before evaluating
- Safe mode (25% bankroll) for consistency
- Paper mode for overnight testing

### What Doesn't Work
- Continuous evaluator loops (over-engineering)
- FAK market orders (bad fills, slippage)
- Ignoring DB config sync (railway env vars get overridden)
- Token price scaling that exceeds bet fraction ceiling

### Key Fixes Applied (2026-04-02)
- `risk_manager.py` — Fixed `sync_bankroll()` to skip in paper mode
- `risk_manager.py` — Added `set_paper_bankroll()` method
- `settings.py` — Added `paper_bankroll` config field
- `orchestrator.py` — Wired paper bankroll (fixed `settings` → `self._settings`)
- `runtime_config.py` — Added SKIP_DB_CONFIG_SYNC option
- Railway — Set PAPER_BANKROLL=160, BET_FRACTION=0.20 (later reverted to 0.10)

## Design Lessons

### Animation Principles
- Scale 0.95→1, never 0→1
- Enter: 200-350ms ease-out-quart
- Exit: 150-200ms ease-in
- Always `prefers-reduced-motion`

### Component Patterns
- shadcn/ui as foundation, extend via CSS vars
- Dark mode from day one
- Systematic spacing: 4/8/12/16/24/32/48/64px
- WCAG AA contrast 4.5:1 minimum

## Billy's Preferences
- Direct, no waffle
- Show the design decision, not just code
- Flag bad animations before they ship
- Log everything to Mission Control
- Ask before pushing to production
- If something looks wrong, say so
