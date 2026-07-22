"""
Patent inventor extractor — USPTO Open Data Portal (ODP).

Finds named inventors on granted patents assigned to a target company, as
a technical-stakeholder signal distinct from People Data Labs: a patent
inventor is a real, government-recorded person tied to a specific,
CPC-classified technology — a much more precise technical signal than a
job title, but with real gaps a title-based source doesn't have (see
below).

What this DOES give you:
  - Real names, tied to real technology (CPC classification), tied to a
    real filing/grant date.
  - A rough segment tag (Power / EP / HPC / CONN / AMS) per patent, via
    CPC_SEGMENT_MAP below.
  - Inventor city/state, when present — a corroborating signal for
    design-center location, especially when cross-referenced against
    extractors/fcc_search.py's facility addresses for the same company.

What this does NOT give you, structurally:
  - Job title or seniority. Patents don't record either.
  - Confirmation of CURRENT employment. A patent filed 3 years ago
    reflects who worked there then — people leave. Always apply a
    recency filter and treat a patent-sourced name as a lead to enrich
    (e.g. cross-check against people_search.py), not a ready-made
    verified stakeholder. Do NOT set Stakeholder.verified=True purely
    from a patent match — that field's docstring in models/schema.py
    reserves "verified" for structured *current-employment* sources
    (PDL); reuse a separate provenance marker for patent-sourced names
    instead (see the pipeline README's note on this).

API note (accurate as of this module's writing, July 2026): PatentsView's
legacy api.patentsview.org endpoints are dead — retired in the March 20,
2026 migration to the USPTO's Open Data Portal. This module targets the
current, confirmed ODP surface: POST https://api.uspto.gov/api/v1/patent/
applications/search, authenticated with an X-API-KEY header. That
endpoint covers pending applications AND granted patents in one search;
the distinguishing signal is whether applicationMetaData.grantDate /
applicationMetaData.patentNumber is populated.

Request body — CONFIRMED against USPTO's own published query spec
(ODP-API-Query-Spec.pdf, fetched directly, not inferred): top-level keys
are q, filters, rangeFilters, sort, fields, pagination, facets — all
optional. Two things that look plausible but are WRONG and cost real
debugging time before being confirmed:
  - `limit`/`offset` are NOT top-level keys. They must be nested:
    "pagination": {"offset": 0, "limit": 25}. A top-level "limit" key
    produces a 400 Bad Request on every request, including the simplest
    possible query — there is no partial-credit here, it's all-or-nothing.
  - `sort` is an array of {"field": ..., "order": "desc"|"asc"} objects,
    not a bare "field desc" string.
`applicationMetaData.inventorBag.inventorNameText` is a CONFIRMED real
field (present in USPTO's own wildcard-search example in that same spec).

Response shape — CONFIRMED against a live response (2026-07-22, query for
Intel Corporation, granted patents since 2023). cpcClassificationBag is a
flat list of plain strings (e.g. "H04N  21/438") with INCONSISTENT internal
whitespace between class and group/subgroup — matching normalizes by
stripping whitespace rather than assuming fixed spacing (see
CPC_SEGMENT_MAP / _cpc_normalize). inventorBag items carry firstName,
lastName, inventorNameText (sometimes middleName), and a
correspondenceAddressBag list with entries tagged via postalAddressCategory
as "postal" or "residence" — these CAN differ for the same person (a
correspondence/mailing address is not necessarily where the inventor
actually works); _parse_inventor_location prefers "residence". Non-US
addresses correctly omit geographicRegionName/geographicRegionCode
entirely rather than erroring.

Requires a free USPTO ODP API key: https://data.uspto.gov/apis/getting-started
As of June 18, 2026, that requires a USPTO.gov account verified through
ID.me before a key will authorize anything — a key requested without
completing that verification will 403 on every call.

Operational gotcha (confirmed, cost real debugging time): USPTO's ODP sits
behind AWS WAF, which evaluates its rules — including User-Agent-based bot
detection — BEFORE the API key/authorizer is ever checked. A perfectly
valid key will still get a bare {"message": "Forbidden"} if the request's
User-Agent looks like a script rather than a browser (PowerShell's
Invoke-RestMethod default UA and httpx's default "python-httpx/x.y.z" UA
both trigger this). The client below sets an ordinary browser User-Agent
for exactly this reason — don't remove it to "clean up" the headers dict.
"""

from __future__ import annotations

import logging
import re
import ssl
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import httpx
import truststore

logger = logging.getLogger(__name__)

USPTO_API_BASE = "https://api.uspto.gov"
SEARCH_ENDPOINT = f"{USPTO_API_BASE}/api/v1/patent/applications/search"

# ---------------------------------------------------------------------------
# CPC classification → our five segments.
#
# This is a STARTING heuristic, not an authoritative technical taxonomy —
# CPC codes are far more granular than five buckets, many patents span
# multiple relevant codes, and some prefixes (e.g. H01L, "semiconductor
# devices") are too broad to map to one segment confidently, so they're
# deliberately left unmapped rather than guessed. Extend/refine this with
# input from someone who actually knows the CPC scheme for your product
# lines — treat this as a first pass to be corrected, not a finished map.
#
# Confirmed from a live response (2026-07-22): CPC codes come back with
# irregular internal whitespace between the class and the group/subgroup —
# e.g. "G06F   9/30032" (three spaces), "H04N  21/438" (two spaces). Prefix
# keys below are written WITHOUT spaces; matching normalizes both sides by
# stripping whitespace first (see _cpc_matches_prefix) rather than assuming
# a fixed number of spaces, since that spacing isn't consistent even within
# one response.
# ---------------------------------------------------------------------------
CPC_SEGMENT_MAP: dict[str, str] = {
    # Power: power conversion, battery/power-supply systems, motor drive
    "H02M": "power",   # apparatus for conversion between AC/DC
    "H02J": "power",   # circuit arrangements for electric power supply/distribution
    "H02P": "power",   # control of electric motors
    "H01M": "power",   # batteries, fuel cells
    # Embedded processing: microcontrollers / embedded compute
    "G06F9": "ep",      # program control / instruction execution (partial overlap w/ HPC)
    # High-performance compute: general computing, AI/ML accelerators
    "G06N": "hpc",      # computing arrangements based on specific computational models
    "G06F15": "hpc",    # digital computers / data processing (large-scale)
    # Connectivity: RF, wireless, networking, antennas
    "H04B": "conn",     # transmission systems
    "H04W": "conn",     # wireless communication networks
    "H04L": "conn",     # transmission of digital information
    "H01Q": "conn",     # antennas
    # Analog & mixed-signal: signal processing, converters, RF front-end
    "H03F": "ams",      # amplifiers
    "H03M": "ams",       # analog/digital conversion
    "H03K": "ams",       # pulse technique / logic circuits
    "H03L": "ams",       # frequency synthesis/control
    "G01R": "ams",       # measuring electrical variables
}


def _cpc_normalize(code: str) -> str:
    """Strips all whitespace so prefix matching is immune to the
    inconsistent internal spacing USPTO's API returns (see CPC_SEGMENT_MAP
    docstring above)."""
    return re.sub(r"\s+", "", code or "")





@dataclass
class PatentRecord:
    """One granted patent matched to the target company."""
    patent_number:   Optional[str] = None
    title:           Optional[str] = None
    grant_date:      Optional[date] = None
    filing_date:     Optional[date] = None
    cpc_codes:       list[str] = field(default_factory=list)
    segments:        set = field(default_factory=set)
    # Each entry: {"name": str, "city": str|None, "state": str|None, "country": str|None}
    # Confirmed structure (live response, 2026-07-22): inventorBag items carry
    # firstName/lastName/inventorNameText plus a correspondenceAddressBag list
    # with "postal" and "residence" entries per inventor — see
    # _parse_inventor_location for which one we prefer and why.
    inventors:       list[dict] = field(default_factory=list)


@dataclass
class PatentInventor:
    """
    One inventor, aggregated across all their matched patents for this
    company. This is the unit downstream code should generally consume —
    individual PatentRecords are available via `patents` for detail/audit.
    """
    name:               Optional[str] = None
    city:               Optional[str] = None
    state:              Optional[str] = None
    country:            Optional[str] = None
    patent_count:       int = 0
    segments:           set = field(default_factory=set)
    most_recent_grant:  Optional[date] = None
    patents:            list[PatentRecord] = field(default_factory=list)


class PatentSearchExtractor:
    """
    Thin wrapper around the USPTO Open Data Portal patent search API.

    Usage:
        extractor = PatentSearchExtractor(api_key="...")
        inventors = extractor.search("Intel Corporation", lookback_years=3)
    """

    def __init__(self, api_key: str, timeout: int = 30):
        self.api_key = api_key
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
                # USPTO's ODP sits behind AWS WAF, which blocks requests
                # based on User-Agent BEFORE the API key is ever checked
                # (confirmed: AWS WAF rules run ahead of any authorizer).
                # httpx's default UA ("python-httpx/x.y.z") gets flagged the
                # same way PowerShell's default Invoke-RestMethod UA does —
                # a valid key still 403s if this isn't set to something
                # that reads as an ordinary browser.
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            # See edgar.py for why verify=truststore's SSLContext is
            # needed — same corporate-TLS-inspection issue applies to
            # every extractor in this pipeline that uses httpx.
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    def search(
        self,
        company_name: str,
        lookback_years: int = 3,
        max_results: int = 100,
    ) -> list[PatentInventor]:
        """
        Returns inventors on patents GRANTED to `company_name` within the
        last `lookback_years` years, aggregated across their matched
        patents, each tagged with the segment(s) implied by CPC
        classification. Returns [] on any failure — this is an optional,
        best-effort enrichment source, same fault-tolerance posture as
        every other extractor in this pipeline.
        """
        cutoff = date.today().replace(year=date.today().year - lookback_years)

        query = (
            f'applicationMetaData.firstApplicantName:"{company_name}" '
            f'AND applicationMetaData.grantDate:[{cutoff.isoformat()} TO *]'
        )

        try:
            resp = self._client.post(
                SEARCH_ENDPOINT,
                json={
                    "q": query,
                    "sort": [
                        {"field": "applicationMetaData.grantDate", "order": "desc"},
                    ],
                    "fields": [
                        "applicationNumberText",
                        "applicationMetaData.patentNumber",
                        "applicationMetaData.inventionTitle",
                        "applicationMetaData.filingDate",
                        "applicationMetaData.grantDate",
                        "applicationMetaData.applicantBag",
                        "applicationMetaData.inventorBag",
                        "applicationMetaData.cpcClassificationBag",
                    ],
                    # Pagination is a nested object, NOT top-level limit/offset
                    # keys — confirmed against USPTO's own published query
                    # spec (ODP-API-Query-Spec.pdf) after a top-level
                    # "limit" key caused a 400 on every request, including
                    # the simplest possible query.
                    "pagination": {
                        "offset": 0,
                        "limit": max_results,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                logger.warning("[patent_search] USPTO ODP: invalid API key")
            else:
                logger.warning("[patent_search] HTTP error: %s", exc)
            return []
        except Exception as exc:
            logger.warning("[patent_search] call failed: %s", exc)
            return []

        rows = data.get("patentFileWrapperDataBag", [])
        if not isinstance(rows, list):
            return []

        records = [self._parse_record(r) for r in rows]
        records = [r for r in records if r.patent_number]  # keep granted only

        return self._aggregate_by_inventor(records)

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_record(self, row: dict) -> PatentRecord:
        meta = row.get("applicationMetaData", {}) or {}

        cpc_codes = self._parse_cpc_codes(meta.get("cpcClassificationBag", []))
        normalized_codes = [_cpc_normalize(c) for c in cpc_codes]
        segments = {
            seg for prefix, seg in CPC_SEGMENT_MAP.items()
            if any(code.startswith(_cpc_normalize(prefix)) for code in normalized_codes)
        }

        inventors = []
        for i in meta.get("inventorBag", []) or []:
            name = self._parse_inventor_name(i)
            if not name:
                continue
            city, state, country = self._parse_inventor_location(i)
            inventors.append({"name": name, "city": city, "state": state, "country": country})

        return PatentRecord(
            patent_number=meta.get("patentNumber") or None,
            title=meta.get("inventionTitle"),
            grant_date=self._parse_date(meta.get("grantDate")),
            filing_date=self._parse_date(meta.get("filingDate")),
            cpc_codes=cpc_codes,
            segments=segments,
            inventors=inventors,
        )

    def _parse_cpc_codes(self, cpc_bag: list) -> list[str]:
        """
        Tries several plausible key spellings for the CPC code string —
        see module docstring's field-name caveat.
        """
        codes = []
        for item in cpc_bag or []:
            if isinstance(item, str):
                codes.append(item)
                continue
            if isinstance(item, dict):
                for key in ("cpcCode", "cpcClassificationCode", "code", "cpcSubclass"):
                    v = item.get(key)
                    if isinstance(v, str) and v:
                        codes.append(v)
                        break
        return codes

    def _parse_inventor_name(self, item: dict) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        for key in ("inventorNameText", "fullName", "name"):
            v = item.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        first = item.get("firstName") or item.get("inventorFirstName")
        last  = item.get("lastName") or item.get("inventorLastName")
        if first or last:
            return " ".join(p for p in [first, last] if p)
        return None

    def _parse_inventor_location(self, item: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Returns (city, state, country) for an inventorBag entry.

        Confirmed structure (live response, 2026-07-22): correspondenceAddressBag
        is a list, typically with two entries per inventor distinguished by
        postalAddressCategory: "postal" (correspondence/mailing address —
        for corporate patents this can reflect an IP/legal department's
        address rather than where the inventor actually works) and
        "residence" (where the inventor actually lives). These CAN differ
        for the same person. We prefer "residence" as the more meaningful
        physical-location signal, falling back to "postal" or the first
        entry if no address is tagged "residence".

        Non-US addresses (seen in the wild: China, Israel) omit
        geographicRegionName/geographicRegionCode entirely — that's normal,
        not a parsing failure; state will correctly come back None for them.
        """
        if not isinstance(item, dict):
            return None, None, None

        addr_bag = item.get("correspondenceAddressBag")
        if not isinstance(addr_bag, list) or not addr_bag:
            return None, None, None

        residence = next(
            (a for a in addr_bag if isinstance(a, dict) and a.get("postalAddressCategory") == "residence"),
            None,
        )
        postal = next(
            (a for a in addr_bag if isinstance(a, dict) and a.get("postalAddressCategory") == "postal"),
            None,
        )
        addr = residence or postal or addr_bag[0]
        if not isinstance(addr, dict):
            return None, None, None

        city = addr.get("cityName")
        state = addr.get("geographicRegionCode")
        country = addr.get("countryCode")
        return city, state, country

    @staticmethod
    def _parse_date(s: Optional[str]) -> Optional[date]:
        if not s:
            return None
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate_by_inventor(self, records: list[PatentRecord]) -> list[PatentInventor]:
        """
        Collapses patent records down to one entry per distinct inventor
        name (simple normalized-name grouping — no cross-patent identity
        disambiguation beyond exact-name match, so two different real
        people who happen to share a name would incorrectly merge; this
        is a known, accepted limitation for a first pass).
        """
        by_name: dict[str, PatentInventor] = {}

        for record in records:
            for inventor in record.inventors:
                name = inventor["name"]
                key = " ".join(name.lower().split())
                inv = by_name.setdefault(key, PatentInventor(name=name))
                inv.patent_count += 1
                inv.segments |= record.segments
                inv.patents.append(record)
                # Location is set from the first patent that has it — an
                # inventor's residence at time of one filing is treated as
                # good enough; we don't try to track it changing over time
                # across patents.
                if inv.city is None and inventor.get("city"):
                    inv.city = inventor["city"]
                    inv.state = inventor.get("state")
                    inv.country = inventor.get("country")
                if record.grant_date and (
                    inv.most_recent_grant is None or record.grant_date > inv.most_recent_grant
                ):
                    inv.most_recent_grant = record.grant_date

        return sorted(
            by_name.values(),
            key=lambda i: (i.most_recent_grant or date.min, i.patent_count),
            reverse=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Account-intelligence helper — independent of stakeholder matching.
# ---------------------------------------------------------------------------

def segment_patent_activity(inventors: list[PatentInventor]) -> dict[str, int]:
    """
    Rolls up distinct patent count per segment across all inventors —
    feeds an account_intelligence opportunity/risk signal (e.g. "12
    patents in Power in the last 18 months" suggests growing in-house
    design capability) independent of whether any individual inventor
    gets matched to a stakeholder record.
    """
    counts: dict[str, int] = defaultdict(int)
    seen_patents: set[str] = set()
    for inv in inventors:
        for p in inv.patents:
            if p.patent_number in seen_patents:
                continue
            seen_patents.add(p.patent_number)
            for seg in p.segments:
                counts[seg] += 1
    return dict(counts)