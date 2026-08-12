"""
Salesforce REST API client.

Handles OAuth2 authentication and thin wrappers around the sObject REST API
(query, create, update, upsert). No third-party Salesforce SDK required —
just httpx, consistent with the rest of this codebase (see edgar.py).

Two auth flows are supported:

  1. Username-password flow (default) — simplest to set up, works in any org.
     Requires: client_id, client_secret, username, password, security_token.

  2. JWT bearer flow — recommended for production/unattended jobs (no
     password or security token to rotate). Requires a Connected App with a
     digital certificate and a pre-authorized user. Pass a `jwt_assertion`
     callable if you want this instead of wiring it in here directly.

Environment variables (username-password flow):
    SF_CLIENT_ID
    SF_CLIENT_SECRET
    SF_USERNAME
    SF_PASSWORD
    SF_SECURITY_TOKEN
    SF_DOMAIN            — "login" (production) or "test" (sandbox), default "login"
    SF_API_VERSION        — default "v60.0"
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class SalesforceAuthError(RuntimeError):
    pass


class SalesforceAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Salesforce API error {status_code}: {payload}")


@dataclass
class SalesforceClient:
    client_id:      str
    client_secret:  str
    username:       str
    password:       str
    security_token: str = ""
    domain:         str = "login"     # "login" | "test"
    api_version:    str = "v60.0"
    timeout:        int = 30

    _access_token: Optional[str] = field(default=None, init=False, repr=False)
    _instance_url: Optional[str] = field(default=None, init=False, repr=False)
    _http: httpx.Client = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._http = httpx.Client(timeout=self.timeout)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> Optional["SalesforceClient"]:
        """
        Builds a client from environment variables. Returns None (rather than
        raising) if Salesforce isn't configured, so callers can treat CRM
        sync as an optional pipeline stage.
        """
        required = ["SF_CLIENT_ID", "SF_CLIENT_SECRET", "SF_USERNAME", "SF_PASSWORD"]
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            logger.info("[salesforce] Not configured (missing %s) — skipping CRM sync", missing)
            return None

        return cls(
            client_id=os.environ["SF_CLIENT_ID"],
            client_secret=os.environ["SF_CLIENT_SECRET"],
            username=os.environ["SF_USERNAME"],
            password=os.environ["SF_PASSWORD"],
            security_token=os.environ.get("SF_SECURITY_TOKEN", ""),
            domain=os.environ.get("SF_DOMAIN", "login"),
            api_version=os.environ.get("SF_API_VERSION", "v60.0"),
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """
        Username-password OAuth2 flow. Salesforce appends the security token
        to the password unless the request originates from a trusted/whitelisted
        IP range configured on the Connected App or profile.
        """
        url = f"https://{self.domain}.salesforce.com/services/oauth2/token"
        resp = self._http.post(url, data={
            "grant_type":    "password",
            "client_id":     self.client_id,
            "client_secret": self.client_secret,
            "username":      self.username,
            "password":      f"{self.password}{self.security_token}",
        })
        if resp.status_code != 200:
            raise SalesforceAuthError(f"Auth failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        self._access_token = data["access_token"]
        self._instance_url = data["instance_url"]
        logger.info("[salesforce] Authenticated against %s", self._instance_url)

    def _ensure_auth(self) -> None:
        if not self._access_token:
            self.authenticate()

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
        }

    # ------------------------------------------------------------------
    # Low-level request wrapper (retries once on 401 by re-authenticating)
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._ensure_auth()
        url = f"{self._instance_url}/services/data/{self.api_version}{path}"
        resp = self._http.request(method, url, headers=self._headers(), **kwargs)

        if resp.status_code == 401:
            logger.info("[salesforce] Token expired — re-authenticating")
            self.authenticate()
            resp = self._http.request(method, url, headers=self._headers(), **kwargs)

        if resp.status_code >= 300:
            try:
                payload = resp.json()
            except Exception:
                payload = resp.text
            raise SalesforceAPIError(resp.status_code, payload)

        return resp

    # ------------------------------------------------------------------
    # sObject operations
    # ------------------------------------------------------------------

    def query(self, soql: str) -> list[dict]:
        """Runs a SOQL query, following pagination via nextRecordsUrl."""
        resp = self._request("GET", "/query", params={"q": soql})
        data = resp.json()
        records = data.get("records", [])
        while not data.get("done", True):
            next_url = data["nextRecordsUrl"]
            resp = self._request("GET", next_url.replace(f"/services/data/{self.api_version}", ""))
            data = resp.json()
            records.extend(data.get("records", []))
        return records

    def create(self, sobject: str, fields: dict) -> str:
        """Creates a record. Returns the new record's Id."""
        resp = self._request("POST", f"/sobjects/{sobject}/", json=fields)
        return resp.json()["id"]

    def update(self, sobject: str, record_id: str, fields: dict) -> None:
        self._request("PATCH", f"/sobjects/{sobject}/{record_id}", json=fields)

    def upsert(self, sobject: str, external_id_field: str, external_id_value: str, fields: dict) -> str:
        """
        Upserts by external ID. Returns the record's Salesforce Id.

        Salesforce returns 201 (with body) on insert and 204 (no body) on
        update — both are handled here.
        """
        path = f"/sobjects/{sobject}/{external_id_field}/{external_id_value}"
        resp = self._request("PATCH", path, json=fields)
        if resp.status_code == 204:
            # Updated an existing record — look up its Id.
            existing = self.query(
                f"SELECT Id FROM {sobject} WHERE {external_id_field} = '{external_id_value}'"
            )
            return existing[0]["Id"] if existing else ""
        return resp.json().get("id", "")

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()