from __future__ import annotations

from typing import Optional

import aiohttp
import structlog

log = structlog.get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


async def chat_completion(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 200,
    temperature: float = 0.2,
    timeout_s: int = 15,
) -> Optional[str]:
    """Call OpenRouter's OpenAI-compatible chat completions API."""
    if not api_key:
        return None

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "openrouter.api_error", status=resp.status, body=body[:200]
                    )
                    return None
                data = await resp.json()
                return (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                ) or None
    except Exception as exc:
        log.warning("openrouter.request_failed", error=str(exc)[:200])
        return None
