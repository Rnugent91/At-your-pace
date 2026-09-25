"""Run a JSON request from inside a real browser page (Cloudflare Browser Rendering).

Used as a fallback by providers whose cruise-line APIs answer plain server
requests today but sit behind bot managers that may start refusing server IPs:
the line's own page is loaded (so its cookies/bot checks pass), the request is
replayed with `fetch()` from inside it, and the response text is stashed in a
DOM node that we read back from the rendered HTML.
"""

import json
import re
from typing import Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup

from ..cf_browser import BrowserRenderingError, CloudflareBrowser
from .base import ProviderError


def fetch_json_in_page(
    cf: Optional[CloudflareBrowser],
    page_url: str,
    url: str,
    method: str = "GET",
    headers: Optional[dict] = None,
    body=None,
    marker: str = "qp-json",
    label: str = "Request",
):
    """Load `page_url` in a browser, run fetch(url) there and return the parsed JSON."""
    if cf is None or not cf.configured:
        raise ProviderError(f"{label} was blocked. Set CLOUDFLARE_ACCOUNT_ID/API_TOKEN to retry through a browser.")
    opts: dict = {"method": method, "headers": {"accept": "application/json", **(headers or {})}}
    if body is not None:
        opts["headers"].setdefault("content-type", "application/json")
        opts["body"] = json.dumps(body)
    script = (
        "(async function(){var out;try{out=await fetch(" + json.dumps(url) + "," + json.dumps(opts) + ")"
        ".then(function(r){return r.text();});}catch(e){out='ERR:'+(e&&e.message||e);}"
        "var d=document.createElement('div');d.id=" + json.dumps(marker) + ";"
        "d.setAttribute('data-json',encodeURIComponent(out));document.body.appendChild(d);})();"
    )
    try:
        html = cf.content(
            {
                "url": page_url,
                "gotoOptions": {"waitUntil": "domcontentloaded", "timeout": 30000},
                "addScriptTag": [{"content": script}],
                "waitForSelector": {"selector": f"#{marker}", "timeout": 55000},
            }
        )
    except BrowserRenderingError as exc:
        raise ProviderError(f"{label} failed in browser: {exc}") from exc
    node = BeautifulSoup(html, "html.parser").find(id=marker)
    if node is None:
        raise ProviderError(f"{label} returned no data in browser")
    text = unquote(node.get("data-json", ""))
    if text.startswith("ERR:"):
        raise ProviderError(f"{label} failed in-page: {text[:200]}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ProviderError(f"{label} returned non-JSON in browser: {text[:120]}") from exc


def page_text(html: str) -> str:
    """Visible text of an HTML page, whitespace collapsed (for reading published prices)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()
