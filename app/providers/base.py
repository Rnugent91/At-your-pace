"""The interface every cruise line (or other supplier) integration implements.

To add a line with a direct integration (e.g. Carnival), write a class with
`name`, `handles()` and `research()` and register it in `providers/__init__.py`.
Anything no provider handles falls through to `ClaudeResearchProvider`, which
works for any line by searching the web and reading pasted booking pages.
"""

from dataclasses import dataclass
from typing import Protocol

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


class ProviderError(Exception):
    """The provider could not produce a usable result; the caller may fall back."""


class Provider(Protocol):
    name: str

    def handles(self, cruise_line: str) -> bool: ...

    def research(self, req: ResearchRequest) -> SailingResearch: ...
