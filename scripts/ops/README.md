# scripts/ops/ — operational scripts

Run on **Montreal only** (`15.223.247.178`) unless stated otherwise. All scripts source `engine/.env` for credentials. Never copy `DATABASE_URL` or `POLY_PRIVATE_KEY` to other hosts.

## Quick start

```bash
# SSH to Montreal (60s key window)
ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2ic_key.pub
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no novakash@15.223.247.178

# On Montreal:
cd /home/novakash/novakash
set -a && source engine/.env && set +a
```

## Scripts

### `check_pending.py` — scan for unredeemed wins

Authoritative redeemable-position scanner. Uses on-chain `CTF.payoutDenominator` across 4 RPCs as truth — never trusts data-api `redeemable` flag alone (lags ~15 min).

```bash
python3 scripts/ops/check_pending.py
```

Output:
```
Total positions (size>0.01): 190
Candidate positions (cv>$0.5): 2

  REDEEMABLE        0xabc123..  Down  cv=$ 5.71  cost=$ 4.17  denoms=[1, 1, 1, 1]  ...
  OPEN/UNRESOLVED   0xdef456..  Up    cv=$ 3.29  cost=$ 4.66  denoms=[0, 0, 0, 0]  ...

Redeemable (on-chain truth): 1
Paste into onchain_redeem.py CONDITION_IDS:
    "0xabc123...",
```

**Algorithm:**
1. GET `data-api.polymarket.com/positions?user={proxy}` — list of held conditionIds.
2. Filter `currentValue > 0.5 AND size > 0.01` — eliminates $0 losing-side dust.
3. For each candidate: `CTF.payoutDenominator(conditionId)` across 4 RPCs.
4. `REDEEMABLE` if >= 2 RPCs return > 0. `OPEN/UNRESOLVED` if all 0.

### `onchain_redeem.py` — direct MATIC-route redemption

Redeems winning CTF positions via raw EOA transaction. Bypasses the 100/day Builder Relayer cap. ~$0.005 MATIC gas per redeem.

```bash
# 1. Run check_pending.py to get conditionIds
# 2. Paste them into CONDITION_IDS in onchain_redeem.py
# 3. Run:
python3 scripts/ops/onchain_redeem.py
```

**Pattern:** `EOA -> factory.proxy([(1, CTF, 0, redeemPositions_calldata)])`. Moves USDC from proxy to same proxy. No fund movement to other addresses.

**Safety:**
- Only mutation = `CTF.redeemPositions(...)`. Cannot trade, withdraw, or bridge.
- 2-of-3 RPC consensus on `payoutDenominator > 0` before any tx.
- Local nonce counter avoids `nonce too low` on back-to-back redeems.
- POA middleware auto-injected for Polygon.

**Known failure modes:**
| Failure | Cause | What happens |
|---|---|---|
| $0 payout on success | Sweeper race (~10-15%) | Harmless, gas wasted |
| `nonce too low` | External tx between script start and submit | Retry with fresh nonce |
| `insufficient funds for gas` | Stale RPC cache | Retry on different RPC |
| Tx reverts | Market not actually resolved | Skip, wait 60 min |

### `wallet_truth.py` — canonical wallet P&L

Single source of truth for "how much money do I have / am I winning". Uses data-api activity + on-chain USDC balance, NOT the trades DB (which lies).

```bash
python3 scripts/ops/wallet_truth.py --hours 24
```

### `shadow_analysis.py` — per-strategy/regime/conviction WR

Strategy-level win-rate analysis using the v3/v4 signal columns.

```bash
python3 scripts/ops/shadow_analysis.py
```

### `strategy_pnl_24h.py` — quick 24h P&L by strategy

```bash
python3 scripts/ops/strategy_pnl_24h.py
```

## Contracts (Polygon mainnet)

| Contract | Address |
|---|---|
| Polymarket Proxy Factory | `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052` |
| ConditionalTokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e (collateral) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| NegRisk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |

## Wallet addresses

| Role | Address |
|---|---|
| EOA (signer) | `0xA515c16E9395e264765C40E292e9D944908880F9` |
| Proxy (funder) | `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10` |

## Related docs

- `docs/agents/redemption-ops-agent.md` — Paperclip agent spec (outsourced redemption loop)
- `docs/agents/redemption-ops-agent-initiation.md` — paste-ready agent init prompt
- `docs/agents/database-access.md` — DB connection runbook
- `docs/superpowers/specs/2026-04-20-wallet-page-v2-spec.md` — wallet FE spec
- `docs/superpowers/specs/2026-04-20-yaml-surface-outcome-cross-referencer.md` — strategy tuning analyser spec
