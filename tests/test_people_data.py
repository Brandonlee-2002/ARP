"""
Offline regression tests for account_research.extractors.people_data.

Covers T-023: both httpx clients must pass truststore's SSLContext, and
_normalize_pdl must survive PDL's true/false-in-place-of-value quirk (see
the module's _pdl_str/_pdl_list docstrings for why that quirk exists).
No network access or API keys required.
"""

from __future__ import annotations

import ssl

import httpx
import truststore

from account_research.extractors.people_data import (
    ApolloClient,
    PDLClient,
    _normalize_pdl,
)


# ---------------------------------------------------------------------------
# TLS verify wiring
# ---------------------------------------------------------------------------

def test_apollo_client_uses_truststore_ssl_context():
    client = ApolloClient(api_key="dummy")
    try:
        verify = client._http._transport._pool._ssl_context
        assert isinstance(verify, ssl.SSLContext)
    finally:
        client.close()


def test_pdl_client_uses_truststore_ssl_context():
    client = PDLClient(api_key="dummy")
    try:
        verify = client._http._transport._pool._ssl_context
        assert isinstance(verify, ssl.SSLContext)
    finally:
        client.close()


def test_apollo_client_timeout_preserved():
    client = ApolloClient(api_key="dummy", timeout=7)
    try:
        assert client._http.timeout.connect == 7
    finally:
        client.close()


def test_pdl_client_timeout_preserved():
    client = PDLClient(api_key="dummy", timeout=7)
    try:
        assert client._http.timeout.connect == 7
    finally:
        client.close()


def test_apollo_and_pdl_clients_build_httpx_client_with_truststore_verify(monkeypatch):
    """Belt-and-suspenders check independent of httpx internals: patch
    httpx.Client itself and assert both constructors pass a verify= kwarg
    that is a truststore-built SSLContext."""
    captured = []
    real_client = httpx.Client

    class _RecordingClient(real_client):
        def __init__(self, *args, **kwargs):
            captured.append(kwargs.get("verify"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(
        "account_research.extractors.people_data.httpx.Client", _RecordingClient
    )

    apollo = ApolloClient(api_key="dummy")
    pdl = PDLClient(api_key="dummy")
    try:
        assert len(captured) == 2
        for verify in captured:
            assert isinstance(verify, ssl.SSLContext)
    finally:
        apollo.close()
        pdl.close()


# ---------------------------------------------------------------------------
# PDL true/false-in-place-of-value guard (_normalize_pdl)
# ---------------------------------------------------------------------------

def _full_record() -> dict:
    """A genuinely-populated PDL record — every field the guard touches."""
    return {
        "full_name": "Jane Doe",
        "job_title": "Principal Engineer",
        "job_title_levels": ["director", "senior"],
        "location_name": "Austin, Texas, United States",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "work_email": "jane.doe@example.com",
    }


def test_normalize_pdl_full_record_normalizes_correctly():
    """The guard must not eat real data."""
    result = _normalize_pdl(_full_record())
    assert result == {
        "name": "Jane Doe",
        "title": "Principal Engineer",
        "seniority": "director",
        "location": "Austin, Texas, United States",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "email": "jane.doe@example.com",
        "source": "pdl",
    }


def test_normalize_pdl_true_full_name_does_not_become_stakeholder_named_true():
    record = _full_record()
    record["full_name"] = True
    result = _normalize_pdl(record)
    assert result["name"] != True  # noqa: E712 - explicitly checking against the bool
    assert result["name"] == ""


def test_normalize_pdl_true_job_title_guarded():
    record = _full_record()
    record["job_title"] = True
    result = _normalize_pdl(record)
    assert result["title"] == ""


def test_normalize_pdl_true_job_title_levels_does_not_crash_on_subscript():
    """This is the crash: `True[0]` raises TypeError without the guard."""
    record = _full_record()
    record["job_title_levels"] = True
    result = _normalize_pdl(record)
    assert result["seniority"] is None


def test_normalize_pdl_false_job_title_levels_does_not_crash():
    record = _full_record()
    record["job_title_levels"] = False
    result = _normalize_pdl(record)
    assert result["seniority"] is None


def test_normalize_pdl_true_location_name_guarded():
    record = _full_record()
    record["location_name"] = True
    result = _normalize_pdl(record)
    assert result["location"] is None


def test_normalize_pdl_true_linkedin_url_guarded():
    record = _full_record()
    record["linkedin_url"] = True
    result = _normalize_pdl(record)
    assert result["linkedin_url"] is None


def test_normalize_pdl_true_work_email_guarded():
    record = _full_record()
    record["work_email"] = True
    result = _normalize_pdl(record)
    assert result["email"] is None


def test_normalize_pdl_all_fields_true_no_exception_and_no_true_values():
    """Every field replaced by PDL's placeholder at once — no exception,
    and no field surfaces the literal boolean anywhere in the result."""
    record = {
        "full_name": True,
        "job_title": True,
        "job_title_levels": True,
        "location_name": True,
        "linkedin_url": True,
        "work_email": True,
    }
    result = _normalize_pdl(record)
    assert True not in result.values()
    assert result["name"] == ""
    assert result["title"] == ""
    assert result["seniority"] is None
    assert result["location"] is None
    assert result["linkedin_url"] is None
    assert result["email"] is None
    assert result["source"] == "pdl"


def test_normalize_pdl_missing_fields_absent_not_placeholder():
    """An empty record (fields simply absent, PDL's non-quirk case) behaves
    the same as before this guard existed."""
    result = _normalize_pdl({})
    assert result["name"] == ""
    assert result["title"] == ""
    assert result["seniority"] is None
    assert result["location"] is None
    assert result["linkedin_url"] is None
    assert result["email"] is None
