"""
Salesforce sync — pushes an assembled AccountResearchRecord into Salesforce.

Design mirrors the rest of the pipeline: fault-tolerant per sub-section
(a failure syncing stakeholders shouldn't block the Account record itself),
and idempotent (safe to re-run on every research refresh).

Identity / de-dup strategy:
    Account   — upserted on Research_Domain__c (external ID), matching the
                same "same company across runs" logic app.jsx uses (normKey).
    Contact   — matched by Name + AccountId (queried first; created if not
                found, updated if found). Salesforce Contacts don't have a
                clean natural external ID here since stakeholders often lack
                emails, so this is a query-then-write instead of a REST upsert.
    Signals   — always inserted fresh as Account_Signal__c child records after
                deleting prior ones for the account, so stale signals don't
                accumulate run over run.

Requires the custom fields described in the setup notes to already exist in
the target org (Setup > Object Manager). Missing fields will raise
SalesforceAPIError with Salesforce's own "No such column" message, so
failures are diagnosable rather than silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .salesforce_client import SalesforceAPIError, SalesforceClient

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    account_id:  str
    contact_ids: list[str]
    signal_ids:  list[str]
    warnings:    list[str]


class SalesforceSync:
    def __init__(self, client: SalesforceClient):
        self.client = client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, record: Any, domain: str) -> SyncResult:
        """
        record: an AccountResearchRecord (or its .model_dump() dict — both
                are handled via the small accessor helpers below).
        domain: the company's website domain — used as the external ID to
                identify the same account across repeated research runs.
        """
        warnings: list[str] = []

        account_id = self._sync_account(record, domain)

        contact_ids: list[str] = []
        try:
            contact_ids = self._sync_stakeholders(record, account_id)
        except SalesforceAPIError as exc:
            logger.warning("[salesforce] Stakeholder sync failed: %s", exc)
            warnings.append(f"stakeholders: {exc}")

        signal_ids: list[str] = []
        try:
            signal_ids = self._sync_signals(record, account_id)
        except SalesforceAPIError as exc:
            logger.warning("[salesforce] Signal sync failed: %s", exc)
            warnings.append(f"signals: {exc}")

        logger.info(
            "[salesforce] Synced account %s (%d contacts, %d signals)",
            account_id, len(contact_ids), len(signal_ids),
        )
        return SyncResult(account_id=account_id, contact_ids=contact_ids,
                           signal_ids=signal_ids, warnings=warnings)

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def _sync_account(self, record: Any, domain: str) -> str:
        ov   = _get(record, "overview", {})
        ec   = _get(record, "economics", {})
        fin  = _get(ec, "financials", {})
        semi = _get(ec, "semiconductor_profile", {})
        ai   = _get(record, "account_intelligence", {})
        meta = _get(record, "meta", {})

        hq = next((l for l in _get(record, "locations", []) if _get(l, "type") == "hq"), None)

        fields = {
            "Name":                          _get(ov, "legal_name") or domain,
            "Website":                       _get(ov, "website") or f"https://{domain}",
            "Research_Domain__c":            domain,
            "Description":                   _get(ov, "summary") or "",
            "Ownership_Type__c":             str(_get(ov, "ownership_type", "")),
            "NumberOfEmployees":             _employee_count(ov),
            "BillingCity":                   _get(hq, "city") if hq else None,
            "BillingCountry":                _get(hq, "country") if hq else None,
            "Chip_Buyer_Type__c":            str(_get(semi, "chip_buyer_type") or ""),
            "Has_Own_Fab__c":                bool(_get(semi, "has_own_fab", False)),
            "Fab_Nodes__c":                  ", ".join(_get(semi, "fab_nodes", []) or []),
            "Known_Chip_Suppliers__c":       ", ".join(_get(semi, "known_chip_suppliers", []) or []),
            "Known_Supply_Gaps__c":          ", ".join(_get(semi, "known_supply_gaps", []) or []),
            "Qualification_Cycle__c":        _get(semi, "qualification_cycle") or "",
            "Annual_Chip_Spend_Est_USD_M__c":_get(semi, "annual_chip_spend_est_usd_m"),
            "Revenue_USD_M__c":              _get(fin, "revenue_usd_m"),
            "RD_Spend_USD_M__c":             _get(fin, "rd_spend_usd_m"),
            "Confidence_Score__c":           str(_get(_get(meta, "confidence", {}), "overall", "")),
            "Current_Supplier_Status__c":    str(_get(ai, "current_supplier_status", "")),
            "Recommended_Next_Action__c":    _get(ai, "recommended_next_action") or "",
            "Research_Last_Refreshed__c":    _get(meta, "refreshed_at"),
        }
        # Drop None values — Salesforce rejects nulls for some field types
        # on insert, and there's no reason to send them.
        fields = {k: v for k, v in fields.items() if v is not None}

        return self.client.upsert("Account", "Research_Domain__c", domain, fields)

    # ------------------------------------------------------------------
    # Stakeholders → Contacts
    # ------------------------------------------------------------------

    def _sync_stakeholders(self, record: Any, account_id: str) -> list[str]:
        contact_ids = []
        for s in _get(record, "stakeholders", []) or []:
            name = (_get(s, "name") or "").strip()
            if not name:
                continue
            first, last = _split_name(name)

            fields = {
                "AccountId":        account_id,
                "FirstName":        first,
                "LastName":         last,
                "Title":            _get(s, "title") or "",
                "Department":       _get(s, "department") or "",
                "Email":            _get(s, "email") or None,
                "Role_Type__c":     str(_get(s, "role_type", "")),
                "Influence_Score__c": _get(s, "influence_score"),
                "Buying_Notes__c":  _get(s, "notes") or "",
            }
            fields = {k: v for k, v in fields.items() if v is not None}

            existing = self.client.query(
                "SELECT Id FROM Contact WHERE AccountId = '{}' AND FirstName = '{}' "
                "AND LastName = '{}' LIMIT 1".format(
                    account_id, _soql_escape(first), _soql_escape(last)
                )
            )
            if existing:
                contact_id = existing[0]["Id"]
                self.client.update("Contact", contact_id, fields)
            else:
                contact_id = self.client.create("Contact", fields)
            contact_ids.append(contact_id)
        return contact_ids

    # ------------------------------------------------------------------
    # Opportunity signals → Account_Signal__c
    # ------------------------------------------------------------------

    def _sync_signals(self, record: Any, account_id: str) -> list[str]:
        # Clear out prior signals for this account so stale ones don't pile up.
        existing = self.client.query(
            f"SELECT Id FROM Account_Signal__c WHERE Account__c = '{account_id}'"
        )
        for row in existing:
            try:
                self.client._request("DELETE", f"/sobjects/Account_Signal__c/{row['Id']}")
            except SalesforceAPIError as exc:
                logger.warning("[salesforce] Failed deleting stale signal %s: %s", row["Id"], exc)

        signal_ids = []
        ai = _get(record, "account_intelligence", {})
        for sig in _get(ai, "opportunity_signals", []) or []:
            fields = {
                "Account__c":     account_id,
                "Signal_Type__c": str(_get(sig, "type", "")),
                "Description__c": _get(sig, "description") or "",
                "Urgency__c":     str(_get(sig, "urgency", "")),
                "Source__c":      _get(sig, "source") or "",
            }
            signal_ids.append(self.client.create("Account_Signal__c", fields))
        return signal_ids


# ---------------------------------------------------------------------------
# Small accessor helpers — work whether `record` is a Pydantic model,
# a plain dict (e.g. from app.jsx's JSON export), or None.
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _employee_count(overview: Any) -> Any:
    ec = _get(overview, "employee_count")
    return _get(ec, "value") if ec else None


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _soql_escape(value: str) -> str:
    return (value or "").replace("\\", "\\\\").replace("'", "\\'")