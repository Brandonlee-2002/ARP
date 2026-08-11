"""
People-data extractor — Apollo.io + People Data Labs.

Pulls structured contact records directly from B2B contact databases,
filtered toward engineering/technical roles, to supplement the LLM's
web-search-derived stakeholder list. Web search alone tends to surface
only people who've been covered in press (CEOs, VPs) — individual
contributors and engineers don't show up there, which is why the LLM
extractor caps out around 5-6 stakeholders. This module targets a
mix — roughly two-thirds engineers, one-third leadership/procurement —
so the final roster looks like what a rep would get from Apollo/PDL
directly.

Both providers are optional and independent. Either or both may be
configured via APOLLO_API_KEY / PDL_API_KEY; a missing key for one
simply means that source is skipped, not an error.

Neither provider's *search* endpoint reliably returns verified email
addresses — both bill email reveals as a separate "enrichment" call.
This module does NOT call enrichment endpoints, to avoid silently
burning credits on every research run. `email` will be None on most
returned contacts unless the provider happens to include one in the
search payload itself. If you want verified emails for a shortlist,
call Apollo's /people/match or PDL's Person Enrichment endpoint
yourself for just the contacts you plan to actually reach out to.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Semiconductor-flavored engineering titles — deliberately broader than
# "Software Engineer" since this pipeline targets hardware/process/fab roles.
ENGINEERING_TITLES = [
    "engineer", "principal engineer", "staff engineer", "senior engineer",
    "process engineer", "device engineer", "test engineer", "product engineer",
    "applications engineer", "design engineer", "validation engineer",
    "packaging engineer", "yield engineer", "hardware engineer", "firmware engineer",
    "systems engineer", "integration engineer",
]

LEADERSHIP_TITLES = [
    "procurement", "supply chain", "sourcing", "purchasing",
    "vp engineering", "director engineering", "cto", "chief technology officer",
    "vp operations", "head of engineering", "engineering manager",
]

SENIORITY_INFLUENCE = {
    "owner": 0.95, "founder": 0.95, "c_suite": 0.95,
    "vp": 0.8, "head": 0.75, "director": 0.65,
    "manager": 0.5, "senior": 0.4, "entry": 0.25,
}


# ---------------------------------------------------------------------------
# Apollo.io client
# ---------------------------------------------------------------------------

class ApolloClient:
    """
    Wraps Apollo's People API Search endpoint.
    Docs: https://docs.apollo.io/reference/people-api-search

    Note: this endpoint does NOT return email addresses (see module
    docstring) — only name/title/seniority/linkedin_url/location.
    """
    BASE = "https://api.apollo.io/api/v1/mixed_people/api_search"

    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self._http = httpx.Client(timeout=timeout)

    def search(self, domain: str, titles: list[str], seniorities: Optional[list[str]] = None,
                per_page: int = 25, max_pages: int = 1) -> list[dict]:
        results: list[dict] = []
        for page in range(1, max_pages + 1):
            params = {
                "q_organization_domains_list[]": domain,
                "person_titles[]": titles,
                "include_similar_titles": "true",
                "per_page": min(per_page, 100),
                "page": page,
            }
            if seniorities:
                params["person_seniorities[]"] = seniorities

            try:
                resp = self._http.post(
                    self.BASE,
                    headers={
                        "X-Api-Key": self.api_key,
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                        "accept": "application/json",
                    },
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("[apollo] search failed for %s: %s", domain, exc)
                break

            people = data.get("people", []) or []
            results.extend(people)
            if len(people) < params["per_page"]:
                break  # no more pages worth fetching
        return results

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# People Data Labs client
# ---------------------------------------------------------------------------

class PDLClient:
    """
    Wraps PDL's Person Search endpoint (Elasticsearch query syntax).
    Docs: https://docs.peopledatalabs.com/docs/person-search-api

    Billed per record retrieved — `size` directly controls cost.
    """
    BASE = "https://api.peopledatalabs.com/v5/person/search"

    def __init__(self, api_key: str, timeout: int = 20):
        self.api_key = api_key
        self._http = httpx.Client(timeout=timeout)

    def search(self, domain: str, job_title_role: Optional[str] = None, size: int = 25) -> list[dict]:
        must: list[dict] = [{"term": {"job_company_website": domain}}]
        if job_title_role:
            must.append({"term": {"job_title_role": job_title_role}})
        es_query = {"query": {"bool": {"must": must}}}

        params = {"query": json.dumps(es_query), "size": size, "pretty": "false"}
        try:
            resp = self._http.get(self.BASE, headers={"X-Api-Key": self.api_key}, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("[pdl] search failed for %s: %s", domain, exc)
            return []

        if data.get("status") != 200:
            logger.warning("[pdl] non-200 status in response body: %s", data.get("status"))
            return []
        return data.get("data", []) or []

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_apollo(person: dict) -> dict:
    name = person.get("name") or f"{person.get('first_name','')} {person.get('last_name','')}".strip()
    location = ", ".join(p for p in [person.get("city"), person.get("state")] if p) or None
    return {
        "name": name,
        "title": person.get("title") or "",
        "seniority": person.get("seniority"),
        "location": location,
        "linkedin_url": person.get("linkedin_url"),
        "email": person.get("email"),   # almost always None from search — see module docstring
        "source": "apollo",
    }


def _normalize_pdl(person: dict) -> dict:
    levels = person.get("job_title_levels") or []
    return {
        "name": person.get("full_name") or "",
        "title": person.get("job_title") or "",
        "seniority": levels[0] if levels else None,
        "location": person.get("location_name"),
        "linkedin_url": person.get("linkedin_url"),
        "email": person.get("work_email"),
        "source": "pdl",
    }


def _dedupe(contacts: list[dict]) -> list[dict]:
    """
    Merges duplicate contacts (same person found via both providers) field
    by field, rather than picking one record wholesale — Apollo often has
    cleaner titles/seniority, PDL more often has a work email, so a person
    found by both should end up with the best of both, not whichever
    source happened to be processed first.
    """
    merged: dict[str, dict] = {}
    for c in contacts:
        key = (c.get("name") or "").strip().lower()
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(c)
        else:
            existing = merged[key]
            for field in ("title", "seniority", "location", "linkedin_url", "email"):
                if not existing.get(field) and c.get(field):
                    existing[field] = c[field]
            if c.get("source") and c["source"] not in existing.get("source", ""):
                existing["source"] = f"{existing.get('source','')}+{c['source']}".strip("+")
    return list(merged.values())


def _infer_role_type(title: str, seniority: Optional[str]) -> str:
    t = (title or "").lower()
    s = (seniority or "").lower()
    if any(k in t for k in ("chief", "president", "cto", "ceo", "coo")) or s in ("c_suite", "owner", "founder"):
        return "executive_sponsor"
    if any(k in t for k in ("procurement", "sourcing", "supply chain", "purchasing")):
        return "economic_buyer"
    if any(k in t for k in ("engineer", "architect", "technical", "developer")):
        return "technical_buyer"
    if s in ("vp", "head", "director"):
        return "executive_sponsor"
    if s == "manager":
        return "influencer"
    return "influencer"


def _infer_influence(seniority: Optional[str], role_type: str) -> float:
    base = SENIORITY_INFLUENCE.get((seniority or "").lower(), 0.35)
    if role_type == "economic_buyer":
        base = max(base, 0.7)
    return round(min(base, 1.0), 2)


def _to_stakeholder_dict(c: dict) -> dict:
    role_type = _infer_role_type(c.get("title", ""), c.get("seniority"))
    return {
        "name": c["name"],
        "title": c.get("title") or "Unknown title",
        "department": "Engineering" if role_type == "technical_buyer" else None,
        "location": c.get("location"),
        "role_type": role_type,
        "influence_score": _infer_influence(c.get("seniority"), role_type),
        "relationship_status": "cold",
        "linkedin_url": c.get("linkedin_url"),
        "email": c.get("email"),
        "notes": f"Sourced from {c.get('source', 'contact database')} — not yet contacted.",
    }


# ---------------------------------------------------------------------------
# Combined extractor
# ---------------------------------------------------------------------------

@dataclass
class PeopleDataExtractor:
    apollo: Optional[ApolloClient] = None
    pdl:    Optional[PDLClient]    = None

    @classmethod
    def from_env(cls) -> Optional["PeopleDataExtractor"]:
        apollo_key = os.environ.get("APOLLO_API_KEY")
        pdl_key    = os.environ.get("PDL_API_KEY")
        if not apollo_key and not pdl_key:
            logger.info("[people_data] Neither APOLLO_API_KEY nor PDL_API_KEY set — skipping")
            return None
        return cls(
            apollo=ApolloClient(apollo_key) if apollo_key else None,
            pdl=PDLClient(pdl_key) if pdl_key else None,
        )

    def fetch(self, company_name: str, domain: str, target_count: int = 20,
              engineer_ratio: float = 0.65) -> list[dict]:
        """
        Returns a list of Stakeholder-schema-shaped dicts (ready for
        assembler._build_stakeholders), biased toward engineering roles.
        """
        engineer_slots = max(1, int(target_count * engineer_ratio))
        leadership_slots = max(1, target_count - engineer_slots)

        raw: list[dict] = []

        if self.apollo:
            raw += [_normalize_apollo(p) for p in
                    self.apollo.search(domain, ENGINEERING_TITLES, per_page=min(engineer_slots * 2, 100))]
            raw += [_normalize_apollo(p) for p in
                    self.apollo.search(domain, LEADERSHIP_TITLES, per_page=min(leadership_slots * 2, 100))]

        if self.pdl:
            raw += [_normalize_pdl(p) for p in
                    self.pdl.search(domain, job_title_role="engineering", size=min(engineer_slots * 2, 100))]
            raw += [_normalize_pdl(p) for p in
                    self.pdl.search(domain, job_title_role=None, size=min(leadership_slots * 2, 100))]

        deduped = _dedupe(raw)
        if not deduped:
            logger.info("[people_data] No contacts found for %s", domain)
            return []

        engineers = [c for c in deduped if _infer_role_type(c["title"], c.get("seniority")) == "technical_buyer"]
        others    = [c for c in deduped if c not in engineers]

        selected = engineers[:engineer_slots] + others[:leadership_slots]
        if len(selected) < target_count:
            remaining = [c for c in deduped if c not in selected]
            selected += remaining[: target_count - len(selected)]

        logger.info("[people_data] %s: %d contacts (%d engineers, %d other) from %s",
                     company_name, len(selected), len([c for c in selected if c in engineers]),
                     len(selected) - len([c for c in selected if c in engineers]),
                     "+".join(s for s, on in [("apollo", self.apollo), ("pdl", self.pdl)] if on))

        return [_to_stakeholder_dict(c) for c in selected[:target_count]]

    def close(self) -> None:
        if self.apollo:
            self.apollo.close()
        if self.pdl:
            self.pdl.close()