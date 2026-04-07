"""
Claude Opus 4.6 Trade Evaluator

Called on every:
- Trade decision (placed or skipped)
- Fill confirmation or unfill
- Resolution (win/loss)

Passes all available data to Claude for objective analysis.
Output sent to Telegram and saved to DB.
"""

import asyncio
import time
from typing import Optional
import structlog
import aiohttp

log = structlog.get_logger(__name__)

CLAUDE_TIMEOUT = 60  # 1 minute timeout


def _escape_telegram_md(text: str) -> str:
    """Escape special chars for Telegram MarkdownV1."""
    # Remove markdown formatting that breaks Telegram
    import re
    # Replace ** bold ** with just the text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    # Replace remaining * that aren't paired
    text = re.sub(r'(?<!\\)\*', '', text)
    # Replace _ italics _ with just text
    text = re.sub(r'(?<!\\)_(.+?)(?<!\\)_', r'\1', text)
    # Remove remaining unpaired underscores
    text = re.sub(r'(?<!\\)_', '', text)
    # Remove ` backticks
    text = text.replace('`', '')
    return text

class ClaudeEvaluator:
    def __init__(self, api_key: str, alerter=None, db_client=None):
        self._api_key = api_key
        self._alerter = alerter
        self._db = db_client
        self._log = log.bind(component="claude_evaluator")
    
    async def evaluate_trade_decision(
        self,
        asset: str,
        timeframe: str,
        direction: str,
        confidence: str,
        delta_pct: float,
        vpin: float,
        regime: str,
        cg_snapshot: dict,
        token_price: float,
        gamma_bestask: float,
        window_open_price: float,
        current_price: float,
        trade_placed: bool,
        skip_reason: str = None,
        fill_status: str = None,  # "FILLED", "UNFILLED", "FOK_KILLED"
        actual_fill_price: float = None,
        shares_matched: float = None,
        price_source: str = "UNKNOWN",  # "LIVE", "STALE", "SYN"
    ) -> Optional[str]:
        """Ask Claude to evaluate a trade decision with all available data."""
        
        prompt = f"""You are a quantitative trading analyst evaluating a 5-minute crypto prediction market trade on Polymarket UpDown markets.

## Trade Context
- **Asset:** {asset} {timeframe}
- **Direction:** {direction} ({"UP" if direction == "YES" else "DOWN"})
- **Confidence:** {confidence}
- **Trade placed:** {"YES" if trade_placed else f"NO — {skip_reason}"}
{f"- **Fill status:** {fill_status}" if fill_status else ""}
{f"- **Actual fill price:** ${actual_fill_price:.4f}" if actual_fill_price else ""}
{f"- **Shares matched:** {shares_matched:.2f}" if shares_matched else ""}

## Signal Data
- **Window delta:** {delta_pct:+.4f}% (price change from window open)
- **VPIN:** {vpin:.4f} (Volume-synchronized Probability of Informed Trading)
- **Regime:** {regime} (CASCADE=high informed flow, TRANSITION=moderate, NORMAL=low)
- **Window open price:** ${window_open_price:,.2f}
- **Current price:** ${current_price:,.2f}

## Pricing
- **Gamma bestAsk:** ${gamma_bestask:.4f} [{price_source}] (indicative market price for this token)
- **Our entry price:** ${token_price:.4f}
- **Price cap:** $0.65 (5m) / $0.70 (15m)
- **Price source:** {price_source} (LIVE=fresh fetch at eval time, STALE=from window open ~5min ago, SYN=synthetic/no market data)

## CoinGlass Derivatives Data ({asset}-specific)
{self._format_cg(cg_snapshot)}

## Your Analysis
2-3 sentences: Was the direction call justified? Key risk factor."""

        try:
            analysis = await self._call_claude(prompt)
            if analysis:
                self._log.info("claude.evaluation_complete", asset=asset, length=len(analysis))
                
                # Send to Telegram
                if self._alerter:
                    emoji = "🤖"
                    status_label = ""
                    if fill_status == "FILLED":
                        status_label = "✅ FILLED"
                    elif fill_status == "UNFILLED":
                        status_label = "❌ UNFILLED"
                    elif fill_status == "FOK_KILLED":
                        status_label = "⚡ NO LIQUIDITY"
                    elif not trade_placed:
                        status_label = "⏭ SKIPPED"
                    else:
                        status_label = "⏳ PLACED"
                    
                    msg = (
                        f"{emoji} *AI Assessment — {asset} {timeframe} {status_label}*\n"
                        f"Direction: {direction} | δ={delta_pct:+.4f}% | VPIN={vpin:.3f}\n"
                        f"Gamma: ${gamma_bestask:.2f} [{price_source}] | Entry: ${token_price:.4f}\n\n"
                        f"{_escape_telegram_md(analysis)}"
                    )
                    try:
                        await self._alerter.send_raw_message(msg)
                    except Exception:
                        # Fallback to system alert
                        try:
                            await self._alerter.send_system_alert(msg, level="info")
                        except Exception:
                            pass
                
                # Save to DB
                if self._db:
                    try:
                        await self._db.write_evaluation({
                            "timestamp": time.time(),
                            "asset": asset,
                            "timeframe": timeframe,
                            "direction": direction,
                            "confidence": confidence,
                            "delta_pct": delta_pct,
                            "vpin": vpin,
                            "regime": regime,
                            "token_price": token_price,
                            "gamma_bestask": gamma_bestask,
                            "trade_placed": trade_placed,
                            "fill_status": fill_status,
                            "actual_fill_price": actual_fill_price,
                            "analysis": analysis,
                        })
                    except Exception:
                        pass
                
                return analysis
        except Exception as exc:
            self._log.warning("claude.evaluation_failed", error=str(exc)[:100])
        
        return None

    async def evaluate_resolution(
        self,
        asset: str,
        timeframe: str,
        direction: str,
        outcome: str,  # "WIN" or "LOSS"
        pnl: float,
        entry_price: float,
        delta_pct: float,
        vpin: float,
        regime: str,
        cg_snapshot: dict,
    ) -> Optional[str]:
        """Ask Claude to evaluate a trade resolution."""
        
        prompt = f"""You are evaluating a resolved {timeframe} {asset} prediction market trade.

## Result
- **Outcome:** {outcome}
- **P&L:** ${pnl:+.2f}
- **Direction bet:** {direction} ({"UP" if direction == "YES" else "DOWN"})
- **Entry price:** ${entry_price:.4f}

## Entry Signals
- Delta: {delta_pct:+.4f}%
- VPIN: {vpin:.4f}
- Regime: {regime}

## CoinGlass Data at Entry
{self._format_cg(cg_snapshot)}

In 2-3 sentences: Was this a good trade regardless of outcome? Did the signals support the direction? Any lessons for the next trade?"""

        try:
            return await self._call_claude(prompt)
        except Exception as exc:
            self._log.warning("claude.resolution_eval_failed", error=str(exc)[:100])
            return None

    def _format_cg(self, cg: dict) -> str:
        if not cg:
            return "CoinGlass data unavailable"
        lines = []
        if cg.get("oi_usd"):
            lines.append(f"- OI: ${cg['oi_usd']/1e9:.1f}B")
        if cg.get("oi_delta_pct"):
            lines.append(f"- OI Delta: {cg['oi_delta_pct']:.3f}%")
        if cg.get("long_pct"):
            lines.append(f"- L/S: {cg['long_pct']:.0f}% long / {cg.get('short_pct', 0):.0f}% short")
        if cg.get("top_short_pct"):
            lines.append(f"- Smart Money: {cg['top_short_pct']:.0f}% short")
        if cg.get("funding_rate") is not None:
            lines.append(f"- Funding: {cg['funding_rate']*100:.4f}%")
        if cg.get("taker_buy") is not None:
            total = (cg.get("taker_buy", 0) + cg.get("taker_sell", 0))
            if total > 0:
                sell_pct = cg.get("taker_sell", 0) / total * 100
                lines.append(f"- Taker: {sell_pct:.0f}% sell")
        return "\n".join(lines) if lines else "CoinGlass data unavailable"

    async def _call_claude(self, prompt: str) -> Optional[str]:
        """Call Claude Opus 4.6 API with timeout."""
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=CLAUDE_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("content"):
                        return data["content"][0].get("text", "")
                else:
                    body = await resp.text()
                    self._log.warning("claude.api_error", status=resp.status, body=body[:200])
        return None
