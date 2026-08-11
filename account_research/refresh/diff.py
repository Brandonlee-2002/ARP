"""
Record diffing.

Compares two AccountResearchRecord snapshots (old vs. new) and produces a
flat, human-readable list of what changed. This is the shared foundation for:

  - the refresh scheduler (refresh_scheduler.py) — decides whether a
    re-run produced anything worth notifying about
  - the CLI — prints a "what changed since last time" summary
  - app.jsx — mirrors this logic in JS (see diff.js) for the browser dashboard

Design notes:
  - Works against dicts OR Pydantic models via the same _get() accessor
    pattern used in crm/salesforce_sync.py, so it can diff a freshly-run
    record against JSON loaded back off disk without re-validating through
    Pydantic.
  - Every change becomes one ChangeItem so callers can render a flat bullet
    list without knowing the schema shape.
  - Deliberately excludes noisy fields that always differ between runs
    (record_id, created_at, refreshed_at, raw source lists) and skips
    free-text fields (overview.summary) that get reworded by the LLM every
    run without a substantive change — only structured fields are compared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ChangeType = Literal["added", "removed", "changed"]


@dataclass
class ChangeItem:
    section:     str          # "overview" | "locations" | "stakeholders" | "economics" | "intelligence" | "news"
    change_type: ChangeType
    headline:    str          # human-readable one-liner
    old_value:   Any = None
    new_value:   Any = None


@dataclass
class RecordDiffReport:
    company_name:         str
    generated_at:         datetime
    changes:              list[ChangeItem] = field(default_factory=list)

    @property
    def has_material_change(self) -> bool:
        return len(self.changes) > 0

    def by_section(self) -> dict[str, list[ChangeItem]]:
        out: dict[str, list[ChangeItem]] = {}
        for c in self.changes:
            out.setdefault(c.section, []).append(c)
        return out

    def to_markdown(self) -> str:
        if not self.changes:
            return f"No material changes for {self.company_name} since the last run."
        lines = [f"# What changed — {self.company_name}",
                 f"*{self.generated_at.strftime('%Y-%m-%d %H:%M UTC')}*", ""]
        for section, items in self.by_section().items():
            lines.append(f"## {section.title()}")
            for item in items:
                icon = {"added": "+", "removed": "-", "changed": "~"}[item.change_type]
                lines.append(f"- [{icon}] {item.headline}")
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "company_name": self.company_name,
            "generated_at": self.generated_at.isoformat(),
            "has_material_change": self.has_material_change,
            "changes": [
                {"section": c.section, "change_type": c.change_type, "headline": c.headline,
                 "old_value": c.old_value, "new_value": c.new_value}
                for c in self.changes
            ],
        }


# ---------------------------------------------------------------------------
# Accessor helpers (mirrors crm/salesforce_sync.py)
# ---------------------------------------------------------------------------

def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def diff_records(old: Any, new: Any) -> RecordDiffReport:
    """
    old may be None (first-ever run for this company) — in that case every
    section is skipped since there's nothing to compare against; callers
    should treat a None `old` as "no diff, this is a baseline."
    """
    company_name = _get(_get(new, "overview", {}), "legal_name") or "Unknown"
    report = RecordDiffReport(company_name=company_name, generated_at=datetime.utcnow())

    if old is None:
        return report

    report.changes.extend(_diff_overview(old, new))
    report.changes.extend(_diff_locations(old, new))
    report.changes.extend(_diff_stakeholders(old, new))
    report.changes.extend(_diff_economics(old, new))
    report.changes.extend(_diff_intelligence(old, new))
    report.changes.extend(_diff_news(old, new))
    report.changes.extend(_diff_confidence(old, new))
    return report


# ---------------------------------------------------------------------------
# Section diffs
# ---------------------------------------------------------------------------

def _diff_overview(old: Any, new: Any) -> list[ChangeItem]:
    o, n = _get(old, "overview", {}), _get(new, "overview", {})
    changes = []

    simple_fields = [
        ("ownership_type",  "Ownership type"),
        ("founded_year",    "Founded year"),
        ("parent_company",  "Parent company"),
    ]
    for field_name, label in simple_fields:
        ov, nv = _get(o, field_name), _get(n, field_name)
        if ov != nv and (ov or nv):
            changes.append(ChangeItem("overview", "changed", f"{label}: {ov or '—'} → {nv or '—'}", ov, nv))

    ot, nt = _get(o, "ticker"), _get(n, "ticker")
    ot_sym = _get(ot, "symbol") if ot else None
    nt_sym = _get(nt, "symbol") if nt else None
    if ot_sym != nt_sym:
        changes.append(ChangeItem("overview", "changed", f"Ticker: {ot_sym or '—'} → {nt_sym or '—'}", ot_sym, nt_sym))

    oe, ne = _get(o, "employee_count"), _get(n, "employee_count")
    oe_val = _get(oe, "value") if oe else None
    ne_val = _get(ne, "value") if ne else None
    if oe_val and ne_val and oe_val != ne_val:
        pct = (ne_val - oe_val) / oe_val * 100
        changes.append(ChangeItem(
            "overview", "changed",
            f"Employee count: {oe_val:,} → {ne_val:,} ({pct:+.0f}%)", oe_val, ne_val,
        ))

    for label, key in [("Industry verticals", "industry_verticals"), ("Competitors", "competitors")]:
        added, removed = _list_diff(_get(o, key, []) or [], _get(n, key, []) or [])
        for a in added:
            changes.append(ChangeItem("overview", "added", f"{label}: +{a}", None, a))
        for r in removed:
            changes.append(ChangeItem("overview", "removed", f"{label}: -{r}", r, None))

    return changes


def _diff_locations(old: Any, new: Any) -> list[ChangeItem]:
    def key(loc):
        return (_norm(_get(loc, "type")), _norm(_get(loc, "city")), _norm(_get(loc, "country")))

    o_locs = {key(l): l for l in (_get(old, "locations", []) or [])}
    n_locs = {key(l): l for l in (_get(new, "locations", []) or [])}
    changes = []

    for k, loc in n_locs.items():
        if k not in o_locs:
            changes.append(ChangeItem(
                "locations", "added",
                f"New location: {_get(loc,'type','').upper()} — {_get(loc,'city')}, {_get(loc,'country')}",
                None, k,
            ))
        else:
            old_status, new_status = _get(o_locs[k], "status"), _get(loc, "status")
            if old_status != new_status:
                changes.append(ChangeItem(
                    "locations", "changed",
                    f"{_get(loc,'city')} status: {old_status} → {new_status}",
                    old_status, new_status,
                ))

    for k, loc in o_locs.items():
        if k not in n_locs:
            changes.append(ChangeItem(
                "locations", "removed",
                f"Location no longer listed: {_get(loc,'type','').upper()} — {_get(loc,'city')}, {_get(loc,'country')}",
                k, None,
            ))
    return changes


def _diff_stakeholders(old: Any, new: Any) -> list[ChangeItem]:
    def key(s):
        return _norm(_get(s, "name"))

    o_stk = {key(s): s for s in (_get(old, "stakeholders", []) or []) if _get(s, "name")}
    n_stk = {key(s): s for s in (_get(new, "stakeholders", []) or []) if _get(s, "name")}
    changes = []

    for k, s in n_stk.items():
        if k not in o_stk:
            changes.append(ChangeItem(
                "stakeholders", "added",
                f"New stakeholder: {_get(s,'name')} ({_get(s,'title')})", None, _get(s, "name"),
            ))
        else:
            old_s = o_stk[k]
            if _norm(_get(old_s, "title")) != _norm(_get(s, "title")):
                changes.append(ChangeItem(
                    "stakeholders", "changed",
                    f"{_get(s,'name')} title changed: {_get(old_s,'title')} → {_get(s,'title')}",
                    _get(old_s, "title"), _get(s, "title"),
                ))
            old_inf, new_inf = _get(old_s, "influence_score", 0) or 0, _get(s, "influence_score", 0) or 0
            if abs(old_inf - new_inf) >= 0.15:
                changes.append(ChangeItem(
                    "stakeholders", "changed",
                    f"{_get(s,'name')} influence: {old_inf:.0%} → {new_inf:.0%}",
                    old_inf, new_inf,
                ))

    for k, s in o_stk.items():
        if k not in n_stk:
            changes.append(ChangeItem(
                "stakeholders", "removed",
                f"Stakeholder no longer listed: {_get(s,'name')} ({_get(s,'title')})",
                _get(s, "name"), None,
            ))
    return changes


def _diff_economics(old: Any, new: Any) -> list[ChangeItem]:
    of = _get(_get(old, "economics", {}), "financials", {})
    nf = _get(_get(new, "economics", {}), "financials", {})
    changes = []

    orev, nrev = _get(of, "revenue_usd_m"), _get(nf, "revenue_usd_m")
    if orev and nrev and abs(orev - nrev) / orev > 0.03:  # >3% change, ignore rounding noise
        pct = (nrev - orev) / orev * 100
        changes.append(ChangeItem(
            "economics", "changed",
            f"Revenue: ${orev:,.0f}M → ${nrev:,.0f}M ({pct:+.1f}%)", orev, nrev,
        ))

    ogm, ngm = _get(of, "gross_margin_pct"), _get(nf, "gross_margin_pct")
    if ogm is not None and ngm is not None and abs(ogm - ngm) >= 1.0:
        changes.append(ChangeItem(
            "economics", "changed",
            f"Gross margin: {ogm:.1f}% → {ngm:.1f}%", ogm, ngm,
        ))

    odq, ndq = _get(of, "data_quality"), _get(nf, "data_quality")
    if str(odq) != str(ndq):
        changes.append(ChangeItem(
            "economics", "changed",
            f"Financial data quality: {odq} → {ndq}", odq, ndq,
        ))

    osemi = _get(_get(old, "economics", {}), "semiconductor_profile", {})
    nsemi = _get(_get(new, "economics", {}), "semiconductor_profile", {})

    obt, nbt = _get(osemi, "chip_buyer_type"), _get(nsemi, "chip_buyer_type")
    if str(obt) != str(nbt):
        changes.append(ChangeItem("economics", "changed", f"Chip buyer type: {obt} → {nbt}", obt, nbt))

    for label, key in [("Fab nodes", "fab_nodes"), ("Known chip suppliers", "known_chip_suppliers"),
                        ("Known supply gaps", "known_supply_gaps")]:
        added, removed = _list_diff(_get(osemi, key, []) or [], _get(nsemi, key, []) or [])
        for a in added:
            changes.append(ChangeItem("economics", "added", f"{label}: +{a}", None, a))
        for r in removed:
            changes.append(ChangeItem("economics", "removed", f"{label}: -{r}", r, None))

    return changes


def _diff_intelligence(old: Any, new: Any) -> list[ChangeItem]:
    oi, ni = _get(old, "account_intelligence", {}), _get(new, "account_intelligence", {})
    changes = []

    ost, nst = _get(oi, "current_supplier_status"), _get(ni, "current_supplier_status")
    if str(ost) != str(nst):
        changes.append(ChangeItem(
            "intelligence", "changed",
            f"Supplier status: {ost} → {nst}", ost, nst,
        ))

    def sig_key(sig):
        return _norm(_get(sig, "description"))[:80]

    o_sigs = {sig_key(s): s for s in (_get(oi, "opportunity_signals", []) or [])}
    n_sigs = {sig_key(s): s for s in (_get(ni, "opportunity_signals", []) or [])}

    for k, sig in n_sigs.items():
        if k not in o_sigs:
            changes.append(ChangeItem(
                "intelligence", "added",
                f"New {_get(sig,'type')} signal: {_get(sig,'description')}", None, _get(sig, "description"),
            ))
    for k, sig in o_sigs.items():
        if k not in n_sigs:
            changes.append(ChangeItem(
                "intelligence", "removed",
                f"Signal resolved/no longer active: {_get(sig,'description')}", _get(sig, "description"), None,
            ))
    return changes


def _diff_news(old: Any, new: Any) -> list[ChangeItem]:
    def key(item):
        return (_norm(_get(item, "headline")), str(_get(item, "date")))

    o_news = {key(n) for n in (_get(old, "news", []) or [])}
    changes = []
    for item in (_get(new, "news", []) or []):
        if key(item) not in o_news:
            changes.append(ChangeItem(
                "news", "added",
                f"{_get(item,'date')} — {_get(item,'headline')}", None, _get(item, "headline"),
            ))
    return changes


def _diff_confidence(old: Any, new: Any) -> list[ChangeItem]:
    oc = _get(_get(old, "meta", {}), "confidence", {})
    nc = _get(_get(new, "meta", {}), "confidence", {})
    oo, no = _get(oc, "overall"), _get(nc, "overall")
    if str(oo) != str(no):
        return [ChangeItem("overview", "changed", f"Overall confidence: {oo} → {no}", oo, no)]
    return []


def _list_diff(old_list: list, new_list: list) -> tuple[list, list]:
    old_norm = {_norm(x) for x in old_list}
    new_norm = {_norm(x) for x in new_list}
    added   = [x for x in new_list if _norm(x) not in old_norm]
    removed = [x for x in old_list if _norm(x) not in new_norm]
    return added, removed