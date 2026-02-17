from __future__ import annotations

from typing import Any

import aiohttp


class WebhookClient:
    def __init__(self, timeout_seconds: int = 8):
        self.timeout_seconds = timeout_seconds

    async def post_json(self, url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        if not url:
            return False, "webhook_url_empty"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.text()
                    if 200 <= resp.status < 300:
                        return True, body[:300]
                    return False, f"status={resp.status} body={body[:300]}"
        except Exception as err:
            return False, str(err)
