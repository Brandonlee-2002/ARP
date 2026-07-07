"""
Record assembler.

Takes the raw dicts returned by LLMEnricher and EDGAR, validates them
against the Pydantic schema, and produces a fully typed AccountResearchRecord.

Handles partial failures gracefully: if the LLM returns a malformed
stakeholders block, we log a warning and continue with an empty list.
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
    EdgarFinancials,    # re-export for type hint
    FinancialDiscrepancy,
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
        # Stakeholders
        # ------------------------------------------------------------------
        stakeholders = self._build_stakeholders(enriched.get("stakeholders", []))

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
        # ------------------------------------------------------------------
        # Verification
        # ------------------------------------------------------------------
        financial_discrepancies = self._check_financial_discrepancies(
            economics, edgar_data
        )
        citation_warnings = self._audit_signal_citations(account_intelligence)

        if financial_discrepancies:
            logger.warning(
                "[assembler] %d financial discrepanc%s vs EDGAR: %s",
                len(financial_discrepancies),
                "ies" if len(financial_discrepancies) > 1 else "y",
                ", ".join(
                    f"{d.field} {d.pct_diff}% off"
                    for d in financial_discrepancies
                ),
            )
        if citation_warnings:
            logger.warning(
                "[assembler] %d signal%s %s no source URL — treat with caution",
                citation_warnings,
                "s" if citation_warnings > 1 else "",
                "have" if citation_warnings > 1 else "has",
            )

        meta = Meta(
            refreshed_at=now,
            refresh_cadence=refresh_cadence,
            confidence=confidence,
            sources=sources,
            financial_discrepancies=financial_discrepancies,
            citation_warnings=citation_warnings,
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

    def _build_stakeholders(self, raw: list) -> list[Stakeholder]:
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
        return sorted(stakeholders, key=lambda s: s.influence_score, reverse=True)

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
    # Verification methods
    # ------------------------------------------------------------------

    _DISCREPANCY_THRESHOLD = 0.20  # flag fields that diverge >20% from EDGAR

    def _check_financial_discrepancies(
        self,
        economics:  Economics,
        edgar:      Optional[EdgarFinancials],
    ) -> list[FinancialDiscrepancy]:
        """
        Compare LLM financial estimates against EDGAR audited figures.
        Returns a list of fields that diverge by more than _DISCREPANCY_THRESHOLD.
        Only meaningful for public companies where EDGAR data was fetched AND
        the LLM returned its own estimates (data_quality != "audited").
        """
        if not edgar:
            return []

        def _to_m(v: Optional[float]) -> Optional[float]:
            return round(v / 1_000_000, 1) if v else None

        edgar_figures = {
            "Revenue":    _to_m(edgar.revenue_usd),
            "R&D spend":  _to_m(edgar.rd_expense_usd),
            "Capex":      _to_m(edgar.capex_usd),
            "Net income": _to_m(edgar.net_income_usd),
        }
        llm_figures = {
            "Revenue":    economics.financials.revenue_usd_m,
            "R&D spend":  economics.financials.rd_spend_usd_m,
            "Capex":      economics.financials.capex_usd_m,
            "Net income": economics.financials.net_income_usd_m,
        }

        discrepancies = []
        for field, edgar_val in edgar_figures.items():
            llm_val = llm_figures.get(field)
            if llm_val is None or edgar_val is None or edgar_val == 0:
                continue
            delta = abs(llm_val - edgar_val) / abs(edgar_val)
            if delta > self._DISCREPANCY_THRESHOLD:
                discrepancies.append(FinancialDiscrepancy(
                    field=field,
                    llm_m=llm_val,
                    edgar_m=edgar_val,
                    pct_diff=round(delta * 100),
                ))
        return discrepancies

    def _audit_signal_citations(
        self,
        account_intelligence: AccountIntelligence,
    ) -> int:
        """
        Mark each OpportunitySignal as cited or uncited based on whether it
        has a non-empty source URL. Mutates the signals in place and returns
        the count of uncited signals.
        """
        uncited = 0
        for signal in account_intelligence.opportunity_signals:
            is_cited = bool(signal.source and signal.source.strip())
            # Pydantic models are immutable by default; use model_copy
            try:
                signal.__dict__["cited"] = is_cited
            except Exception:
                pass  # if mutation fails, cited stays False (schema default)
            if not is_cited:
                uncited += 1
        return uncited

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