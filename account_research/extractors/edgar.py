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
from datetime import date
from typing import Optional

import httpx
import truststore

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://data.sec.gov"

# The ticker-to-CIK map is served from www.sec.gov, not data.sec.gov — only
# the submissions/ and api/xbrl/ endpoints live on data.sec.gov. Confirmed
# live: data.sec.gov/files/company_tickers.json -> 404,
# www.sec.gov/files/company_tickers.json -> 200. See docs/NOTES.md.
SEC_WWW_BASE = "https://www.sec.gov"

# ECB reference rates via the Frankfurter API (api.frankfurter.dev) — no API
# key needed, and it supports historical dates, which a period-end FX
# conversion needs. ECB publishes reference rates for ~30 currencies; a
# currency it doesn't cover (e.g. TWD) 404s and is treated as unconvertible
# rather than guessed at. See docs/NOTES.md "Network and TLS gotchas".
FRANKFURTER_BASE = "https://api.frankfurter.dev/v1"

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
    # Populated only when the filer reports outside USD and a conversion
    # was actually performed (or attempted) — see _extract_financials and
    # _fetch_fx_rate. All four stay None on the USD path, unchanged from
    # pre-T-008 behavior.
    reporting_currency:   Optional[str]   = None
    fx_rate_to_usd:       Optional[float] = None   # period-end closing rate
    fx_rate_date:         Optional[str]   = None
    fx_rate_source:       Optional[str]   = None
    # Period-average rate — used for flow (income-statement) items instead
    # of the closing rate above. See _fetch_fx_rate_average and T-018.
    fx_rate_avg_to_usd:   Optional[float] = None
    fx_rate_avg_start:    Optional[str]   = None
    fx_rate_avg_end:      Optional[str]   = None
    fx_rate_avg_source:   Optional[str]   = None
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
        or it's private) — logged at INFO, since that outcome is expected
        and benign, not a bug.

        Any other failure (a broken ticker-map/companyfacts request, an
        unexpected HTTP status, a malformed payload) is a *hard* failure —
        it means our request is broken, not that this ticker doesn't exist —
        and is logged at ERROR with the URL involved. It still returns None
        rather than raising, so one company's failure never takes down a
        batch run; but ERROR (not WARNING) means it won't go unnoticed the
        way the data.sec.gov 404 previously did.
        """
        ticker_map_url = f"{SEC_WWW_BASE}/files/company_tickers.json"
        try:
            cik = self._resolve_cik(ticker)
        except Exception as exc:
            logger.error(
                "[edgar] Ticker lookup failed for %s at %s: %s",
                ticker, ticker_map_url, exc,
            )
            return None

        if not cik:
            logger.info("[edgar] No CIK found for %s — skipping", ticker)
            return None

        facts_url = f"{EDGAR_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
        try:
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
            logger.error(
                "[edgar] Hard failure fetching financials for %s (CIK %s) at %s: %s",
                ticker, cik, facts_url, exc,
            )
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
        resp = self._client.get(f"{SEC_WWW_BASE}/files/company_tickers.json")
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

        # The revenue lookup also tells us the fiscal period, the filing
        # metadata, and — since T-008 — the currency the filer reports in
        # (USD, or whatever single foreign-currency unit key is present).
        revenue_result = self._latest_annual_entry(concepts, [
            "Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet", "RevenueFromSaleOfGoods",
        ])

        # Raw figures, in whatever currency `revenue_result` was reported in
        # (not yet converted to USD).
        raw_revenue          = None
        currency              = "USD"
        if revenue_result:
            revenue_entry, currency = revenue_result
            raw_revenue = float(revenue_entry["val"])
            result.fiscal_year = int(revenue_entry.get("end", "0000")[:4])
            result.filed_date  = revenue_entry.get("filed")
            result.form_type   = revenue_entry.get("form")

        raw_gross_profit     = self._latest_annual(concepts, ["GrossProfit"])
        raw_operating_income = self._latest_annual(concepts, [
            "OperatingIncomeLoss", "ProfitLossFromOperatingActivities",
        ])
        raw_net_income       = self._latest_annual(concepts, [
            "NetIncomeLoss", "ProfitLoss",
        ])
        raw_rd_expense       = self._latest_annual(concepts, [
            "ResearchAndDevelopmentExpense",
            "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        ])
        raw_capex            = self._latest_annual(concepts, [
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PurchaseOfPropertyPlantAndEquipment",
        ])
        raw_total_assets     = self._latest_annual(concepts, ["Assets"])

        if currency == "USD":
            # Unchanged from pre-T-008 behavior: no conversion, no FX fields.
            result.revenue_usd          = raw_revenue
            result.gross_profit_usd     = raw_gross_profit
            result.operating_income_usd = raw_operating_income
            result.net_income_usd       = raw_net_income
            result.rd_expense_usd       = raw_rd_expense
            result.capex_usd            = raw_capex
            result.total_assets_usd     = raw_total_assets
            return result

        # Foreign filer reporting outside USD (e.g. ASML in EUR). Populate
        # what we know for certain regardless of whether FX conversion
        # succeeds, so the record is never empty just because conversion
        # failed.
        result.reporting_currency = currency
        if not revenue_result:
            return result

        period_end   = revenue_entry.get("end")
        period_start = self._period_start(revenue_entry)

        # Two independent rates for two different kinds of figure:
        #   - closing (period-end)   -> total_assets_usd (a balance-sheet /
        #     stock item, stated as of the period end)
        #   - average (period-range) -> revenue/gross profit/operating
        #     income/net income/R&D/capex (flow items spanning the fiscal
        #     period). See T-018 / docs/NOTES.md.
        closing = self._fetch_fx_rate(currency, period_end)
        average = self._fetch_fx_rate_average(currency, period_start, period_end)

        if not closing and not average:
            logger.warning(
                "[edgar] No USD conversion rate (closing or period-average) "
                "available for %s as of %s — leaving *_usd fields None for %s",
                currency, period_end, ticker,
            )
            return result

        if closing:
            rate_value, rate_date = closing
            result.fx_rate_to_usd = rate_value
            result.fx_rate_date   = rate_date
            result.fx_rate_source = (
                f"ECB reference rate via Frankfurter (api.frankfurter.dev), {rate_date}"
            )
            result.total_assets_usd = (
                raw_total_assets * rate_value if raw_total_assets is not None else None
            )
        else:
            logger.warning(
                "[edgar] No closing USD rate available for %s as of %s — "
                "leaving total_assets_usd None for %s",
                currency, period_end, ticker,
            )

        if average:
            avg_value, avg_start, avg_end = average
            result.fx_rate_avg_to_usd = avg_value
            result.fx_rate_avg_start  = avg_start
            result.fx_rate_avg_end    = avg_end
            result.fx_rate_avg_source = (
                f"ECB reference rate via Frankfurter (api.frankfurter.dev), "
                f"period average {avg_start}..{avg_end}"
            )

            def _to_usd_avg(raw_value: Optional[float]) -> Optional[float]:
                return raw_value * avg_value if raw_value is not None else None

            result.revenue_usd          = _to_usd_avg(raw_revenue)
            result.gross_profit_usd     = _to_usd_avg(raw_gross_profit)
            result.operating_income_usd = _to_usd_avg(raw_operating_income)
            result.net_income_usd       = _to_usd_avg(raw_net_income)
            result.rd_expense_usd       = _to_usd_avg(raw_rd_expense)
            result.capex_usd            = _to_usd_avg(raw_capex)
        else:
            logger.warning(
                "[edgar] No period-average USD rate available for %s over "
                "%s..%s — leaving flow *_usd fields None for %s",
                currency, period_start, period_end, ticker,
            )

        return result

    def _latest_annual(self, concepts: dict, keys: list[str]) -> Optional[float]:
        result = self._latest_annual_entry(concepts, keys)
        return float(result[0]["val"]) if result else None

    def _latest_annual_entry(
        self, concepts: dict, keys: list[str]
    ) -> Optional[tuple[dict, str]]:
        """
        Finds the most recent annual (10-K or 20-F) filing entry for any of
        the given XBRL concept keys, and the currency it was reported in.

        A single 10-K's XBRL facts include comparative prior-year periods
        that all share the same `filed` date, so sorting by `filed` alone
        is not enough — Python's stable sort then keeps the original
        (oldest-first) array order and returns the stalest period. Sort by
        reporting PERIOD (`end`) descending first, with `filed` descending
        as the tiebreaker so a period restated in a later filing still
        prefers the newer filing.

        Currency selection (T-008): prefers `units["USD"]`. When a concept
        has no USD unit — a foreign private issuer reporting only in its
        home currency, e.g. ASML in EUR or TSMC in TWD — falls back to
        whichever single non-USD unit key is present and reports that
        currency code back to the caller. A concept reporting in more than
        one non-USD currency (rare, ambiguous) is skipped rather than
        guessed at.
        """
        for key in keys:
            concept = concepts.get(key, {})
            units = concept.get("units", {})

            if "USD" in units:
                currency, entries = "USD", units["USD"]
            else:
                non_usd = {k: v for k, v in units.items() if v}
                if len(non_usd) != 1:
                    continue
                currency, entries = next(iter(non_usd.items()))

            # Filter to annual filings only (form 10-K or 20-F). Require a
            # non-empty `end` too, so a malformed entry can't reach the sort.
            annual = [
                e for e in entries
                if e.get("form") in ("10-K", "20-F")
                and e.get("filed")
                and e.get("end")
            ]
            if annual:
                annual.sort(key=lambda e: (e["end"], e["filed"]), reverse=True)
                return annual[0], currency
        return None

    def _fetch_fx_rate(
        self, currency: str, period_end: Optional[str]
    ) -> Optional[tuple[float, str]]:
        """
        Historical ECB reference rate converting `currency` -> USD, as of
        (at/nearest before) `period_end`, via Frankfurter. Frankfurter snaps
        a weekend/holiday date to the previous business day and reports the
        date it actually used in the response body — return that date, not
        the one requested, so callers record the rate actually applied.

        Returns None — never a guessed or hardcoded rate — if `period_end`
        is missing, the currency isn't one of ECB's ~30 reference
        currencies (e.g. TWD), or the request fails for any reason.
        """
        if not period_end:
            return None
        try:
            resp = self._client.get(
                f"{FRANKFURTER_BASE}/{period_end}",
                params={"base": currency, "symbols": "USD"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            rate = data.get("rates", {}).get("USD")
            rate_date = data.get("date")
            if rate is None or not rate_date:
                return None
            return float(rate), rate_date
        except Exception as exc:
            logger.warning(
                "[edgar] FX lookup failed for %s -> USD as of %s: %s",
                currency, period_end, exc,
            )
            return None

    def _fetch_fx_rate_average(
        self, currency: str, period_start: Optional[str], period_end: Optional[str]
    ) -> Optional[tuple[float, str, str]]:
        """
        Period-average ECB reference rate converting `currency` -> USD over
        [period_start, period_end], via Frankfurter's date-range endpoint
        (`GET /v1/{start}..{end}?base={cur}&symbols=USD`), which returns one
        entry per ECB business day in the range. The simple mean of those
        daily rates is the period-average rate applied to flow
        (income-statement) items — see the flow-vs-stock note in
        `_extract_financials` and T-018.

        Returns None — never a guessed or hardcoded rate — if either date is
        missing, the currency isn't one of ECB's ~30 reference currencies
        (e.g. TWD), the request fails, or the range contains no rates at all
        (e.g. a malformed or empty response).
        """
        if not period_start or not period_end:
            return None
        try:
            resp = self._client.get(
                f"{FRANKFURTER_BASE}/{period_start}..{period_end}",
                params={"base": currency, "symbols": "USD"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            daily_rates = [
                day_rates["USD"]
                for day_rates in data.get("rates", {}).values()
                if day_rates.get("USD") is not None
            ]
            if not daily_rates:
                return None
            avg_rate = sum(daily_rates) / len(daily_rates)
            start_date = data.get("start_date") or period_start
            end_date   = data.get("end_date") or period_end
            return float(avg_rate), start_date, end_date
        except Exception as exc:
            logger.warning(
                "[edgar] Period-average FX lookup failed for %s -> USD over "
                "%s..%s: %s",
                currency, period_start, period_end, exc,
            )
            return None

    def _period_start(self, entry: dict) -> Optional[str]:
        """
        The fiscal-period start date for a duration XBRL fact. Prefers the
        entry's own `start` field (duration concepts like revenue carry
        one); falls back to the `end` date minus one calendar year when
        `start` is absent, per T-018.
        """
        start = entry.get("start")
        if start:
            return start
        end = entry.get("end")
        if not end:
            return None
        try:
            year, month, day = (int(part) for part in end.split("-"))
        except (ValueError, AttributeError):
            return None
        try:
            return date(year - 1, month, day).isoformat()
        except ValueError:
            # end date was Feb 29 and (year - 1) isn't a leap year.
            return date(year - 1, month, day - 1).isoformat()

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()