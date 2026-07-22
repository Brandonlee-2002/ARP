"""
FCC facility-location extractor.

Finds physical facility addresses a company has on file with the FCC for
RF (radio frequency) equipment certification. This is NOT a patent or
personnel database — the FCC has no record of individual engineers. What
it does have is a structured, government-recorded company-name-to-address
mapping for anyone who has ever certified an RF-emitting product, which is
a genuinely useful corroborating signal for design-center locations —
especially for the Connectivity segment (cellular/WiFi/BLE modules, RF
front-ends) and, more weakly, AMS (RF/analog test equipment).

Coverage caveat: this ONLY surfaces anything for companies whose products
include RF/wireless components. A pure power-semiconductor or industrial
customer with no radios in their products will not appear here at all —
this is a segment-skewed source, not a general-purpose one.

Data source: the FCC's Equipment Authorization System (EAS) grantee
registrations, published as a Socrata Open Data ("SODA") dataset at
opendata.fcc.gov. This is a *stable, documented API surface* (Socrata's
API contract is the same across every Socrata-hosted government portal —
see https://dev.socrata.com/), unlike the FCC's own apps.fcc.gov OET
Equipment Authorization Search, which has a documented JSON API (KDB
publication 953436, "getFCCIDList" etc.) but whose exact request/response
shape was not confirmed at the time this module was written. That richer,
per-product filing history (multiple addresses over time, product
categories, filing dates) is a natural follow-up once that surface is
confirmed against a live response — see the module-level TODO below.

Free, no API key required (Socrata's public read endpoints are keyless
for reasonable request volumes; an X-App-Token can be added later if rate
limiting becomes an issue — see https://dev.socrata.com/docs/app-tokens).
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass, field
from typing import Optional

import httpx
import truststore

logger = logging.getLogger(__name__)

# The EAS Equipment Authorization Grantee Registrations dataset on FCC's
# Socrata portal — one row per grantee code, with company name + address.
# This is a bulk registration list (grantee codes are assigned once and
# rarely change), not a per-product filing history.
FCC_SOCRATA_BASE = "https://opendata.fcc.gov"
GRANTEE_REGISTRATIONS_DATASET_ID = "3b3k-34jp"

# TODO (unconfirmed API surface, follow-up item):
# apps.fcc.gov's OET Equipment Authorization Search / EAS Web API
# Services (KDB publication 953436) documents a getFCCIDList API that
# would give per-product filing history — multiple addresses over time
# per grantee, plus product category and filing date, which is richer
# than the one-address-per-grantee-code snapshot below. Its exact
# request/response shape wasn't confirmed against a live call when this
# module was written; validate it against a real response before adding
# a second extraction path here.


@dataclass
class FccFacility:
    """One FCC-registered address for a company's grantee code."""
    grantee_code:   Optional[str] = None
    company_name:   Optional[str] = None
    address_line1:  Optional[str] = None
    city:           Optional[str] = None
    state:          Optional[str] = None
    zip_code:       Optional[str] = None
    country:        Optional[str] = None

    @property
    def location_str(self) -> Optional[str]:
        parts = [p for p in [self.city, self.state, self.country] if p]
        return ", ".join(parts) or None


class FccSearchExtractor:
    """
    Thin wrapper around the FCC's Socrata-hosted grantee registrations
    dataset.

    Usage:
        extractor = FccSearchExtractor()
        facilities = extractor.search("Intel Corporation")
    """

    def __init__(
        self,
        dataset_id: str = GRANTEE_REGISTRATIONS_DATASET_ID,
        app_token: Optional[str] = None,
        timeout: int = 20,
    ):
        """
        dataset_id: override to point at a different Socrata dataset
        (e.g. a richer per-product EA grants table, if/when one is
        confirmed) without changing any calling code.

        app_token: optional Socrata "X-App-Token" — not required for
        the request volumes this pipeline generates, but raises the
        (generous, shared-pool) rate limit if supplied. See
        https://dev.socrata.com/docs/app-tokens
        """
        self.dataset_id = dataset_id
        self._headers = {"X-App-Token": app_token} if app_token else {}
        # See edgar.py for why verify=truststore's SSLContext is needed —
        # same corporate-TLS-inspection issue applies to every extractor
        # in this pipeline that uses httpx.
        self._client = httpx.Client(
            timeout=timeout,
            headers=self._headers,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    def search(self, company_name: str, max_results: int = 20) -> list[FccFacility]:
        """
        Returns FCC-registered facilities whose company name matches
        `company_name` (Socrata full-text match — tolerant of partial
        and case-insensitive matches, but company-name variants like
        "Intel Corp" vs "Intel Corporation" may still miss each other;
        try a couple of variants if the first search comes back empty).
        """
        try:
            resp = self._client.get(
                f"{FCC_SOCRATA_BASE}/resource/{self.dataset_id}.json",
                params={"$q": company_name, "$limit": max_results},
            )
            resp.raise_for_status()
            rows = resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("[fcc_search] HTTP error: %s", exc)
            return []
        except Exception as exc:
            logger.warning("[fcc_search] call failed: %s", exc)
            return []

        if not isinstance(rows, list):
            return []

        return [self._parse_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_row(self, row: dict) -> FccFacility:
        """
        Socrata field names are set per-dataset by the publisher and are
        not guaranteed stable across dataset revisions. This tries a few
        plausible spellings for each field (the dataset's actual column
        names as published, plus common alternates) rather than assuming
        one fixed schema — cheap insurance against a silent field rename.
        """
        def _first(*keys: str) -> Optional[str]:
            for k in keys:
                v = row.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None

        return FccFacility(
            grantee_code=_first("grantee_code", "granteecode", "grantee_code_s"),
            company_name=_first("grantee_name", "company_name", "applicant_name", "entity_name"),
            address_line1=_first("address", "address_line_1", "street_address"),
            city=_first("city", "city_name"),
            state=_first("state", "state_code"),
            zip_code=_first("zip", "zip_code", "postal_code"),
            country=_first("country", "country_code") or "US",
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()