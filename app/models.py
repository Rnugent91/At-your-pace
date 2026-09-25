"""Data shapes for sailing research and quotes.

`SailingResearch` (and the models it contains) is also the structured-output
schema Claude fills in, so every field is required and nullable rather than
defaulted.
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ItineraryDay(BaseModel):
    day: int
    date: Optional[str] = Field(description="ISO date, e.g. 2026-10-26")
    port: str = Field(description="Port name, or 'At Sea'")
    arrive: Optional[str]
    depart: Optional[str]


class Stateroom(BaseModel):
    category: Literal["Interior", "Ocean View", "Balcony", "Suite", "Other"]
    name: str = Field(description="Stateroom type as the cruise line names it, e.g. 'Spacious Ocean View Balcony'")
    code: Optional[str] = Field(description="Cruise line category code, e.g. '2D'")
    price_per_person: Optional[float] = Field(
        description="Cruise fare per person, double occupancy, in the sailing currency. Null if unknown or sold out."
    )
    taxes_fees_per_person: Optional[float] = Field(description="Taxes, fees and port expenses per person, if shown")
    sold_out: bool
    notes: Optional[str]
    source: str = Field(description="Where the price came from: 'scraped page', a URL, or 'estimate'")

    @property
    def key(self) -> str:
        return f"{self.category}|{self.name}"


class AddOn(BaseModel):
    kind: Literal["beverage", "dining", "internet", "excursion", "activity", "photo", "other"]
    name: str
    price: Optional[float]
    price_unit: Literal["per_person_per_day", "per_person", "per_device_per_day", "per_device", "flat"]
    unit_label: Optional[str] = Field(description="The unit exactly as the seller shows it, e.g. 'per person, per day'")
    port: Optional[str] = Field(description="Port for shore excursions, else null")
    description: Optional[str]
    source: str

    @property
    def key(self) -> str:
        return f"{self.kind}|{self.name}|{self.port or ''}"


class SailingResearch(BaseModel):
    cruise_line: str
    ship: str
    sail_date: str = Field(description="ISO date of embarkation")
    nights: Optional[int]
    departure_port: Optional[str]
    itinerary_name: Optional[str]
    itinerary: list[ItineraryDay]
    currency: str = Field(description="ISO currency code, e.g. USD")
    staterooms: list[Stateroom]
    addons: list[AddOn]
    sources: list[str] = Field(description="URLs or 'scraped page' used")
    warnings: list[str] = Field(
        description="Anything the advisor should double-check: stale, estimated, or missing prices"
    )


# Selections are stored by key (not list position) so they survive a price re-check.
class AddOnSelection(BaseModel):
    key: str
    # Advisor can override the computed quantity (e.g. only 2 of 4 guests buy the drink package).
    quantity: Optional[float] = None


class Quote(BaseModel):
    id: str
    created_at: str
    status: Literal["researching", "ready", "error"]
    error: Optional[str] = None
    client_name: str
    client_email: str = ""
    cruise_line: str = "Royal Caribbean"
    ship: str
    sail_date: str
    adults: int = 2
    children: int = 0
    booking_url: str = ""
    # Text the advisor copied from a booking page, for lines without a direct integration.
    pasted_text: str = ""
    provider: str = ""
    research: Optional[SailingResearch] = None
    selected_staterooms: list[str] = []
    selected_addons: list[AddOnSelection] = []
    tracking_enabled: bool = True
    tracking_fee: float = 0.0
    advisor_notes: str = ""
    # Short personalised intro for the PDF, drafted by Claude and editable by the advisor.
    client_intro: str = ""
    valid_until: str = ""
    demo: bool = False
    # Earlier research runs, oldest first, for price tracking.
    price_history: list[dict] = []

    @property
    def guests(self) -> int:
        return self.adults + self.children
