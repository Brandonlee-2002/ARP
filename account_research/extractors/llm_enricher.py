"""
LLM enrichment layer.

Takes raw text chunks from web search + EDGAR and uses Claude to
extract structured data matching the AccountResearchRecord schema.

Design choices:
- One LLM call per schema section (not one giant prompt) — keeps context
  focused and makes partial failures recoverable.
- Each call returns JSON only; we validate with Pydantic before merging.
- System prompt is minimal; all schema detail goes in the user turn.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from ..extractors.web_search import SearchResult
from ..extractors.edgar import EdgarFinancials
from ..extractors.grounding import (
    is_url_grounded,
    number_grounded,
    text_contains_claim,
)

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2048


@dataclass
class LLMEnricher:
    """
    Orchestrates section-by-section LLM extraction.

    client: anthropic.Anthropic instance (caller owns lifecycle)
    """
    client: anthropic.Anthropic

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enrich(
        self,
        company_name: str,
        domain: str,
        search_results: list[SearchResult],
        edgar_data: Optional[EdgarFinancials] = None,
        programmatic_contacts: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """
        Returns a dict with keys matching AccountResearchRecord sections.
        Each section is independently extracted; failures return {} for
        that section so the rest of the record still populates.

        `programmatic_contacts` is the Apollo/PDL contact list (stage 2b
        of the pipeline, Stakeholder-shaped dicts from
        `people_data._to_stakeholder_dict()`). It is a grounding source in
        its own right: a real contact the LLM correctly surfaced has no
        web-snippet backing, and checking snippets alone would get it
        discarded as "ungrounded".
        """
        # Group search snippets by topic for targeted prompts
        by_topic = self._group_by_topic(search_results)

        raw_text_all = self._format_snippets(search_results)

        sections: dict[str, Any] = {}

        sections["overview"]      = self._extract_overview(company_name, domain, by_topic)
        sections["locations"]     = self._extract_locations(company_name, by_topic)

        stakeholders = self._extract_stakeholders(company_name, by_topic)
        sections["stakeholders"] = self._ground_stakeholders(
            stakeholders,
            by_topic.get("stakeholders", ""),
            programmatic_contacts or [],
        )

        economics = self._extract_economics(company_name, by_topic, edgar_data)
        econ_source_text = "\n".join(
            [by_topic.get("financials", ""), by_topic.get("semi_profile", "")]
        )
        sections["economics"] = self._ground_economics(economics, econ_source_text, edgar_data)

        sections["account_intelligence"] = self._extract_account_intelligence(
            company_name, raw_text_all
        )

        news_items = self._extract_news(company_name, by_topic)
        news_results = [r for r in search_results if r.topic == "news"]
        sections["news"] = self._ground_news(news_items, news_results)

        return sections

    # ------------------------------------------------------------------
    # Section extractors
    # ------------------------------------------------------------------

    def _extract_overview(
        self,
        company_name: str,
        domain: str,
        by_topic: dict[str, str],
    ) -> dict:
        prompt = f"""
Extract company overview information for "{company_name}" (website: {domain}).

SOURCE TEXT:
{by_topic.get("overview", "(no overview snippets found)")}

Return a JSON object with EXACTLY these fields (use null for unknowns):
{{
  "legal_name": string,
  "doing_business_as": string | null,
  "ticker": {{ "symbol": string, "exchange": string, "alt_otc": string | null }} | null,
  "ownership_type": "public" | "private" | "subsidiary" | "jv" | "government",
  "parent_company": string | null,
  "founded_year": integer | null,
  "website": string,
  "industry_verticals": [string],
  "employee_count": {{ "value": integer, "approximate": boolean, "as_of_year": integer | null }} | null,
  "summary": "2-4 sentence description of what the company does",
  "competitors": [string],
  "key_products": [string]
}}
"""
        return self._call(prompt, section="overview")

    def _extract_locations(self, company_name: str, by_topic: dict[str, str]) -> list:
        prompt = f"""
Extract all known office, fab, R&D, and sales locations for "{company_name}".

SOURCE TEXT:
{by_topic.get("locations", "(no location snippets found)")}

Return a JSON array. Each item must have EXACTLY these fields:
{{
  "type": "hq" | "regional_hq" | "fab" | "assembly" | "rd_center" | "design_center" | "sales_office" | "warehouse",
  "name": string | null,
  "city": string,
  "state_region": string | null,
  "country": string,
  "country_code": "2-letter ISO" | "",
  "status": "active" | "under_construction" | "ramping" | "planned" | "closed",
  "headcount": integer | null,
  "notes": string | null
}}

For semiconductor companies, distinguish fabs from sales offices.
Put HQ first. Include at most 12 locations.
"""
        result = self._call(prompt, section="locations", expect_list=True)
        return result if isinstance(result, list) else []

    def _extract_stakeholders(self, company_name: str, by_topic: dict[str, str]) -> list:
        prompt = f"""
Extract key stakeholders and decision-makers for "{company_name}" who would be
relevant to a semiconductor supplier's account manager. Include a mix of
leadership AND individual-contributor engineers — do not limit yourself to
executives who'd appear in press coverage; the source text below includes
engineering-focused search results specifically so you can surface working
engineers (process, device, test, validation, applications, etc.), not just
the CEO/CTO.

SOURCE TEXT:
{by_topic.get("stakeholders", "(no stakeholder snippets found)")}

Return a JSON array. Each person must have EXACTLY these fields:
{{
  "name": string,
  "title": string,
  "department": string | null,
  "location": string | null,
  "role_type": "economic_buyer" | "technical_buyer" | "executive_sponsor" | "champion" | "influencer" | "gatekeeper" | "end_user" | "blocker",
  "role_type_secondary": [],
  "influence_score": float between 0.0 and 1.0,
  "relationship_status": "cold",
  "linkedin_url": null,
  "email": null,
  "notes": string describing their buying relevance | null,
  "last_contacted": null
}}

Role guidance for semiconductor sales:
- economic_buyer: owns budget/contracts (procurement VP, CPO)
- technical_buyer: evaluates specs (chip architect, principal engineer, staff engineer, process/device/test engineer)
- executive_sponsor: approves strategic supplier list (CTO, CSO)
- gatekeeper: controls vendor qualification process

Include up to 15 people if the source text supports it — don't pad with
invented names, but don't stop at the first handful either. Aim for roughly
two-thirds technical_buyer (engineers) and one-third leadership/procurement,
matching the mix a real account manager would build. Set influence_score based
on role (economic_buyer 0.8-1.0, technical_buyer 0.4-0.7, executive_sponsor
0.5-0.7, others 0.3-0.6).
"""
        result = self._call(prompt, section="stakeholders", expect_list=True)
        return result if isinstance(result, list) else []

    def _extract_economics(
        self,
        company_name: str,
        by_topic: dict[str, str],
        edgar: Optional[EdgarFinancials],
    ) -> dict:
        edgar_block = ""
        if edgar:
            edgar_block = f"""
EDGAR AUDITED DATA (use these numbers — higher priority than web snippets):
  fiscal_year:    {edgar.fiscal_year}
  revenue_USD:    {edgar.revenue_usd}
  gross_profit:   {edgar.gross_profit_usd}
  net_income:     {edgar.net_income_usd}
  R&D expense:    {edgar.rd_expense_usd}
  capex:          {edgar.capex_usd}
  data_quality:   audited
"""

        prompt = f"""
Extract financial and semiconductor economics for "{company_name}".

{edgar_block}
WEB SOURCE TEXT:
{by_topic.get("financials", "")}
{by_topic.get("semi_profile", "")}

Return a JSON object with EXACTLY this structure (null for unknowns):
{{
  "financials": {{
    "fiscal_year": integer | null,
    "currency": "USD",
    "revenue_usd_m": float | null,        // in MILLIONS
    "revenue_growth_pct": float | null,
    "gross_margin_pct": float | null,
    "operating_margin_pct": float | null,
    "net_income_usd_m": float | null,
    "rd_spend_usd_m": float | null,
    "rd_pct_revenue": float | null,
    "capex_usd_m": float | null,
    "market_cap_usd_m": float | null,
    "data_quality": "audited" | "estimated" | "inferred"
  }},
  "revenue_segments": [
    {{
      "name": string,
      "revenue_usd_m": float,
      "pct_of_total": float,
      "growth_pct": float | null,
      "relevant_to_us": boolean
    }}
  ],
  "semiconductor_profile": {{
    "chip_buyer_type": "fabless" | "fab_lite" | "idc" | "ems" | "oem_buyer_only" | null,
    "has_own_fab": boolean,
    "fab_nodes": [string],
    "known_chip_suppliers": [string],
    "known_supply_gaps": [string],
    "design_win_notes": string | null,
    "qualification_cycle": string | null,
    "annual_chip_spend_est_usd_m": float | null
  }}
}}

IMPORTANT: convert raw USD figures to millions (divide by 1,000,000).
Mark data_quality as "audited" only if from SEC filing.
"""
        return self._call(prompt, section="economics")

    def _extract_account_intelligence(self, company_name: str, raw_text: str) -> dict:
        prompt = f"""
You are advising a semiconductor company's account manager researching "{company_name}".
Based on all available information, identify actionable intelligence.

SOURCE TEXT (all topics combined):
{raw_text[:6000]}

Return a JSON object with EXACTLY this structure:
{{
  "opportunity_signals": [
    {{
      "type": "opportunity" | "risk" | "watch",
      "description": string (1-2 sentences, specific and actionable),
      "urgency": "immediate" | "near_term" | "long_term",
      "source": string | null
    }}
  ],
  "recommended_next_action": string | null,
  "current_supplier_status": "no_relationship" | "evaluation" | "approved_vendor" | "active_account" | "strategic_partner"
}}

Include 2-5 signals. Focus on:
- Procurement timing windows (fab ramps, product launches)
- Competitive threats (incumbent suppliers, insourcing)
- Key qualification cycles
- Budget signals (capex increases, R&D expansion)
"""
        return self._call(prompt, section="account_intelligence")

    def _extract_news(self, company_name: str, by_topic: dict[str, str]) -> list:
        prompt = f"""
Extract recent news items about "{company_name}" relevant to a semiconductor supplier.

SOURCE TEXT:
{by_topic.get("news", "(no news snippets found)")}

Return a JSON array. Each item must have EXACTLY these fields:
{{
  "date": "YYYY-MM-DD" or "2025-01-01" if only year known,
  "headline": string (concise, factual),
  "summary": string | null (1 sentence),
  "source_name": string,
  "url": string,
  "tags": [string]   // e.g. ["fab", "capex", "earnings", "partnership", "leadership"]
}}

Include at most 8 items. Sort newest first. Prioritize news about:
fab investments, chip sourcing, earnings, leadership changes, partnerships.
"""
        result = self._call(prompt, section="news", expect_list=True)
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Grounding — catches invented names, figures, and URLs that are
    # schema-valid but never actually appeared in retrieved text. See
    # ../extractors/grounding.py for the underlying checks; this is just
    # the wiring: which value gets checked against which source, and
    # what happens on failure (placeholder, drop, or null — never a
    # silent keep).
    # ------------------------------------------------------------------

    # Financials fields the LLM prompt explicitly hands EDGAR's audited
    # numbers for (see the `edgar_block` in `_extract_economics`). Those
    # numbers legitimately won't appear in the web-search source text, and
    # get overridden again by the audited EDGAR figures later in
    # `assembler._build_economics` — so they must never be nulled here.
    _EDGAR_SOURCED_FINANCIAL_FIELDS = frozenset({
        "revenue_usd_m", "net_income_usd_m", "rd_spend_usd_m",
        "capex_usd_m", "gross_margin_pct",
    })

    # All numeric financials fields eligible for grounding (superset of
    # the EDGAR-sourced ones above).
    _ECONOMICS_NUMERIC_FIELDS = (
        "revenue_usd_m", "revenue_growth_pct", "gross_margin_pct",
        "operating_margin_pct", "net_income_usd_m", "rd_spend_usd_m",
        "rd_pct_revenue", "capex_usd_m", "market_cap_usd_m",
    )

    @staticmethod
    def _normalize_name(name: Optional[str]) -> str:
        return re.sub(r"\s+", " ", (name or "").strip().lower())

    def _ground_stakeholders(
        self,
        stakeholders: list,
        source_text: str,
        programmatic_contacts: list[dict],
    ) -> list[dict]:
        """
        A stakeholder name is grounded if it appears in the stakeholder
        snippets OR matches a programmatic (Apollo/PDL) contact name —
        the contact list counts as a grounding source in its own right so
        a real contact never gets destroyed for lacking snippet backing.
        Ungrounded names are replaced with a role-title placeholder, never
        dropped — the stakeholder (title, role_type, notes) still holds
        useful signal even if the name doesn't check out.
        """
        contact_names = {
            self._normalize_name(c.get("name"))
            for c in programmatic_contacts
            if isinstance(c, dict) and c.get("name")
        }

        grounded: list[dict] = []
        for item in stakeholders:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            if self._normalize_name(name) in contact_names or text_contains_claim(name, source_text):
                grounded.append(item)
                continue

            item = dict(item)
            title = item.get("title") or "Stakeholder"
            item["name"] = f"{title} (name not found)"
            logger.info(
                "[grounding] stakeholder name %r ungrounded — replaced with placeholder %r",
                name, item["name"],
            )
            grounded.append(item)
        return grounded

    def _ground_news(
        self,
        news_items: list,
        news_results: list[SearchResult],
    ) -> list[dict]:
        """
        A news item's URL is grounded only if it matches a URL actually
        returned by that run's news-topic search results. Ungrounded
        items are dropped outright — an invented citation has no salvage
        value the way an invented name does.
        """
        known_urls = {r.url for r in news_results if r.url}

        kept: list[dict] = []
        for item in news_items:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            if is_url_grounded(url, known_urls):
                kept.append(item)
            else:
                logger.info(
                    "[grounding] news item dropped — url not returned by search: %r (%r)",
                    item.get("headline"), url,
                )
        return kept

    def _ground_economics(
        self,
        economics: Any,
        source_text: str,
        edgar: Optional[EdgarFinancials],
    ) -> Any:
        """
        Nulls any economics.financials numeric figure that doesn't appear
        in the economics-topic source text — except the fields the model
        was explicitly handed EDGAR's audited numbers for, which are
        exempt whenever EDGAR data was available for this run (see
        `_EDGAR_SOURCED_FINANCIAL_FIELDS`).
        """
        if not isinstance(economics, dict):
            return economics
        financials = economics.get("financials")
        if not isinstance(financials, dict):
            return economics

        for field_name in self._ECONOMICS_NUMERIC_FIELDS:
            if edgar is not None and field_name in self._EDGAR_SOURCED_FINANCIAL_FIELDS:
                continue
            value = financials.get(field_name)
            if value is None:
                continue
            if not number_grounded(value, source_text):
                logger.info(
                    "[grounding] economics.financials.%s=%s ungrounded — set to null",
                    field_name, value,
                )
                financials[field_name] = None

        economics["financials"] = financials
        return economics

    # ------------------------------------------------------------------
    # LLM call wrapper
    # ------------------------------------------------------------------

    def _call(
        self,
        user_prompt: str,
        section: str,
        expect_list: bool = False,
    ) -> Any:
        """
        Calls Claude, extracts JSON from the response, and returns it.
        Returns {} (or []) on any failure so the pipeline continues.
        """
        system = (
            "You are a structured data extractor for a B2B semiconductor sales intelligence system. "
            "Respond ONLY with valid JSON — no markdown, no prose, no code fences. "
            "If information is unavailable, use null for optional fields. "
            "Never fabricate specific numbers — use null instead."
        )
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()

            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            parsed = json.loads(raw)
            logger.info("[llm] %s → ok", section)
            return parsed

        except json.JSONDecodeError as exc:
            logger.warning("[llm] %s → JSON parse error: %s", section, exc)
            return [] if expect_list else {}
        except Exception as exc:
            logger.warning("[llm] %s → call failed: %s", section, exc)
            return [] if expect_list else {}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _group_by_topic(results: list[SearchResult]) -> dict[str, str]:
        """
        Collapses search results into one text block per topic.
        Keeps the top-N snippets per topic to stay within token budget —
        stakeholders gets a higher cap since it now runs more queries
        (leadership + engineering-specific) and needs enough raw material
        for the LLM to actually find 15+ people rather than just the same
        handful of execs every other topic would surface.
        """
        grouped: dict[str, list[str]] = {}
        for r in results:
            grouped.setdefault(r.topic, [])
            grouped[r.topic].append(
                f"[{r.title}] ({r.url})\n{r.snippet}"
            )
        topic_caps = {"stakeholders": 20}
        return {
            topic: "\n\n".join(items[:topic_caps.get(topic, 5)])
            for topic, items in grouped.items()
        }

    @staticmethod
    def _format_snippets(results: list[SearchResult]) -> str:
        return "\n\n".join(
            f"[{r.topic.upper()}] {r.title}\n{r.snippet}"
            for r in results[:30]  # cap at 30 to avoid token overflow
        )