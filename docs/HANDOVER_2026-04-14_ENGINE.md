# Engine Handover — 2026-04-14

## Current State

- Engine is currently in `PAPER` mode on Montreal.
- TimesFM service is back online and healthy.
- Strategy registry execution wiring bug has been fixed in code and pushed.
- Conservative `trade_advised` gating has been restored to the strategy YAMLs.
- Live trading is **not ready** to be re-enabled yet.

## What Happened

### 1. TimesFM instability

- TimesFM host: `3.98.114.0`
- Instance ID: `i-05dbd2ca41f8a75ec`
- Symptom: `/health` and `/v4/snapshot` timed out from both local and Montreal.
- AWS status at failure time: instance initially showed `impaired` and later became SSH-unreachable while EC2 health still showed `ok`.
- EC2 console output showed an OOM kill of uvicorn.

Relevant recovery:

```bash
aws ec2 stop-instances --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-stopped --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 start-instances --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-running --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
aws ec2 wait instance-status-ok --region ca-central-1 --instance-ids i-05dbd2ca41f8a75ec
```

Post-recovery verification:

- `docker compose ps` on `/home/ubuntu/timesfm-service`
- `curl http://localhost:8080/health`
- `curl http://localhost:8080/v4/health`
- memory observed healthy around `1.8 GiB / 7.6 GiB`

### 2. Live duplicate / oversize trade incident

The real issues were:

- `ExecuteTradeUseCase` was wired before `_window_state_repo` existed.
- This caused `execute_trade.dedup_check_error` and `execute_trade.mark_traded_error`.
- Duplicate orders were placed in the same window because dedup was effectively broken.
- Stake sizing used a stale bankroll path and a hardcoded `$50` cap in `ExecuteTradeUseCase`.
- That made the config look safe while the actual execution path was not.

Confirmed duplicate live window examples from Montreal:

- `btc-updown-5m-1776129300` / `v4_down_only` -> 5 orders, total stake `$186.00`
- `btc-updown-5m-1776129000` / `v4_down_only` -> 2 orders, total stake `$68.34`

### 3. Real live outcome

Confirmed resolved live trade:

- strategy: `v4_down_only`
- trade id: `4421`
- order id: `0xa493ae12aac099ac856960f3b35b21af034d3d3bfb6f564607f81ed61c76dc27`
- created: `2026-04-14 00:58:05+00`
- outcome: `LOSS`
- pnl: `-$2.32`

Current wallet check from Montreal via live Polymarket client:

- wallet balance: about `$29.62 USDC`

## Fixes Already Applied In Code

### Pushed commits

- `ff07708` — defer `ExecuteTradeUseCase` wiring until after DB/repo init
- `b5aee5b` — restore `trade_advised` gate on `v4_down_only`
- `b2aa97e` — enable all 5 strategies to `LIVE` with `trade_advised` in repo YAMLs

### Additional local changes ready to commit

1. `engine/config/runtime_config.py`

- map `absolute_max_bet` -> `max_position_usd`
- this makes the DB config's true hard cap flow into runtime

2. `engine/use_cases/execute_trade.py`

- remove stale hardcoded `$50` ceiling logic
- use `runtime.max_position_usd` as the real execution hard cap
- use `runtime.max_drawdown_kill` instead of the old baked-in constant

3. `engine/adapters/persistence/pg_trade_repo.py`

- stop misclassifying paper trades as live in the `trades` table
- derive `mode` and `is_live` from `execution_mode`

## Paper Overnight Findings

Paper mode did place trades overnight, but the persistence/reconciliation data is not yet trustworthy.

Observed using `execution_mode='paper'`:

- `v4_down_only`: 15 paper trades
- `v4_fusion`: 22 paper trades
- resolved wins: 0
- resolved losses: 0
- realized paper pnl: `$0.00`

Meaning:

- paper execution happened
- but paper resolution did not close out cleanly
- the overnight paper run cannot yet be used as a trustworthy win/loss proof

## Montreal Ops Details

### Montreal engine box

- host: `15.223.247.178`
- instance id: `i-0785ed930423ae9fd`
- os user: `novakash`

### TimesFM box

- host: `3.98.114.0`
- instance id: `i-05dbd2ca41f8a75ec`
- os user: `ubuntu`

### Useful checks

Engine status:

```bash
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-0785ed930423ae9fd \
  --instance-os-user novakash \
  --ssh-public-key file:///tmp/key.pub \
  --availability-zone ca-central-1b \
  --region ca-central-1
ssh -i /tmp/key novakash@15.223.247.178
```

TimesFM status:

```bash
aws ec2-instance-connect send-ssh-public-key \
  --instance-id i-05dbd2ca41f8a75ec \
  --instance-os-user ubuntu \
  --ssh-public-key file:///tmp/key.pub \
  --availability-zone ca-central-1d \
  --region ca-central-1
ssh -i /tmp/key ubuntu@3.98.114.0
cd /home/ubuntu/timesfm-service && docker compose ps
curl http://localhost:8080/health
curl http://localhost:8080/v4/health
```

## Next Steps

1. Commit and push the remaining local safety fixes:
   - `runtime_config.py`
   - `execute_trade.py`
   - `pg_trade_repo.py`

2. Deploy those fixes to Montreal.

3. Keep engine in `PAPER` mode.

4. Verify after several windows that:
   - dedup works
   - `mark_traded` works
   - paper trades are recorded as paper
   - paper trades resolve correctly against outcomes
   - no trade exceeds the configured hard cap

5. Only after that, do a controlled live re-enable.

## Bottom Line

The system is no longer in a dangerous live state.

But it is also **not yet safe to re-enable live trading** until the remaining hard-cap and persistence fixes are deployed and verified in paper mode.
