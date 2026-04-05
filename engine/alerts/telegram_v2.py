"""
Enhanced Telegram Alert Format — Crystal Clear Strategy Comparison

Shows:
1. What TimesFM predicted
2. What v5.7c predicted
3. Which one was RIGHT (after window close)
4. Side-by-side P&L comparison
"""

from datetime import datetime, timezone


def format_window_alert_v2(
    window_ts: int,
    asset: str,
    timeframe: str,
    open_price: float,
    close_price: float,
    timesfm_forecast=None,
    twap_result=None,
    actual_outcome: str = None,  # "UP" or "DOWN" at window close
) -> str:
    """
    Generate crystal-clear window alert showing strategy predictions vs outcome.
    
    Args:
        window_ts: Window open timestamp
        asset: BTC/ETH/SOL/XRP
        timeframe: "5m" or "15m"
        open_price: Window open price
        close_price: Window close price (actual outcome)
        timesfm_forecast: TimesFMForecast object
        twap_result: TWAPResult object
        actual_outcome: "UP" or "DOWN" (computed from close vs open)
    """
    
    ts_str = datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime("%H:%M UTC")
    delta_pct = ((close_price or open_price) - open_price) / open_price * 100 if open_price else 0
    actual_outcome = "UP" if delta_pct > 0 else "DOWN"
    
    lines = [
        f"🎯 {asset} {timeframe} — {ts_str}",
        f"",
        f"📊 Window: `${open_price:,.2f}` → `${close_price:,.2f}` | Δ `{delta_pct:+.4f}%`",
        f"Outcome: {('📈 UP' if delta_pct > 0 else '📉 DOWN')}",
        f"",
        f"🤖 *PREDICTIONS*",
        f"",
    ]
    
    # TimesFM prediction
    if timesfm_forecast and not timesfm_forecast.error:
        tfm_dir = timesfm_forecast.direction
        tfm_conf = timesfm_forecast.confidence
        tfm_correct = "✅ CORRECT" if tfm_dir == actual_outcome else "❌ WRONG"
        lines += [
            f"**TimesFM (v6.0):** {('📈 UP' if tfm_dir == 'UP' else '📉 DOWN')} | Confidence `{tfm_conf*100:.0f}%`",
            f"Predicted close: `${timesfm_forecast.predicted_close:,.2f}`",
            f"Result: {tfm_correct}",
            f"",
        ]
    else:
        lines += [
            f"**TimesFM (v6.0):** Not available (BTC only)",
            f"",
        ]
    
    # v5.7c prediction
    if twap_result and not twap_result.should_skip:
        v57_dir = twap_result.gamma_direction
        v57_correct = "✅ CORRECT" if v57_dir == actual_outcome else "❌ WRONG"
        v57_confidence = twap_result.agreement_score / 3  # 0-1
        lines += [
            f"**v5.7c (Multi):** {('📈 UP' if v57_dir == 'UP' else '📉 DOWN')} | Agreement `{twap_result.agreement_score}/3`",
            f"(TWAP: {twap_result.twap_direction} | Point: {twap_result.point_direction} | Gamma: {twap_result.gamma_direction})",
            f"Result: {v57_correct}",
            f"",
        ]
    elif twap_result and twap_result.should_skip:
        skip_reason = getattr(twap_result, 'skip_reason', 'unknown')
        lines += [
            f"**v5.7c (Multi):** ⛔ SKIPPED — `{skip_reason}`",
            f"",
        ]
    else:
        lines += [
            f"**v5.7c (Multi):** Not evaluated",
            f"",
        ]
    
    # Summary
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Winner: {('TimesFM 🧠' if (timesfm_forecast and timesfm_forecast.direction == actual_outcome) else ('v5.7c 📊' if (twap_result and twap_result.gamma_direction == actual_outcome) else 'Neither ❌'))}",
    ]
    
    return "\n".join(lines)


def format_resolution_alert_v2(
    order_id: str,
    asset: str,
    strategy: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    outcome: str,  # "WIN" or "LOSS"
    pnl_usd: float,
    fee_usd: float,
) -> str:
    """
    Format a trade resolution alert — clear win/loss outcome.
    """
    
    emoji = "✅" if outcome == "WIN" else "❌"
    pnl_emoji = "📈" if pnl_usd > 0 else "📉"
    
    lines = [
        f"{emoji} *TRADE RESOLVED — {strategy.upper()}*",
        f"",
        f"Asset: `{asset}` | Direction: `{direction}`",
        f"Entry: `${entry_price:.4f}` → Exit: `${exit_price:.4f}`",
        f"",
        f"Outcome: {('WIN 🎉' if outcome == 'WIN' else 'LOSS 💔')}",
        f"P&L: {pnl_emoji} `${pnl_usd:+.2f}` (after `${fee_usd:.2f}` fee)",
        f"Order: `{order_id}`",
    ]
    
    return "\n".join(lines)
