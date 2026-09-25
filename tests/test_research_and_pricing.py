from types import SimpleNamespace

import pytest

from app.demo import sample_research
from app.models import AddOnSelection, Quote
from app.pricing import compute_totals, price_changes, snapshot
from app.providers import ClaudeResearchProvider, ProviderError, ResearchRequest, run_research
from app.providers.claude_research import draft_client_intro

REQ = ResearchRequest("Carnival", "Carnival Jubilee", "2026-10-26")


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClaude:
    """Mimics the parts of anthropic.Anthropic the app uses."""

    def __init__(self, research, pause_first=False):
        self.research = research
        self.pause_first = pause_first
        self.stream_calls = []
        outer = self

        class Beta:
            class messages:
                @staticmethod
                def stream(**kw):
                    outer.stream_calls.append(kw)
                    paused = outer.pause_first and len(outer.stream_calls) == 1
                    text = "partial notes" if paused else "Balcony $700 pp (carnival.com)"
                    return FakeStream(SimpleNamespace(
                        stop_reason="pause_turn" if paused else "end_turn",
                        content=[SimpleNamespace(type="text", text=text)],
                    ))

                @staticmethod
                def parse(**kw):
                    outer.parse_kwargs = kw
                    return SimpleNamespace(stop_reason="end_turn", parsed_output=outer.research.model_copy(deep=True))

        class Messages:
            @staticmethod
            def create(**kw):
                return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" Hi Jane. ")])

        self.beta = Beta()
        self.messages = Messages()


class FailingProvider:
    name = "Direct"

    def handles(self, line):
        return True

    def research(self, req):
        raise ProviderError("blocked")


class PartialProvider(FailingProvider):
    def research(self, req):
        r = sample_research(req)
        r.staterooms, r.warnings = [], []
        return r


def test_claude_research_resumes_paused_turns_and_uses_notes():
    fake = FakeClaude(sample_research(REQ), pause_first=True)
    r = ClaudeResearchProvider(fake, "claude-opus-5").research(REQ)
    assert len(fake.stream_calls) == 2
    assert fake.stream_calls[1]["messages"][-1]["role"] == "assistant"  # resumed the paused turn
    assert "partial notes" in fake.parse_kwargs["messages"][0]["content"]
    assert fake.stream_calls[0]["fallbacks"] == "default"
    assert r.staterooms


def test_pasted_page_is_sent_to_claude():
    fake = FakeClaude(sample_research(REQ))
    req = ResearchRequest("MSC", "MSC World America", "2026-10-26", pasted_text="Balcony from $899")
    ClaudeResearchProvider(fake, "m").research(req)
    assert "Balcony from $899" in fake.stream_calls[0]["messages"][0]["content"]


def test_falls_back_to_claude_when_direct_provider_fails():
    claude = ClaudeResearchProvider(FakeClaude(sample_research(REQ)), "m")
    r, used = run_research(REQ, [FailingProvider()], claude)
    assert used == "Claude web research"
    assert "Direct unavailable: blocked" in r.warnings[0]


def test_claude_fills_gaps_from_direct_provider():
    claude = ClaudeResearchProvider(FakeClaude(sample_research(REQ)), "m")
    r, used = run_research(REQ, [PartialProvider()], claude)
    assert used == "Direct + Claude web research"
    assert r.staterooms and any("web research" in w for w in r.warnings)


def test_no_claude_key_and_no_direct_provider_errors():
    with pytest.raises(ProviderError):
        run_research(REQ, [], ClaudeResearchProvider(None, "m"))


def test_intro_draft():
    assert draft_client_intro(FakeClaude(sample_research(REQ)), "m", {}) == "Hi Jane."
    assert draft_client_intro(None, "m", {}) == ""


def make_quote(**kw):
    r = sample_research(ResearchRequest("Royal Caribbean", "Utopia of the Seas", "2026-10-26"))
    defaults = dict(id="q1", created_at="2026-09-25", status="ready", client_name="Jane", ship="Utopia",
                    sail_date="2026-10-26", adults=2, children=1, research=r, tracking_fee=50)
    return Quote(**{**defaults, **kw})


def test_totals():
    q = make_quote()
    rooms, addons = q.research.staterooms, q.research.addons
    q.selected_staterooms = [rooms[0].key]
    q.selected_addons = [
        AddOnSelection(key=addons[0].key),              # drinks: 3 guests × 3 nights × 89.99
        AddOnSelection(key=addons[2].key, quantity=2),  # wifi: override to 2 device-days
    ]
    t = compute_totals(q)
    assert t.addons[0].quantity == 9 and t.addons[0].total == 809.91
    assert t.addons[1].total == 49.98
    assert t.addons_total == 859.89
    opt = t.rooms[0]
    assert opt.fare_total == 1467.0 and opt.taxes_total == 285.0
    assert opt.grand_total == round(1467 + 285 + 859.89 + 50, 2)
    q.tracking_enabled = False
    assert compute_totals(q).rooms[0].grand_total == round(1467 + 285 + 859.89, 2)


def test_price_changes_track_quoted_items_only():
    q = make_quote()
    q.selected_staterooms = [q.research.staterooms[0].key]
    q.selected_addons = [AddOnSelection(key=q.research.addons[0].key)]
    q.price_history = [snapshot(q.research)]
    q.research.staterooms[0].price_per_person -= 40
    q.research.staterooms[1].price_per_person -= 99  # not quoted: ignored
    q.research.addons[0].price += 5
    changes = price_changes(q)
    assert [(c.label, c.dropped) for c in changes] == [("Interior (per person)", True), ("Deluxe Beverage Package", False)]
