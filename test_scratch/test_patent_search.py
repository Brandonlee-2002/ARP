#!/usr/bin/env python3
"""
Standalone test for patent_search.py — run this directly, no pipeline needed.

Usage:
    export USPTO_API_KEY="your-key-here"   # https://data.uspto.gov/apis/getting-started
    python test_patent_search.py "Intel Corporation"
    python test_patent_search.py "Qualcomm" --years 5

IMPORTANT: this prints raw inventor/segment output AND the number of
inventors that came back with no name and no location, so you can see
immediately whether the field-name guesses in _parse_inventor_name /
_parse_inventor_location need adjusting for the live response shape —
see the caveat in patent_search.py's module docstring.
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from extractors.patent_search import PatentSearchExtractor, segment_patent_activity


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("company_name")
    parser.add_argument("--years", type=int, default=3, help="Lookback window in years")
    args = parser.parse_args()

    api_key = os.environ.get("USPTO_API_KEY")
    if not api_key:
        print("Set USPTO_API_KEY first — get a free key at")
        print("https://data.uspto.gov/apis/getting-started")
        sys.exit(1)

    print(f"Searching granted patents for: {args.company_name} (last {args.years} years)\n")

    with PatentSearchExtractor(api_key=api_key) as extractor:
        inventors = extractor.search(args.company_name, lookback_years=args.years)

    if not inventors:
        print("No results. This could mean: no matching patents in the window,")
        print("a company-name mismatch (try the name exactly as it appears on")
        print("their patents' assignee field), or a field-name parsing issue —")
        print("run the raw curl check from the module docstring to rule that out.")
        return

    unnamed = sum(1 for i in inventors if not i.name)
    no_location = sum(1 for i in inventors if not i.city and not i.state)
    print(f"Found {len(inventors)} distinct inventor(s).")
    if unnamed or no_location:
        print(f"  ⚠ {unnamed} came back with no parsed name, {no_location} with no city/state —")
        print("    if these numbers look high, check the field-name caveat in the module docstring.")
    print()

    for inv in inventors[:15]:
        segs = ", ".join(sorted(inv.segments)) or "(unclassified)"
        loc = ", ".join(p for p in [inv.city, inv.state] if p) or "(no location parsed)"
        print(f"  {inv.name or '(unparsed name)'}")
        print(f"    Location:      {loc}")
        print(f"    Patents:       {inv.patent_count} (most recent grant: {inv.most_recent_grant})")
        print(f"    Segment tags:  {segs}")
        print("-" * 50)

    print("\nSegment-level patent activity (feeds account_intelligence signals):")
    for seg, count in sorted(segment_patent_activity(inventors).items(), key=lambda x: -x[1]):
        print(f"  {seg:6s} — {count} patents")


if __name__ == "__main__":
    main()