"""
Offline regression tests for account_research.extractors.edgar.

These build fake XBRL/companyfacts payloads as plain dicts and, where an
HTTP call is unavoidable (CIK resolution), monkeypatch the extractor's
httpx client rather than touching the network. Only the test marked
`network` makes a real call.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from account_research.extractors.edgar import (
    EDGAR_BASE,
    SEC_WWW_BASE,
    EdgarExtractor,
    EdgarFinancials,
)


# ---------------------------------------------------------------------------
# Latest-period selection (the stable-sort / oldest-first bug)
# ---------------------------------------------------------------------------

def test_latest_annual_entry_picks_newest_end_not_list_order():
    """
    Three annual entries share one `filed` date but carry `end` dates in
    three different years. The OLDEST is listed first — the exact shape
    that broke when sorting was done on `filed` alone: a stable sort with
    all-equal keys preserves original (oldest-first) order, so `[0]` was
    the stalest period. Sorting by (end, filed) descending must return the
    newest `end` regardless of input order.
    """
    concepts = {
        "Revenues": {
            "units": {
                "USD": [
                    {"val": 100.0, "end": "2021-12-31", "filed": "2024-01-01", "form": "10-K"},
                    {"val": 200.0, "end": "2022-12-31", "filed": "2024-01-01", "form": "10-K"},
                    {"val": 300.0, "end": "2023-12-31", "filed": "2024-01-01", "form": "10-K"},
                ]
            }
        }
    }

    extractor = EdgarExtractor()
    result = extractor._latest_annual_entry(concepts, ["Revenues"])

    assert result is not None
    entry, currency = result
    assert entry["end"] == "2023-12-31"
    assert entry["val"] == 300.0
    assert currency == "USD"


def test_latest_annual_entry_none_when_no_annual_forms():
    """Only annual (10-K/20-F) filings are eligible; a 10-Q-only concept yields None."""
    concepts = {
        "Revenues": {
            "units": {
                "USD": [
                    {"val": 50.0, "end": "2023-09-30", "filed": "2023-11-01", "form": "10-Q"},
                ]
            }
        }
    }
    extractor = EdgarExtractor()
    assert extractor._latest_annual_entry(concepts, ["Revenues"]) is None


# ---------------------------------------------------------------------------
# Ticker-map host (data.sec.gov vs www.sec.gov)
# ---------------------------------------------------------------------------

def test_edgar_base_and_sec_www_base_hosts():
    assert EDGAR_BASE == "https://data.sec.gov"
    assert SEC_WWW_BASE == "https://www.sec.gov"


def test_resolve_cik_targets_www_sec_gov(monkeypatch):
    """The ticker-to-CIK lookup must hit www.sec.gov/files/company_tickers.json,
    not data.sec.gov (which 404s for that path)."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            }

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        return FakeResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    cik = extractor._resolve_cik("AAPL")

    assert captured["url"] == f"{SEC_WWW_BASE}/files/company_tickers.json"
    assert cik == "0000320193"


# ---------------------------------------------------------------------------
# Unknown ticker returns None cleanly
# ---------------------------------------------------------------------------

def test_fetch_unknown_ticker_returns_none_without_raising(monkeypatch):
    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor, "_resolve_cik", lambda ticker: None)

    result = extractor.fetch("ZZZZNOTAREAL")

    assert result is None


# ---------------------------------------------------------------------------
# T-017: not-found (benign) vs hard failure (bug) must log at different
# levels, so a real breakage can't hide behind the same log line as an
# expected "this ticker isn't on EDGAR" outcome.
# ---------------------------------------------------------------------------

def test_fetch_unknown_ticker_logs_info_not_error(monkeypatch, caplog):
    """A ticker genuinely absent from the SEC ticker map (a 200 response
    whose body just doesn't contain it — e.g. a private company) is
    expected and benign. Must log at INFO, and must not log anything at
    ERROR."""

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
            }

    def fake_get(url, *args, **kwargs):
        return FakeResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    with caplog.at_level(logging.INFO):
        result = extractor.fetch("ZZZZNOTAREAL")

    assert result is None
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("ZZZZNOTAREAL" in r.message for r in info_records)


@pytest.mark.parametrize("status_code", [404, 500])
def test_fetch_ticker_map_hard_failure_logs_error(monkeypatch, caplog, status_code):
    """A 404 or 500 on the ticker-map endpoint itself means the request is
    broken — not that this particular ticker doesn't exist (that's a 200
    with the ticker absent from the body, covered above). Must log at
    ERROR, and must still return None rather than raising."""

    def fake_get(url, *args, **kwargs):
        request = httpx.Request("GET", url)
        return httpx.Response(status_code, request=request)

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    with caplog.at_level(logging.INFO):
        result = extractor.fetch("AAPL")

    assert result is None
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR-level log for a hard HTTP failure"
    assert str(status_code) in error_records[0].message
    assert "AAPL" in error_records[0].message


# ---------------------------------------------------------------------------
# T-018: period-average FX rate for flow items, closing rate for stock items
# ---------------------------------------------------------------------------

def _foreign_filer_facts():
    """A minimal EUR-reporting facts payload: revenue (flow, duration) and
    total assets (stock, instant), both on the same 20-F fiscal period."""
    return {
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "EUR": [
                            {
                                "val": 1000.0,
                                "start": "2023-01-01",
                                "end": "2023-12-31",
                                "filed": "2024-02-01",
                                "form": "20-F",
                            },
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "EUR": [
                            {
                                "val": 5000.0,
                                "end": "2023-12-31",
                                "filed": "2024-02-01",
                                "form": "20-F",
                            },
                        ]
                    }
                },
            }
        }
    }


class _FakeFxResponse:
    """Mimics an httpx.Response for a single-date Frankfurter lookup."""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "amount": 1.0,
            "base": "EUR",
            "date": "2023-12-29",
            "rates": {"USD": 1.10},
        }


class _FakeFxRangeResponse:
    """Mimics an httpx.Response for a Frankfurter date-range lookup: three
    daily rates whose mean is 1.08, deliberately different from the closing
    rate of 1.10 above so a test can assert the two never collapse to the
    same value."""

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "amount": 1.0,
            "base": "EUR",
            "start_date": "2023-01-02",
            "end_date": "2023-12-29",
            "rates": {
                "2023-01-02": {"USD": 1.05},
                "2023-06-15": {"USD": 1.08},
                "2023-12-29": {"USD": 1.11},
            },
        }


def test_fetch_fx_rate_average_averages_daily_rates(monkeypatch):
    """A range payload of several daily rates averages correctly (simple
    mean), and the actual start/end dates Frankfurter used are returned."""

    def fake_get(url, *args, **kwargs):
        assert url.endswith("2023-01-01..2023-12-31")
        assert kwargs["params"] == {"base": "EUR", "symbols": "USD"}
        return _FakeFxRangeResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    result = extractor._fetch_fx_rate_average("EUR", "2023-01-01", "2023-12-31")

    assert result is not None
    avg_rate, start_date, end_date = result
    assert avg_rate == pytest.approx((1.05 + 1.08 + 1.11) / 3)
    assert start_date == "2023-01-02"
    assert end_date == "2023-12-29"


def test_fetch_fx_rate_average_empty_rates_returns_none(monkeypatch):
    """An empty `rates` object (e.g. a range with no ECB business days) is
    treated as unconvertible, never guessed at."""

    class _EmptyRangeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"amount": 1.0, "base": "EUR", "rates": {}}

    def fake_get(url, *args, **kwargs):
        return _EmptyRangeResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    assert extractor._fetch_fx_rate_average("EUR", "2023-01-01", "2023-12-31") is None


def test_fetch_fx_rate_average_request_failure_returns_none(monkeypatch, caplog):
    """A failed range request (network error, unsupported currency, etc.)
    fails soft to None with a WARNING, never a guessed rate."""

    def fake_get(url, *args, **kwargs):
        raise httpx.ConnectError("boom")

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    with caplog.at_level(logging.WARNING):
        result = extractor._fetch_fx_rate_average("TWD", "2023-01-01", "2023-12-31")

    assert result is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_fetch_fx_rate_average_missing_dates_returns_none():
    extractor = EdgarExtractor()
    assert extractor._fetch_fx_rate_average("EUR", None, "2023-12-31") is None
    assert extractor._fetch_fx_rate_average("EUR", "2023-01-01", None) is None


def test_period_start_uses_start_field_when_present():
    extractor = EdgarExtractor()
    entry = {"start": "2022-06-01", "end": "2023-05-31"}
    assert extractor._period_start(entry) == "2022-06-01"


def test_period_start_falls_back_to_end_minus_one_year():
    extractor = EdgarExtractor()
    entry = {"end": "2023-12-31"}  # no `start` — an instant/point-in-time fact
    assert extractor._period_start(entry) == "2022-12-31"


def test_period_start_handles_leap_day_end_date():
    extractor = EdgarExtractor()
    entry = {"end": "2024-02-29"}  # 2023 isn't a leap year
    assert extractor._period_start(entry) == "2023-02-28"


def test_extract_financials_applies_average_to_revenue_closing_to_assets(monkeypatch):
    """The core of T-018: revenue_usd (a flow item) must be converted at the
    period-average rate, and total_assets_usd (a stock item) at the closing
    rate — and, since the two rates differ here, the resulting figures must
    differ from what a single-rate conversion would have produced."""

    def fake_get(url, *args, **kwargs):
        if ".." in url:
            return _FakeFxRangeResponse()
        return _FakeFxResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    result = extractor._extract_financials("ASML", "0000000", _foreign_filer_facts())

    assert result.reporting_currency == "EUR"

    # Closing rate -> total_assets_usd, recorded on fx_rate_to_usd.
    assert result.fx_rate_to_usd == pytest.approx(1.10)
    assert result.fx_rate_date == "2023-12-29"
    assert result.total_assets_usd == pytest.approx(5000.0 * 1.10)

    # Period-average rate -> revenue_usd, recorded on fx_rate_avg_to_usd.
    expected_avg = (1.05 + 1.08 + 1.11) / 3
    assert result.fx_rate_avg_to_usd == pytest.approx(expected_avg)
    assert result.fx_rate_avg_start == "2023-01-02"
    assert result.fx_rate_avg_end == "2023-12-29"
    assert result.revenue_usd == pytest.approx(1000.0 * expected_avg)

    # The two rates must actually differ, or this test would pass by
    # accident even if both fields were wired to the same rate.
    assert result.fx_rate_to_usd != result.fx_rate_avg_to_usd
    assert result.revenue_usd != pytest.approx(1000.0 * result.fx_rate_to_usd)


def test_extract_financials_range_failure_leaves_flow_fields_none(monkeypatch, caplog):
    """If only the range (period-average) request fails, flow items stay
    None rather than falling back to the closing rate — a stock-item rate
    is never substituted for a flow-item rate. The closing rate still
    applies to total_assets_usd, and reporting_currency/form_type/
    fiscal_year are still populated."""

    def fake_get(url, *args, **kwargs):
        if ".." in url:
            raise httpx.ConnectError("range endpoint unreachable")
        return _FakeFxResponse()

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    with caplog.at_level(logging.WARNING):
        result = extractor._extract_financials("ASML", "0000000", _foreign_filer_facts())

    assert result.reporting_currency == "EUR"
    assert result.form_type == "20-F"
    assert result.fiscal_year == 2023

    assert result.revenue_usd is None
    assert result.fx_rate_avg_to_usd is None
    assert result.total_assets_usd == pytest.approx(5000.0 * 1.10)
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_extract_financials_both_rates_fail_leaves_all_usd_fields_none(monkeypatch, caplog):
    """An unsupported currency (ECB has no TWD) or a total network failure
    fails both lookups soft: every `*_usd` field stays None, never an
    approximate or hardcoded rate, while reporting_currency/form_type/
    fiscal_year are still populated."""

    def fake_get(url, *args, **kwargs):
        raise httpx.ConnectError("no network")

    extractor = EdgarExtractor()
    monkeypatch.setattr(extractor._client, "get", fake_get)

    with caplog.at_level(logging.WARNING):
        result = extractor._extract_financials("ASML", "0000000", _foreign_filer_facts())

    assert result.reporting_currency == "EUR"
    assert result.form_type == "20-F"
    assert result.fiscal_year == 2023

    assert result.revenue_usd is None
    assert result.total_assets_usd is None
    assert result.fx_rate_to_usd is None
    assert result.fx_rate_avg_to_usd is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# Live network smoke tests — skippable via `-m "not network"`
# ---------------------------------------------------------------------------

@pytest.mark.network
def test_fetch_intc_live():
    extractor = EdgarExtractor()
    result = extractor.fetch("INTC")

    assert result is not None
    assert isinstance(result, EdgarFinancials)
    assert result.cik is not None
    # INTC reports in USD, so no conversion of any kind should occur.
    assert result.reporting_currency is None
    assert result.fx_rate_to_usd is None
    assert result.fx_rate_avg_to_usd is None


@pytest.mark.network
def test_fetch_asml_live_average_and_closing_rates_differ():
    extractor = EdgarExtractor()
    result = extractor.fetch("ASML")

    assert result is not None
    assert isinstance(result, EdgarFinancials)
    assert result.reporting_currency == "EUR"

    assert result.fx_rate_to_usd is not None
    assert result.fx_rate_avg_to_usd is not None
    assert result.fx_rate_to_usd != result.fx_rate_avg_to_usd

    assert result.revenue_usd is not None
    # ASML's annual revenue is tens of billions of USD.
    assert 5e9 < result.revenue_usd < 1e11
