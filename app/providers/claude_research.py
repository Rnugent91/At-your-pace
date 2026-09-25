"""Works for any cruise line: Claude searches the web and reads booking pages.

This is the fallback for lines without a direct integration, and it fills gaps
(e.g. stateroom prices) a direct integration couldn't get. If the advisor pastes
text copied from the line's booking page, that is treated as the primary source.
"""

import json
from typing import Optional

import anthropic

from ..models import SailingResearch
from .base import ProviderError, ResearchRequest

FALLBACK_BETA = "server-side-fallback-2026-07-01"

RESEARCH_SYSTEM = """You research cruise pricing for a travel advisor who will send the result to a client as a quote.
Accuracy matters more than completeness: the advisor reviews everything, but a wrong price that looks confident damages client trust.

- Prefer the cruise line's own website. Use reputable aggregators only to fill gaps, and say so.
- Never invent a price. If you cannot find one, say it is unknown.
- For every price, note where it came from (URL, or "pasted booking page").
- Cruise fares are usually quoted per person, double occupancy; say if a source shows something else and whether taxes and fees are included.
- Add-ons: beverage packages, Wi-Fi/internet, specialty dining packages, shore excursions, and other packages the line sells pre-cruise. Note the unit (per person per day, per person, per device...)."""

EXTRACT_SYSTEM = """Convert cruise research notes into the requested structure.
Use only facts present in the notes. Use null for anything unknown; never fill in a guessed price.
Put anything the advisor should double-check (estimates, stale or third-party prices, missing categories) in `warnings`."""


class ClaudeResearchProvider:
    name = "Claude web research"

    def __init__(self, client: Optional[anthropic.Anthropic], model: str):
        self.client = client
        self.model = model

    def handles(self, cruise_line: str) -> bool:
        return True

    @property
    def configured(self) -> bool:
        return self.client is not None

    def research(self, req: ResearchRequest, focus: str = "", known: Optional[SailingResearch] = None) -> SailingResearch:
        if not self.client:
            raise ProviderError("ANTHROPIC_API_KEY is not set, so Claude research is unavailable")
        notes = self._gather_notes(req, focus, known)
        return self._structure(req, notes)

    # Step 1: free-form research with web search / fetch.
    def _gather_notes(self, req: ResearchRequest, focus: str, known: Optional[SailingResearch]) -> str:
        guests = f"{req.adults} adult(s)" + (f" and {req.children} child(ren)" if req.children else "")
        task = [
            f"Find current pricing for {req.cruise_line} — {req.ship}, sailing {req.sail_date}, for {guests}.",
            "Gather: itinerary (ports by day, times if available), number of nights, departure port, "
            "stateroom categories with per-person fares, and pre-cruise add-on packages with prices.",
        ]
        if focus:
            task.append(f"Other sources already covered part of this. Focus only on: {focus}.")
        if known:
            task.append(f"Already known (do not re-research):\n{known.model_dump_json(include={'itinerary_name', 'nights', 'departure_port'})}")
        if req.booking_url:
            task.append(f"The advisor's booking link for this sailing: {req.booking_url}")
        if req.pasted_text:
            task.append(
                "The advisor pasted this text from the booking page. Treat it as the primary source for prices:\n"
                f"<pasted_booking_page>\n{req.pasted_text}\n</pasted_booking_page>"
            )
        task.append("Finish with a complete summary of everything you found, with a source for each price.")

        tools = [
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 10},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10},
        ]
        messages: list = [{"role": "user", "content": "\n\n".join(task)}]
        text_parts: list[str] = []
        for _ in range(6):  # server tools can pause long turns; resume until done
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=32000,
                system=RESEARCH_SYSTEM,
                thinking={"type": "adaptive"},
                tools=tools,
                messages=messages,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                resp = stream.get_final_message()
            if resp.stop_reason == "refusal":
                raise ProviderError("Claude declined this research request")
            text_parts.extend(b.text for b in resp.content if b.type == "text")
            if resp.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": resp.content})
        notes = "\n".join(text_parts).strip()
        if not notes:
            raise ProviderError("Claude research returned no findings")
        return notes

    # Step 2: turn the notes into a validated SailingResearch.
    def _structure(self, req: ResearchRequest, notes: str) -> SailingResearch:
        resp = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            system=EXTRACT_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Sailing: {req.cruise_line} — {req.ship}, embarking {req.sail_date}.\n\n"
                        f"<research_notes>\n{notes}\n</research_notes>"
                    ),
                }
            ],
            output_format=SailingResearch,
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise ProviderError("Claude could not structure the research results")
        return resp.parsed_output


INTRO_SYSTEM = """You write the short opening note of a travel advisor's cruise quote.
Warm, specific and brief: 2-3 sentences, no more than 70 words. Mention something concrete about the itinerary.
No prices (they are in the quote below), no exclamation-mark overload, no invented facts, no sign-off."""


def draft_client_intro(client: Optional[anthropic.Anthropic], model: str, quote_summary: dict) -> str:
    if not client:
        return ""
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=INTRO_SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": json.dumps(quote_summary)}],
    )
    if resp.stop_reason == "refusal":
        return ""
    return "".join(b.text for b in resp.content if b.type == "text").strip()
