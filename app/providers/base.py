"""The interface every cruise line (or other supplier) integration implements.

To add a line with a direct integration (e.g. Carnival), write a class with
`name`, `handles()` and `research()` and register it in `providers/__init__.py`.
Anything no provider handles falls through to `ClaudeResearchProvider`, which
works for any line by searching the web and reading pasted booking pages.
"""

from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol

from ..models import SailingResearch


@dataclass
class ResearchRequest:
    cruise_line: str
    ship: str
    sail_date: str  # ISO yyyy-mm-dd
    adults: int = 2
    children: int = 0
    booking_url: str = ""
    pasted_text: str = ""


@dataclass
class CatalogSailing:
    """One sailing in the master catalog, with lead-in (cheapest) fares per room class.

    Produced in bulk by `CatalogProvider.iter_catalog()` from each line's own search
    API — cheap JSON calls, no page rendering — so every sailing of every line can be
    refreshed daily. Individual room-type prices come from `research()`.
    """

    cruise_line: str  # canonical name, as in CRUISE_LINES
    sailing_key: str  # the line's own stable id for this sailing (unique per line)
    ship: str
    sail_date: str  # ISO yyyy-mm-dd
    nights: Optional[int] = None
    ship_code: Optional[str] = None
    itinerary_name: Optional[str] = None
    departure_port: Optional[str] = None
    ports: list[str] = field(default_factory=list)  # ports of call in order, no sea days
    booking_url: Optional[str] = None
    # Room class ("Interior", "Ocean View", "Balcony", "Suite", or a line-specific
    # class name such as "Concierge Class") → cruise fare per person for 2 adults,
    # excluding taxes. None means listed but sold out.
    prices: dict[str, Optional[float]] = field(default_factory=dict)
    taxes_fees_per_person: Optional[float] = None
    currency: str = "USD"


class ProviderError(Exception):
    """The provider could not produce a usable result; the caller may fall back."""


class Provider(Protocol):
    name: str

    def handles(self, cruise_line: str) -> bool: ...

    def research(self, req: ResearchRequest) -> SailingResearch: ...


class CatalogProvider(Provider, Protocol):
    """A provider that can also list every bookable sailing for the master catalog."""

    cruise_line: str  # canonical name, as in CRUISE_LINES

    def iter_catalog(self) -> Iterator[CatalogSailing]: ...
