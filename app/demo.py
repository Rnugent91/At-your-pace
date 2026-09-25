"""Clearly-labelled SAMPLE data for DEMO_MODE, so the workflow can be tried
without API keys. These are not real prices."""

from datetime import date, timedelta

from .models import AddOn, ItineraryDay, SailingResearch, Stateroom
from .providers.base import ResearchRequest

SAMPLE = "SAMPLE DATA"


def sample_research(req: ResearchRequest) -> SailingResearch:
    start = date.fromisoformat(req.sail_date)
    ports = [
        ("Port Canaveral, Florida", None, "16:30"),
        ("Nassau, Bahamas", "07:00", "17:00"),
        ("Perfect Day at CocoCay, Bahamas", "07:00", "17:00"),
        ("Port Canaveral, Florida", "06:00", None),
    ]
    itinerary = [
        ItineraryDay(day=i + 1, date=(start + timedelta(days=i)).isoformat(), port=p, arrive=a, depart=d)
        for i, (p, a, d) in enumerate(ports)
    ]

    def room(category, name, price):
        return Stateroom(category=category, name=name, code=None, price_per_person=price,
                         taxes_fees_per_person=95.0, sold_out=False, notes=None, source=SAMPLE)

    def addon(kind, name, price, unit, label, port=None):
        return AddOn(kind=kind, name=name, price=price, price_unit=unit, unit_label=label,
                     port=port, description=None, source=SAMPLE)

    return SailingResearch(
        cruise_line=req.cruise_line,
        ship=req.ship,
        sail_date=req.sail_date,
        nights=3,
        departure_port="Port Canaveral, Florida",
        itinerary_name="3 Night Bahamas & Perfect Day Cruise",
        itinerary=itinerary,
        currency="USD",
        staterooms=[
            room("Interior", "Interior", 489.0),
            room("Ocean View", "Ocean View", 569.0),
            room("Balcony", "Ocean View Balcony", 689.0),
            room("Suite", "Junior Suite", 1149.0),
        ],
        addons=[
            addon("beverage", "Deluxe Beverage Package", 89.99, "per_person_per_day", "per person, per day"),
            addon("beverage", "Refreshment Package", 44.99, "per_person_per_day", "per person, per day"),
            addon("internet", "VOOM Surf + Stream", 24.99, "per_device_per_day", "per device, per day"),
            addon("dining", "Unlimited Dining Package", 129.0, "per_person", "per person"),
            addon("excursion", "Nassau Beach Break", 79.0, "per_person", "per person", "Nassau, Bahamas"),
        ],
        sources=[SAMPLE],
        warnings=["DEMO MODE: these are made-up sample prices, not real Royal Caribbean pricing."],
    )
