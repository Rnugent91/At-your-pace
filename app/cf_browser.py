"""Cloudflare Browser Rendering client.

Royal Caribbean's site sits behind Akamai Bot Manager, which blocks requests
from server IPs. Cloudflare's Browser Rendering API loads the page in a real
headless Chromium and returns the rendered HTML — the same approach Quackport
uses for its RC room prices and sailing sync.
"""

import json
import time
from typing import Optional

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class BrowserRenderingError(Exception):
    pass


class CloudflareBrowser:
    def __init__(self, account_id: str, api_token: str, client: Optional[httpx.Client] = None):
        self.account_id = account_id
        self.api_token = api_token
        self.client = client or httpx.Client(base_url="https://api.cloudflare.com/client/v4/", timeout=90)

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.api_token)

    def content(self, payload: dict, attempts: int = 3) -> str:
        """POST /browser-rendering/content and return the rendered HTML."""
        body = {"userAgent": USER_AGENT, **payload}
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                resp = self.client.post(
                    f"accounts/{self.account_id}/browser-rendering/content",
                    headers={"Authorization": f"Bearer {self.api_token}"},
                    content=json.dumps(body),
                )
            except httpx.HTTPError as exc:
                last = str(exc)
                time.sleep(attempt)
                continue
            if resp.status_code == 429:  # account-wide rate limit (free plan: 1 request / 10s)
                last = "rate limited (429)"
                time.sleep(10 * attempt)
                continue
            if resp.status_code >= 400:
                raise BrowserRenderingError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            data = resp.json()
            if data.get("success") and isinstance(data.get("result"), str):
                return data["result"]
            last = resp.text[:300]
            time.sleep(attempt)
        raise BrowserRenderingError(f"Browser Rendering failed after {attempts} attempts: {last}")
