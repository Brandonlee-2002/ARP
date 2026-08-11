"""
Refresh scheduler.

Maintains a small registry of tracked accounts (company, domain, ticker,
cadence), decides which are due for a refresh, re-runs the pipeline for
each, diffs the new record against the last saved one, and reports what
changed.

This is intentionally a *library* + thin CLI, not a long-running service —
run it from cron (or any scheduler) on whatever cadence you like; it figures
out per-account what's actually due:

    # every hour via cron, checks all accounts, only refreshes what's due
    0 * * * *  cd /path/to/project && python -m account_research.refresh.refresh_scheduler --run-due

Registry file (JSON) tracks accounts:
    [
      {"company_name": "Intel Corporation", "domain": "intel.com", "ticker": "INTC", "cadence": "weekly"},
      {"company_name": "Arm Holdings",       "domain": "arm.com",   "ticker": null,  "cadence": "monthly"}
    ]

"Due" is computed from the *latest saved record's* meta.refreshed_at, not
from a separate timestamp file — the output directory is the source of
truth, so there's nothing to get out of sync.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .diff import RecordDiffReport, diff_records

logger = logging.getLogger(__name__)

CADENCE_INTERVALS = {
    "daily":   timedelta(days=1),
    "weekly":  timedelta(days=7),
    "monthly": timedelta(days=30),
    # "on-demand" is intentionally excluded — never auto-refreshed.
}


@dataclass
class RefreshResult:
    company_name: str
    domain:       str
    record:       Any
    diff:         RecordDiffReport
    saved_paths:  dict
    error:        Optional[str] = None


@dataclass
class RefreshScheduler:
    pipeline:       Any                 # an AccountResearchPipeline
    output_dir:     str = "./output"
    registry_path:  str = "./output/accounts_registry.json"

    def __post_init__(self):
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Registry management
    # ------------------------------------------------------------------

    def load_registry(self) -> list[dict]:
        path = Path(self.registry_path)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def save_registry(self, entries: list[dict]) -> None:
        Path(self.registry_path).write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def add_account(self, company_name: str, domain: str, ticker: Optional[str] = None,
                     cadence: str = "weekly") -> None:
        entries = self.load_registry()
        domain = domain.lower().strip()
        entries = [e for e in entries if e["domain"].lower() != domain]
        entries.append({"company_name": company_name, "domain": domain, "ticker": ticker, "cadence": cadence})
        self.save_registry(entries)
        logger.info("[scheduler] Tracking %s (%s) on %s cadence", company_name, domain, cadence)

    def remove_account(self, domain: str) -> None:
        entries = self.load_registry()
        domain = domain.lower().strip()
        entries = [e for e in entries if e["domain"].lower() != domain]
        self.save_registry(entries)
        logger.info("[scheduler] Stopped tracking %s", domain)

    # ------------------------------------------------------------------
    # Finding the prior record for a domain
    # ------------------------------------------------------------------

    def _find_latest_record(self, domain: str) -> Optional[dict]:
        """
        Scans output_dir for the most recently modified JSON record whose
        overview.website matches this domain. Returns the raw dict, or None
        if no prior record exists (first run for this company).
        """
        domain = domain.lower().strip()
        registry_name = Path(self.registry_path).name
        candidates = []
        for path in Path(self.output_dir).glob("*.json"):
            if path.name == registry_name:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            website = (data.get("overview", {}) or {}).get("website", "")
            netloc = urlparse(website).netloc.lower().replace("www.", "")
            if netloc == domain or domain in website.lower():
                candidates.append((path.stat().st_mtime, data))

        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0], reverse=True)
        return candidates[0][1]

    # ------------------------------------------------------------------
    # Due detection
    # ------------------------------------------------------------------

    def due_accounts(self, now: Optional[datetime] = None) -> list[dict]:
        now = now or datetime.utcnow()
        due = []
        for entry in self.load_registry():
            cadence = entry.get("cadence", "weekly")
            interval = CADENCE_INTERVALS.get(cadence)
            if interval is None:   # on-demand — never auto-due
                continue

            prior = self._find_latest_record(entry["domain"])
            if prior is None:
                due.append(entry)   # never run before — always due
                continue

            refreshed_at_raw = (prior.get("meta", {}) or {}).get("refreshed_at")
            try:
                refreshed_at = datetime.fromisoformat(refreshed_at_raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                due.append(entry)   # can't parse timestamp — be safe and refresh
                continue

            if now - refreshed_at >= interval:
                due.append(entry)
        return due

    # ------------------------------------------------------------------
    # Running refreshes
    # ------------------------------------------------------------------

    def run_due(self) -> list[RefreshResult]:
        results = []
        for entry in self.due_accounts():
            results.append(self.refresh_one(
                company_name=entry["company_name"],
                domain=entry["domain"],
                ticker=entry.get("ticker"),
                cadence=entry.get("cadence", "weekly"),
            ))
        return results

    def refresh_one(self, company_name: str, domain: str, ticker: Optional[str] = None,
                     cadence: str = "weekly") -> RefreshResult:
        logger.info("[scheduler] Refreshing %s (%s)...", company_name, domain)
        prior = self._find_latest_record(domain)

        try:
            record = self.pipeline.run(
                company_name=company_name, domain=domain, ticker=ticker, refresh_cadence=cadence,
            )
        except Exception as exc:
            logger.error("[scheduler] Refresh failed for %s: %s", company_name, exc)
            return RefreshResult(company_name=company_name, domain=domain, record=None,
                                  diff=RecordDiffReport(company_name=company_name, generated_at=datetime.utcnow()),
                                  saved_paths={}, error=str(exc))

        report = diff_records(prior, record.model_dump(mode="json") if hasattr(record, "model_dump") else record)
        saved = self.pipeline.save(record, output_dir=self.output_dir)

        if report.has_material_change:
            logger.info("[scheduler] %s: %d changes detected", company_name, len(report.changes))
            diff_path = Path(self.output_dir) / f"{domain.replace('.', '_')}_diff_{datetime.utcnow():%Y%m%d_%H%M%S}.md"
            diff_path.write_text(report.to_markdown(), encoding="utf-8")
            saved["diff"] = diff_path
        else:
            logger.info("[scheduler] %s: no material changes", company_name)

        return RefreshResult(company_name=company_name, domain=domain, record=record,
                              diff=report, saved_paths=saved)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse

    from ..pipeline.pipeline import AccountResearchPipeline  # local import to keep module import-safe without full env

    parser = argparse.ArgumentParser(description="Refresh scheduler for tracked accounts")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Start tracking a company")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--domain", required=True)
    p_add.add_argument("--ticker", default=None)
    p_add.add_argument("--cadence", default="weekly", choices=["daily", "weekly", "monthly", "on-demand"])

    p_rm = sub.add_parser("remove", help="Stop tracking a company")
    p_rm.add_argument("--domain", required=True)

    sub.add_parser("list", help="List tracked accounts")
    sub.add_parser("run-due", help="Refresh every account that's due")

    p_run_one = sub.add_parser("run-one", help="Force-refresh a single company now")
    p_run_one.add_argument("--domain", required=True)

    args = parser.parse_args()

    def _make_scheduler(need_pipeline: bool) -> RefreshScheduler:
        # Only `run-due` and `run-one` actually call the pipeline; `add`,
        # `remove` and `list` just read/write the local registry file and
        # must work with no API keys in the environment at all.
        pipeline = AccountResearchPipeline.from_env() if need_pipeline else None
        return RefreshScheduler(pipeline=pipeline)

    if args.cmd == "add":
        scheduler = _make_scheduler(need_pipeline=False)
        scheduler.add_account(args.company, args.domain, args.ticker, args.cadence)
    elif args.cmd == "remove":
        scheduler = _make_scheduler(need_pipeline=False)
        scheduler.remove_account(args.domain)
    elif args.cmd == "list":
        scheduler = _make_scheduler(need_pipeline=False)
        for e in scheduler.load_registry():
            print(f"  {e['company_name']:30s} {e['domain']:20s} {e.get('ticker') or '—':8s} {e['cadence']}")
    elif args.cmd == "run-due":
        scheduler = _make_scheduler(need_pipeline=True)
        results = scheduler.run_due()
        if not results:
            print("Nothing due for refresh.")
        for r in results:
            status = f"ERROR: {r.error}" if r.error else f"{len(r.diff.changes)} changes"
            print(f"  {r.company_name:30s} {status}")
    elif args.cmd == "run-one":
        scheduler = _make_scheduler(need_pipeline=True)
        entries = {e["domain"]: e for e in scheduler.load_registry()}
        entry = entries.get(args.domain)
        if not entry:
            print(f"'{args.domain}' isn't tracked yet — use `add` first.")
            return
        result = scheduler.refresh_one(entry["company_name"], entry["domain"], entry.get("ticker"),
                                        entry.get("cadence", "weekly"))
        print(result.diff.to_markdown())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    _cli()