"""
Offline regression tests for account_research.pipeline.assembler.

Drives RecordAssembler.assemble() end-to-end with a synthetic EdgarFinancials
object; no network access.
"""

from __future__ import annotations

import re

import pytest

from account_research.extractors.edgar import EdgarFinancials
from account_research.pipeline.assembler import RecordAssembler


def test_fx_conversion_marks_estimated_with_notes():
    """An EdgarFinancials with fx_rate_to_usd set (foreign filer, converted)
    must yield data_quality == 'estimated' and a notes string naming the
    currency and rate."""
    edgar = EdgarFinancials(
        ticker="ASML",
        cik="0000937966",
        fiscal_year=2023,
        form_type="20-F",
        revenue_usd=27000000000.0,
        reporting_currency="EUR",
        fx_rate_to_usd=1.08,
        fx_rate_date="2023-12-29",
        fx_rate_source="ECB reference rate via Frankfurter (api.frankfurter.dev), 2023-12-29",
    )

    record = RecordAssembler().assemble(
        company_name="ASML",
        domain="asml.com",
        enriched={},
        edgar_data=edgar,
        search_sources=[],
    )

    financials = record.economics.financials
    assert financials.data_quality == "estimated"
    assert financials.notes is not None
    assert "EUR" in financials.notes
    assert "1.08" in financials.notes


def test_no_fx_marks_audited_with_no_notes():
    """An EdgarFinancials with the FX fields None (USD filer, no conversion
    needed) must yield data_quality == 'audited' and notes is None."""
    edgar = EdgarFinancials(
        ticker="INTC",
        cik="0000050863",
        fiscal_year=2024,
        form_type="10-K",
        revenue_usd=53000000000.0,
        fx_rate_to_usd=None,
    )

    record = RecordAssembler().assemble(
        company_name="Intel Corporation",
        domain="intel.com",
        enriched={},
        edgar_data=edgar,
        search_sources=[],
    )

    financials = record.economics.financials
    assert financials.data_quality == "audited"
    assert financials.notes is None


def test_fx_note_names_both_rates_and_which_figures_they_produced():
    """T-022: when both the period-average and period-end closing rates are
    present, the note must name both rate values and distinguish which
    figures each one produced — this is the ASML case that motivated the
    task: revenue/income/R&D/capex use the average rate, total assets use
    the closing rate, and the note used to only ever mention the closing
    rate."""
    edgar = EdgarFinancials(
        ticker="ASML",
        cik="0000937966",
        fiscal_year=2023,
        form_type="20-F",
        revenue_usd=32667000000.0 * 1.1296,   # already converted at the avg rate, like edgar.py does
        reporting_currency="EUR",
        fx_rate_to_usd=1.175,
        fx_rate_date="2023-12-31",
        fx_rate_source="ECB reference rate via Frankfurter (api.frankfurter.dev), 2023-12-31",
        fx_rate_avg_to_usd=1.1296,
        fx_rate_avg_start="2023-01-01",
        fx_rate_avg_end="2023-12-31",
        fx_rate_avg_source=(
            "ECB reference rate via Frankfurter (api.frankfurter.dev), "
            "period average 2023-01-01..2023-12-31"
        ),
    )

    record = RecordAssembler().assemble(
        company_name="ASML",
        domain="asml.com",
        enriched={},
        edgar_data=edgar,
        search_sources=[],
    )

    financials = record.economics.financials
    assert financials.data_quality == "estimated"
    assert financials.notes is not None
    assert "1.175" in financials.notes
    assert "1.1296" in financials.notes
    assert "None" not in financials.notes

    # The note must be unambiguous about which figures each rate produced —
    # not just mention both numbers in any order.
    notes = financials.notes
    avg_idx = notes.index("1.1296")
    close_idx = notes.index("1.175")
    revenue_idx = notes.lower().index("revenue")
    assets_idx = notes.lower().index("total assets")
    assert revenue_idx < avg_idx, "revenue must be described near the average rate"
    assert assets_idx < close_idx, "total assets must be described near the closing rate"

    # The round-trip check that is the actual point of this task: dividing
    # the recorded revenue_usd_m by the average rate *named in the note*
    # (not just the dataclass field) must recover the original EUR figure.
    avg_rate_in_note = float(re.search(r"period-average rate ([\d.]+)", notes).group(1))
    assert avg_rate_in_note == 1.1296
    recovered_eur_m = financials.revenue_usd_m / avg_rate_in_note
    assert recovered_eur_m == pytest.approx(32667.0, abs=1.0)


def test_fx_note_average_rate_only_does_not_print_none():
    """Average rate set, closing rate None (T-018 made the two independent):
    the note must name the average rate and must not print 'None' or imply
    total assets were converted."""
    edgar = EdgarFinancials(
        ticker="ASML",
        cik="0000937966",
        fiscal_year=2023,
        form_type="20-F",
        revenue_usd=32667000000.0 * 1.1296,
        reporting_currency="EUR",
        fx_rate_to_usd=None,
        fx_rate_avg_to_usd=1.1296,
        fx_rate_avg_start="2023-01-01",
        fx_rate_avg_end="2023-12-31",
        fx_rate_avg_source=(
            "ECB reference rate via Frankfurter (api.frankfurter.dev), "
            "period average 2023-01-01..2023-12-31"
        ),
    )

    record = RecordAssembler().assemble(
        company_name="ASML",
        domain="asml.com",
        enriched={},
        edgar_data=edgar,
        search_sources=[],
    )

    financials = record.economics.financials
    assert financials.data_quality == "estimated"
    assert financials.notes is not None
    assert "1.1296" in financials.notes
    assert "None" not in financials.notes
    assert "total assets" not in financials.notes.lower()


def test_fx_note_closing_rate_only_does_not_print_none():
    """Closing rate set, average rate None: the note must name the closing
    rate and must not print 'None' or imply revenue was converted at a rate
    that was never computed."""
    edgar = EdgarFinancials(
        ticker="ASML",
        cik="0000937966",
        fiscal_year=2023,
        form_type="20-F",
        revenue_usd=27000000000.0,
        reporting_currency="EUR",
        fx_rate_to_usd=1.175,
        fx_rate_date="2023-12-31",
        fx_rate_source="ECB reference rate via Frankfurter (api.frankfurter.dev), 2023-12-31",
        fx_rate_avg_to_usd=None,
    )

    record = RecordAssembler().assemble(
        company_name="ASML",
        domain="asml.com",
        enriched={},
        edgar_data=edgar,
        search_sources=[],
    )

    financials = record.economics.financials
    assert financials.data_quality == "estimated"
    assert financials.notes is not None
    assert "1.175" in financials.notes
    assert "None" not in financials.notes
    assert "revenue" not in financials.notes.lower()


# ---------------------------------------------------------------------------
# T-020: Stakeholder.verified populated from programmatic (Apollo/PDL) contacts
# ---------------------------------------------------------------------------

def test_exact_name_match_sets_verified_and_overrides_title_and_email():
    """An LLM stakeholder whose name exactly matches a programmatic contact
    (case-insensitive) must come out verified=True, with title/email
    overridden from the structured contact."""
    enriched = {
        "stakeholders": [
            {
                "name": "Jane Doe",
                "title": "Someone Important (LLM guess)",
                "role_type": "executive_sponsor",
                "influence_score": 0.8,
                "email": "guessed@example.com",
                "notes": "Quoted in a press release about the new fab.",
            }
        ]
    }
    programmatic = [
        {
            "name": "jane doe",
            "title": "VP Engineering",
            "location": "Austin, TX",
            "role_type": "executive_sponsor",
            "influence_score": 0.8,
            "relationship_status": "cold",
            "linkedin_url": "https://linkedin.com/in/janedoe",
            "email": "jane.doe@acme.com",
            "notes": "Sourced from apollo — not yet contacted.",
        }
    ]

    record = RecordAssembler().assemble(
        company_name="Acme",
        domain="acme.com",
        enriched=enriched,
        edgar_data=None,
        search_sources=[],
        programmatic_stakeholders=programmatic,
    )

    assert len(record.stakeholders) == 1
    stakeholder = record.stakeholders[0]
    assert stakeholder.verified is True
    assert stakeholder.title == "VP Engineering"
    assert stakeholder.email == "jane.doe@acme.com"
    assert stakeholder.linkedin_url == "https://linkedin.com/in/janedoe"
    assert stakeholder.location == "Austin, TX"
    # The LLM's own notes/buying-relevance context must survive the override.
    assert stakeholder.notes == "Quoted in a press release about the new fab."


def test_surname_only_match_sets_verified():
    """A surname-only match (different first name capitalization/spelling
    quirks, or the LLM only surfacing a partial name) must still set
    verified=True via the fallback."""
    enriched = {
        "stakeholders": [
            {
                "name": "J. Smith",
                "title": "Head of Procurement",
                "role_type": "economic_buyer",
                "influence_score": 0.6,
            }
        ]
    }
    programmatic = [
        {
            "name": "Jonathan Smith",
            "title": "Director of Procurement",
            "location": "Dallas, TX",
            "role_type": "economic_buyer",
            "influence_score": 0.65,
            "relationship_status": "cold",
            "linkedin_url": None,
            "email": None,
            "notes": "Sourced from pdl — not yet contacted.",
        }
    ]

    record = RecordAssembler().assemble(
        company_name="Acme",
        domain="acme.com",
        enriched=enriched,
        edgar_data=None,
        search_sources=[],
        programmatic_stakeholders=programmatic,
    )

    assert len(record.stakeholders) == 1
    stakeholder = record.stakeholders[0]
    assert stakeholder.verified is True
    assert stakeholder.title == "Director of Procurement"
    assert stakeholder.location == "Dallas, TX"


def test_unmatched_llm_stakeholder_stays_unverified():
    """An LLM stakeholder with no corresponding programmatic contact must
    remain verified=False — no match, no claim of current-employment
    confirmation."""
    enriched = {
        "stakeholders": [
            {
                "name": "Someone Else Entirely",
                "title": "CFO",
                "role_type": "economic_buyer",
                "influence_score": 0.9,
            }
        ]
    }
    programmatic = [
        {
            "name": "Completely Different Person",
            "title": "Staff Engineer",
            "role_type": "technical_buyer",
            "influence_score": 0.4,
            "relationship_status": "cold",
        }
    ]

    record = RecordAssembler().assemble(
        company_name="Acme",
        domain="acme.com",
        enriched=enriched,
        edgar_data=None,
        search_sources=[],
        programmatic_stakeholders=programmatic,
    )

    by_name = {s.name: s for s in record.stakeholders}
    assert by_name["Someone Else Entirely"].verified is False


def test_grounding_placeholder_never_matches_a_programmatic_contact():
    """T-019's grounding pass replaces an ungrounded LLM name with a
    role-title placeholder like 'Chief Nothing (name not found)'. Even if
    that placeholder's surname-like last token happens to collide with a
    real contact's surname, it must NEVER be stamped verified=True — that
    would assert a structured-source match for a name the model invented,
    which is exactly the false signal `verified` exists to prevent."""
    enriched = {
        "stakeholders": [
            {
                "name": "Chief Nothing (name not found)",
                "title": "Chief Nothing",
                "role_type": "executive_sponsor",
                "influence_score": 0.5,
            }
        ]
    }
    programmatic = [
        {
            # Deliberately share a "surname" token with the placeholder
            # text (the word right before the closing paren) to prove the
            # placeholder guard, not just an unlucky non-match.
            "name": "Chief Found",
            "title": "VP Engineering",
            "role_type": "executive_sponsor",
            "influence_score": 0.8,
            "relationship_status": "cold",
        }
    ]

    record = RecordAssembler().assemble(
        company_name="Acme",
        domain="acme.com",
        enriched=enriched,
        edgar_data=None,
        search_sources=[],
        programmatic_stakeholders=programmatic,
    )

    by_name = {s.name: s for s in record.stakeholders}
    placeholder = by_name["Chief Nothing (name not found)"]
    assert placeholder.verified is False
    assert placeholder.title == "Chief Nothing"
    # The real programmatic contact must still be added on its own, verified.
    assert by_name["Chief Found"].verified is True


def test_unmatched_programmatic_contact_added_as_verified_stakeholder():
    """A programmatic contact the LLM never surfaced at all must still be
    added to the roster, marked verified=True — the safety net that keeps
    a real Apollo/PDL contact from being silently dropped."""
    enriched = {
        "stakeholders": [
            {
                "name": "LLM Found Person",
                "title": "CEO",
                "role_type": "executive_sponsor",
                "influence_score": 0.95,
            }
        ]
    }
    programmatic = [
        {
            "name": "Never Surfaced Engineer",
            "title": "Principal Engineer",
            "location": "Boise, ID",
            "role_type": "technical_buyer",
            "influence_score": 0.4,
            "relationship_status": "cold",
            "linkedin_url": None,
            "email": None,
            "notes": "Sourced from apollo — not yet contacted.",
        }
    ]

    record = RecordAssembler().assemble(
        company_name="Acme",
        domain="acme.com",
        enriched=enriched,
        edgar_data=None,
        search_sources=[],
        programmatic_stakeholders=programmatic,
    )

    by_name = {s.name: s for s in record.stakeholders}
    assert "Never Surfaced Engineer" in by_name
    added = by_name["Never Surfaced Engineer"]
    assert added.verified is True
    assert added.title == "Principal Engineer"
    assert added.role_type == "technical_buyer"
