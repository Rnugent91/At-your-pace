"""At Your Pace — quote builder web app."""

import logging
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import anthropic
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import pdf
from .cf_browser import CloudflareBrowser
from .config import Settings, settings as default_settings
from .db import QuoteStore
from .demo import sample_research
from .models import AddOnSelection, Quote
from .pricing import compute_totals, default_quantity, price_changes, snapshot
from .providers import CRUISE_LINES, ClaudeResearchProvider, ResearchRequest, run_research
from .providers.claude_research import draft_client_intro
from .providers.carnival import CarnivalProvider
from .providers.celebrity import CelebrityProvider
from .providers.royal_caribbean import RoyalCaribbeanProvider
from .providers.viking import VikingProvider

log = logging.getLogger(__name__)
HERE = Path(__file__).parent


def money(value, currency: str = "USD") -> str:
    if value is None:
        return "—"
    symbol = "$" if currency in ("USD", "CAD", "AUD") else ""
    suffix = "" if symbol else f" {currency}"
    return f"{symbol}{value:,.2f}{suffix}"


def nice_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return d.strftime("%A, %B %-d, %Y")


def short_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%a, %b %-d")
    except ValueError:
        return iso


def create_app(
    settings: Settings = default_settings,
    direct_providers: Optional[list] = None,
    claude_client: Optional[anthropic.Anthropic] = None,
) -> FastAPI:
    app = FastAPI(title="At Your Pace — Quote Builder")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = money
    templates.env.filters["nice_date"] = nice_date
    templates.env.filters["short_date"] = short_date
    templates.env.globals["settings"] = settings

    store = QuoteStore(settings.data_dir)
    if claude_client is None and settings.anthropic_api_key:
        claude_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    claude = ClaudeResearchProvider(claude_client, settings.model)
    if direct_providers is None:
        cf = CloudflareBrowser(settings.cf_account_id, settings.cf_api_token)
        direct_providers = [RoyalCaribbeanProvider(cf, settings.rccl_graphql_url), CelebrityProvider(cf), VikingProvider(cf), CarnivalProvider(cf)]

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # Browsers resend Basic-auth credentials on cross-site form posts, so reject
        # POSTs whose Origin isn't this site.
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and origin not in ("null",):
            if urlparse(origin).netloc != request.headers.get("host", ""):
                return Response("Cross-site request blocked", status_code=403)
        return await call_next(request)

    security = HTTPBasic(auto_error=False)

    def require_login(creds: Optional[HTTPBasicCredentials] = Depends(security)):
        if not settings.app_password:
            return
        if creds is None or not secrets.compare_digest(creds.password.encode(), settings.app_password.encode()):
            raise HTTPException(401, "Login required", headers={"WWW-Authenticate": 'Basic realm="Quotes"'})

    def get_quote(quote_id: str) -> Quote:
        q = store.get(quote_id)
        if q is None:
            raise HTTPException(404, "Quote not found")
        return q

    # ── Research (runs in the background) ────────────────────────────────

    def research_quote(quote_id: str, recheck: bool = False) -> None:
        q = store.get(quote_id)
        if q is None:
            return
        req = ResearchRequest(
            cruise_line=q.cruise_line, ship=q.ship, sail_date=q.sail_date, adults=q.adults,
            children=q.children, booking_url=q.booking_url, pasted_text=q.pasted_text,
        )
        try:
            if settings.demo_mode:
                research, provider = sample_research(req), "Demo (sample data)"
            else:
                research, provider = run_research(req, direct_providers, claude)
        except Exception as exc:
            log.exception("Research failed for quote %s", quote_id)
            q = store.get(quote_id) or q
            if recheck and q.research:
                q.status = "ready"
                q.error = f"Price re-check failed: {exc}"
            else:
                q.status, q.error = "error", str(exc)
            store.save(q)
            return

        q = store.get(quote_id) or q  # pick up edits made while researching
        q.research, q.provider, q.error = research, provider, None
        q.price_history.append(snapshot(research))
        if not recheck:
            q.selected_staterooms = default_room_selection(q)
        q.status = "ready"
        store.save(q)

    def default_room_selection(q: Quote) -> list[str]:
        """Cheapest available room in each category."""
        best = {}
        for room in q.research.staterooms:
            if room.price_per_person is None:
                continue
            cur = best.get(room.category)
            if cur is None or room.price_per_person < cur.price_per_person:
                best[room.category] = room
        return [r.key for r in best.values()]

    # ── Pages ────────────────────────────────────────────────────────────

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", dependencies=[Depends(require_login)])
    def home():
        return RedirectResponse("/quotes", 303)

    @app.get("/quotes", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def list_quotes(request: Request):
        return templates.TemplateResponse(request, "list.html", {"quotes": store.list()})

    @app.get("/quotes/new", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def new_quote(request: Request):
        return templates.TemplateResponse(
            request, "new.html",
            {"cruise_lines": CRUISE_LINES, "default_fee": settings.default_tracking_fee,
             "claude_ready": claude.configured, "demo": settings.demo_mode},
        )

    @app.post("/quotes", dependencies=[Depends(require_login)])
    async def create_quote(request: Request, background: BackgroundTasks):
        f = await request.form()
        sail_date = str(f.get("sail_date", ""))
        try:
            date.fromisoformat(sail_date)
        except ValueError:
            raise HTTPException(400, "Sail date must be a valid date")
        q = Quote(
            id=uuid.uuid4().hex[:10],
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status="researching",
            client_name=str(f.get("client_name", "")).strip() or "Client",
            client_email=str(f.get("client_email", "")).strip(),
            cruise_line=str(f.get("cruise_line", "Royal Caribbean")).strip() or "Royal Caribbean",
            ship=str(f.get("ship", "")).strip(),
            sail_date=sail_date,
            adults=max(1, int(f.get("adults") or 2)),
            children=max(0, int(f.get("children") or 0)),
            booking_url=str(f.get("booking_url", "")).strip(),
            pasted_text=str(f.get("pasted_text", "")).strip(),
            tracking_enabled=f.get("tracking_enabled") == "on",
            tracking_fee=float(f.get("tracking_fee") or settings.default_tracking_fee),
            advisor_notes=str(f.get("advisor_notes", "")).strip(),
            valid_until=(date.today() + timedelta(days=settings.quote_valid_days)).isoformat(),
            demo=settings.demo_mode,
        )
        if not q.ship:
            raise HTTPException(400, "Ship is required")
        store.save(q)
        background.add_task(research_quote, q.id)
        return RedirectResponse(f"/quotes/{q.id}", 303)

    @app.get("/quotes/{quote_id}", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def view_quote(request: Request, quote_id: str):
        q = get_quote(quote_id)
        nights = q.research.nights if q.research else None
        return templates.TemplateResponse(
            request, "quote.html",
            {
                "q": q,
                "totals": compute_totals(q),
                "changes": price_changes(q),
                "chosen_addons": {s.key: s.quantity for s in q.selected_addons},
                "default_qty": lambda a: default_quantity(a, q.guests, nights),
                "claude_ready": claude.configured,
            },
        )

    @app.post("/quotes/{quote_id}", dependencies=[Depends(require_login)])
    async def save_quote(request: Request, quote_id: str):
        q = get_quote(quote_id)
        f = await request.form()
        q.client_name = str(f.get("client_name", q.client_name)).strip() or q.client_name
        q.client_email = str(f.get("client_email", q.client_email)).strip()
        q.adults = max(1, int(f.get("adults") or q.adults))
        q.children = max(0, int(f.get("children") or 0))
        q.tracking_enabled = f.get("tracking_enabled") == "on"
        q.tracking_fee = float(f.get("tracking_fee") or 0)
        q.advisor_notes = str(f.get("advisor_notes", "")).strip()
        q.client_intro = str(f.get("client_intro", "")).strip()
        q.valid_until = str(f.get("valid_until", q.valid_until))
        if q.research:
            q.selected_staterooms = [str(k) for k in f.getlist("room")]
            # Advisor price edits (e.g. a promo the live site didn't show)
            for i, room in enumerate(q.research.staterooms):
                if (v := f.get(f"room_price_{i}")) not in (None, ""):
                    room.price_per_person = float(v)
            selections = []
            for i, addon in enumerate(q.research.addons):
                if f.get(f"addon_{i}") == "on":
                    qty = f.get(f"qty_{i}")
                    selections.append(AddOnSelection(key=addon.key, quantity=float(qty) if qty not in (None, "") else None))
            q.selected_addons = selections
        store.save(q)
        return RedirectResponse(f"/quotes/{q.id}#saved", 303)

    @app.post("/quotes/{quote_id}/recheck", dependencies=[Depends(require_login)])
    def recheck(quote_id: str, background: BackgroundTasks):
        q = get_quote(quote_id)
        q.status = "researching"
        store.save(q)
        background.add_task(research_quote, q.id, True)
        return RedirectResponse(f"/quotes/{q.id}", 303)

    @app.post("/quotes/{quote_id}/retry", dependencies=[Depends(require_login)])
    async def retry(request: Request, quote_id: str, background: BackgroundTasks):
        q = get_quote(quote_id)
        f = await request.form()
        q.booking_url = str(f.get("booking_url", q.booking_url)).strip()
        q.pasted_text = str(f.get("pasted_text", q.pasted_text)).strip()
        q.status, q.error = "researching", None
        q.price_history = []
        store.save(q)
        background.add_task(research_quote, q.id)
        return RedirectResponse(f"/quotes/{q.id}", 303)

    @app.post("/quotes/{quote_id}/intro", dependencies=[Depends(require_login)])
    def draft_intro(quote_id: str):
        q = get_quote(quote_id)
        if q.research is None:
            raise HTTPException(400, "Research isn't finished yet")
        r = q.research
        summary = {
            "client_first_name": q.client_name.split()[0],
            "guests": {"adults": q.adults, "children": q.children},
            "cruise_line": r.cruise_line, "ship": r.ship, "sail_date": r.sail_date,
            "nights": r.nights, "itinerary_name": r.itinerary_name,
            "ports": [d.port for d in r.itinerary],
            "advisor_notes": q.advisor_notes,
        }
        try:
            q.client_intro = draft_client_intro(claude.client, settings.model, summary) or q.client_intro
        except anthropic.APIError as exc:
            log.warning("Intro draft failed: %s", exc)
        store.save(q)
        return RedirectResponse(f"/quotes/{q.id}#intro", 303)

    @app.post("/quotes/{quote_id}/delete", dependencies=[Depends(require_login)])
    def delete(quote_id: str):
        store.delete(quote_id)
        return RedirectResponse("/quotes", 303)

    def pdf_context(q: Quote) -> dict:
        if q.research is None:
            raise HTTPException(400, "Research isn't finished yet")
        return {"q": q, "r": q.research, "totals": compute_totals(q), "settings": settings}

    @app.get("/quotes/{quote_id}/preview", response_class=HTMLResponse, dependencies=[Depends(require_login)])
    def preview(quote_id: str):
        return HTMLResponse(pdf.render_quote_html(templates.env, pdf_context(get_quote(quote_id))))

    @app.get("/quotes/{quote_id}/pdf", dependencies=[Depends(require_login)])
    def download_pdf(quote_id: str):
        q = get_quote(quote_id)
        html = pdf.render_quote_html(templates.env, pdf_context(q))
        data = pdf.html_to_pdf(html, settings)
        name = f"Quote - {q.client_name} - {q.ship} {q.sail_date}.pdf".replace("/", "-").replace('"', "")
        return Response(data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


app = create_app()
