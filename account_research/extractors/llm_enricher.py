"""
LLM enrichment layer.

Takes raw text chunks from web search + EDGAR (+ optionally verified People
Data Labs contacts) and uses Claude to extract structured data matching the
AccountResearchRecord schema.

Design choices:
- One LLM call per schema section (not one giant prompt) — keeps context
  focused and makes partial failures recoverable.
- Each call returns JSON only; we validate with Pydantic before merging.
- System prompt is minimal; all schema detail goes in the user turn.
- JSON extraction uses a balanced-bracket scanner (_extract_json), not a
  plain json.loads() on the raw response — Claude occasionally appends a
  trailing sentence after the JSON closes (e.g. "I found these on the
  investor relations page"), which breaks naive parsing. Ported from the
  identical fix in app.jsx, where this failure mode was first observed.
- Segment SAM estimation is deliberately split in two: the LLM only ever
  identifies a vertical + a disclosed unit volume (or returns null rather
  than inventing one); the dollar math is done in segment_benchmarks.py,
  never by the model.
- Grounding checks (extractors/grounding.py) run AFTER the LLM call for the
  fields most prone to confident fabrication — stakeholder names, disclosed
  unit volume, and news URLs. Pydantic validates shape; grounding checks
  are the closest thing this pipeline has to validating truthfulness.
  IMPORTANT: when verified People Data Labs contacts are supplied, their
  names are included in the grounding source text — a name the LLM copied
  correctly from a PDL contact often won't appear anywhere in the raw web
  snippets, and would otherwise be wrongly treated as fabricated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from ..extractors.web_search import SearchResult
from ..extractors.edgar import EdgarFinancials
from ..extractors.people_search import PersonProfile
from ..extractors.patent_search import PatentInventor
from ..extractors.segment_benchmarks import (
    BENCHMARKS, guess_vertical, estimate_segment_sam, sic_vertical_hint,
)
from ..extractors.grounding import text_contains_claim, number_grounded, is_url_grounded

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096  # raised from 2048 as a defensive margin against truncation
                    # on data-heavy responses (many locations/stakeholders)


def _extract_json(text: str) -> Optional[str]:
    """
    Finds the first complete JSON value (object or array) in `text` by
    scanning for balanced brackets, correctly skipping over string contents
    (so braces/brackets inside quoted strings don't throw off the depth
    count, and escaped quotes don't prematurely end a string). Returns the
    exact substring for that value — ignoring any leading preamble *and*
    any trailing prose Claude appended after the JSON closes. Returns None
    if the brackets never balance (e.g. a genuinely truncated response) —
    the caller's own json.loads then surfaces that failure normally.

    Ported from the identical `extractJson` in app.jsx.
    """
    start = None
    for i, ch in enumerate(text):
        if ch in "{[":
            start = i
            break
    if start is None:
        return None

    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    return None


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
        pdl_contacts: Optional[list[PersonProfile]] = None,
        patent_inventors: Optional[list[PatentInventor]] = None,
    ) -> dict[str, Any]:
        """
        Returns a dict with keys matching AccountResearchRecord sections.
        Each section is independently extracted; failures return {} for
        that section so the rest of the record still populates.

        pdl_contacts: verified People Data Labs contacts (optional). When
        present, they anchor the stakeholder-extraction prompt so Claude
        uses real names/titles instead of inferring purely from search
        snippets. Assembler.py does the actual name-matching + verified
        flag + safety-net merge; this method only makes the LLM aware of
        them as candidates.

        patent_inventors: PatentInventor objects from extractors/
        patent_search.py (optional). Also anchor the stakeholder prompt as
        candidates, but with a DIFFERENT caveat than PDL — a patent proves
        someone invented something at some point, not that they still
        work there. The prompt below reflects that distinction, and
        assembler.py never adds an unmatched patent inventor as a new
        stakeholder record the way it does for unmatched PDL contacts.
        """
        # Group search snippets by topic for targeted prompts
        by_topic = self._group_by_topic(search_results)

        raw_text_all = self._format_snippets(search_results)

        sections: dict[str, Any] = {}

        sections["overview"]      = self._extract_overview(company_name, domain, by_topic)
        sections["locations"]     = self._extract_locations(company_name, by_topic)
        sections["stakeholders"]  = self._extract_stakeholders(
            company_name, by_topic, pdl_contacts, patent_inventors
        )
        sections["economics"]     = self._extract_economics(company_name, by_topic, edgar_data)

        # Segment SAM depends on overview (business description) and
        # economics (audited revenue, if any) already being extracted,
        # so it runs after both and merges into the economics section.
        # edgar_data is passed through here for its sic/sic_description
        # fields — an audited anchor fact for the vertical classification,
        # not a substitute for the LLM's own judgment (see
        # segment_benchmarks.py's SIC_VERTICAL_HINTS docstring for why).
        sections["economics"]["segment_sam_estimates"] = self._extract_segment_sam(
            company_name, sections["overview"], by_topic, edgar_data
        )

        sections["account_intelligence"] = self._extract_account_intelligence(
            company_name, raw_text_all
        )
        sections["news"]          = self._extract_news(company_name, by_topic, search_results)

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

    def _extract_stakeholders(
        self,
        company_name: str,
        by_topic: dict[str, str],
        pdl_contacts: Optional[list[PersonProfile]] = None,
        patent_inventors: Optional[list[PatentInventor]] = None,
    ) -> list:
        source_text = by_topic.get("stakeholders", "")
        pdl_contacts = pdl_contacts or []
        patent_inventors = patent_inventors or []

        pdl_block = ""
        pdl_names_text = ""
        if pdl_contacts:
            lines = []
            for c in pdl_contacts:
                if not c.name:
                    continue
                bits = [c.name]
                if c.title:
                    bits.append(f"— {c.title}")
                if c.department:
                    bits.append(f"({c.department})")
                lines.append(" ".join(bits))
            pdl_names_text = "\n".join(f"[{c.name}]" for c in pdl_contacts if c.name)
            pdl_block = f"""
KNOWN VERIFIED CONTACTS (from a structured people-search source — these are
REAL, confirmed people currently at this company; prefer using their exact
name and title over inferring a different person for the same role):
{chr(10).join(lines)}
"""

        # Patent inventors get a DIFFERENT framing than PDL contacts — a
        # patent proves someone invented something at some point, not that
        # they still work here. The model is told not to assume current
        # employment or a specific title from this list alone.
        patent_block = ""
        patent_names_text = ""
        if patent_inventors:
            lines = []
            for inv in patent_inventors[:15]:  # cap — some companies have hundreds
                if not inv.name:
                    continue
                segs = ", ".join(sorted(inv.segments)) if inv.segments else None
                bits = [inv.name, f"— {inv.patent_count} granted patent(s)"]
                if segs:
                    bits.append(f"(segments: {segs})")
                if inv.most_recent_grant:
                    bits.append(f"most recent grant: {inv.most_recent_grant}")
                lines.append(" ".join(bits))
            patent_names_text = "\n".join(f"[{inv.name}]" for inv in patent_inventors if inv.name)
            patent_block = f"""
KNOWN PATENT INVENTORS (real people who have been granted patents assigned to
this company — NOT confirmed current employees; a patent can be years old.
Only use one of these as a stakeholder if the source text ALSO corroborates
they currently work here — otherwise leave them out rather than assuming an
active role from patent data alone):
{chr(10).join(lines)}
"""

        prompt = f"""
Extract key stakeholders and decision-makers for "{company_name}" who would be
relevant to a semiconductor supplier's account manager.
{pdl_block}{patent_block}
SOURCE TEXT:
{source_text or "(no stakeholder snippets found)"}

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

IMPORTANT — do not fabricate names. If a KNOWN VERIFIED CONTACT above fits a
relevant role, use their exact name and title. A KNOWN PATENT INVENTOR may
only be used if the source text corroborates current employment — do not
promote a patent inventor to a stakeholder based on the patent list alone.
For any other role, only use a real person's name if it literally appears in
the source text. If you cannot find a real name for a role you believe
exists, use the role title itself as the "name" field instead — e.g. "VP of
Procurement (name not found)" — rather than inventing a plausible-sounding
person. A made-up name is worse than an honest placeholder.
"""
        result = self._call(prompt, section="stakeholders", expect_list=True)
        result = result if isinstance(result, list) else []

        # Grounding pass: re-check every name against source text OR the
        # verified PDL/patent lists. A name that only appears in one of
        # those (not in the raw web snippets) is still genuinely grounded —
        # it's sourced from a structured data provider, just not from text
        # the model was quoting. Without including these here, a correctly
        # anchored name would be wrongly treated as invented.
        grounding_source = f"{source_text}\n{pdl_names_text}\n{patent_names_text}"
        for item in result:
            name  = item.get("name", "") or ""
            title = item.get("title", "") or "Unknown role"
            if "(name not found)" in name.lower():
                continue  # model already self-flagged, nothing to check
            if not text_contains_claim(name, grounding_source):
                logger.info(
                    "[llm] stakeholders → ungrounded name '%s' replaced with role placeholder", name
                )
                item["name"] = f"{title} (name not found)"

        return result

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

    def _extract_segment_sam(
        self,
        company_name: str,
        overview: dict,
        by_topic: dict[str, str],
        edgar: Optional[EdgarFinancials] = None,
    ) -> list[dict]:
        """
        Two-step estimate of Power / EP / HPC / CONN / AMS SAM for this
        account:

          1. This method's LLM call ONLY identifies which benchmarked
             vertical fits the account and whether the source text
             discloses an annual unit volume. It is explicitly told to
             return null rather than invent a number. When available,
             SEC's own SIC classification (audited, structured — not an
             LLM guess) is given to the model as an anchor fact via
             segment_benchmarks.sic_vertical_hint(), narrowing the likely
             candidates without dictating the answer — SIC codes are
             coarser than our vertical taxonomy (can't distinguish EV vs
             ICE, or AI vs general-purpose servers), so this informs the
             model's judgment rather than replacing it.
          2. A grounding check re-verifies that any returned unit volume
             actually appears in the source text — the LLM can comply
             with the "don't fabricate" instruction in spirit but still
             be wrong in practice, so this is checked rather than trusted.
          3. segment_benchmarks.estimate_segment_sam() does the actual
             dollar math in plain Python — no LLM arithmetic involved.

        Always returns 5 entries (see estimate_segment_sam docstring) so
        the record is uniform whether or not we could compute a number.
        """
        vertical_options = ", ".join(f'"{k}"' for k in BENCHMARKS.keys())
        summary = overview.get("summary", "") or ""
        verticals = overview.get("industry_verticals", []) or []
        combined_source = f"{by_topic.get('overview', '')}\n{by_topic.get('financials', '')}"

        sic_code = edgar.sic if edgar else None
        sic_description = edgar.sic_description if edgar else None
        hint = sic_vertical_hint(sic_code)

        sic_block = ""
        if sic_description:
            if hint:
                sic_block = f"""
SEC FILER CLASSIFICATION (audited, structured fact from EDGAR — not a guess):
SIC code {sic_code} ("{sic_description}"). Companies with this SIC code are
usually one of: {", ".join(hint["candidates"])}. Use the source text below
to decide which of these (if any) actually fits — SIC classification is too
coarse to make this call by itself (e.g. it cannot distinguish an EV
automaker from an ICE one, or an AI server maker from a general-purpose
one), so don't treat this as the final answer on its own.
"""
            else:
                sic_block = f"""
SEC FILER CLASSIFICATION (audited, structured fact from EDGAR — not a guess):
SIC code {sic_code} ("{sic_description}"). Not pre-mapped to any of the
listed verticals below — use your own judgment on whether it's relevant.
"""

        prompt = f"""
Identify which semiconductor end-market vertical best fits "{company_name}"
as a customer/OEM (not as a chip supplier), and find their most recent
disclosed annual unit volume, if any.
{sic_block}
COMPANY SUMMARY:
{summary}
Industry verticals (as already extracted): {", ".join(verticals) or "(none extracted)"}

SOURCE TEXT:
{combined_source}

Return a JSON object with EXACTLY these fields:
{{
  "vertical_key": one of [{vertical_options}] | null,
  "vertical_confidence": "high" | "medium" | "low",
  "annual_unit_volume": integer | null,
  "unit_type": string | null,
  "volume_source": string | null,
  "reasoning": "1-2 sentences explaining the vertical match and, if applicable, where the unit volume came from"
}}

Rules:
- vertical_key MUST be null if none of the listed options clearly fits this
  account's actual product/business — do not force a match onto the
  closest-sounding option, even if a SIC hint above suggests a candidate.
- annual_unit_volume MUST be null unless the source text discloses or very
  strongly implies a specific figure (e.g. "vehicles produced last year",
  "base stations deployed", "servers shipped"). Never estimate, infer from
  revenue, or fabricate a volume — null is the correct answer far more
  often than a real number.
"""
        classification = self._call(prompt, section="segment_sam_classification")
        if not isinstance(classification, dict):
            classification = {}

        vertical_key = classification.get("vertical_key")
        if not vertical_key or vertical_key not in BENCHMARKS:
            # Fallback order when the LLM returns null:
            #   1. SIC hint, but ONLY if it unambiguously implies exactly
            #      one candidate — an ambiguous SIC hint (e.g. "could be
            #      auto_ice or auto_ev") isn't a safe deterministic
            #      fallback, since picking either half of an ambiguous
            #      pair is just as likely to be wrong as guessing blind.
            #   2. The cheap keyword guess, same as before SIC existed.
            if hint and len(hint["candidates"]) == 1:
                vertical_key = hint["candidates"][0]
            else:
                vertical_key = guess_vertical(verticals, summary)

        unit_volume = classification.get("annual_unit_volume")
        reasoning   = classification.get("reasoning")

        # Grounding check: the model was told not to fabricate a volume,
        # but "told not to" isn't "verified not to." Discard silently
        # rather than pass an ungrounded number into the SAM math — an
        # ungrounded volume is worse than no volume at all, since it would
        # otherwise produce a confident-looking dollar estimate.
        if unit_volume is not None and not number_grounded(unit_volume, combined_source):
            logger.info(
                "[llm] segment_sam → ungrounded unit_volume %s discarded for %s",
                unit_volume, company_name,
            )
            unit_volume = None
            reasoning = (reasoning or "") + " (disclosed unit volume could not be verified in source text; discarded.)"

        return estimate_segment_sam(
            vertical_key=vertical_key,
            unit_volume=unit_volume,
            reasoning=reasoning,
        )

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

    def _extract_news(
        self,
        company_name: str,
        by_topic: dict[str, str],
        search_results: list[SearchResult],
    ) -> list:
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

IMPORTANT — only report items whose URL appears in the source text above.
Do not invent a headline, source, or URL that isn't backed by the source
text, even if it sounds plausible.
"""
        result = self._call(prompt, section="news", expect_list=True)
        result = result if isinstance(result, list) else []

        # Grounding pass: cross-check each item's URL against the URLs
        # actually returned by the "news" search queries. An item whose
        # URL doesn't match anything we actually retrieved is dropped
        # outright rather than kept with a caveat — an invented news
        # citation is a worse outcome than a shorter, all-real news list.
        known_urls = {r.url for r in search_results if r.topic == "news" and r.url}
        grounded = []
        for item in result:
            url = item.get("url", "") or ""
            if known_urls and not is_url_grounded(url, known_urls):
                logger.info("[llm] news → ungrounded URL dropped: %s", url)
                continue
            grounded.append(item)

        return grounded

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
            "Never fabricate specific numbers, names, or citations — use null "
            "or an explicit placeholder instead. An honest gap is always "
            "preferable to a plausible-sounding invention."
        )
        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
            raw = response.content[0].text.strip()

            # Strip accidental markdown fences before hunting for JSON
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            extracted = _extract_json(raw)
            parsed = json.loads(extracted if extracted is not None else raw)
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