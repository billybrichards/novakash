#!/usr/bin/env bash
# Run this script to create the feat/live-polymarket-orders branch and PR.
# All file changes are already committed — just run this once.

set -euo pipefail

cd "$(dirname "$0")"

echo "==> Switching to develop and pulling latest..."
git checkout develop
git pull origin develop

echo "==> Creating feature branch..."
git checkout -b feat/live-polymarket-orders

echo "==> Staging all changes..."
git add -A

echo "==> Committing..."
git commit -m "feat: implement live Polymarket CLOB order placement

- Add py-clob-client>=0.18.0 to requirements
- Implement ClobClient connection with API credentials in connect()
- Implement _live_place_order() with signed CLOB order submission
- Fix Gamma API clobTokenIds extraction in polymarket_5min.py
- Pass token_id parameter from strategy through to client
- Safety: max \$50 trade size cap, LIVE_TRADING_ENABLED env var required
- First live trade emits RuntimeWarning + structured WARNING log
- Paper mode unchanged and still default"

echo "==> Pushing branch..."
git push origin feat/live-polymarket-orders

echo "==> Creating PR..."
gh pr create \
  --base develop \
  --head feat/live-polymarket-orders \
  --title "feat: Live Polymarket CLOB order placement" \
  --body "## Summary
Implements live order placement on Polymarket's CLOB API for 5-minute BTC Up/Down markets.

## Changes
- Add \`py-clob-client>=0.18.0\` to \`engine/requirements.txt\`
- Implement \`PolymarketClient.connect()\` for live CLOB initialisation
- Implement \`_live_place_order()\` with signed order submission via py-clob-client
- Fix Gamma API token ID extraction in \`polymarket_5min.py\` (\`clobTokenIds[0]\` = YES, \`[1]\` = NO)
- Pass \`token_id\` from strategy through to \`place_order()\`
- Safety guards: \$50 max trade size cap, \`LIVE_TRADING_ENABLED=true\` env var required, first-trade warning

## Safety
- Live mode is blocked at construction unless \`LIVE_TRADING_ENABLED=true\` is set
- Single trade hard-capped at \$50 (raises \`ValueError\` if exceeded)
- First live trade emits a \`RuntimeWarning\` + structured \`WARNING\` log for manual verification

## Testing
- Paper mode: unchanged, still default
- Live mode: requires \`PAPER_MODE=false\` AND \`LIVE_TRADING_ENABLED=true\`
- First live trade should be manually verified with \$1 stake

## To Go Live
1. Verify paper win rate ~82% over several hours
2. Set \`PAPER_MODE=false\` and \`LIVE_TRADING_ENABLED=true\` on Railway
3. Monitor first few live trades
4. Manual daily claiming on polymarket.com/portfolio"

echo "==> Done! PR created."
