"""
PlaywrightService — manages a persistent headless Chromium browser
for Polymarket account interaction.

Lifecycle: start() → [use methods] → stop()
Orchestrator calls start/stop like every other engine service.
"""

import asyncio
import json
from pathlib import Path
from typing import Optional

import structlog
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from playwright.login import login_to_polymarket
from playwright.account import (
    get_portfolio_balance,
    get_positions,
    get_order_history,
)
from playwright.redeemer import get_redeemable_positions, redeem_all_positions

log = structlog.get_logger(__name__)


class PlaywrightService:
    """Persistent browser session for Polymarket account management."""

    def __init__(
        self,
        gmail_address: str,
        gmail_app_password: str,
        cookie_path: str = "data/.polymarket_cookies.json",
        headless: bool = True,
    ) -> None:
        self._gmail_address = gmail_address
        self._gmail_app_password = gmail_app_password
        self._cookie_path = Path(cookie_path)
        self._headless = headless

        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._browser_alive: bool = False
        self._logged_in: bool = False
        self._last_screenshot: Optional[bytes] = None

    async def start(self) -> None:
        """Launch browser, restore cookies, attempt login if needed."""
        log.info("playwright.starting", headless=self._headless)
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            self._page = await self._context.new_page()
            self._browser_alive = True

            await self._load_cookies()

            await self._page.goto("https://polymarket.com", wait_until="networkidle", timeout=30000)
            self._logged_in = await self._check_logged_in()

            if not self._logged_in:
                log.info("playwright.login_required")
                self._logged_in = await self.login()

            if self._logged_in:
                await self._save_cookies()
                log.info("playwright.started", logged_in=True)
            else:
                log.warning("playwright.started_without_login")

        except Exception as e:
            log.error("playwright.start_failed", error=str(e))
            self._browser_alive = False

    async def stop(self) -> None:
        """Save cookies and close browser."""
        log.info("playwright.stopping")
        try:
            if self._context:
                await self._save_cookies()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            log.error("playwright.stop_error", error=str(e))
        finally:
            self._browser_alive = False
            self._logged_in = False
            log.info("playwright.stopped")

    async def login(self) -> bool:
        """Full login flow via Gmail verification code."""
        if not self._page or not self._browser_alive:
            return False
        try:
            success = await login_to_polymarket(
                page=self._page,
                gmail_address=self._gmail_address,
                gmail_app_password=self._gmail_app_password,
            )
            self._logged_in = success
            if success:
                await self._save_cookies()
            return success
        except Exception as e:
            log.error("playwright.login_failed", error=str(e))
            return False

    async def is_logged_in(self) -> bool:
        """Check current login state."""
        if not self._page or not self._browser_alive:
            return False
        try:
            self._logged_in = await self._check_logged_in()
            return self._logged_in
        except Exception:
            return False

    async def get_portfolio_balance(self) -> dict:
        """Get USDC balance + positions value from account page."""
        if not await self._ensure_alive():
            return {"usdc": 0.0, "positions_value": 0.0, "total": 0.0}
        return await get_portfolio_balance(self._page)

    async def get_positions(self) -> list[dict]:
        """Get all current positions."""
        if not await self._ensure_alive():
            return []
        return await get_positions(self._page)

    async def get_redeemable(self) -> list[dict]:
        """Get settled positions with Redeem available."""
        if not await self._ensure_alive():
            return []
        return await get_redeemable_positions(self._page)

    async def redeem_all(self) -> dict:
        """Click all Redeem buttons, return summary."""
        if not await self._ensure_alive():
            return {"redeemed": 0, "failed": 0, "total_value": 0.0, "details": []}
        result = await redeem_all_positions(self._page)
        if result["redeemed"] > 0:
            log.info(
                "playwright.redeemed",
                count=result["redeemed"],
                value=result["total_value"],
            )
        return result

    async def get_order_history(self, limit: int = 50) -> list[dict]:
        """Scrape order history from activity page."""
        if not await self._ensure_alive():
            return []
        return await get_order_history(self._page, limit=limit)

    async def screenshot(self) -> Optional[bytes]:
        """Capture current page as PNG."""
        if not self._page or not self._browser_alive:
            return self._last_screenshot
        try:
            self._last_screenshot = await self._page.screenshot(type="png")
            return self._last_screenshot
        except Exception as e:
            log.error("playwright.screenshot_failed", error=str(e))
            return self._last_screenshot

    async def _ensure_alive(self) -> bool:
        """Check browser is alive and logged in. Attempt recovery if not."""
        if not self._browser_alive or not self._page:
            log.warning("playwright.browser_dead, attempting relaunch")
            await self.stop()
            await self.start()
        if not self._logged_in:
            self._logged_in = await self.login()
        return self._browser_alive and self._logged_in

    async def _check_logged_in(self) -> bool:
        """Check if the current page shows a logged-in state."""
        try:
            wallet = await self._page.query_selector('[data-testid="user-menu"], [class*="ProfileIcon"], a[href="/profile"]')
            return wallet is not None
        except Exception:
            return False

    async def _load_cookies(self) -> None:
        """Load cookies from disk into browser context."""
        if self._cookie_path.exists():
            try:
                cookies = json.loads(self._cookie_path.read_text())
                await self._context.add_cookies(cookies)
                log.info("playwright.cookies_loaded", count=len(cookies))
            except Exception as e:
                log.warning("playwright.cookies_load_failed", error=str(e))

    async def _save_cookies(self) -> None:
        """Save browser cookies to disk."""
        try:
            cookies = await self._context.cookies()
            self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self._cookie_path.write_text(json.dumps(cookies, indent=2))
            log.info("playwright.cookies_saved", count=len(cookies))
        except Exception as e:
            log.warning("playwright.cookies_save_failed", error=str(e))
