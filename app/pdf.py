"""Renders the client-facing quote to PDF with headless Chromium (Playwright)."""

import base64
from pathlib import Path

from jinja2 import Environment

from .config import Settings

FONT_PATH = Path(__file__).parent / "static" / "fonts" / "fraunces-900-latin.woff2"


def _font_data_uri() -> str:
    return "data:font/woff2;base64," + base64.b64encode(FONT_PATH.read_bytes()).decode()


def render_quote_html(env: Environment, context: dict) -> str:
    return env.get_template("pdf_quote.html").render(font_uri=_font_data_uri(), **context)


def html_to_pdf(html: str, settings: Settings) -> bytes:
    from playwright.sync_api import sync_playwright

    launch = {"executable_path": settings.chromium_path} if settings.chromium_path else {}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            return page.pdf(
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.6in", "left": "0.55in", "right": "0.55in"},
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=(
                    "<div style='font-size:8px;color:#6b7785;width:100%;text-align:center;'>"
                    "Page <span class='pageNumber'></span> of <span class='totalPages'></span></div>"
                ),
            )
        finally:
            browser.close()
