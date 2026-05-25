# Telegram Notifications Fix Plan

**Base branch:** `fix/telegram-notifications-rework`  
**Reported issues:** asset not discriminated, ghost mode not useful, double-firing, unreliable delivery  
**Target:** Production rollout by 2026-05-28

---

## Executive Summary

Four core bugs reported in the Telegram notification pipeline:

1. **Asset not discriminated** - Window signals and resolved trades don't include asset identifier
2. **Ghost mode not useful** - GHOST/LIVE mode isn't clearly labeled in messages
3. **Double-firing** - Same trade resolutions fire multiple cards
4. **Unreliable delivery** - Queue worker doesn't handle exceptions properly

This plan provides actionable, file-specific fixes with testing strategy and rollout approach.

---

## Priority-Ordered Action Items

### P0: Fix Asset Discrimination (Critical)

**Affected:** `engine/adapters/alert/telegram_renderer.py:282-319`

**Problem:** `WindowSignalPayload` `render()` excludes asset identifier in header but includes it in footer. Users can't distinguish BTC vs ETH vs SOL windows without checking window IDs manually.

**Location:** Lines 291-304

**Current behavior:**
```python
strat_lines: list[str] = []
live = [s for s in p.strategies if s.mode == "LIVE"]
ghost = [s for s in p.strategies if s.mode == "GHOST"]
disabled = [s for s in p.strategies if s.mode == "DISABLED"]
```

**Fix:** Include asset in window header line

```python
def _render_window_signal(self, p: WindowSignalPayload) -> str:
    model = ""
    if p.p_up is not None:
        model = f"P(UP)={p.p_up:.2f}"
        if p.p_up_distance is not None:
            model += f", dist={p.p_up_distance:.2f}"
    vpin = f"VPIN {p.vpin:.2f}" if p.vpin is not None else ""
    meta_line = "  |  ".join(x for x in (model, vpin) if x)

    strat_lines: list[str] = []
    live = [s for s in p.strategies if s.mode == "LIVE"]
    ghost = [s for s in p.strategies if s.mode == "GHOST"]
    disabled = [s for s in p.strategies if s.mode == "DISABLED"]
    
    # Extract asset from window_id (format: "BTC-1712345678")
    asset = "UNKNOWN"
    if p.header.window_id:
        parts = p.header.window_id.split("-", 1)
        if len(parts) >= 1:
            asset = parts[0]
    asset_tag = f"  [{asset}]" if asset != "UNKNOWN" else ""
    
    lines = [
        self._render_header(p.header, p.tier),
        DIVIDER,
        self._render_btc(p.btc),
    ]
    if meta_line:
        lines.append(meta_line)
    lines.append(self._render_health(p.health))
    if strat_lines:
        lines.append(SUB_DIVIDER)
        lines.extend(strat_lines)
    lines.append(DIVIDER)
    lines.append(self._render_footer(p.footer))
    return "\n".join(lines)
```

**Testing strategy:**
1. Unit test: Render `WindowSignalPayload` with `window_id="ETH-1712345678"` → verify header contains "ETH"
2. Integration test: Emit window signal in `5m v9_5_eth_blend` (GHOST mode) → verify Telegram contains ETH
3. Regression test: Confirm BTC window still shows BTC correctly

---

### P0: Fix Ghost Mode Labeling (Critical)

**Affected:** `engine/adapters/alert/telegram_renderer.py:295-304`

**Problem:** GHOST mode strategies are labeled "*GHOST (shadow):*" but no way to distinguish which strategies are in GHOST vs LIVE. This defeats the purpose of A/B testing.

**Location:** Lines 295-304

**Current behavior:**
```python
if live:
    strat_lines.append("*LIVE:*")
    for s in live:
        strat_lines.append(f"  {self._render_strategy(s)}")
if ghost:
    strat_lines.append("*GHOST (shadow):*")
    for s in ghost:
        strat_lines.append(f"  {self._render_strategy(s)}")
```

**Fix:** Include mode in each strategy line with explicit emoji

```python
def _render_window_signal(self, p: WindowSignalPayload) -> str:
    model = ""
    if p.p_up is not None:
        model = f"P(UP)={p.p_up:.2f}"
        if p.p_up_distance is not None:
            model += f", dist={p.p_up_distance:.2f}"
    vpin = f"VPIN {p.vpin:.2f}" if p.vpin is not None else ""
    meta_line = "  |  ".join(x for x in (model, vpin) if x)

    strat_lines: list[str] = []
    live = [s for s in p.strategies if s.mode == "LIVE"]
    ghost = [s for s in p.strategies if s.mode == "GHOST"]
    disabled = [s for s in p.strategies if s.mode == "DISABLED"]
    
    if live:
        strat_lines.append("🔥 LIVE:")
        for s in live:
            strat_lines.append(f"  {self._render_strategy(s)}")
    if ghost:
        strat_lines.append("👻 GHOST (shadow):")
        for s in ghost:
            strat_lines.append(f"  {self._render_strategy(s)}")
    if disabled:
        strat_lines.append(f"_disabled: {', '.join(s.strategy_id for s in disabled)}_")

    lines = [
        self._render_header(p.header, p.tier),
        DIVIDER,
        self._render_btc(p.btc),
    ]
    if meta_line:
        lines.append(meta_line)
    lines.append(self._render_health(p.health))
    if strat_lines:
        lines.append(SUB_DIVIDER)
        lines.extend(strat_lines)
    lines.append(DIVIDER)
    lines.append(self._render_footer(p.footer))
    return "\n".join(lines)
```

**Testing strategy:**
1. Unit test: Create payload with 2 LIVE + 2 GHOST strategies → verify emoji labels
2. Integration test: Render `WindowSignalPayload` with mixed modes → verify emoji prefixes
3. Snapshot test: Capture sample Telegram message for manual review

---

### P1: Fix Double-Firing (High Priority)

**Affected:** `engine/alerts/telegram.py:500-634`

**Problem:** `emit_per_trade_resolved_v2()` uses `(trade_id or window_ts, condition_id)` as dedup key, but `window_ts` isn't unique when re-evaluating same window multiple times (T-90, T-60, T-30). Same trade can fire twice.

**Location:** Lines 542-559

**Current behavior:**
```python
# LRU dedup
_dedup_key = (trade_id or int(window_ts or 0), condition_id or "")
if _dedup_key in self._resolved_dedup:
    self._log.debug(...)
    self._resolved_dedup.move_to_end(_dedup_key)
    return
```

**Fix:** Add an explicit "resolved_at_ts" field to the dedup key and ensure it's only stored once per resolution

```python
async def emit_per_trade_resolved_v2(
    self,
    *,
    direction: str,
    outcome: str,
    pnl: float,
    entry_price: float,
    cost: float,
    window_ts: int,
    strategy: str,
    order_id: str | None = None,
    timeframe: str = "5m",
    asset: str = "BTC",
    trade_id: Optional[str] = None,
    condition_id: Optional[str] = None,
    actual_open_usd: Optional[float] = None,
    actual_close_usd: Optional[float] = None,
) -> None:
    """Emit a rich individual v2 resolved card for a single trade.

    Called from ReconcilePositionsUseCase._notify_resolution so each
    resolved trade gets its own four-quadrant card (in addition to
    the batched reconcile-pass summary).

    Derives actual_direction from (predicted + outcome) since the
    reconciler doesn't have BTC open/close prices itself — but the
    ``actual_open_usd`` / ``actual_close_usd`` kwargs let callers feed
    canonical Polymarket HTML / window_snapshots prices so the card
    shows real numbers.

    Audit #350 follow-up: when canonical prices are NOT available we
    SKIP the card entirely rather than fabricate the $100,000 / $100,001
    synthetic placeholder this method previously used. The batched
    summary still fires unaffected.

    Dedup: key on ``(trade_id or (window_ts, strategy), condition_id)`` to
    prevent double-firing when the same trade is re-seen across multiple
    reconciler passes or eval ticks.
    """
    if not self._v2_ready():
        return

    # ── LRU dedup ──────────────────────────────────────────────
    # Prefer trade_id when available; fall back to (window_ts + strategy)
    # to avoid false negatives when the same window is re-evaluated
    # multiple times.
    if trade_id:
        _dedup_key = (trade_id, condition_id or "")
    else:
        # Without trade_id, mix in strategy to avoid collision across
        # different strategies resolving the same window_ts
        _dedup_key = (int(window_ts or 0), strategy or "unknown", condition_id or "")
    
    if _dedup_key in self._resolved_dedup:
        self._log.debug(
            "telegram.per_trade_resolved_v2.dedup_hit",
            trade_id=trade_id,
            window_ts=window_ts,
            strategy=strategy,
            condition_id=(condition_id or "")[:20],
        )
        self._resolved_dedup.move_to_end(_dedup_key)
        return
    self._resolved_dedup[_dedup_key] = None
    # Trim oldest when over cap.
    while len(self._resolved_dedup) > self._resolved_dedup_cap:
        self._resolved_dedup.popitem(last=False)
    
    # ... rest of method unchanged
```

**Key change:** When `trade_id` is not available, dedup key becomes `(window_ts, strategy, condition_id)` instead of `(window_ts, condition_id)`.

**Testing strategy:**
1. Unit test: Call `emit_per_trade_resolved_v2()` twice with same `window_ts` but different `strategy` → verify only 1 message fires
2. Unit test: Call with same `trade_id` twice → verify dedup works
3. Integration test: Simulate reconciler call for same trade 3× → verify 1 message

**Relevant test file:** `engine/tests/alerts/test_telegram_dedup.py`

---

### P1: Fix Queue Worker Exception Handling (High Priority)

**Affected:** `engine/alerts/telegram.py:1700-1790`

**Problem:** `_queue_worker()` swallows exceptions silently, leading to dropped messages with no error logged.

**Location:** Lines 1700-1790 (search for `_queue_worker`)

**Current behavior (approximate):**
```python
async def _queue_worker(self) -> None:
    while not self._shutting_down:
        try:
            priority, seq, msg = await asyncio.wait_for(
                self._send_queue.get(), timeout=1.0
            )
            await self._send_raw_message(msg)
            self._send_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            # Silently continue on error
            pass
```

**Fix:** Add structured error logging

```python
async def _queue_worker(self) -> None:
    while not self._shutting_down:
        try:
            priority, seq, msg = await asyncio.wait_for(
                self._send_queue.get(), timeout=1.0
            )
            try:
                await self._send_raw_message(msg)
            except aiohttp.ClientError as e:
                self._log.error(
                    "telegram.send_failed_client_error",
                    message_preview=msg[:100],
                    error=str(e)[:200],
                )
            except Exception as e:
                self._log.error(
                    "telegram.send_failed_unknown_error",
                    message_preview=msg[:100],
                    error=str(e)[:200],
                    exc_info=True,
                )
            finally:
                self._send_queue.task_done()
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            self._log.info("telegram.queue_worker_cancelled")
            break
        except Exception as exc:
            self._log.error(
                "telegram.queue_worker_unexpected_error",
                error=str(exc)[:200],
                exc_info=True,
            )
```

**Testing strategy:**
1. Unit test: Mock `_send_raw_message` to raise `aiohttp.ClientError` → verify error logged
2. Integration test: Simulate network disconnect → verify messages are queued with errors logged
3. Regression test: Normal messages still fire without error

---

### P2: Add Asset to Resolved Trade Cards (Medium Priority)

**Affected:** `engine/alerts/telegram.py:424-496`

**Problem:** `_emit_narrative_v2_resolved()` doesn't include asset in window_id format (uses only `f"{asset}-{window_ts}"` but no asset in displayed content).

**Location:** Lines 465, 619

**Current behavior:**
```python
window_id = f"{asset}-{window_ts}"
```

**Fix:** Include asset in payload or window_id display

Add to `BuildResolvedAlertInput`:

```python
async def _emit_narrative_v2_resolved(
    self,
    *,
    order,
    window_ts: int,
    asset: str,
    timeframe: str,
    open_price: float,
    close_price: float,
) -> None:
    from decimal import Decimal
    from use_cases.alerts.build_resolved_alert import (
        BuildResolvedAlertInput,
        BuildResolvedAlertUseCase,
    )

    _d = str(getattr(order, "direction", "") or "").upper()
    if _d in ("UP", "YES"):
        predicted = "UP"
    elif _d in ("DOWN", "NO"):
        predicted = "DOWN"
    else:
        predicted = "UP"
    actual = "UP" if close_price > open_price else "DOWN"
    try:
        pnl = Decimal(str(order.pnl_usd or 0))
    except Exception:
        pnl = Decimal("0")
    stake = Decimal(str(getattr(order, "stake_usd", 0) or 0))
    entry_price = float(order.price) if order.price else 0.50
    order_id = getattr(order, "order_id", None) or None
    strategy_id = (
        (order.metadata or {}).get("strategy_id", "unknown")
        if getattr(order, "metadata", None)
        else "unknown"
    )
    window_id_display = f"{asset}-{window_ts}"
    now_unix = int(self._narrative_v2_clock.now())

    inp = BuildResolvedAlertInput(
        timeframe=timeframe or "5m",
        strategy_id=str(strategy_id),
        mode="LIVE" if not getattr(self, "_paper_mode", False) else "GHOST",
        predicted_direction=predicted,
        actual_direction=actual,
        pnl_usdc=pnl,
        entry_price_cents=entry_price,
        stake_usdc=stake,
        window_id=window_id_display,
        window_asset=asset,  # NEW: Explicit asset field
        order_id=order_id,
        event_ts_unix=int(window_ts + (300 if (timeframe or "5m") == "5m" else 900)),
        actual_open_usd=open_price,
        actual_close_usd=close_price,
    )
    # ... rest unchanged
```

**Note:** Requires `WindowId` value object change to accept `window_asset` field.

**Testing strategy:**
1. Unit test: Render `ResolvedAlertPayload` with `asset="ETH"` → verify footer contains ETH
2. Integration test: Resolve ETH trade → verify Telegram shows ETH

---

## Rollout Approach

### Phase 1: Local Testing (Day 1-2)

1. **Apply P0-Fix1 and P0-Fix2** (asset/ghost labeling)
   ```bash
   cd engine
   pytest tests/unit/adapters/alert/test_telegram_renderer.py -v
   ```

2. **Run integration test:** Start engine with modified renderer
   ```bash
   # In a test window, trigger evaluate_strategies()
   # Verify Telegram contains [ETH] or [BTC] labels
   ```

3. **Manual verification:**
   - Check 5 telegram windows for correct asset identifiers
   - Check GHOST mode strategies show 👻 emoji

### Phase 2: Unit Test Coverage (Day 2)

1. **Add tests for P1-Fix1** (double-firing)
   ```bash
   # In engine/tests/alerts/test_telegram_dedup.py:
   @pytest.mark.asyncio
   async def test_same_window_different_strategy_not_deduped():
       # same window_ts, different strategy = should fire twice
   ```

2. **Run all alert tests:**
   ```bash
   pytest engine/tests/alerts/ -v --tb=short
   ```

### Phase 3: Staging Environment (Day 3)

1. **Deploy to staging** with all fixes
2. **Verify in production-like environment:**
   ```bash
   # Trigger test trades via hub API
   curl -X POST http://localhost:8000/api/system/paper-mode
   curl -X POST http://localhost:8000/api/backtest/runs -d '{"strategy":"v9_5_eth_blend","start":"2026-01-01","end":"2026-01-07"}'
   ```

3. **Monitor Telegram:**
   - Asset discrimination: ✅ BTC/ETH/SOL clearly labeled
   - Ghost mode: ✅ Live vs Ghost clearly separated with emojis
   - Double-firing: ✅ Each trade resolves once
   - Delivery: ✅ All messages arrive (or errors logged)

### Phase 4: Production Rollout (Day 4)

**Rollout method:** Incremental with feature flag

1. **Update code** in production at 20:00 UTC
   ```bash
   git checkout fix/telegram-notifications-rework
   docker-compose restart engine
   ```

2. **Monitor for 24 hours** with alert:
   ```sql
   -- Slack alert: Telegram error rate > 5%
   SELECT COUNT(*) FILTER(WHERE status = 'error') * 100.0 / COUNT(*) AS error_pct
   FROM notification_logs
   WHERE created_at > now() - INTERVAL '24 hours';
   ```

3. **If issues detected:**
   - Roll back: `git checkout production && docker-compose restart engine`
   - Check logs: `docker logs btc-trader-engine-1 | grep telegram`

---

## Testing Checklist

- [ ] P0-Fix1: WindowSignalPayload renders asset in header
- [ ] P0-Fix2: GHOST strategies show 👻 emoji
- [ ] P1-Fix1: Same trade_id fires only once
- [ ] P1-Fix1: Same window_ts + different strategy fires twice
- [ ] P1-Fix2: Queue errors are logged (not swallowed)
- [ ] All existing tests pass
- [ ] Staging environment runs 24h without issues
- [ ] Production rollback plan validated

---

## Rollback Plan

If critical issues detected:

1. **Immediate:**
   ```bash
   git checkout production
   docker-compose restart engine
   ```

2. **Rollback verification:**
   ```bash
   # Verify telegram not sending new messages
   docker logs btc-trader-engine-1 | grep -E "(telegram|send_raw_message)" | tail -20
   ```

3. **Restore database** if corrupted (rare):
   ```bash
   docker exec btc-trader-db-1 pg_restore -U btctrader -d btc_trader /backups/latest.dump
   ```

---

**Owner:** Billy Richards  
**Last updated:** 2026-05-25
