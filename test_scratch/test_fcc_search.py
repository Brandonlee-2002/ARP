#!/usr/bin/env python3
"""
Standalone test for fcc_search.py — run this directly, no pipeline needed.

Usage:
    python test_fcc_search.py "Intel Corporation"
    python test_fcc_search.py "Qualcomm"

No API key required.
"""

import sys
from pathlib import Path

# Allow running this script from anywhere without installing the package —
# assumes it sits next to the `extractors/` folder from the deliverable.
sys.path.insert(0, str(Path(__file__).parent))

from extractors.fcc_search import FccSearchExtractor


def main():
    if len(sys.argv) < 2:
        print('Usage: python test_fcc_search.py "Company Name"')
        sys.exit(1)

    company_name = sys.argv[1]
    print(f"Searching FCC grantee registrations for: {company_name}\n")

    with FccSearchExtractor() as extractor:
        facilities = extractor.search(company_name)

    if not facilities:
        print("No results. Try a shorter/partial name — e.g. 'Intel' instead of")
        print("'Intel Corporation' — FCC filings don't always use the full legal name.")
        return

    print(f"Found {len(facilities)} facility record(s):\n")
    for f in facilities:
        print(f"  Grantee code:  {f.grantee_code}")
        print(f"  Company name:  {f.company_name}")
        print(f"  Address:       {f.address_line1}")
        print(f"  Location:      {f.location_str}")
        print(f"  ZIP:           {f.zip_code}")
        print("-" * 50)


if __name__ == "__main__":
    main()