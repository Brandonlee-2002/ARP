"""
Record assembler.

Takes the raw dicts returned by LLMEnricher and EDGAR, validates them
against the Pydantic schema, and produces a fully typed AccountResearchRecord.

Handles partial failures gracefully: if the LLM returns a malformed
stakeholders block, we log a warning and continue with an empty list.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from ..models.schema import (
    AccountResearchRecord,
    AccountIntelligence,
    Confidence,
    ConfidenceMap,
    Economics,
    EdgarFinancials,    # re-export for type hint
    Financials,
    Location,
    Meta,
    NewsItem,
    OpportunitySignal,
    Overview,
    RevenueSegment,
    SemiconductorProfile,
    SourceRecord,
    Stakeholder,
)
from ..extractors.edgar import EdgarFinancials

logger = logging.getLogger(__name__)

# Marker substring of the role-title placeholder T-019's grounding pass
# (llm_enricher._ground_stakeholders) writes over an ungrounded LLM name,
# e.g. "Chief Nothing (name not found)". Such a placeholder must never be
# treated as a match against a programmatic contact — doing so would stamp
# verified=True on a name the model invented, the opposite of what the flag
# means (see CLAUDE.md's "Grounding rules are deliberate" section).
PLACEHOLDER_NAME_MARKER = "(name not found)"


class RecordAssembler:
    """
    Merges enriched dicts → validated AccountResearchRecord.
    """

    def assemble(
        self,
        company_name: str,
        domain: str,
        enriched: dict[str, Any],
        edgar_data: Optional[EdgarFinancials],
        search_sources: list[dict],      # [{name, url, fetched_at, fields_populated}]
        refresh_cadence: str = "weekly",
        programmatic_stakeholders: Optional[list[dict]] = None,
    ) -> AccountResearchRecord:

        now = datetime.utcnow()

        # ------------------------------------------------------------------
        # Build sources list
        # ------------------------------------------------------------------
        sources = []
        for s in search_sources:
            try:
                sources.append(SourceRecord(**s))
            except Exception:
                pass

        if edgar_data:
            sources.append(SourceRecord(
                name=f"SEC EDGAR {edgar_data.form_type or '10-K'} {edgar_data.fiscal_year or ''}".strip(),
                url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={edgar_data.cik}",
                fetched_at=now,
                fields_populated=["economics.financials"],
            ))

        # ------------------------------------------------------------------
        # Overview
        # ------------------------------------------------------------------
        overview = self._build_overview(enriched.get("overview", {}), domain)

        # ------------------------------------------------------------------
        # Locations
        # ------------------------------------------------------------------
        locations = self._build_locations(enriched.get("locations", []))

        # ------------------------------------------------------------------
        # Stakeholders — merge LLM-found (web search) with programmatic
        # (Apollo/PDL) sources before validation. An LLM stakeholder whose
        # name matches a programmatic contact (exact, then surname,
        # case-insensitive) is stamped verified=True and has its
        # title/linkedin_url/email/location overridden from the structured
        # contact; its role_type/notes/influence_score stay the LLM's own.
        # A programmatic contact nobody in the LLM list matched is still
        # added as its own verified=True stakeholder rather than dropped.
        # ------------------------------------------------------------------
        raw_stakeholders = self._merge_stakeholder_sources(
            enriched.get("stakeholders", []) or [],
            programmatic_stakeholders or [],
        )
        stakeholders = self._build_stakeholders(raw_stakeholders)

        # ------------------------------------------------------------------
        # Economics — merge EDGAR (higher priority) with LLM estimates
        # ------------------------------------------------------------------
        economics = self._build_economics(enriched.get("economics", {}), edgar_data)

        # ------------------------------------------------------------------
        # Account intelligence
        # ------------------------------------------------------------------
        account_intelligence = self._build_account_intelligence(
            enriched.get("account_intelligence", {})
        )

        # ------------------------------------------------------------------
        # News
        # ------------------------------------------------------------------
        news = self._build_news(enriched.get("news", []))

        # ------------------------------------------------------------------
        # Confidence scoring
        # ------------------------------------------------------------------
        confidence = self._score_confidence(
            overview, locations, stakeholders, economics, edgar_data
        )

        # ------------------------------------------------------------------
        # Assemble
        # ------------------------------------------------------------------
        meta = Meta(
            refreshed_at=now,
            refresh_cadence=refresh_cadence,
            confidence=confidence,
            sources=sources,
        )

        return AccountResearchRecord(
            meta=meta,
            overview=overview,
            locations=locations,
            stakeholders=stakeholders,
            economics=economics,
            account_intelligence=account_intelligence,
            news=news,
        )

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_overview(self, raw: dict, domain: str) -> Overview:
        raw.setdefault("website", f"https://{domain}")
        raw.setdefault("legal_name", "Unknown")
        try:
            return Overview(**raw)
        except ValidationError as exc:
            logger.warning("[assembler] overview validation error: %s", exc)
            # Return minimal valid record
            return Overview(
                legal_name=raw.get("legal_name", "Unknown"),
                website=raw.get("website", f"https://{domain}"),
            )

    def _build_locations(self, raw: list) -> list[Location]:
        locations = []
        for item in raw:
            try:
                locations.append(Location(**item))
            except (ValidationError, TypeError) as exc:
                logger.debug("[assembler] location skipped: %s — %s", item, exc)
        return locations

    @staticmethod
    def _normalize_name(name: Optional[str]) -> str:
        # Strip punctuation (so "(name not found)" doesn't leave a stray
        # "found)" token) before collapsing whitespace and lowercasing.
        cleaned = re.sub(r"[^\w\s'-]", " ", (name or "").lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _surname(normalized_name: str) -> str:
        parts = normalized_name.split()
        return parts[-1] if parts else ""

    def _find_programmatic_match(
        self, name: Optional[str], programmatic_list: list[dict]
    ) -> Optional[dict]:
        """
        Find the programmatic (Apollo/PDL) contact matching `name`: exact
        normalized-name match first, then a surname-only fallback, both
        case-insensitive. A T-019 grounding placeholder (e.g. "Chief
        Nothing (name not found)") never matches anything — checked against
        the raw name before punctuation stripping, since PLACEHOLDER_NAME_
        MARKER itself contains parentheses. This guard is load-bearing: the
        placeholder's last word before the closing paren ("found") could
        otherwise collide with a real contact's surname under the
        punctuation-stripped surname fallback. Stamping verified=True on an
        LLM-invented name would be exactly the false signal `verified`
        exists to prevent.
        """
        if PLACEHOLDER_NAME_MARKER in (name or "").strip().lower():
            return None

        normalized = self._normalize_name(name)
        if not normalized:
            return None

        for contact in programmatic_list:
            if self._normalize_name(contact.get("name")) == normalized:
                return contact

        surname = self._surname(normalized)
        if not surname:
            return None
        for contact in programmatic_list:
            contact_normalized = self._normalize_name(contact.get("name"))
            if contact_normalized and self._surname(contact_normalized) == surname:
                return contact
        return None

    def _merge_stakeholder_sources(self, llm_list: list[dict], programmatic_list: list[dict]) -> list[dict]:
        """
        Match each LLM-derived stakeholder against `programmatic_list` by
        normalized name (see `_find_programmatic_match`). A match sets
        verified=True and overrides title/linkedin_url/email/location from
        the structured contact — the contact is the current-employment
        signal `verified` represents, so it wins on those fields; the LLM
        keeps role_type/notes/influence_score, which the contact search
        alone can't judge. Programmatic contacts nobody in the LLM list
        matched are still appended as their own verified=True stakeholders
        (the safety net the original design had) rather than dropped.
        """
        merged: list[dict] = []
        matched_contact_ids: set[int] = set()

        for item in llm_list:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            match = self._find_programmatic_match(item.get("name"), programmatic_list)
            if match is not None:
                item["verified"] = True
                for field in ("title", "linkedin_url", "email", "location"):
                    if match.get(field) is not None:
                        item[field] = match[field]
                matched_contact_ids.add(id(match))
            merged.append(item)

        for contact in programmatic_list:
            if id(contact) in matched_contact_ids:
                continue
            new_stakeholder = dict(contact)
            new_stakeholder["verified"] = True
            merged.append(new_stakeholder)

        return merged

    def _build_stakeholders(self, raw: list, max_stakeholders: int = 25) -> list[Stakeholder]:
        stakeholders = []
        for item in raw:
            try:
                # Clamp influence_score to [0, 1]
                if "influence_score" in item:
                    item["influence_score"] = max(0.0, min(1.0, float(item["influence_score"])))
                stakeholders.append(Stakeholder(**item))
            except (ValidationError, TypeError, ValueError) as exc:
                logger.debug("[assembler] stakeholder skipped: %s — %s", item, exc)
        # Sort by influence descending
        return sorted(stakeholders, key=lambda s: s.influence_score, reverse=True)[:max_stakeholders]

    def _build_economics(
        self, raw: dict, edgar: Optional[EdgarFinancials]
    ) -> Economics:
        fin_raw  = raw.get("financials", {})
        segs_raw = raw.get("revenue_segments", [])
        semi_raw = raw.get("semiconductor_profile", {})

        # EDGAR overrides LLM for financial figures (audited > estimated)
        if edgar:
            def _to_m(v: Optional[float]) -> Optional[float]:
                return round(v / 1_000_000, 1) if v else None

            fin_raw["revenue_usd_m"]          = _to_m(edgar.revenue_usd) or fin_raw.get("revenue_usd_m")
            fin_raw["net_income_usd_m"]        = _to_m(edgar.net_income_usd) or fin_raw.get("net_income_usd_m")
            fin_raw["rd_spend_usd_m"]          = _to_m(edgar.rd_expense_usd) or fin_raw.get("rd_spend_usd_m")
            fin_raw["capex_usd_m"]             = _to_m(edgar.capex_usd) or fin_raw.get("capex_usd_m")
            fin_raw["fiscal_year"]             = edgar.fiscal_year or fin_raw.get("fiscal_year")

            # T-018 split conversion into two independent rates: the
            # period-average rate for flow items (revenue, net income, R&D,
            # capex) and the period-end closing rate for total assets.
            # Either can be None on its own — name only the rate(s) that
            # actually exist, and say which figures each one produced, so a
            # reader can recover the original figure from the right rate.
            if edgar.fx_rate_avg_to_usd is not None or edgar.fx_rate_to_usd is not None:
                fin_raw["data_quality"] = "estimated"
                sentences = []
                if edgar.fx_rate_avg_to_usd is not None:
                    sentences.append(
                        f"Revenue, net income, R&D and capex converted from "
                        f"{edgar.reporting_currency} at the period-average rate "
                        f"{edgar.fx_rate_avg_to_usd} "
                        f"({edgar.fx_rate_avg_start}..{edgar.fx_rate_avg_end}, "
                        f"source: {edgar.fx_rate_avg_source})"
                    )
                if edgar.fx_rate_to_usd is not None:
                    sentences.append(
                        f"Total assets converted from {edgar.reporting_currency} "
                        f"at the period-end closing rate {edgar.fx_rate_to_usd} "
                        f"({edgar.fx_rate_date}, source: {edgar.fx_rate_source})"
                    )
                fin_raw["notes"] = ". ".join(sentences) + "."
            else:
                fin_raw["data_quality"] = "audited"

            # Compute gross margin if we have the pieces
            if edgar.gross_profit_usd and edgar.revenue_usd and edgar.revenue_usd > 0:
                fin_raw["gross_margin_pct"] = round(
                    edgar.gross_profit_usd / edgar.revenue_usd * 100, 1
                )
            # Compute R&D as % of revenue
            if edgar.rd_expense_usd and edgar.revenue_usd and edgar.revenue_usd > 0:
                fin_raw["rd_pct_revenue"] = round(
                    edgar.rd_expense_usd / edgar.revenue_usd * 100, 1
                )

        try:
            financials = Financials(**fin_raw)
        except (ValidationError, TypeError) as exc:
            logger.warning("[assembler] financials validation error: %s", exc)
            financials = Financials()

        segments = []
        for seg in segs_raw:
            try:
                segments.append(RevenueSegment(**seg))
            except (ValidationError, TypeError) as exc:
                logger.debug("[assembler] segment skipped: %s", exc)

        try:
            semi_profile = SemiconductorProfile(**semi_raw)
        except (ValidationError, TypeError) as exc:
            logger.warning("[assembler] semi_profile validation error: %s", exc)
            semi_profile = SemiconductorProfile()

        return Economics(
            financials=financials,
            revenue_segments=segments,
            semiconductor_profile=semi_profile,
        )

    def _build_account_intelligence(self, raw: dict) -> AccountIntelligence:
        signals = []
        for sig in raw.get("opportunity_signals", []):
            try:
                signals.append(OpportunitySignal(**sig))
            except (ValidationError, TypeError) as exc:
                logger.debug("[assembler] signal skipped: %s", exc)

        return AccountIntelligence(
            opportunity_signals=signals,
            recommended_next_action=raw.get("recommended_next_action"),
            current_supplier_status=raw.get("current_supplier_status", "no_relationship"),
        )

    def _build_news(self, raw: list) -> list[NewsItem]:
        news = []
        for item in raw:
            try:
                news.append(NewsItem(**item))
            except (ValidationError, TypeError) as exc:
                logger.debug("[assembler] news item skipped: %s", exc)
        return sorted(news, key=lambda n: n.date, reverse=True)[:10]

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    def _score_confidence(
        self,
        overview:      Overview,
        locations:     list[Location],
        stakeholders:  list[Stakeholder],
        economics:     Economics,
        edgar:         Optional[EdgarFinancials],
    ) -> ConfidenceMap:
        """
        Simple heuristic: high if key fields are populated from reliable
        sources; medium if partially populated; low if mostly empty.
        """
        def _overview_conf() -> Confidence:
            filled = sum([
                bool(overview.founded_year),
                bool(overview.employee_count),
                bool(overview.ticker),
                bool(overview.summary),
                len(overview.industry_verticals) > 0,
            ])
            return Confidence.HIGH if filled >= 4 else Confidence.MEDIUM if filled >= 2 else Confidence.LOW

        def _econ_conf() -> Confidence:
            if edgar:
                return Confidence.HIGH
            f = economics.financials
            filled = sum([
                f.revenue_usd_m is not None,
                f.gross_margin_pct is not None,
                f.rd_spend_usd_m is not None,
            ])
            return Confidence.MEDIUM if filled >= 2 else Confidence.LOW

        ov_conf  = _overview_conf()
        eco_conf = _econ_conf()
        loc_conf = Confidence.HIGH if len(locations) >= 2 else Confidence.MEDIUM if locations else Confidence.LOW
        stk_conf = Confidence.HIGH if len(stakeholders) >= 3 else Confidence.MEDIUM if stakeholders else Confidence.LOW

        # Overall = weakest section
        all_confs = [ov_conf, eco_conf, loc_conf, stk_conf]
        if Confidence.LOW in all_confs:
            overall = Confidence.LOW
        elif all_confs.count(Confidence.HIGH) >= 3:
            overall = Confidence.HIGH
        else:
            overall = Confidence.MEDIUM

        return ConfidenceMap(
            overall=overall,
            overview=ov_conf,
            locations=loc_conf,
            stakeholders=stk_conf,
            economics=eco_conf,
        )