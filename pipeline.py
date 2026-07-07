"""
Account research pipeline — main orchestrator.

Entry point:
    pipeline = AccountResearchPipeline.from_env()
    record   = pipeline.run("Intel Corporation", domain="intel.com", ticker="INTC")
    pipeline.save(record, output_dir="./output")

Environment variables:
    ANTHROPIC_API_KEY   — required
    SEARCH_API_KEY      — required (for web search)
    SEARCH_PROVIDER     — "serper" | "brave" | "serpapi"  (default: serper)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic

from ..extractors.edgar import EdgarExtractor
from ..extractors.llm_enricher import LLMEnricher
from ..extractors.web_search import WebSearchExtractor
from ..models.schema import AccountResearchRecord
from .assembler import RecordAssembler

logger = logging.getLogger(__name__)


class AccountResearchPipeline:
    """
    Orchestrates the full research pipeline:

        1. Web search  — broad multi-query scrape across all topics
        2. EDGAR       — audited financials for public companies
        3. LLM enrich  — section-by-section structured extraction
        4. Assemble    — validate, merge, score confidence
        5. Save        — write JSON + summary to disk

    All stages are independently fault-tolerant.
    """

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        search_api_key:   str,
        search_provider:  str = "serper",
    ):
        self._llm       = LLMEnricher(client=anthropic_client)
        self._web       = WebSearchExtractor(
            api_key=search_api_key,
            provider=search_provider,
        )
        self._edgar     = EdgarExtractor()
        self._assembler = RecordAssembler()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "AccountResearchPipeline":
        """Create pipeline from environment variables."""
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        search_key    = os.environ.get("SEARCH_API_KEY")
        provider      = os.environ.get("SEARCH_PROVIDER", "serper")

        if not anthropic_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable not set")
        if not search_key:
            raise ValueError("SEARCH_API_KEY environment variable not set")

        return cls(
            anthropic_client=anthropic.Anthropic(api_key=anthropic_key),
            search_api_key=search_key,
            search_provider=provider,
        )

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(
        self,
        company_name:     str,
        domain:           str,
        ticker:           Optional[str] = None,
        refresh_cadence:  str = "weekly",
    ) -> AccountResearchRecord:
        """
        Run the full pipeline for one company.

        Args:
            company_name:    Human-readable name, e.g. "Intel Corporation"
            domain:          Company website domain, e.g. "intel.com"
            ticker:          Stock ticker for EDGAR lookup, e.g. "INTC" (optional)
            refresh_cadence: How often this record should be refreshed

        Returns:
            Fully validated AccountResearchRecord
        """
        logger.info("=" * 60)
        logger.info("Starting research pipeline for: %s", company_name)
        logger.info("=" * 60)

        # ------------------------------------------------------------------
        # Stage 1: Web search
        # ------------------------------------------------------------------
        logger.info("[Stage 1/4] Web search...")
        search_results = self._web.fetch_all(company_name, domain)
        logger.info("  → %d search snippets collected", len(search_results))

        search_sources = self._build_source_records(search_results)

        # ------------------------------------------------------------------
        # Stage 2: EDGAR (public companies only)
        # ------------------------------------------------------------------
        edgar_data = None
        if ticker:
            logger.info("[Stage 2/4] EDGAR lookup for ticker %s...", ticker)
            edgar_data = self._edgar.fetch(ticker)
            if edgar_data:
                logger.info(
                    "  → FY%s data retrieved (revenue: $%.1fM)",
                    edgar_data.fiscal_year,
                    (edgar_data.revenue_usd or 0) / 1_000_000,
                )
            else:
                logger.info("  → No EDGAR data found (private company or foreign issuer)")
        else:
            logger.info("[Stage 2/4] Skipping EDGAR (no ticker provided)")

        # ------------------------------------------------------------------
        # Stage 3: LLM enrichment
        # ------------------------------------------------------------------
        logger.info("[Stage 3/4] LLM enrichment (6 sections)...")
        enriched = self._llm.enrich(
            company_name=company_name,
            domain=domain,
            search_results=search_results,
            edgar_data=edgar_data,
        )

        # ------------------------------------------------------------------
        # Stage 4: Assemble and validate
        # ------------------------------------------------------------------
        logger.info("[Stage 4/4] Assembling and validating record...")
        record = self._assembler.assemble(
            company_name=company_name,
            domain=domain,
            enriched=enriched,
            edgar_data=edgar_data,
            search_sources=search_sources,
            refresh_cadence=refresh_cadence,
        )

        logger.info("Pipeline complete.")
        logger.info(
            "  Confidence: %s | Locations: %d | Stakeholders: %d | News items: %d",
            record.meta.confidence.overall,
            len(record.locations),
            len(record.stakeholders),
            len(record.news),
        )

        return record

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def save(
        self,
        record: AccountResearchRecord,
        output_dir: str = "./output",
        formats: list[str] = None,
    ) -> dict[str, Path]:
        """
        Save the record to disk.

        formats: list of "json" | "summary"  (default: both)

        Returns dict mapping format → file path.
        """
        if formats is None:
            formats = ["json", "summary"]

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        slug      = record.overview.legal_name.lower().replace(" ", "_")[:40]
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        base_name = f"{slug}_{timestamp}"

        saved: dict[str, Path] = {}

        if "json" in formats:
            json_path = output_path / f"{base_name}.json"
            json_path.write_text(
                record.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )
            saved["json"] = json_path
            logger.info("Saved JSON → %s", json_path)

        if "summary" in formats:
            summary_path = output_path / f"{base_name}_summary.md"
            summary_path.write_text(
                self._render_summary(record),
                encoding="utf-8",
            )
            saved["summary"] = summary_path
            logger.info("Saved summary → %s", summary_path)

        return saved

    # ------------------------------------------------------------------
    # Summary renderer
    # ------------------------------------------------------------------

    def _render_summary(self, r: AccountResearchRecord) -> str:
        ov = r.overview
        ec = r.economics.financials
        semi = r.economics.semiconductor_profile

        def _fmt_m(v: Optional[float]) -> str:
            return f"${v:,.0f}M" if v else "N/A"

        stakeholder_lines = "\n".join(
            f"  - **{s.name}** ({s.title}) — {s.role_type} | influence: {s.influence_score:.0%}"
            for s in r.stakeholders[:6]
        )

        location_lines = "\n".join(
            f"  - [{loc.type.upper()}] {loc.city}, {loc.country}"
            + (f" — {loc.name}" if loc.name else "")
            + (f" ({loc.status})" if loc.status != "active" else "")
            for loc in r.locations[:8]
        )

        signal_lines = "\n".join(
            f"  - **[{sig.type.upper()}]** {sig.description} *(urgency: {sig.urgency})*"
            for sig in r.account_intelligence.opportunity_signals
        )

        news_lines = "\n".join(
            f"  - {item.date} — {item.headline} [{item.source_name}]"
            for item in r.news[:5]
        )

        segments = "\n".join(
            f"  - {seg.name}: {_fmt_m(seg.revenue_usd_m)} ({seg.pct_of_total:.0f}%)"
            + (" ✓ relevant" if seg.relevant_to_us else "")
            for seg in r.economics.revenue_segments
        )

        return f"""# Account research: {ov.legal_name}
*Generated: {r.meta.refreshed_at.strftime("%Y-%m-%d %H:%M UTC")} | Confidence: {r.meta.confidence.overall} | Schema v{r.meta.schema_version}*

---

## Overview
- **Legal name:** {ov.legal_name}
- **Ticker:** {f"{ov.ticker.symbol}:{ov.ticker.exchange}" if ov.ticker else "Private"}
- **Founded:** {ov.founded_year or "N/A"}
- **Employees:** {f"~{ov.employee_count.value:,}" if ov.employee_count else "N/A"}
- **Ownership:** {ov.ownership_type}

{ov.summary}

---

## Locations
{location_lines or "  None identified"}

---

## Stakeholder map
{stakeholder_lines or "  None identified"}

---

## Economics (FY{ec.fiscal_year or "N/A"})
| Metric | Value |
|---|---|
| Revenue | {_fmt_m(ec.revenue_usd_m)} |
| Revenue growth | {f"{ec.revenue_growth_pct:+.1f}%" if ec.revenue_growth_pct else "N/A"} |
| Gross margin | {f"{ec.gross_margin_pct:.1f}%" if ec.gross_margin_pct else "N/A"} |
| R&D spend | {_fmt_m(ec.rd_spend_usd_m)} ({f"{ec.rd_pct_revenue:.1f}% of rev" if ec.rd_pct_revenue else "N/A"}) |
| Capex | {_fmt_m(ec.capex_usd_m)} |
| Data quality | {ec.data_quality} |

### Revenue segments
{segments or "  Not available"}

### Semiconductor profile
- **Buyer type:** {semi.chip_buyer_type or "N/A"}
- **Has own fab:** {"Yes" if semi.has_own_fab else "No"}
- **Fab nodes:** {", ".join(semi.fab_nodes) or "N/A"}
- **Known chip suppliers:** {", ".join(semi.known_chip_suppliers) or "N/A"}
- **Supply gaps:** {", ".join(semi.known_supply_gaps) or "N/A"}
- **Qualification cycle:** {semi.qualification_cycle or "N/A"}

---

## Account intelligence
**Supplier status:** {r.account_intelligence.current_supplier_status}
**Recommended next action:** {r.account_intelligence.recommended_next_action or "N/A"}

{signal_lines or "  No signals identified"}

---

## Recent news
{news_lines or "  No recent news found"}

---

## Verification
{self._render_verification(r)}

---

*Sources: {", ".join(s.name for s in r.meta.sources[:5])}{"..." if len(r.meta.sources) > 5 else ""}*
"""

    # ------------------------------------------------------------------
    # Verification renderer
    # ------------------------------------------------------------------

    def _render_verification(self, r: AccountResearchRecord) -> str:
        """
        Renders a concise verification summary for the Markdown one-pager.
        Shows financial discrepancies vs EDGAR and citation warnings.
        """
        lines = []

        discrepancies = r.meta.financial_discrepancies
        if discrepancies:
            lines.append("**⚠ Financial discrepancies vs EDGAR audited filing:**")
            for d in discrepancies:
                lines.append(
                    f"  - **{d.field}**: LLM estimated ${d.llm_m:,.0f}M vs "
                    f"EDGAR audited ${d.edgar_m:,.0f}M ({d.pct_diff}% off) — "
                    f"use EDGAR figure"
                )
        else:
            lines.append("✓ No financial discrepancies detected vs EDGAR")

        citation_warnings = r.meta.citation_warnings
        total_signals = len(r.account_intelligence.opportunity_signals)
        cited = total_signals - citation_warnings
        if citation_warnings > 0:
            lines.append(
                f"**⚠ Citation check**: {cited}/{total_signals} signals have a "
                f"source URL — {citation_warnings} signal"
                f"{'s are' if citation_warnings > 1 else ' is'} unverified"
            )
        elif total_signals > 0:
            lines.append(f"✓ All {total_signals} signals have source URLs")
        else:
            lines.append("✓ No signals to audit")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_source_records(self, results) -> list[dict]:
        """
        Deduplicate URLs and build source records for the meta section.
        """
        seen: set[str] = set()
        sources = []
        for r in results:
            if r.url and r.url not in seen:
                seen.add(r.url)
                sources.append({
                    "name":             r.title or r.url,
                    "url":              r.url,
                    "fetched_at":       datetime.utcnow(),
                    "fields_populated": [r.topic],
                })
        return sources