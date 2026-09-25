"""Provider registry and the research orchestration.

Direct integrations are tried first; Claude research covers everything else
and fills any sections a direct integration couldn't get.
"""

import logging

from ..models import SailingResearch
from .base import Provider, ProviderError, ResearchRequest
from .claude_research import ClaudeResearchProvider

log = logging.getLogger(__name__)

# Known lines for the form's picker. Only Royal Caribbean has a direct
# integration today; the rest use Claude research.
CRUISE_LINES = [
    "Royal Caribbean",
    "Carnival",
    "Celebrity",
    "Norwegian",
    "MSC",
    "Disney",
    "Princess",
    "Holland America",
    "Virgin Voyages",
    "Viking",
]


def run_research(
    req: ResearchRequest, direct: list[Provider], claude: ClaudeResearchProvider
) -> tuple[SailingResearch, str]:
    """Returns the research and a description of which providers produced it."""
    primary = next((p for p in direct if p.handles(req.cruise_line)), None)
    result = None
    notes: list[str] = []

    if primary:
        try:
            result = primary.research(req)
        except ProviderError as exc:
            log.warning("%s failed: %s", primary.name, exc)
            notes.append(f"{primary.name} unavailable: {exc}")

    if result is None:
        result = claude.research(req)
        result.warnings[:0] = notes + ["Prices were found by web research — verify each one before sending."]
        return result, claude.name

    gaps = []
    if not result.staterooms:
        gaps.append("stateroom categories and per-person fares")
    if not result.itinerary:
        gaps.append("the day-by-day itinerary")
    if not result.addons:
        gaps.append("add-on packages (drinks, Wi-Fi, dining, excursions)")
    if not gaps or not claude.configured:
        return result, primary.name

    try:
        fill = claude.research(req, focus="; ".join(gaps), known=result)
    except ProviderError as exc:
        result.warnings.append(f"Couldn't fill missing sections with web research: {exc}")
        return result, primary.name

    if not result.staterooms and fill.staterooms:
        result.staterooms = fill.staterooms
        result.warnings.append("Stateroom prices came from web research, not the cruise line's live system — verify them.")
    if not result.itinerary and fill.itinerary:
        result.itinerary = fill.itinerary
    if not result.addons and fill.addons:
        result.addons = fill.addons
        result.warnings.append("Add-on prices came from web research — verify them.")
    result.sources += [s for s in fill.sources if s not in result.sources]
    result.warnings += fill.warnings
    return result, f"{primary.name} + {claude.name}"


def build_direct_providers(settings) -> list:
    """Every live cruise-line integration. Shared by the web app and the daily sync."""
    from ..cf_browser import CloudflareBrowser
    from .carnival import CarnivalProvider
    from .celebrity import CelebrityProvider
    from .disney import DisneyProvider
    from .holland_america import HollandAmericaProvider
    from .msc import MSCProvider
    from .norwegian import NorwegianProvider
    from .princess import PrincessProvider
    from .royal_caribbean import RoyalCaribbeanProvider
    from .viking import VikingProvider
    from .virgin import VirginVoyagesProvider

    cf = CloudflareBrowser(settings.cf_account_id, settings.cf_api_token)
    return [
        RoyalCaribbeanProvider(cf, settings.rccl_graphql_url),
        CelebrityProvider(cf),
        VikingProvider(cf),
        CarnivalProvider(cf),
        MSCProvider(cf),
        VirginVoyagesProvider(cf),
        PrincessProvider(cf),
        HollandAmericaProvider(cf),
        DisneyProvider(cf),
        NorwegianProvider(cf),
    ]


__all__ = ["build_direct_providers", "CRUISE_LINES", "Provider", "ProviderError", "ResearchRequest", "run_research", "ClaudeResearchProvider"]
