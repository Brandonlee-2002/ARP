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
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from ..extractors.web_search import SearchResult
from ..extractors.edgar import EdgarFinancials

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
    ) -> dict[str, Any]:
        """
        Returns a dict with keys matching AccountResearchRecord sections.
        Each section is independently extracted; failures return {} for
        that section so the rest of the record still populates.
        """
        # Group search snippets by topic for targeted prompts
        by_topic = self._group_by_topic(search_results)

        raw_text_all = self._format_snippets(search_results)

        sections: dict[str, Any] = {}

        sections["overview"]      = self._extract_overview(company_name, domain, by_topic)
        sections["locations"]     = self._extract_locations(company_name, by_topic)
        sections["stakeholders"]  = self._extract_stakeholders(company_name, by_topic)
        sections["economics"]     = self._extract_economics(company_name, by_topic, edgar_data)
        sections["account_intelligence"] = self._extract_account_intelligence(
            company_name, raw_text_all
        )
        sections["news"]          = self._extract_news(company_name, by_topic)

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
relevant to a semiconductor supplier's account manager.

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
- technical_buyer: evaluates specs (chip architect, principal engineer)
- executive_sponsor: approves strategic supplier list (CTO, CSO)
- gatekeeper: controls vendor qualification process

Include 4-8 people. Set influence_score based on role (economic_buyer 0.8-1.0,
technical_buyer 0.6-0.8, executive_sponsor 0.5-0.7, others 0.3-0.6).
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
        Keeps the top-5 snippets per topic to stay within token budget.
        """
        grouped: dict[str, list[str]] = {}
        for r in results:
            grouped.setdefault(r.topic, [])
            grouped[r.topic].append(
                f"[{r.title}] ({r.url})\n{r.snippet}"
            )
        return {
            topic: "\n\n".join(items[:5])
            for topic, items in grouped.items()
        }

    @staticmethod
    def _format_snippets(results: list[SearchResult]) -> str:
        return "\n\n".join(
            f"[{r.topic.upper()}] {r.title}\n{r.snippet}"
            for r in results[:30]  # cap at 30 to avoid token overflow
        )
