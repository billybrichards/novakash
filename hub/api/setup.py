"""
Setup / Onboarding API

GET  /api/config/setup           — return which fields are configured (no secrets exposed)
PUT  /api/config/setup           — save setup configuration to .env / DB
POST /api/config/setup/test-telegram   — send a test Telegram alert
POST /api/config/setup/derive-poly-keys — derive Polymarket CLOB API keys from private key
"""

from __future__ import annotations

import os
import json
import httpx
import structlog
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.jwt import TokenData
from auth.middleware import get_current_user
from db.database import get_session
from db.models import SystemState

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/config", tags=["setup"])

# Path to the project .env file (one level up from hub/)
ENV_FILE = Path(__file__).parent.parent.parent / ".env"


# ─── Models ───────────────────────────────────────────────────────────────────

class SetupPayload(BaseModel):
    # Wallet / Polymarket
    poly_private_key: Optional[str] = None
    poly_api_key: Optional[str] = None
    poly_api_secret: Optional[str] = None
    poly_api_passphrase: Optional[str] = None
    poly_funder_address: Optional[str] = None
    # Opinion Markets
    opinion_api_key: Optional[str] = None
    opinion_wallet_key: Optional[str] = None
    # Data feeds
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    coinglass_api_key: Optional[str] = None
    polygon_rpc_url: Optional[str] = None
    # Alerts
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    # System
    starting_bankroll: Optional[float] = None
    paper_mode: Optional[bool] = None
    domain: Optional[str] = None


class TelegramTestPayload(BaseModel):
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _read_env() -> dict[str, str]:
    """Read the .env file into a dict."""
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _write_env(env: dict[str, str]) -> None:
    """Write a dict back to the .env file, preserving existing keys."""
    existing = _read_env()
    existing.update(env)
    lines = [f'{k}="{v}"' for k, v in sorted(existing.items())]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def _field_configured(value: str | None) -> bool:
    return bool(value and value.strip())


# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/setup")
async def get_setup_status(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Return which fields are configured (booleans only — secrets are never exposed).
    Also returns non-sensitive values like domain, paper_mode, starting_bankroll.
    """
    env = _read_env()

    configured = {
        # Wallet
        "poly_private_key":     _field_configured(env.get("POLY_PRIVATE_KEY")),
        "poly_api_key":         _field_configured(env.get("POLY_API_KEY")),
        "poly_api_secret":      _field_configured(env.get("POLY_API_SECRET")),
        "poly_api_passphrase":  _field_configured(env.get("POLY_API_PASSPHRASE")),
        "poly_funder_address":  _field_configured(env.get("POLY_FUNDER_ADDRESS")),
        "opinion_api_key":      _field_configured(env.get("OPINION_API_KEY")),
        "opinion_wallet_key":   _field_configured(env.get("OPINION_WALLET_KEY")),
        # Data feeds
        "binance_api_key":      _field_configured(env.get("BINANCE_API_KEY")),
        "binance_api_secret":   _field_configured(env.get("BINANCE_API_SECRET")),
        "coinglass_api_key":    _field_configured(env.get("COINGLASS_API_KEY")),
        "polygon_rpc_url":      _field_configured(env.get("POLYGON_RPC_URL")),
        # Alerts
        "telegram_bot_token":   _field_configured(env.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id":     _field_configured(env.get("TELEGRAM_CHAT_ID")),
    }

    # Non-sensitive values returned as-is
    non_secret = {
        "poly_funder_address":  env.get("POLY_FUNDER_ADDRESS", ""),
        "polygon_rpc_url":      env.get("POLYGON_RPC_URL", ""),
        "domain":               env.get("DOMAIN", ""),
        "paper_mode":           env.get("PAPER_MODE", "true").lower() == "true",
        "starting_bankroll":    float(env.get("STARTING_BANKROLL", "1000")),
        # Masked API key hints (first 6 chars only)
        "poly_api_key_hint":    (env.get("POLY_API_KEY", "")[:6] + "…") if env.get("POLY_API_KEY") else "",
        "opinion_api_key_hint": (env.get("OPINION_API_KEY", "")[:6] + "…") if env.get("OPINION_API_KEY") else "",
        "binance_api_key_hint": (env.get("BINANCE_API_KEY", "")[:6] + "…") if env.get("BINANCE_API_KEY") else "",
    }

    return {"configured": configured, **non_secret}


@router.put("/setup")
async def update_setup(
    payload: SetupPayload,
    session: AsyncSession = Depends(get_session),
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Save setup configuration.

    Sensitive values (keys, secrets, tokens) are written to .env.
    Non-sensitive settings (paper_mode, bankroll, domain) are also saved to .env
    and mirrored into the system_state DB record for the engine to pick up.
    """
    updates: dict[str, str] = {}

    field_map = {
        "poly_private_key":    ("POLY_PRIVATE_KEY",    payload.poly_private_key),
        "poly_api_key":        ("POLY_API_KEY",         payload.poly_api_key),
        "poly_api_secret":     ("POLY_API_SECRET",      payload.poly_api_secret),
        "poly_api_passphrase": ("POLY_API_PASSPHRASE",  payload.poly_api_passphrase),
        "poly_funder_address": ("POLY_FUNDER_ADDRESS",  payload.poly_funder_address),
        "opinion_api_key":     ("OPINION_API_KEY",      payload.opinion_api_key),
        "opinion_wallet_key":  ("OPINION_WALLET_KEY",   payload.opinion_wallet_key),
        "binance_api_key":     ("BINANCE_API_KEY",      payload.binance_api_key),
        "binance_api_secret":  ("BINANCE_API_SECRET",   payload.binance_api_secret),
        "coinglass_api_key":   ("COINGLASS_API_KEY",    payload.coinglass_api_key),
        "polygon_rpc_url":     ("POLYGON_RPC_URL",      payload.polygon_rpc_url),
        "telegram_bot_token":  ("TELEGRAM_BOT_TOKEN",   payload.telegram_bot_token),
        "telegram_chat_id":    ("TELEGRAM_CHAT_ID",     payload.telegram_chat_id),
        "domain":              ("DOMAIN",               payload.domain),
    }

    for _, (env_key, value) in field_map.items():
        if value is not None and value.strip():
            updates[env_key] = value.strip()

    if payload.paper_mode is not None:
        updates["PAPER_MODE"] = "true" if payload.paper_mode else "false"

    if payload.starting_bankroll is not None:
        updates["STARTING_BANKROLL"] = str(payload.starting_bankroll)

    if updates:
        try:
            _write_env(updates)
        except Exception as exc:
            log.error("setup.env_write_failed", error=str(exc))
            raise HTTPException(status_code=500, detail=f"Failed to write .env: {exc}")

    # Mirror non-sensitive config into system_state for the engine
    try:
        result = await session.execute(select(SystemState).where(SystemState.id == 1))
        state = result.scalar_one_or_none()
        if state:
            current = state.state or {}
            setup_cfg = current.get("setup", {})
            if payload.paper_mode is not None:
                setup_cfg["paper_mode"] = payload.paper_mode
            if payload.starting_bankroll is not None:
                setup_cfg["starting_bankroll"] = payload.starting_bankroll
            if payload.domain:
                setup_cfg["domain"] = payload.domain
            current["setup"] = setup_cfg
            state.state = current
            await session.commit()
    except Exception as exc:
        log.warning("setup.db_mirror_failed", error=str(exc))
        # Not fatal — .env write succeeded

    log.info("setup.updated", fields=list(updates.keys()))
    return {"success": True, "updated_fields": list(updates.keys())}


@router.post("/setup/test-telegram")
async def test_telegram(
    payload: TelegramTestPayload,
    user: TokenData = Depends(get_current_user),
) -> dict:
    """Send a test Telegram alert using the provided (or saved) bot token + chat ID."""
    env = _read_env()

    bot_token = payload.telegram_bot_token or env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id   = payload.telegram_chat_id   or env.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        raise HTTPException(
            status_code=400,
            detail="Telegram bot token and chat ID are required",
        )

    message = (
        "✅ *BTC Trader — Test Alert*\n\n"
        "Your Telegram alerts are configured correctly\\.\n"
        "Trading signals and P&L updates will appear here\\."
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "MarkdownV2",
                },
            )
            data = resp.json()
            if not data.get("ok"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Telegram API error: {data.get('description', 'Unknown error')}",
                )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Network error: {exc}")

    return {"success": True, "message": "Test alert sent"}


@router.post("/setup/derive-poly-keys")
async def derive_polymarket_keys(
    user: TokenData = Depends(get_current_user),
) -> dict:
    """
    Derive Polymarket CLOB API keys from the stored private key.

    Requires py-clob-client to be installed. Reads POLY_PRIVATE_KEY from .env.
    """
    env = _read_env()
    private_key = env.get("POLY_PRIVATE_KEY", "")

    if not private_key:
        raise HTTPException(
            status_code=400,
            detail="POLY_PRIVATE_KEY not configured. Set it in the Exchange API Keys section first.",
        )

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=POLYGON,
            key=private_key,
        )
        api_creds = client.create_or_derive_api_creds()

        # Save derived creds to .env
        _write_env({
            "POLY_API_KEY":        api_creds.api_key,
            "POLY_API_SECRET":     api_creds.api_secret,
            "POLY_API_PASSPHRASE": api_creds.api_passphrase,
        })

        return {
            "success": True,
            "api_key": api_creds.api_key,
            # Return hints only
            "api_key_hint": api_creds.api_key[:6] + "…",
        }

    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="py-clob-client not installed. Run: pip install py-clob-client",
        )
    except Exception as exc:
        log.error("setup.derive_poly_keys_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
