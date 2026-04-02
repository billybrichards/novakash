"""
Find and redeem settled Polymarket positions via browser UI.
"""

import re

import structlog
from playwright.async_api import Page

log = structlog.get_logger(__name__)

PORTFOLIO_URL = "https://polymarket.com/portfolio"


async def get_redeemable_positions(page: Page) -> list[dict]:
    """
    Find positions with a "Redeem" button on the portfolio page.
    Returns: [{market: str, value: str, element_index: int}]
    """
    redeemable = []
    try:
        if "/portfolio" not in (page.url or ""):
            await page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        redeem_buttons = await page.query_selector_all(
            'button:has-text("Redeem"), button:has-text("Claim"), '
            '[data-testid*="redeem"], [data-testid*="claim"]'
        )

        for i, btn in enumerate(redeem_buttons):
            try:
                parent = await btn.evaluate_handle("el => el.closest('[class*=\"position\"], [class*=\"Position\"], [class*=\"card\"], tr')")
                market_name = "Unknown Market"
                value_str = ""

                if parent:
                    parent_text = await parent.inner_text()
                    lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                    if lines:
                        market_name = lines[0]
                    money = re.findall(r"\$?([\d,]+\.?\d*)", parent_text)
                    if money:
                        value_str = money[-1]

                redeemable.append({
                    "market": market_name,
                    "value": value_str,
                    "element_index": i,
                })
            except Exception:
                redeemable.append({
                    "market": f"Position {i + 1}",
                    "value": "",
                    "element_index": i,
                })

        log.info("playwright.redeemable", count=len(redeemable))

    except Exception as e:
        log.error("playwright.redeemable.error", error=str(e))

    return redeemable


async def redeem_all_positions(page: Page) -> dict:
    """
    Click all Redeem buttons on the portfolio page.
    Returns: {redeemed: int, failed: int, total_value: float, details: list}
    """
    result = {"redeemed": 0, "failed": 0, "total_value": 0.0, "details": []}

    try:
        if "/portfolio" not in (page.url or ""):
            await page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        max_iterations = 50
        iteration = 0

        while iteration < max_iterations:
            iteration += 1

            redeem_btn = await page.query_selector(
                'button:has-text("Redeem"), button:has-text("Claim"), '
                '[data-testid*="redeem"], [data-testid*="claim"]'
            )
            if not redeem_btn:
                break

            market_name = "Unknown"
            try:
                parent = await redeem_btn.evaluate_handle(
                    "el => el.closest('[class*=\"position\"], [class*=\"Position\"], [class*=\"card\"], tr')"
                )
                if parent:
                    try:
                        parent_text = await parent.inner_text()
                        lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                        if lines:
                            market_name = lines[0]
                    except Exception:
                        pass

                await redeem_btn.click()
                await page.wait_for_timeout(2000)

                confirm_btn = await page.query_selector(
                    'button:has-text("Confirm"), button:has-text("Yes"), '
                    '[data-testid*="confirm"]'
                )
                if confirm_btn:
                    await confirm_btn.click()
                    await page.wait_for_timeout(3000)

                await page.wait_for_timeout(2000)

                result["redeemed"] += 1
                result["details"].append({
                    "market": market_name,
                    "status": "redeemed",
                })
                log.info("playwright.redeemed_position", market=market_name)

            except Exception as e:
                result["failed"] += 1
                result["details"].append({
                    "market": market_name,
                    "status": "failed",
                    "error": str(e),
                })
                log.error("playwright.redeem_failed", error=str(e))
                await page.wait_for_timeout(1000)

        log.info(
            "playwright.redeem_sweep_complete",
            redeemed=result["redeemed"],
            failed=result["failed"],
        )

    except Exception as e:
        log.error("playwright.redeem_sweep_error", error=str(e))

    return result
