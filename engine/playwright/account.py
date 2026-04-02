"""
Scrape Polymarket account data: balance, positions, order history.
"""

import re
from typing import Optional

import structlog
from playwright.async_api import Page

log = structlog.get_logger(__name__)

PORTFOLIO_URL = "https://polymarket.com/portfolio"
ACTIVITY_URL = "https://polymarket.com/portfolio?tab=activity"


async def get_portfolio_balance(page: Page) -> dict:
    """
    Navigate to portfolio page and extract balance info.
    Returns: {usdc: float, positions_value: float, total: float}
    """
    result = {"usdc": 0.0, "positions_value": 0.0, "total": 0.0}
    try:
        if "/portfolio" not in (page.url or ""):
            await page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=30000)
        else:
            await page.reload(wait_until="networkidle", timeout=30000)

        await page.wait_for_timeout(3000)

        all_text = await page.inner_text("body")

        cash_match = re.search(r"(?:Cash|USDC|Available)[\s:]*\$?([\d,]+\.?\d*)", all_text, re.IGNORECASE)
        if cash_match:
            result["usdc"] = _parse_money(cash_match.group(1))

        total_match = re.search(r"(?:Portfolio Value|Total|Net Worth)[\s:]*\$?([\d,]+\.?\d*)", all_text, re.IGNORECASE)
        if total_match:
            result["total"] = _parse_money(total_match.group(1))

        if result["total"] > 0 and result["usdc"] > 0:
            result["positions_value"] = round(result["total"] - result["usdc"], 2)
        elif result["total"] > 0:
            result["positions_value"] = result["total"]

        log.info("playwright.balance", **result)

    except Exception as e:
        log.error("playwright.balance.error", error=str(e))

    return result


async def get_positions(page: Page) -> list[dict]:
    """
    Scrape current positions from portfolio page.
    Returns list of: {market, outcome, shares, value, status}
    """
    positions = []
    try:
        if "/portfolio" not in (page.url or ""):
            await page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=30000)

        await page.wait_for_timeout(3000)

        position_els = await page.query_selector_all(
            '[class*="position"], [class*="Position"], '
            '[data-testid*="position"], [class*="market-card"]'
        )

        if not position_els:
            position_els = await page.query_selector_all("table tbody tr")

        for el in position_els:
            text = await el.inner_text()
            position = _parse_position_text(text)
            if position:
                positions.append(position)

        log.info("playwright.positions", count=len(positions))

    except Exception as e:
        log.error("playwright.positions.error", error=str(e))

    return positions


async def get_order_history(page: Page, limit: int = 50) -> list[dict]:
    """
    Scrape order history from activity tab.
    Returns list of: {market, side, amount, price, date, status}
    """
    orders = []
    try:
        if "tab=activity" not in (page.url or ""):
            await page.goto(ACTIVITY_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        activity_tab = await page.query_selector(
            'button:has-text("Activity"), [data-testid="activity-tab"], '
            'a:has-text("Activity")'
        )
        if activity_tab:
            await activity_tab.click()
            await page.wait_for_timeout(2000)

        rows = await page.query_selector_all(
            '[class*="activity"], [class*="Activity"], '
            'table tbody tr, [class*="trade-row"], [class*="TradeRow"]'
        )

        for row in rows[:limit]:
            text = await row.inner_text()
            order = _parse_order_text(text)
            if order:
                orders.append(order)

        log.info("playwright.history", count=len(orders))

    except Exception as e:
        log.error("playwright.history.error", error=str(e))

    return orders


def _parse_money(s: str) -> float:
    """Parse a money string like '1,234.56' to float."""
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _parse_position_text(text: str) -> Optional[dict]:
    """Parse position text into structured dict. Best-effort extraction."""
    if not text or len(text.strip()) < 5:
        return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    position = {
        "market": lines[0] if lines else "Unknown",
        "outcome": "Unknown",
        "shares": 0.0,
        "value": 0.0,
        "status": "active",
    }

    full_text = " ".join(lines)

    if re.search(r"\bYes\b", full_text, re.IGNORECASE):
        position["outcome"] = "Yes"
    elif re.search(r"\bNo\b", full_text, re.IGNORECASE):
        position["outcome"] = "No"

    money_matches = re.findall(r"\$?([\d,]+\.?\d*)", full_text)
    if money_matches:
        values = [_parse_money(m) for m in money_matches]
        values = [v for v in values if v > 0]
        if values:
            position["value"] = values[-1]
            if len(values) > 1:
                position["shares"] = values[0]

    if re.search(r"\b(settled|resolved|ended|expired)\b", full_text, re.IGNORECASE):
        position["status"] = "settled"
    if re.search(r"\bredeem\b", full_text, re.IGNORECASE):
        position["status"] = "redeemable"

    return position


def _parse_order_text(text: str) -> Optional[dict]:
    """Parse order/activity text into structured dict. Best-effort extraction."""
    if not text or len(text.strip()) < 5:
        return None

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return None

    order = {
        "market": lines[0] if lines else "Unknown",
        "side": "Unknown",
        "amount": 0.0,
        "price": 0.0,
        "date": "",
        "status": "filled",
    }

    full_text = " ".join(lines)

    if re.search(r"\b(bought|buy)\b", full_text, re.IGNORECASE):
        order["side"] = "Buy"
    elif re.search(r"\b(sold|sell)\b", full_text, re.IGNORECASE):
        order["side"] = "Sell"

    money_matches = re.findall(r"\$?([\d,]+\.?\d*)", full_text)
    values = [_parse_money(m) for m in money_matches if _parse_money(m) > 0]
    if values:
        order["amount"] = values[0]
        if len(values) > 1:
            order["price"] = values[1]

    date_match = re.search(r"(\w+ \d+,? \d{4}|\d{1,2}/\d{1,2}/\d{2,4})", full_text)
    if date_match:
        order["date"] = date_match.group(1)

    return order
