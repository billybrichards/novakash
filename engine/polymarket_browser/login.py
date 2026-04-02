"""
Polymarket login via email + Gmail IMAP verification code retrieval.
"""

import asyncio
import email
import imaplib
import re
import time
from typing import Optional

import structlog
from playwright.async_api import Page

log = structlog.get_logger(__name__)

EMAIL_POLL_TIMEOUT = 120
EMAIL_POLL_INTERVAL = 3


async def login_to_polymarket(
    page: Page,
    gmail_address: str,
    gmail_app_password: str,
) -> bool:
    """
    Full login flow:
    1. Navigate to Polymarket
    2. Click Log In -> Continue with Email
    3. Enter email address
    4. Fetch verification code from Gmail via IMAP
    5. Enter code
    6. Wait for logged-in state
    """
    try:
        if "polymarket.com" not in (page.url or ""):
            await page.goto("https://polymarket.com", wait_until="networkidle", timeout=30000)

        # Dismiss any overlay modals (cookie consent, promotions, etc.)
        await _dismiss_overlays(page)

        login_btn = await page.query_selector('button:has-text("Log In"), a:has-text("Log In"), [data-testid="login-button"]')
        if not login_btn:
            log.warning("playwright.login.no_login_button")
            return False
        await login_btn.click()
        await page.wait_for_timeout(2000)

        # Dismiss any overlays that appeared after clicking Log In
        await _dismiss_overlays(page)

        email_btn = await page.query_selector(
            'button:has-text("email"), button:has-text("Email"), '
            '[data-testid="email-login"], button:has-text("Continue with email")'
        )
        if email_btn:
            await email_btn.click()
            await page.wait_for_timeout(2000)

        email_input = await page.query_selector(
            'input[type="email"], input[name="email"], input[placeholder*="email" i], '
            'input[placeholder*="Email" i]'
        )
        if not email_input:
            log.error("playwright.login.no_email_input")
            return False

        await email_input.fill(gmail_address)
        await page.wait_for_timeout(500)

        submit_btn = await page.query_selector(
            'button[type="submit"], button:has-text("Continue"), '
            'button:has-text("Send"), button:has-text("Log in")'
        )
        if submit_btn:
            await submit_btn.click()
        else:
            await email_input.press("Enter")

        await page.wait_for_timeout(3000)

        code_request_time = time.time()

        code = await asyncio.to_thread(
            _fetch_verification_code,
            gmail_address,
            gmail_app_password,
            code_request_time,
        )
        if not code:
            log.error("playwright.login.no_verification_code")
            return False

        log.info("playwright.login.code_received", code_length=len(code))

        code_input = await page.query_selector(
            'input[type="text"], input[type="number"], '
            'input[placeholder*="code" i], input[placeholder*="verification" i], '
            'input[name="code"], input[autocomplete="one-time-code"]'
        )
        if not code_input:
            digit_inputs = await page.query_selector_all('input[maxlength="1"]')
            if digit_inputs and len(digit_inputs) >= len(code):
                for i, digit in enumerate(code):
                    await digit_inputs[i].fill(digit)
                    await page.wait_for_timeout(100)
            else:
                log.error("playwright.login.no_code_input")
                return False
        else:
            await code_input.fill(code)

        await page.wait_for_timeout(1000)

        verify_btn = await page.query_selector(
            'button:has-text("Verify"), button:has-text("Submit"), '
            'button:has-text("Continue"), button[type="submit"]'
        )
        if verify_btn:
            await verify_btn.click()

        await page.wait_for_timeout(5000)

        wallet = await page.query_selector(
            '[data-testid="user-menu"], [class*="ProfileIcon"], a[href="/profile"]'
        )
        if wallet:
            log.info("playwright.login.success")
            return True

        await page.wait_for_timeout(5000)
        wallet = await page.query_selector(
            '[data-testid="user-menu"], [class*="ProfileIcon"], a[href="/profile"]'
        )
        success = wallet is not None
        if success:
            log.info("playwright.login.success")
        else:
            log.error("playwright.login.failed_after_code_entry")
        return success

    except Exception as e:
        log.error("playwright.login.error", error=str(e))
        return False


def _fetch_verification_code(
    gmail_address: str,
    gmail_app_password: str,
    after_timestamp: float,
) -> Optional[str]:
    """
    Poll Gmail via IMAP for a Polymarket verification code.
    Blocking — called via asyncio.to_thread().
    """
    deadline = time.time() + EMAIL_POLL_TIMEOUT
    log.info("playwright.imap.polling", address=gmail_address)

    while time.time() < deadline:
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            imap.login(gmail_address, gmail_app_password)
            imap.select("INBOX")

            _, msg_ids = imap.search(None, '(FROM "polymarket" UNSEEN)')
            if not msg_ids[0]:
                _, msg_ids = imap.search(None, '(SUBJECT "verification" UNSEEN)')

            if msg_ids[0]:
                ids = msg_ids[0].split()
                for msg_id in reversed(ids):
                    _, data = imap.fetch(msg_id, "(RFC822)")
                    raw = data[0][1]
                    msg = email.message_from_bytes(raw)

                    date_tuple = email.utils.parsedate_tz(msg["Date"])
                    if date_tuple:
                        email_time = email.utils.mktime_tz(date_tuple)
                        if email_time < after_timestamp - 30:
                            continue

                    body = _get_email_body(msg)
                    if body:
                        code = _extract_code(body)
                        if code:
                            imap.store(msg_id, "+FLAGS", "\\Seen")
                            imap.logout()
                            return code

            imap.logout()

        except Exception as e:
            log.warning("playwright.imap.error", error=str(e))

        time.sleep(EMAIL_POLL_INTERVAL)

    log.error("playwright.imap.timeout")
    return None


def _get_email_body(msg: email.message.Message) -> str:
    """Extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def _extract_code(body: str) -> Optional[str]:
    """Extract a 6-digit verification code from email body."""
    matches = re.findall(r"\b(\d{6})\b", body)
    if matches:
        return matches[0]
    matches = re.findall(r"\b(\d{4})\b", body)
    if matches:
        return matches[0]
    return None


async def _dismiss_overlays(page) -> None:
    """
    Dismiss any overlay modals that block interaction on Polymarket.

    Polymarket shows various popups on first visit:
    - Cookie consent banners
    - Promotional modals
    - Terms/region modals
    - Generic dialog overlays

    Strategy: Try multiple dismiss approaches in order.
    """
    try:
        # 1. Click any close/X buttons on modals
        close_selectors = [
            'button[aria-label="Close"]',
            'button[aria-label="close"]',
            '[data-testid="close-button"]',
            '[data-testid="modal-close"]',
            'button:has-text("Close")',
            'button:has-text("Got it")',
            'button:has-text("Accept")',
            'button:has-text("I agree")',
            'button:has-text("OK")',
            'button:has-text("Dismiss")',
            'button:has-text("Continue")',
            'button:has-text("I understand")',
            # Common X button patterns
            'div[data-slot="modal-overlay"] ~ div button',
            '[role="dialog"] button[aria-label]',
        ]

        for selector in close_selectors:
            btn = await page.query_selector(selector)
            if btn:
                try:
                    await btn.click(timeout=3000)
                    log.info("playwright.overlay_dismissed", selector=selector)
                    await page.wait_for_timeout(1000)
                    return
                except Exception:
                    continue

        # 2. Press Escape to dismiss any open modal
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1000)

        # 3. If there's still a modal overlay, try clicking it to dismiss
        overlay = await page.query_selector('[data-slot="modal-overlay"]')
        if overlay:
            # Click outside the modal content (on the overlay background)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)

        # 4. Last resort: use JS to remove blocking overlays
        await page.evaluate("""
            () => {
                // Remove any modal overlays blocking interaction
                document.querySelectorAll('[data-slot="modal-overlay"]').forEach(el => el.remove());
                // Also remove any fixed overlays with pointer-events
                document.querySelectorAll('div[class*="fixed"][class*="inset-0"]').forEach(el => {
                    if (el.style.pointerEvents !== 'none') el.remove();
                });
            }
        """)
        log.info("playwright.overlays_cleared_via_js")
        await page.wait_for_timeout(500)

    except Exception as e:
        log.warning("playwright.dismiss_overlays.error", error=str(e))
