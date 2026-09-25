"""Quote maths: add-on quantities, per-option totals, and price-change tracking."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models import AddOn, Quote, SailingResearch, Stateroom


def default_quantity(addon: AddOn, guests: int, nights: Optional[int]) -> float:
    days = nights or 1
    return {
        "per_person_per_day": guests * days,
        "per_person": guests,
        "per_device_per_day": days,
        "per_device": 1,
        "flat": 1,
    }[addon.price_unit]


def quantity_label(addon: AddOn, qty: float, nights: Optional[int]) -> str:
    q, s = f"{qty:g}", "" if qty == 1 else "s"
    return {
        "per_person_per_day": f"{q} person-day{s}",
        "per_person": f"{q} guest{s}",
        "per_device_per_day": f"{q} device-day{s}",
        "per_device": f"{q} device{s}",
        "flat": f"× {q}",
    }[addon.price_unit]


@dataclass
class AddOnLine:
    addon: AddOn
    quantity: float
    quantity_label: str
    total: float


@dataclass
class RoomOption:
    room: Stateroom
    fare_total: float
    taxes_total: float
    grand_total: float


@dataclass
class QuoteTotals:
    rooms: list[RoomOption] = field(default_factory=list)
    addons: list[AddOnLine] = field(default_factory=list)
    addons_total: float = 0.0
    tracking_fee: float = 0.0


def compute_totals(quote: Quote) -> QuoteTotals:
    r = quote.research
    totals = QuoteTotals()
    if r is None:
        return totals

    chosen = {s.key: s.quantity for s in quote.selected_addons}
    for addon in r.addons:
        if addon.key not in chosen or addon.price is None:
            continue
        qty = chosen[addon.key]
        if qty is None:
            qty = default_quantity(addon, quote.guests, r.nights)
        line_total = round(addon.price * qty, 2)
        totals.addons.append(AddOnLine(addon, qty, quantity_label(addon, qty, r.nights), line_total))
    totals.addons_total = round(sum(line.total for line in totals.addons), 2)
    totals.tracking_fee = quote.tracking_fee if quote.tracking_enabled else 0.0

    for room in r.staterooms:
        if room.key not in quote.selected_staterooms or room.price_per_person is None:
            continue
        fare = round(room.price_per_person * quote.guests, 2)
        taxes = round((room.taxes_fees_per_person or 0) * quote.guests, 2)
        totals.rooms.append(
            RoomOption(room, fare, taxes, round(fare + taxes + totals.addons_total + totals.tracking_fee, 2))
        )
    return totals


# ── Price tracking ───────────────────────────────────────────────────────


def snapshot(research: SailingResearch) -> dict:
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rooms": {s.key: s.price_per_person for s in research.staterooms},
        "addons": {a.key: a.price for a in research.addons},
    }


@dataclass
class PriceChange:
    label: str
    old: float
    new: float

    @property
    def dropped(self) -> bool:
        return self.new < self.old


def price_changes(quote: Quote) -> list[PriceChange]:
    """Changes in the quoted items between the first snapshot and now."""
    if not quote.price_history or quote.research is None:
        return []
    first = quote.price_history[0]
    now = snapshot(quote.research)
    changes = []
    for key in quote.selected_staterooms:
        old, new = first["rooms"].get(key), now["rooms"].get(key)
        if old is not None and new is not None and old != new:
            changes.append(PriceChange(key.split("|", 1)[1] + " (per person)", old, new))
    for sel in quote.selected_addons:
        old, new = first["addons"].get(sel.key), now["addons"].get(sel.key)
        if old is not None and new is not None and old != new:
            changes.append(PriceChange(sel.key.split("|")[1], old, new))
    return changes
