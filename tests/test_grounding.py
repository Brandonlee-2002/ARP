"""
Offline regression tests for T-019: re-wiring the grounding pass.

Pydantic validates shape, not truth — a schema-valid stakeholder name, news
URL, or economics figure can still be entirely invented. These tests drive
`account_research.extractors.llm_enricher.LLMEnricher`'s grounding helpers
directly, and exercise the wiring through `enrich()` and
`AccountResearchPipeline.run()` with injected fakes, following the
monkeypatch style already used in `tests/test_edgar.py` and
`tests/test_assembler.py`. No network access, no API key required.
"""

from __future__ import annotations

import logging

from account_research.extractors.edgar import EdgarFinancials
from account_research.extractors.llm_enricher import LLMEnricher
from account_research.extractors.web_search import SearchResult
from account_research.pipeline.pipeline import AccountResearchPipeline


def _enricher() -> LLMEnricher:
    # The grounding helpers never touch `self.client`, so a plain sentinel
    # object stands in for the real anthropic.Anthropic instance.
    return LLMEnricher(client=object())


# ---------------------------------------------------------------------------
# Stakeholder name grounding — the regression that matters most: a real
# Apollo/PDL contact must never be destroyed just because it has no
# web-snippet backing.
# ---------------------------------------------------------------------------

def test_contact_only_stakeholder_name_is_kept():
    """A name present ONLY in the programmatic-contact list (no snippet
    backing at all) must be kept unchanged — this is the exact failure mode
    that would silently delete real Apollo/PDL contacts and make the output
    worse, not better."""
    enricher = _enricher()
    stakeholders = [{
        "name": "Priya Natarajan",
        "title": "Staff Process Engineer",
        "role_type": "technical_buyer",
    }]
    programmatic_contacts = [{
        "name": "Priya Natarajan",
        "title": "Staff Process Engineer",
        "source": "apollo",
    }]

    result = enricher._ground_stakeholders(
        stakeholders,
        source_text="",   # no snippet backing whatsoever
        programmatic_contacts=programmatic_contacts,
    )

    assert len(result) == 1
    assert result[0]["name"] == "Priya Natarajan"


def test_contact_only_stakeholder_name_matches_case_and_whitespace_insensitively():
    """Matching against the contact list must not be defeated by trivial
    formatting differences (case, doubled whitespace)."""
    enricher = _enricher()
    stakeholders = [{"name": "  priya   NATARAJAN ", "title": "Staff Process Engineer"}]
    programmatic_contacts = [{"name": "Priya Natarajan"}]

    result = enricher._ground_stakeholders(stakeholders, "", programmatic_contacts)

    assert result[0]["name"] == "  priya   NATARAJAN "  # kept as-is, not replaced


def test_stakeholder_name_grounded_via_snippet_text_is_kept():
    """A name that genuinely appears in the stakeholder-topic snippets is
    grounded even with zero programmatic contacts."""
    enricher = _enricher()
    stakeholders = [{"name": "John Smith", "title": "CTO"}]
    source_text = "[Acme names John Smith as new CTO] (https://example.com)\nJohn Smith joins as CTO."

    result = enricher._ground_stakeholders(stakeholders, source_text, [])

    assert result[0]["name"] == "John Smith"


def test_ungrounded_stakeholder_name_replaced_with_placeholder_not_dropped():
    """A name in neither the snippets nor the contact list is replaced with
    a role-title placeholder — the stakeholder itself must survive."""
    enricher = _enricher()
    stakeholders = [{"name": "Totally Invented Person", "title": "VP of Procurement"}]

    result = enricher._ground_stakeholders(
        stakeholders,
        source_text="unrelated snippet text about something else entirely",
        programmatic_contacts=[],
    )

    assert len(result) == 1   # not dropped
    assert result[0]["name"] == "VP of Procurement (name not found)"


def test_ungrounded_stakeholder_replacement_logs_info(caplog):
    enricher = _enricher()
    stakeholders = [{"name": "Nobody Real", "title": "Director of Sourcing"}]

    with caplog.at_level(logging.INFO):
        enricher._ground_stakeholders(stakeholders, "", [])

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("grounding" in r.message and "Nobody Real" in r.message for r in info_records)


# ---------------------------------------------------------------------------
# News URL grounding
# ---------------------------------------------------------------------------

def _news_result(url: str) -> SearchResult:
    return SearchResult(query="q", topic="news", url=url, title="t", snippet="s")


def test_news_item_with_unreturned_url_is_dropped():
    enricher = _enricher()
    news_items = [{
        "date": "2026-01-01",
        "headline": "Fabricated headline",
        "source_name": "Nowhere Times",
        "url": "https://nowhere.example.com/never-returned",
        "tags": [],
    }]
    news_results = [_news_result("https://real-source.example.com/article")]

    result = enricher._ground_news(news_items, news_results)

    assert result == []


def test_news_item_with_returned_url_is_kept():
    enricher = _enricher()
    url = "https://real-source.example.com/article"
    news_items = [{
        "date": "2026-01-01",
        "headline": "Real headline",
        "source_name": "Real Times",
        "url": url,
        "tags": [],
    }]
    news_results = [_news_result(url)]

    result = enricher._ground_news(news_items, news_results)

    assert len(result) == 1
    assert result[0]["url"] == url


def test_news_item_url_grounding_ignores_trailing_slash_and_case():
    enricher = _enricher()
    news_items = [{"headline": "h", "url": "HTTPS://Real-Source.example.com/Article/", "date": "2026-01-01",
                   "source_name": "s", "tags": []}]
    news_results = [_news_result("https://real-source.example.com/Article")]

    result = enricher._ground_news(news_items, news_results)

    assert len(result) == 1


def test_news_dropped_item_logs_info(caplog):
    enricher = _enricher()
    news_items = [{"headline": "Fabricated", "url": "https://invented.example.com", "date": "2026-01-01",
                   "source_name": "s", "tags": []}]

    with caplog.at_level(logging.INFO):
        enricher._ground_news(news_items, [])

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("grounding" in r.message and "dropped" in r.message for r in info_records)


# ---------------------------------------------------------------------------
# Economics figure grounding
# ---------------------------------------------------------------------------

def test_invented_economics_figure_is_nulled():
    enricher = _enricher()
    economics = {"financials": {"market_cap_usd_m": 999999.0}}

    result = enricher._ground_economics(
        economics,
        source_text="Acme reported strong quarterly results with no cap figures mentioned.",
        edgar=None,
    )

    assert result["financials"]["market_cap_usd_m"] is None


def test_economics_figure_present_in_source_text_is_kept():
    enricher = _enricher()
    economics = {"financials": {"market_cap_usd_m": 42000.0}}

    result = enricher._ground_economics(
        economics,
        source_text="Acme's market capitalization stood at roughly 42000 million dollars.",
        edgar=None,
    )

    assert result["financials"]["market_cap_usd_m"] == 42000.0


def test_edgar_sourced_figure_is_never_nulled_even_without_snippet_backing():
    """revenue_usd_m is one of the fields the LLM prompt is explicitly handed
    EDGAR's audited numbers for. It legitimately won't appear in the web
    source text — grounding must never null it when EDGAR data was available
    this run, since assembler._build_economics overrides it later anyway
    and nulling it here would just be actively wrong in between."""
    enricher = _enricher()
    economics = {"financials": {
        "revenue_usd_m": 53000.0,       # EDGAR-sourced — exempt
        "market_cap_usd_m": 1234567.0,  # LLM-invented, not EDGAR-sourced — must be nulled
    }}
    edgar = EdgarFinancials(ticker="INTC", revenue_usd=53_000_000_000.0)

    result = enricher._ground_economics(
        economics,
        source_text="no numbers here at all",
        edgar=edgar,
    )

    assert result["financials"]["revenue_usd_m"] == 53000.0
    assert result["financials"]["market_cap_usd_m"] is None


def test_edgar_exemption_does_not_apply_when_no_edgar_data():
    """The same field name (revenue_usd_m) is NOT exempt when this run had
    no EDGAR data at all — in that case the LLM must have pulled the number
    from the web snippets, and it should be grounded like anything else."""
    enricher = _enricher()
    economics = {"financials": {"revenue_usd_m": 53000.0}}

    result = enricher._ground_economics(
        economics,
        source_text="no numbers here at all",
        edgar=None,
    )

    assert result["financials"]["revenue_usd_m"] is None


def test_ground_economics_handles_missing_financials_gracefully():
    enricher = _enricher()
    assert enricher._ground_economics({}, "text", None) == {}
    assert enricher._ground_economics({"financials": None}, "text", None) == {"financials": None}


# ---------------------------------------------------------------------------
# Wiring through enrich() — programmatic_contacts must actually reach the
# stakeholder grounding step, and news-topic search results must be the
# ones used to ground news URLs.
# ---------------------------------------------------------------------------

def test_enrich_wires_programmatic_contacts_into_stakeholder_grounding(monkeypatch):
    enricher = _enricher()

    monkeypatch.setattr(enricher, "_extract_overview", lambda *a, **kw: {})
    monkeypatch.setattr(enricher, "_extract_locations", lambda *a, **kw: [])
    monkeypatch.setattr(
        enricher, "_extract_stakeholders",
        lambda *a, **kw: [
            {"name": "Priya Natarajan", "title": "Staff Process Engineer"},  # contact-only
            {"name": "Totally Invented Person", "title": "VP of Procurement"},  # neither source
        ],
    )
    monkeypatch.setattr(enricher, "_extract_economics", lambda *a, **kw: {"financials": {}})
    monkeypatch.setattr(enricher, "_extract_account_intelligence", lambda *a, **kw: {})
    monkeypatch.setattr(
        enricher, "_extract_news",
        lambda *a, **kw: [
            {"headline": "Kept", "url": "https://real.example.com/a", "date": "2026-01-01",
             "source_name": "s", "tags": []},
            {"headline": "Dropped", "url": "https://fake.example.com/b", "date": "2026-01-01",
             "source_name": "s", "tags": []},
        ],
    )

    search_results = [
        SearchResult(query="q", topic="news", url="https://real.example.com/a", title="t", snippet="s"),
    ]
    programmatic_contacts = [{"name": "Priya Natarajan", "title": "Staff Process Engineer"}]

    sections = enricher.enrich(
        company_name="Acme",
        domain="acme.com",
        search_results=search_results,
        edgar_data=None,
        programmatic_contacts=programmatic_contacts,
    )

    names = {s["name"] for s in sections["stakeholders"]}
    assert "Priya Natarajan" in names  # kept — the regression that matters most
    assert "VP of Procurement (name not found)" in names  # replaced, not dropped
    assert len(sections["stakeholders"]) == 2  # neither stakeholder was dropped

    news_urls = {n["url"] for n in sections["news"]}
    assert news_urls == {"https://real.example.com/a"}


def test_enrich_without_programmatic_contacts_still_works(monkeypatch):
    """programmatic_contacts is optional — omitting it must not raise, and
    stakeholders should fall back to snippet-only grounding."""
    enricher = _enricher()

    monkeypatch.setattr(enricher, "_extract_overview", lambda *a, **kw: {})
    monkeypatch.setattr(enricher, "_extract_locations", lambda *a, **kw: [])
    monkeypatch.setattr(
        enricher, "_extract_stakeholders",
        lambda *a, **kw: [{"name": "Nobody", "title": "Engineer"}],
    )
    monkeypatch.setattr(enricher, "_extract_economics", lambda *a, **kw: {"financials": {}})
    monkeypatch.setattr(enricher, "_extract_account_intelligence", lambda *a, **kw: {})
    monkeypatch.setattr(enricher, "_extract_news", lambda *a, **kw: [])

    sections = enricher.enrich(
        company_name="Acme",
        domain="acme.com",
        search_results=[],
        edgar_data=None,
    )

    assert sections["stakeholders"][0]["name"] == "Engineer (name not found)"


# ---------------------------------------------------------------------------
# Wiring through the pipeline — programmatic_stakeholders from stage 2b must
# reach LLMEnricher.enrich() as programmatic_contacts.
# ---------------------------------------------------------------------------

class _FakeWebSearch:
    def fetch_all(self, company_name, domain):
        return []


class _FakeEdgar:
    def fetch(self, ticker):
        return None


class _FakePeople:
    def __init__(self, contacts):
        self._contacts = contacts

    def fetch(self, company_name, domain, target_count=20):
        return self._contacts


class _FakeConfidence:
    overall = "medium"


class _FakeMeta:
    confidence = _FakeConfidence()


class _FakeRecord:
    meta = _FakeMeta()
    overview = type("O", (), {"legal_name": "Acme"})()
    locations = []
    stakeholders = []
    news = []


class _FakeAssembler:
    def assemble(self, **kwargs):
        return _FakeRecord()


def test_pipeline_passes_programmatic_stakeholders_into_enrich(monkeypatch):
    captured = {}

    pipeline = AccountResearchPipeline(
        anthropic_client=object(),
        search_api_key="dummy",
    )

    contacts = [{"name": "Priya Natarajan", "title": "Staff Process Engineer"}]
    pipeline._web = _FakeWebSearch()
    pipeline._edgar = _FakeEdgar()
    pipeline._people = _FakePeople(contacts)
    pipeline._assembler = _FakeAssembler()

    def fake_enrich(**kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr(pipeline._llm, "enrich", fake_enrich)

    pipeline.run(company_name="Acme", domain="acme.com")

    assert captured.get("programmatic_contacts") == contacts
