# At Your Pace — Quote Builder

A private web app for searching every cruise sailing, tracking prices, and building personalised cruise quotes, powered by Claude.

## Search every sailing

**Search sailings** (`/search`) holds every bookable sailing of every line with a live integration, refreshed daily. Filter by line, ship, port or region, dates, nights, room class and price, and sort by price, price per night, or recent price changes. Each sailing shows its price history. **Watch** a sailing to re-price each of its individual room types every day, or press **Check room prices now**. **Quote** opens a pre-filled new quote.

The daily sync (`python -m app.sync`, run by `deploy/quotes-sync.timer`) pulls each line's whole catalog from its own search API: lead-in fare per room class for every sailing. A price is recorded only when it changes, so the history stays small. A line that fails doesn't stop the others; the search page shows each line's last sync.

| Line | Catalog (every sailing) | Individual room types | Cloudflare needed? |
|---|---|---|---|
| Royal Caribbean | ~3,200 sailings, ~10 s | Room-selection pages via Cloudflare | Room types only |
| Celebrity | ~2,000 sailings, ~5 s | Celebrity's rooms API (one call per sailing) | No (fallback only) |
| Viking (ocean, river, expedition) | ~9,900 sailings, ~40 s | Dates & Pricing API | No (fallback only) |
| MSC | ~6,900 sailings, ~17 s | Per category code, cruise-only and with Drinks & Wi-Fi | No (fallback only) |
| Virgin Voyages | ~420 sailings, ~2 min | Cabin categories API | No (fallback only) |
| Carnival | in progress | Booking API | No (fallback only) |

Timings are from a datacenter server. "Fallback only" means the provider switches to Cloudflare Browser Rendering automatically if the line starts blocking direct requests.

To re-price every room type on every sailing daily (not just watched ones), add `--all-rooms all --room-workers 10` to the sync command. That is the main Cloudflare cost: Royal Caribbean's room types take one or more browser renders per sailing, which the paid plan covers comfortably.

1. Enter the client, cruise line, ship and sail date (e.g. *Royal Caribbean · Utopia of the Seas · Oct 26*).
2. The app pulls every stateroom price, the itinerary, and the add-on packages (drinks, Wi-Fi, dining, shore excursions…).
3. Pick the rooms and add-ons to offer, adjust quantities or prices, add the optional **price tracking service** fee, and have Claude draft a personal opening note.
4. Download a branded **PDF quote** for the client.
5. Later, hit **Re-check prices**. The quote shows which quoted items went up or down since you sent it, which is the core of the tracking service.

It's meant to live at a subdomain such as `quotes.quackport.com`, on the same server as Quackport.

## Where prices come from

Each cruise line is a *provider* (`app/providers/`):

| Line | How | Accuracy |
|---|---|---|
| **Royal Caribbean** | Direct from RC's own systems, using the same approach Quackport's price tracker uses: sailing lookup through RC's cruise search, add-on prices through RC's Cruise Planner API, room prices from RC's room-selection page rendered with Cloudflare Browser Rendering (RC's bot protection blocks plain server requests). | Live prices |
| **Any other line** (Carnival, NCL, MSC, Celebrity, Disney…) | Claude searches the web and reads the cruise line's pages, then returns structured prices with a source for each one. | Good, but verify. The quote screen lists anything to double-check. |
| **Any line, most accurate** | Paste the cruise line's room-selection page (Ctrl+A, Ctrl+C) into the quote. Claude reads prices straight from it. | As accurate as the page |

If a direct integration only gets part of the data (for example rooms fail but add-ons work), Claude fills in the missing sections and flags them as web-researched.

**Adding a direct integration for another line** means one new file in `app/providers/` with a class that has `name`, `handles(cruise_line)` and `research(request) -> SailingResearch`, added to the provider list in `app/main.py`. See `royal_caribbean.py` for the pattern.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium     # used to render PDFs
cp .env.example .env                      # then fill it in
set -a; source .env; set +a
uvicorn app.main:app --reload
```

Open http://localhost:8000. To try it without any API keys, set `DEMO_MODE=1`: quotes then get clearly-labelled **made-up** sample prices.

Tests: `python -m pytest -q`

## Configuration

All settings are environment variables. `.env.example` documents each one. The important ones:

- `ANTHROPIC_API_KEY`: Claude, used for research on non-RC lines, reading pasted pages, filling gaps, and drafting opening notes. Uses `claude-opus-5` by default (`CLAUDE_MODEL`).
- `APP_PASSWORD`: **always set this in production.** The whole site sits behind a browser login prompt (any username).
- `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN`: needed for Royal Caribbean room prices and sailing lookup from a server. Use the same values as Quackport's `CloudflareBrowserRendering` settings.
- `RCCL_GRAPHQL_URL`: RC add-on prices. In production use the same Cloudflare Worker URL as Quackport's `RcclGraphQlUrl`.
- `BUSINESS_NAME`, `ADVISOR_NAME`, `ADVISOR_TAGLINE`, `ADVISOR_EMAIL`, `ADVISOR_PHONE`, `BUSINESS_WEBSITE`: printed on every quote.
- `DEFAULT_TRACKING_FEE`, `QUOTE_VALID_DAYS`: defaults for new quotes.

## Deploy to quotes.quackport.com

This follows the Quackport server setup (nginx + systemd, deployed by GitHub Actions over SSH).

**One-time server setup** (as root on the Quackport server):

```bash
# 1. DNS: add an A record  quotes  ->  the same IP as quackport.com

# 2. App directory, data directory, Python env, Chromium for PDFs
apt install -y python3-venv
mkdir -p /var/www/quotes /var/lib/quotes && chown www-data /var/lib/quotes
cd /var/www/quotes && git clone https://github.com/Rnugent91/At-your-pace.git . 
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH=/var/www/quotes/.browsers .venv/bin/python -m playwright install --with-deps chromium

# 3. Secrets: copy .env.example to /etc/quotes.env, fill it in, then
#    add PLAYWRIGHT_BROWSERS_PATH=/var/www/quotes/.browsers to it
chmod 600 /etc/quotes.env

# 4. Service + nginx + HTTPS
cp deploy/quotes.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now quotes
cp deploy/quotes-sync.service deploy/quotes-sync.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now quotes-sync.timer   # daily catalog sync
systemctl start quotes-sync                                           # first sync now
cp deploy/nginx-quotes.conf /etc/nginx/sites-available/quotes
ln -s /etc/nginx/sites-available/quotes /etc/nginx/sites-enabled/ && nginx -t && systemctl reload nginx
certbot --nginx -d quotes.quackport.com
```

**Automatic deploys:** add the repo secrets `SSH_HOST`, `SSH_USER` and `SSH_PRIVATE_KEY` (the same ones DuckPassport.Web uses). Every push to `main` then runs the tests, rsyncs the app, restarts the service, and checks `https://quotes.quackport.com/healthz`. The deploy never touches `/var/lib/quotes` (saved quotes) or `/etc/quotes.env`.

## Project layout

```
app/
  main.py              web routes (FastAPI)
  providers/           one module per cruise line + Claude research fallback
  pricing.py           quote totals and price-change tracking
  catalog.py           master sailing catalog + price history (SQLite)
  sync.py              daily catalog sync (python -m app.sync)
  pdf.py               PDF rendering (headless Chromium)
  templates/           advisor screens + pdf_quote.html (the client-facing quote)
deploy/                systemd units (app + daily sync timer) and nginx site
```
