"""
Web search extractor.

Searches for company information across multiple queries and returns
raw text chunks tagged by topic. Calls an external search API.
Uses a simple rate-limiting wrapper so the pipeline doesn't hammer the API.
"""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    query:   str
    topic:   str
    url:     str
    title:   str
    snippet: str


@dataclass
class WebSearchExtractor:
    """
    Wraps a SerpAPI / Brave / Serper call.
    Set SEARCH_API_KEY and SEARCH_API_URL in your environment or pass directly.

    provider: "serper" | "brave" | "serpapi"
    """
    api_key:        str
    provider:       str = "serper"
    requests_per_s: float = 2.0          # conservative rate limit
    timeout:        int   = 15

    _last_call: float = field(default=0.0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(self, company_name: str, domain: str) -> list[SearchResult]:
        """
        Run a battery of targeted searches and return all results.
        Each query is labeled with a topic so the LLM extractor knows
        what it's looking at.
        """
        queries = self._build_queries(company_name, domain)
        results: list[SearchResult] = []
        for topic, query in queries:
            try:
                batch = self._search(query, topic)
                results.extend(batch)
                logger.info("  [web] %s → %d results", query, len(batch))
            except Exception as exc:
                logger.warning("  [web] query failed (%s): %s", query, exc)
        return results

    # ------------------------------------------------------------------
    # Query construction
    # ------------------------------------------------------------------

    def _build_queries(self, name: str, domain: str) -> list[tuple[str, str]]:
        """
        Returns (topic, query) pairs covering every schema section.
        Semiconductor-specific queries are included by default.
        """
        return [
            # Overview
            ("overview",      f"{name} company overview headquarters founded employees"),
            ("overview",      f"site:{domain} about"),

            # Locations
            ("locations",     f"{name} office locations worldwide fab sites"),
            ("locations",     f"{name} manufacturing plants semiconductor fab"),

            # Stakeholders
            ("stakeholders",  f"{name} executive team CTO CEO procurement leadership"),
            ("stakeholders",  f"{name} VP semiconductor engineering supply chain"),
            ("stakeholders",  f"{name} \"principal engineer\" OR \"staff engineer\" OR \"senior engineer\""),
            ("stakeholders",  f"site:linkedin.com/in {name} process engineer OR device engineer OR test engineer"),

            # Economics — public filings preferred, then news
            ("financials",    f"{name} annual revenue profit margin R&D spend 2024 2025"),
            ("financials",    f"{name} annual report 10-K investor relations"),
            ("financials",    f"{name} capital expenditure capex semiconductor investment"),

            # Semiconductor-specific
            ("semi_profile",  f"{name} chip suppliers foundry design wins"),
            ("semi_profile",  f"{name} semiconductor supply chain external chip sourcing"),
            ("semi_profile",  f"{name} semiconductor qualification RFQ 2025 2026"),

            # Recent news (last 6 months)
            ("news",          f"{name} semiconductor news 2025 2026"),
            ("news",          f"{name} fab expansion acquisition partnership 2026"),
        ]

    # ------------------------------------------------------------------
    # Provider-specific HTTP calls
    # ------------------------------------------------------------------

    def _search(self, query: str, topic: str) -> list[SearchResult]:
        self._rate_limit()

        if self.provider == "serper":
            return self._serper_search(query, topic)
        elif self.provider == "brave":
            return self._brave_search(query, topic)
        elif self.provider == "serpapi":
            return self._serpapi_search(query, topic)
        else:
            raise ValueError(f"Unknown search provider: {self.provider}")

    def _serper_search(self, query: str, topic: str) -> list[SearchResult]:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("organic", []):
            results.append(SearchResult(
                query=query, topic=topic,
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
            ))
        return results

    def _brave_search(self, query: str, topic: str) -> list[SearchResult]:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": self.api_key},
            params={"q": query, "count": 5},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            results.append(SearchResult(
                query=query, topic=topic,
                url=item.get("url", ""),
                title=item.get("title", ""),
                snippet=item.get("description", ""),
            ))
        return results

    def _serpapi_search(self, query: str, topic: str) -> list[SearchResult]:
        resp = httpx.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": self.api_key, "num": 5, "engine": "google"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("organic_results", []):
            results.append(SearchResult(
                query=query, topic=topic,
                url=item.get("link", ""),
                title=item.get("title", ""),
                snippet=item.get("snippet", ""),
            ))
        return results

    def _rate_limit(self) -> None:
        now = time.monotonic()
        wait = (1.0 / self.requests_per_s) - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()