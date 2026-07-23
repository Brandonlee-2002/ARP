"""
SEC EDGAR extractor.

For US-listed public companies: fetches the most recent 10-K (domestic)
or 20-F (foreign private issuer) and extracts the key financial line items
we need to populate the Economics section.

No API key required — EDGAR has a free public REST API.
Rate limit: 10 requests/second per EDGAR's fair-use policy.

Uses truststore (not the bundled certifi CA list) for TLS verification —
see the note above self._client below for why.
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import truststore

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://data.sec.gov"

# EDGAR requires a real name and contact email in the User-Agent header
# as part of their fair-use policy: https://www.sec.gov/developer
# Replace the values below with your own before running.
EDGAR_USER_AGENT = "AccountResearchPipeline your-email@example.com"
HEADERS = {"User-Agent": EDGAR_USER_AGENT}


@dataclass
class EdgarFinancials:
    """Raw financial figures pulled from EDGAR XBRL facts."""
    ticker:               str
    cik:                  Optional[str]   = None
    fiscal_year:          Optional[int]   = None
    form_type:            Optional[str]   = None
    filed_date:           Optional[str]   = None
    revenue_usd:          Optional[float] = None   # in USD (not millions)
    gross_profit_usd:     Optional[float] = None
    operating_income_usd: Optional[float] = None
    net_income_usd:       Optional[float] = None
    rd_expense_usd:       Optional[float] = None
    capex_usd:            Optional[float] = None
    total_assets_usd:     Optional[float] = None
    data_quality:         str             = "audited"
    # SEC's own structured filer classification — NOT a financial figure,
    # but bundled here since it comes from the same free CIK-based lookup
    # path and there's no other natural home for it. Used to give the
    # segment SAM vertical classification an audited anchor fact instead
    # of relying purely on web search + LLM judgment. See
    # segment_benchmarks.py's SIC_VERTICAL_HINTS for how this gets used —
    # SIC codes are coarser than our vertical taxonomy (e.g. one code
    # covers both EV and ICE automakers), so this informs the LLM's
    # classification call, it doesn't override it.
    sic:                  Optional[str]   = None
    sic_description:      Optional[str]   = None


class EdgarExtractor:
    """
    Pulls financial data from SEC EDGAR for any company with a US CIK.

    Usage:
        extractor = EdgarExtractor()
        data = extractor.fetch(ticker="INTC")
    """

    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        # verify=truststore's SSLContext uses the OS certificate store
        # (Windows CryptoAPI, macOS Keychain, etc.) instead of httpx's
        # bundled certifi list. Without this, any machine behind corporate
        # TLS inspection (Zscaler, Palo Alto, etc.) gets a
        # CERTIFICATE_VERIFY_FAILED / "self-signed certificate in
        # certificate chain" error — confirmed on a real corporate network,
        # not theoretical. httpx's own docs document this exact pattern:
        # https://www.python-httpx.org/advanced/ssl/
        self._client = httpx.Client(
            headers=HEADERS,
            timeout=timeout,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    def fetch(self, ticker: str) -> Optional[EdgarFinancials]:
        """
        Main entry point. Returns None if the company isn't found on EDGAR
        (e.g. it's a foreign private issuer not cross-listed on US exchanges,
        or it's private).
        """
        try:
            cik = self._resolve_cik(ticker)
            if not cik:
                logger.info("[edgar] No CIK found for %s — skipping", ticker)
                return None

            facts = self._fetch_facts(cik)
            if not facts:
                return None

            result = self._extract_financials(ticker, cik, facts)

            # SIC classification — best-effort. A failure here should never
            # take down the financials fetch; it's a nice-to-have anchor
            # fact for segment SAM classification, not a required field.
            try:
                submissions = self._fetch_submissions(cik)
                if submissions:
                    result.sic = submissions.get("sic")
                    result.sic_description = submissions.get("sicDescription")
            except Exception as exc:
                logger.debug("[edgar] SIC lookup failed for %s: %s", ticker, exc)

            logger.info("[edgar] Fetched financials for %s (CIK %s)", ticker, cik)
            return result

        except Exception as exc:
            logger.warning("[edgar] Failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # CIK resolution
    # ------------------------------------------------------------------

    def _resolve_cik(self, ticker: str) -> Optional[str]:
        """
        EDGAR's company tickers endpoint maps ticker → CIK.
        Returns zero-padded 10-digit CIK string.
        """
        time.sleep(0.1)   # stay under 10 req/s
        resp = self._client.get(f"{EDGAR_BASE}/files/company_tickers.json")
        resp.raise_for_status()
        data = resp.json()

        ticker_upper = ticker.upper()
        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker_upper:
                return str(entry["cik_str"]).zfill(10)
        return None

    # ------------------------------------------------------------------
    # XBRL facts
    # ------------------------------------------------------------------

    def _fetch_facts(self, cik: str) -> Optional[dict]:
        """
        Downloads the full XBRL facts JSON for a given CIK.
        This is a ~2-10MB payload — we parse only what we need.
        """
        time.sleep(0.1)
        url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        resp = self._client.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _fetch_submissions(self, cik: str) -> Optional[dict]:
        """
        Downloads the filer submissions/metadata JSON for a given CIK.
        Used here only for its `sic` / `sicDescription` fields — SEC's own
        structured, audited industry classification, e.g. Apple returns
        sic="3571", sicDescription="Electronic Computers". This is a much
        smaller payload than companyfacts (metadata + filing history, not
        full financial data), but we still only need two fields out of it.
        """
        time.sleep(0.1)
        url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
        resp = self._client.get(url, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_financials(
        self, ticker: str, cik: str, facts: dict
    ) -> EdgarFinancials:
        result = EdgarFinancials(ticker=ticker, cik=cik)

        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        ifrs    = facts.get("facts", {}).get("ifrs-full", {})   # for foreign filers

        # Prefer US GAAP; fall back to IFRS
        concepts = us_gaap if us_gaap else ifrs

        result.revenue_usd          = self._latest_annual(concepts, [
            "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet", "RevenueFromSaleOfGoods",
        ])
        result.gross_profit_usd     = self._latest_annual(concepts, ["GrossProfit"])
        result.operating_income_usd = self._latest_annual(concepts, [
            "OperatingIncomeLoss", "ProfitLossFromOperatingActivities",
        ])
        result.net_income_usd       = self._latest_annual(concepts, [
            "NetIncomeLoss", "ProfitLoss",
        ])
        result.rd_expense_usd       = self._latest_annual(concepts, [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        ])
        result.capex_usd            = self._latest_annual(concepts, [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PurchaseOfPropertyPlantAndEquipment",
        ])
        result.total_assets_usd     = self._latest_annual(concepts, ["Assets"])

        # Infer fiscal year from the revenue filing date
        revenue_entry = self._latest_annual_entry(concepts, [
            "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
        ])
        if revenue_entry:
            result.fiscal_year = int(revenue_entry.get("end", "0000")[:4])
            result.filed_date  = revenue_entry.get("filed")
            result.form_type   = revenue_entry.get("form")

        return result

    def _latest_annual(self, concepts: dict, keys: list[str]) -> Optional[float]:
        entry = self._latest_annual_entry(concepts, keys)
        return float(entry["val"]) if entry else None

    def _latest_annual_entry(self, concepts: dict, keys: list[str]) -> Optional[dict]:
        """
        Finds the most recent annual (10-K or 20-F) filing entry
        for any of the given XBRL concept keys.
        """
        for key in keys:
            concept = concepts.get(key, {})
            units = concept.get("units", {})
            usd_entries = units.get("USD", [])

            # Filter to annual filings only (form 10-K or 20-F)
            annual = [
                e for e in usd_entries
                if e.get("form") in ("10-K", "20-F")
                and e.get("filed")
            ]
            if annual:
                # Most recently filed
                annual.sort(key=lambda e: e["filed"], reverse=True)
                return annual[0]
        return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()