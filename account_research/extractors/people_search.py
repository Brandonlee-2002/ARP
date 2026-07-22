"""
People search extractor — People Data Labs (PDL).

Finds decision-maker contacts at a target company by domain, so the
stakeholder section can anchor on real names/titles instead of the LLM
inventing plausible-sounding ones from search snippets alone.

Optional stage: if PDL_API_KEY isn't set, the pipeline skips this stage
entirely and stakeholders fall back to pure LLM inference, same as before
this extractor existed. No ticker-style special-casing needed — this works
for public and private companies alike, unlike EDGAR.

Free tier: 1,000 records/month, no credit card required.
Get a key at https://www.peopledatalabs.com
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass, field
from typing import Optional

import httpx
import truststore

logger = logging.getLogger(__name__)

PDL_SEARCH_URL = "https://api.peopledatalabs.com/v5/person/search"

# Seniority levels we care about for B2B buying-committee mapping.
RELEVANT_LEVELS = ["cxo", "vp", "director", "owner", "partner"]


@dataclass
class PersonProfile:
    """One person, normalized from PDL's raw record to the fields we care about."""
    name:              Optional[str]  = None
    title:             Optional[str]  = None
    job_title_role:    Optional[str]  = None          # PDL's coarse function, e.g. "engineering"
    job_title_levels:  list[str]      = field(default_factory=list)   # e.g. ["vp"]
    department:        Optional[str] = None
    location:          Optional[str] = None
    linkedin_url:      Optional[str] = None
    emails:            list[str]     = field(default_factory=list)
    phone_numbers:     list[str]     = field(default_factory=list)
    company_name:      Optional[str] = None
    pdl_likelihood:    Optional[int] = None            # PDL's own confidence, 1-10

    @staticmethod
    def _str(r: dict, key: str) -> Optional[str]:
        """
        Get `key` from a raw PDL record as a string, else None.

        PDL returns `true`/`false` in place of the real value — for
        strings AND lists — whenever your plan doesn't have access to
        that field, instead of omitting the key or using null. Any
        field pulled from PDL needs this guard.
        """
        v = r.get(key)
        return v if isinstance(v, str) and v.strip() else None

    @staticmethod
    def _list(r: dict, key: str) -> list:
        """Same guard as _str, but for fields expected to be lists."""
        v = r.get(key)
        return v if isinstance(v, list) else []

    @classmethod
    def from_pdl_record(cls, r: dict) -> "PersonProfile":
        full_name = cls._str(r, "full_name") or " ".join(
            filter(None, [cls._str(r, "first_name"), cls._str(r, "last_name")])
        ) or None

        loc_parts = [
            cls._str(r, "location_locality"),
            cls._str(r, "location_region"),
            cls._str(r, "location_country"),
        ]

        # PDL sometimes returns emails as dicts ({"address": ...}), sometimes as plain strings
        emails_raw = cls._list(r, "emails")
        emails = [
            (e.get("address") if isinstance(e, dict) else e)
            for e in emails_raw if e
        ]
        emails = [e for e in emails if isinstance(e, str) and e]

        likelihood = r.get("likelihood")
        likelihood = (
            likelihood if isinstance(likelihood, (int, float)) and not isinstance(likelihood, bool)
            else None
        )

        return cls(
            name=full_name,
            title=cls._str(r, "job_title"),
            job_title_role=cls._str(r, "job_title_role"),
            job_title_levels=cls._list(r, "job_title_levels"),
            department=cls._str(r, "job_title_sub_role"),
            location=", ".join(p for p in loc_parts if p) or None,
            linkedin_url=cls._str(r, "linkedin_url"),
            emails=emails,
            phone_numbers=cls._list(r, "phone_numbers"),
            company_name=cls._str(r, "job_company_name"),
            pdl_likelihood=likelihood,
        )


class PeopleSearchExtractor:
    """
    Thin wrapper around PDL's Person Search API (v5).

    Usage:
        extractor = PeopleSearchExtractor(api_key="...")
        people = extractor.search("Intel Corporation", "intel.com")
    """

    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        # See edgar.py for why verify=truststore's SSLContext is needed —
        # same corporate-TLS-inspection issue applies to every extractor
        # in this pipeline that uses httpx.
        self._client = httpx.Client(
            timeout=timeout,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )

    def search(
        self,
        company_name: str,
        domain: str,
        max_results: int = 10,
        role_levels: Optional[list[str]] = None,
    ) -> list[PersonProfile]:
        """
        Returns up to `max_results` PersonProfile objects for people currently
        at `company_name` / `domain`, restricted to decision-maker seniority
        levels (cxo/vp/director/owner/partner by default).
        """
        role_levels = role_levels or RELEVANT_LEVELS
        levels_clause = " OR ".join(f"job_title_levels='{lvl}'" for lvl in role_levels)

        sql_by_domain = (
            f"SELECT * FROM person "
            f"WHERE job_company_website='{domain}' "
            f"AND ({levels_clause})"
        )
        raw = self._call(sql_by_domain, max_results)

        if not raw or not raw.get("data"):
            logger.info("[people_search] no results by domain — retrying by company name")
            sql_by_name = (
                f"SELECT * FROM person "
                f"WHERE job_company_name='{company_name}' "
                f"AND ({levels_clause})"
            )
            raw = self._call(sql_by_name, max_results)

        if not raw or not raw.get("data"):
            return []

        return [PersonProfile.from_pdl_record(r) for r in raw["data"]]

    def _call(self, sql: str, size: int) -> Optional[dict]:
        try:
            resp = self._client.post(
                PDL_SEARCH_URL,
                headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
                json={"sql": sql, "size": size, "pretty": False},
            )
            if resp.status_code == 402:
                logger.warning("[people_search] PDL: free-tier quota exhausted")
                return None
            if resp.status_code == 401:
                logger.warning("[people_search] PDL: invalid API key")
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("[people_search] PDL HTTP error: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[people_search] PDL call failed: %s", exc)
            return None

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()