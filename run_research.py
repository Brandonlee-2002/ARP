#!/usr/bin/env python3
"""
Account Research Pipeline — CLI entry point.

Usage:
    python run_research.py --company "Intel Corporation" --domain intel.com --ticker INTC
    python run_research.py --company "ASML Holding" --domain asml.com --ticker ASML
    python run_research.py --company "Arm Holdings" --domain arm.com            # no ticker = private/foreign
    python run_research.py --company "Marvell Technology" --domain marvell.com --ticker MRVL --cadence daily

Environment variables required:
    ANTHROPIC_API_KEY   — your Anthropic API key
    SEARCH_API_KEY      — your Serper / Brave / SerpAPI key
    SEARCH_PROVIDER     — "serper" | "brave" | "serpapi"  (default: serper)

Environment variables optional:
    PDL_API_KEY         — People Data Labs key, for verified stakeholder
                          contacts (free tier: 1,000 records/mo, no card
                          required — https://www.peopledatalabs.com).
                          If unset, stakeholders are LLM-inferred from web
                          snippets only, same as before this stage existed.

Output:
    ./output/<company>_<timestamp>.json          — full structured record
    ./output/<company>_<timestamp>_summary.md    — human-readable one-pager
"""

import argparse
import logging
import sys
from pathlib import Path

# Allow running from the repo root
sys.path.insert(0, str(Path(__file__).parent))

from account_research import AccountResearchPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research a target company for semiconductor account management"
    )
    parser.add_argument(
        "--company", required=True,
        help='Company legal name, e.g. "Intel Corporation"'
    )
    parser.add_argument(
        "--domain", required=True,
        help="Company website domain, e.g. intel.com"
    )
    parser.add_argument(
        "--ticker", default=None,
        help="Stock ticker for EDGAR lookup, e.g. INTC (omit for private companies)"
    )
    parser.add_argument(
        "--output", default="./output",
        help="Output directory (default: ./output)"
    )
    parser.add_argument(
        "--cadence", default="weekly",
        choices=["daily", "weekly", "monthly", "on-demand"],
        help="How often this record should be refreshed"
    )
    parser.add_argument(
        "--formats", default="json,summary",
        help="Comma-separated output formats: json,summary (default: both)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    pipeline = AccountResearchPipeline.from_env()

    record = pipeline.run(
        company_name=args.company,
        domain=args.domain,
        ticker=args.ticker,
        refresh_cadence=args.cadence,
    )

    formats = [f.strip() for f in args.formats.split(",")]
    saved = pipeline.save(record, output_dir=args.output, formats=formats)

    print("\n✓ Research complete")
    for fmt, path in saved.items():
        print(f"  {fmt:8s} → {path}")


if __name__ == "__main__":
    main()