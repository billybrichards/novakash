# Redemption Ops Agent — spec

**Owner:** billy
**Status:** draft v1 (2026-04-20)
**Primary branch target:** `develop`
**Related:** audit-task #252 (engine redeemer scan miss), #259 (wallet v2 DB-only bug), note #189 (wallet v2 spec)

A self-contained runbook for a cold-start agent (Paperclip, Claude, any LLM host) that handles Polymarket CTF-position redemption for the novakash prediction-market bot. Scope: scan for unredeemed wins, verify on-chain, redeem via direct MATIC-route tx, report.

The agent has **no prior session context**. Everything it needs is in this file plus the Hub API + Montreal SSH credentials the operator provides at init time.

---

## 0. Hard rules — read before any action

1. **Never trade, withdraw, or move funds to a different address.** This agent's only authorised mutation is `CTF.redeemPositions(...)` which moves USDC from the novakash proxy to the **same** proxy. No bridging. No transfers out. No buy/sell orders.
2. **Never push to `main` or `develop` directly.** Branch + PR, even for doc changes.
3. **Never run Playwright or any `*.polymarket.com` client from a non-Montreal host.** See `feedback_no_local_polymarket.md` in operator memory: read-only / no-auth is NOT an exception. CLOB, gamma, data-api, relay — all banned except via Montreal (15.223.247.178). Polygon RPC chain reads are fine.
4. **Do not trust a single Polygon RPC** for any post-tx balance read. Cache lag on public RPCs (publicnode, drpc, 1rpc) has produced false "balance unchanged" panics twice. Always cross-check ≥ 2 RPCs, or verify via decoded tx logs.
5. **On-chain `CTF.payoutDenominator(conditionId) > 0` is the ONLY authoritative "is this redeemable" signal.** The `data-api.polymarket.com/positions[].redeemable` flag lags resolution by up to ~15 min. Using it alone has caused this agent's predecessors to miss ~15-20% of redeemable wins.
6. **Auto-sweeper race is real.** Polymarket's own sweeper or the engine's in-process redeemer may redeem a position between the time you scan and the time your tx lands. Your tx will still succeed with $0 payout, wasting ~$0.005 MATIC. Rate: ~10-15% of scanned candidates. Accept it; don't retry.
7. **If in doubt, stop and report.** Do not improvise. Do not "fix" unrelated bugs. Do not commit code that wasn't explicitly requested. Every action above "read the scan output" should be preceded by a status message to the operator.

---

## 1. What the agent does, one loop

```
1. SSH to Montreal (see §3 for key bootstrap)
2. Run /tmp/check_pending.py (see §5) — prints redeemable conditionIds
3. If zero: report, exit.
4. If > 0: update /tmp/onchain_redeem.py CONDITION_IDS list (see §6)
5. SCP updated script to Montreal
6. Run it on Montreal with engine/.env sourced
7. Parse output: per-row {tx_hash, payout_usd, status ∈ {ok, reverted, $0_sweeper_race}}
8. Post summary to Hub as a note (§7) + optional Telegram via engine's alerts hook
9. Sleep N minutes, goto 1
```

Acceptable cadence: every 10–15 min. More aggressive = more sweeper-race $0s. Less aggressive = wins sit longer.

---

## 2. Inputs the operator must provide at init

| Variable | Example | Source |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | `AKIA…` | operator's AWS console |
| `AWS_SECRET_ACCESS_KEY` | `…` | same |
| `AWS_REGION` | `ca-central-1` | fixed — Montreal is in `ca-central-1b` |
| `HUB_USERNAME` | `billy` | hub login |
| `HUB_PASSWORD` | (provided at init) | hub login |
| `POLY_FUNDER_ADDRESS` | `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10` | **READ ONLY** — proxy address, confirm this matches before any tx |
| `POLY_SIGNER_EOA` | `0xA515c16E9395e264765C40E292e9D944908880F9` | **READ ONLY** — EOA controlling proxy |

The agent does NOT need `POLY_PRIVATE_KEY` on its own host. That key lives on Montreal only (`/home/novakash/novakash/engine/.env`). The agent SSHes in, sources that env, and runs the local redeem script. Private key never leaves Montreal.

**Verification step at init:** before first action, the agent must:
1. SSH to Montreal and `cat /home/novakash/novakash/engine/.env | grep POLY_FUNDER_ADDRESS`.
2. Assert value exactly equals `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10`.
3. If mismatch, stop, report, do nothing. Anything else = session hijack or test key (see `reference_onchain_redeem.md` line 20 — stale `engine/.env.local` files contain test keys `0xb210b8e0…` / `0x330ec131…`).

---

## 3. Montreal SSH — EC2 Instance Connect, 60-second window

Montreal is an AWS EC2 instance, not a traditional SSH-key server. You push a fresh public key via the Instance Connect API, and it's valid for 60 seconds.

```bash
# A. Generate a one-shot key (or reuse if already present)
ssh-keygen -t rsa -b 2048 -f /tmp/ec2ic_key -N "" -q

# B. Push it — grants 60s SSH access for user `novakash`
aws ec2-instance-connect send-ssh-public-key \
  --region ca-central-1 \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/ec2ic_key.pub

# C. Connect WITHIN 60s
ssh -i /tmp/ec2ic_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
  novakash@15.223.247.178 'YOUR COMMAND HERE'
```

Every new SSH command needs A + B + C. Don't try to `keep` the session — just rerun A+B before each batch. A+B+C together take ~2 s.

| Thing | Value |
|---|---|
| Instance ID | `i-0785ed930423ae9fd` |
| Public IP (Elastic IP — stable) | `15.223.247.178` |
| OS user | `novakash` |
| Region / AZ | `ca-central-1` / `ca-central-1b` |
| Engine repo root | `/home/novakash/novakash/` |
| Engine `.env` | `/home/novakash/novakash/engine/.env` |
| Engine log (current) | `/home/novakash/engine.log` |

**Troubleshooting:**
- `Permission denied (publickey)` — key expired (> 60 s since push). Redo A + B.
- `Connection refused` — instance stopped. Check AWS console; don't proceed.
- `Host key verification failed` — use `-o StrictHostKeyChecking=no`; Elastic IP means it's correct.

---

## 4. Hub API — for reporting + audit-task updates

Hub is on a separate AWS box (not Montreal). Used by this agent for reading wallet_truth, posting session notes, updating audit tasks. **Do NOT call Hub endpoints for "is this redeemable" checks** — they are currently DB-only and wrong (see audit #259). Use the scan script instead.

```bash
TOKEN=$(curl -s -X POST http://16.54.141.121:8091/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"billy","password":"<password>"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Post a note
curl -s -X POST http://16.54.141.121:8091/api/notes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"...","body":"...","tags":"redemption,agent","author":"paperclip-agent"}'

# Update an audit task
curl -s -X PATCH "http://16.54.141.121:8091/api/audit-tasks/<id>" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"IN_PROGRESS"}'
```

Token expires in 15 min. Re-fetch if a call returns 401.

**Direct IP `16.54.141.121:8091` only** — the nginx proxy at `99.79.41.246` is stale and returns 404 for `/api/*`.

---

## 5. `/tmp/check_pending.py` — authoritative redeemable scan

Canonical source: `/tmp/check_pending.py` on Montreal as of 2026-04-20. Promote to `scripts/ops/check_pending.py` when the agent ships.

**Algorithm:**
1. GET `data-api.polymarket.com/positions?user={proxy}&sizeThreshold=0.01&limit=500` — gets list of held conditionIds.
2. Filter: `currentValue > 0.5 AND size > 0.01` (eliminates losing-side dust tokens).
3. For each candidate: call `CTF.payoutDenominator(conditionId)` across 4 RPCs (Alchemy from `POLYGON_RPC_URL` + publicnode + drpc + 1rpc).
4. Classify `REDEEMABLE` only if **≥ 2 RPCs return > 0**. All-zero across 2+ = `OPEN/UNRESOLVED`. Mixed = inconclusive, skip and retry next loop.
5. Print table + paste-ready conditionId list for `onchain_redeem.py`.

Key behavior: **ignore `data-api.positions[].redeemable` flag for gating.** Track it as a diagnostic column ("data-api lag") but never gate redemption decision on it.

Run:
```bash
cd /home/novakash/novakash
set -a && source engine/.env && set +a
python3 /tmp/check_pending.py
```

Expected output:
```
Total positions (size>0.01): 131
Candidate positions (cv>$0.5): 1

  REDEEMABLE        0xabc…  Down  cv=$ 4.85  cost=$ 4.17  denoms=[1, 1, 1, 1]  dataapi_flag=False  Bitcoin Up or Down - ...

Redeemable (on-chain truth): 1
Paste into onchain_redeem.py CONDITION_IDS:
    "0xabc…",
```

---

## 6. `/tmp/onchain_redeem.py` — direct MATIC-route redeem

Canonical source: `/tmp/onchain_redeem.py` on Montreal + operator Mac as of 2026-04-20. Promote to `scripts/ops/onchain_redeem.py` when the agent ships.

**Pattern** (proven 2026-04-16, re-proven 2026-04-20, 11 successful redeems + 2 sweeper-race $0s):

```python
# 1. inner calldata — CTF.redeemPositions(USDC_E, 0x00…, conditionId, [1,2])
sel_redeem = keccak(b"redeemPositions(address,bytes32,bytes32,uint256[])")[:4]
inner = sel_redeem + abi_encode(
    ["address","bytes32","bytes32","uint256[]"],
    [USDC_E, b"\x00"*32, bytes.fromhex(conditionId[2:]), [1, 2]],
)

# 2. outer — factory.proxy([(typeCode=1=CALL, to=CTF, value=0, data=inner)])
sel_proxy = keccak(b"proxy((uint8,address,uint256,bytes)[])")[:4]
outer = sel_proxy + abi_encode(
    ["(uint8,address,uint256,bytes)[]"],
    [[(1, CTF, 0, inner)]],
)

# 3. Sign with POLY_PRIVATE_KEY (EOA), submit to Polygon
tx = {
    "from": EOA, "to": FACTORY, "value": 0, "data": "0x" + outer.hex(),
    "gas": 450_000,
    "maxFeePerGas": base_fee * 2 + prio, "maxPriorityFeePerGas": prio,
    "nonce": nonce_pending, "chainId": 137, "type": 2,
}
```

**Polygon mainnet contract refs:**

| Contract | Address |
|---|---|
| Polymarket Proxy Factory | `0xaB45c5A4B0c941a2F231C04C3f49182e1A254052` |
| ConditionalTokens (CTF) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` |
| USDC.e (collateral) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |
| NegRisk Adapter (for non-binary markets only) | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |

**web3.py gotchas:**
- `ExtraDataToPOAMiddleware` (or `geth_poa_middleware` on web3.py < 7) must be injected at layer 0 — Polygon's `extraData` is 902 bytes, not 32.
- Nonce must come from `pending` block, not `latest`.
- `raw_transaction` on web3.py ≥ 6, `rawTransaction` on ≤ 5. Defensive `.raw_transaction if hasattr(...) else .rawTransaction`.
- `"insufficient funds for gas: balance 0"` is a spurious stale-cache error in ~2% of submits. Re-query balance via a second RPC; if fine, retry submit to a different RPC. Do NOT increment nonce — tx was never accepted.

**Success detection** — don't trust balance delta. Decode the tx receipt logs:
- Look for `Transfer(USDC_E, from=CTF, to=PROXY, value=X)` event.
- `payout_usd = X / 1e6`.
- No such log → $0 sweeper race (still counts as `status=ok`, payout=$0).

**Expected runtime:** ~5–8 seconds per redeem (nonce increment + block confirm).

---

## 7. Reporting to operator

After every scan loop, post to Hub `/api/notes`:

```json
{
  "title": "Redemption sweep — YYYY-MM-DDTHH:MM:SSZ",
  "body": "Scanned N, redeemable M, redeemed K, payout $X.XX, sweeper_races S, errors E\n\n| tx | cid | payout | status |\n...",
  "tags": "redemption,agent,paperclip",
  "author": "paperclip-agent"
}
```

On zero-redeemable scans: only post a note once per hour to avoid spam. Silent loops OK.

Escalate to Telegram (engine has a Telegram bot) only on:
- MATIC balance on EOA < 0.5 (gas running low)
- >3 consecutive sweeper-race $0s (engine redeemer bug returning)
- Any `ERROR: …` line from `onchain_redeem.py` that's not in the known-good error list

---

## 8. Known failure modes + expected agent response

| Failure | Cause | Agent response |
|---|---|---|
| `Permission denied (publickey)` on SSH | 60s key expired | Re-push key via `aws ec2-instance-connect send-ssh-public-key`, retry. |
| `Connection refused` on SSH | Instance stopped | Stop loop, Telegram alert, do NOT try to start it. |
| 401 from Hub API | Token expired (15 min) | Re-auth via `/auth/login`. |
| `HTTP 403` from RPC | Missing User-Agent header | Add `"User-Agent": "Mozilla/5.0"` to request. |
| `ExtraDataLengthError` | POA middleware missing | Inject `ExtraDataToPOAMiddleware` at layer 0. |
| $0 payout on successful tx | Sweeper race | Log, move on, don't retry same cid. |
| Tx reverts | Market not actually resolved (RPC returned stale `payoutDenominator`) | Skip cid for 60 min (cache cooldown). Re-scan after. |
| `data-api` returns 5xx | Polymarket API hiccup | Exponential backoff: 30s, 60s, 120s. Max 3 retries then pause for 10min. |
| `POLY_FUNDER_ADDRESS` mismatch | Wrong env, session hijack | **STOP. Telegram alert. Do nothing.** |
| MATIC balance < 0.1 on EOA | Gas exhausted | Telegram alert; agent does NOT auto-refill. |

---

## 9. What this agent MUST NOT do

- Create buy/sell orders on Polymarket, Opinion, or any other venue.
- Withdraw USDC from proxy to any other address.
- Redeem on behalf of any address other than `0x181D2ED714E0f7Fe9c6e4f13711376eDaab25E10`.
- Modify `.env` on Montreal.
- Modify engine strategy YAML or strategy code.
- Commit code to `main`, `develop`, or any branch — doc updates go as PRs only, and ONLY if operator explicitly requests.
- Consume more than 100 calls/min against data-api.polymarket.com (rate-limit guard).
- Consume more than 5 redeem txs/min (sweeper etiquette).

---

## 10. Ship checklist — operator verifies before first unattended run

- [ ] `POLY_FUNDER_ADDRESS` assertion wired at init.
- [ ] `/tmp/check_pending.py` present on Montreal, tested against live.
- [ ] `/tmp/onchain_redeem.py` present, tested against live (1 successful redeem this session).
- [ ] EOA MATIC balance ≥ 1.0 (enough for ~200 redeems).
- [ ] Hub API reachable from agent host.
- [ ] AWS credentials valid (EC2 Instance Connect works).
- [ ] Audit #259 acknowledged — agent understands Hub `/api/wallet/*` cannot be trusted until #259 lands.
- [ ] Telegram alert channel configured + tested with dry-run message.

---

## 11. Related files in this repo

- `docs/agents/redemption-ops-agent-initiation.md` — copy-paste initiation task for the agent (pair to this spec).
- `docs/superpowers/specs/2026-04-20-wallet-page-v2-spec.md` — spec that audit #259 tracks against.
- `engine/execution/redeemer.py` — engine's in-process redeemer (the thing this agent works around until #252 is fixed).
- `scripts/ops/wallet_truth.py` — canonical wallet P&L; agent uses read-only.
- `scripts/ops/onchain_redeem.py` (promoted target) — replace `/tmp/onchain_redeem.py` when agent ships.
- `scripts/ops/check_pending.py` (promoted target) — replace `/tmp/check_pending.py` when agent ships.
