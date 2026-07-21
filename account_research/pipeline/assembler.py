"""
Record assembler.

Takes the raw dicts returned by LLMEnricher, EDGAR, and (optionally) People
Data Labs, validates them against the Pydantic schema, and produces a fully
typed AccountResearchRecord.

Handles partial failures gracefully: if the LLM returns a malformed
stakeholders block, we log a warning and continue with an empty list.

Note on imports: EdgarFinancials is a dataclass defined in extractors/edgar.py
— it is NOT defined in models/schema.py. A previous version of this file
imported it from both places, and the first (wrong) import raised an
ImportError before ever reaching the correct one, breaking every run. Do not
reintroduce that import from models.schema.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from pydantic import ValidationError

from ..models.schema import (
    AccountResearchRecord,
    AccountIntelligence,
    Confidence,
    ConfidenceMap,
    Economics,
    Financials,
    Location,
    Meta,
    NewsItem,
    OpportunitySignal,
    Overview,
    RevenueSegment,
    SegmentSAMEstimate,
    SemiconductorProfile,
    SourceRecord,
    Stakeholder,
)
from ..extractors.edgar import EdgarFinancials
from ..extractors.people_search import PersonProfile

logger = logging.getLogger(__name__)

# Seniority levels PDL is asked to search for (mirrors people_search.py's
# RELEVANT_LEVELS) — used only to pick a sensible default role/influence
# for a verified contact the LLM never surfaced. Not used for filtering;
# PeopleSearchExtractor already restricts results before they reach here.
_PDL_SAFETY_NET_ROLE = "influencer"
_PDL_SAFETY_NET_INFLUENCE = 0.4


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
        pdl_contacts: Optional[list[PersonProfile]] = None,
    ) -> AccountResearchRecord:

        now = datetime.utcnow()
        pdl_contacts = pdl_contacts or []

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

        # meta.sources gets a People Data Labs entry whenever that stage ran,
        # regardless of whether it actually found any contacts — this
        # records that the lookup happened, not just that it succeeded.
        if pdl_contacts:
            sources.append(SourceRecord(
                name="People Data Labs",
                url="https://www.peopledatalabs.com",
                fetched_at=now,
                fields_populated=["stakeholders"],
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
        # Stakeholders — merges LLM output with verified PDL contacts
        # ------------------------------------------------------------------
        stakeholders = self._build_stakeholders(enriched.get("stakeholders", []), pdl_contacts)

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

    def _build_stakeholders(
        self,
        raw: list,
        pdl_contacts: Optional[list[PersonProfile]] = None,
    ) -> list[Stakeholder]:
        pdl_contacts = pdl_contacts or []

        stakeholders = []
        for item in raw:
            try:
                # Clamp influence_score to [0, 1]
                if "influence_score" in item:
                    item["influence_score"] = max(0.0, min(1.0, float(item["influence_score"])))
                item.setdefault("verified", False)
                stakeholders.append(Stakeholder(**item))
            except (ValidationError, TypeError, ValueError) as exc:
                logger.debug("[assembler] stakeholder skipped: %s — %s", item, exc)

        # ------------------------------------------------------------------
        # Merge with PDL — structured source overrides LLM inference,
        # following the pipeline's usual "structured > LLM estimate > null"
        # rule. Matching is by normalized name; the LLM's own say-so about
        # who's real is never sufficient to set verified=True.
        # ------------------------------------------------------------------
        matched_pdl_indices: set[int] = set()

        for s in stakeholders:
            match_idx = self._match_pdl_contact(s.name, pdl_contacts, matched_pdl_indices)
            if match_idx is None:
                continue
            contact = pdl_contacts[match_idx]
            matched_pdl_indices.add(match_idx)
            s.verified = True
            # Structured contact info overrides LLM-inferred (which was
            # null/None here in practice — the LLM prompt never asks it to
            # guess a real email or LinkedIn URL).
            if contact.linkedin_url:
                s.linkedin_url = contact.linkedin_url
            if contact.emails:
                s.email = contact.emails[0]
            if contact.location and not s.location:
                s.location = contact.location
            if contact.department and not s.department:
                s.department = contact.department

        # Safety net: any verified PDL contact the LLM completely failed to
        # surface still gets included, rather than silently dropped, with a
        # conservative default role/influence rather than a guessed one.
        for i, contact in enumerate(pdl_contacts):
            if i in matched_pdl_indices or not contact.name:
                continue
            try:
                stakeholders.append(Stakeholder(
                    name=contact.name,
                    title=contact.title or "Unknown title",
                    department=contact.department,
                    location=contact.location,
                    role_type=_PDL_SAFETY_NET_ROLE,
                    influence_score=_PDL_SAFETY_NET_INFLUENCE,
                    linkedin_url=contact.linkedin_url,
                    email=contact.emails[0] if contact.emails else None,
                    notes="Added from People Data Labs — not surfaced by LLM extraction.",
                    verified=True,
                ))
            except (ValidationError, TypeError, ValueError) as exc:
                logger.debug("[assembler] PDL safety-net contact skipped: %s — %s", contact, exc)

        # Sort by influence descending (unchanged behavior)
        return sorted(stakeholders, key=lambda s: s.influence_score, reverse=True)

    @staticmethod
    def _match_pdl_contact(
        llm_name: str,
        pdl_contacts: list[PersonProfile],
        already_matched: set[int],
    ) -> Optional[int]:
        """
        Matches an LLM-extracted stakeholder name against the PDL contact
        list. Deliberately simple: normalized full-name equality first,
        then a last-token (surname) fallback for minor formatting
        differences (middle initials, nicknames in parens, etc.). Returns
        the matching index in pdl_contacts, or None.
        """
        def _norm(s: str) -> str:
            return " ".join((s or "").lower().replace(".", "").split())

        llm_norm = _norm(llm_name)
        if not llm_norm or "(name not found)" in llm_norm:
            return None

        # Pass 1: exact normalized match
        for i, contact in enumerate(pdl_contacts):
            if i in already_matched or not contact.name:
                continue
            if _norm(contact.name) == llm_norm:
                return i

        # Pass 2: surname match (last whitespace-separated token)
        llm_tokens = llm_norm.split()
        if not llm_tokens:
            return None
        llm_surname = llm_tokens[-1]
        for i, contact in enumerate(pdl_contacts):
            if i in already_matched or not contact.name:
                continue
            contact_tokens = _norm(contact.name).split()
            if contact_tokens and contact_tokens[-1] == llm_surname:
                return i

        return None

    def _build_economics(
        self, raw: dict, edgar: Optional[EdgarFinancials]
    ) -> Economics:
        fin_raw         = raw.get("financials", {})
        segs_raw        = raw.get("revenue_segments", [])
        semi_raw        = raw.get("semiconductor_profile", {})
        segment_sam_raw = raw.get("segment_sam_estimates", [])

        # EDGAR overrides LLM for financial figures (audited > estimated)
        if edgar:
            def _to_m(v: Optional[float]) -> Optional[float]:
                return round(v / 1_000_000, 1) if v else None

            fin_raw["revenue_usd_m"]          = _to_m(edgar.revenue_usd) or fin_raw.get("revenue_usd_m")
            fin_raw["net_income_usd_m"]        = _to_m(edgar.net_income_usd) or fin_raw.get("net_income_usd_m")
            fin_raw["rd_spend_usd_m"]          = _to_m(edgar.rd_expense_usd) or fin_raw.get("rd_spend_usd_m")
            fin_raw["capex_usd_m"]             = _to_m(edgar.capex_usd) or fin_raw.get("capex_usd_m")
            fin_raw["fiscal_year"]             = edgar.fiscal_year or fin_raw.get("fiscal_year")
            fin_raw["data_quality"]            = "audited"

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

        # Segment SAM estimates — never let a validation failure here take
        # down the rest of economics; a missing estimate is normal (means
        # "needs research"), so we log at debug, not warning.
        segment_sam_estimates = []
        for item in segment_sam_raw:
            try:
                segment_sam_estimates.append(SegmentSAMEstimate(**item))
            except (ValidationError, TypeError) as exc:
                logger.debug("[assembler] segment_sam_estimate skipped: %s — %s", item, exc)

        return Economics(
            financials=financials,
            revenue_segments=segments,
            semiconductor_profile=semi_profile,
            segment_sam_estimates=segment_sam_estimates,
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

        Note: segment_sam_estimates deliberately does NOT factor into the
        economics confidence score below — those estimates are benchmark-
        derived, not disclosed, and mixing them into the audited-financials
        confidence tier would overstate how well-founded the overall
        economics section is.
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

        def _stakeholder_conf() -> Confidence:
            if not stakeholders:
                return Confidence.LOW
            verified_count = sum(1 for s in stakeholders if s.verified)
            # Reaches HIGH on either 3+ total OR 2+ verified — a smaller
            # set of PDL-confirmed contacts is at least as trustworthy as
            # a larger set of purely LLM-inferred ones.
            if len(stakeholders) >= 3 or verified_count >= 2:
                return Confidence.HIGH
            return Confidence.MEDIUM

        ov_conf  = _overview_conf()
        eco_conf = _econ_conf()
        loc_conf = Confidence.HIGH if len(locations) >= 2 else Confidence.MEDIUM if locations else Confidence.LOW
        stk_conf = _stakeholder_conf()

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