"""
Playwright automation API routes.
Reads cached data from playwright_state table (engine writes it).
"""

import json

from fastapi import APIRouter, Depends, Response

from auth.middleware import get_current_user, TokenData
from db.database import get_asyncpg_pool

router = APIRouter()


@router.get("/playwright/status")
async def get_status(user: TokenData = Depends(get_current_user)):
    """Current Playwright automation status."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM playwright_state WHERE id = 1")
    if not row:
        return {"logged_in": False, "browser_alive": False, "last_update": None}
    return {
        "logged_in": row["logged_in"],
        "browser_alive": row["browser_alive"],
        "usdc_balance": float(row["usdc_balance"] or 0),
        "positions_value": float(row["positions_value"] or 0),
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/playwright/balance")
async def get_balance(user: TokenData = Depends(get_current_user)):
    """Portfolio balance from Playwright scrape."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT usdc_balance, positions_value FROM playwright_state WHERE id = 1"
        )
    if not row:
        return {"usdc": 0.0, "positions_value": 0.0, "total": 0.0}
    usdc = float(row["usdc_balance"] or 0)
    pos = float(row["positions_value"] or 0)
    return {"usdc": usdc, "positions_value": pos, "total": round(usdc + pos, 2)}


@router.get("/playwright/positions")
async def get_positions(user: TokenData = Depends(get_current_user)):
    """Current positions scraped from account page."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT positions_json FROM playwright_state WHERE id = 1"
        )
    if not row or not row["positions_json"]:
        return []
    data = row["positions_json"]
    return json.loads(data) if isinstance(data, str) else data


@router.get("/playwright/redeemable")
async def get_redeemable(user: TokenData = Depends(get_current_user)):
    """Settled positions awaiting redemption."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT redeemable_json FROM playwright_state WHERE id = 1"
        )
    if not row or not row["redeemable_json"]:
        return []
    data = row["redeemable_json"]
    return json.loads(data) if isinstance(data, str) else data


@router.post("/playwright/redeem")
async def trigger_redeem(user: TokenData = Depends(get_current_user)):
    """Trigger an immediate redeem sweep (engine picks up on next loop)."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE playwright_state SET redeem_requested = TRUE WHERE id = 1"
        )
    return {"triggered": True}


@router.get("/playwright/history")
async def get_history(user: TokenData = Depends(get_current_user)):
    """Order history scraped from account activity page."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT history_json FROM playwright_state WHERE id = 1"
        )
    if not row or not row["history_json"]:
        return []
    data = row["history_json"]
    return json.loads(data) if isinstance(data, str) else data


@router.get("/playwright/screenshot")
async def get_screenshot(user: TokenData = Depends(get_current_user)):
    """Latest browser screenshot as PNG image."""
    pool = await get_asyncpg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT screenshot_png FROM playwright_state WHERE id = 1"
        )
    if not row or not row["screenshot_png"]:
        return Response(content=b"", media_type="image/png", status_code=204)
    return Response(content=bytes(row["screenshot_png"]), media_type="image/png")
