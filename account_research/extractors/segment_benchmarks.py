"""
Semiconductor content benchmarks per customer vertical.

Converts a target account's disclosed annual unit volume (vehicles
produced, servers shipped, etc. — found in their 10-K / investor
disclosures) into an ESTIMATED semiconductor SAM, split across the five
product segments: Power, Embedded Processing (EP), High-Performance
Compute (HPC), Connectivity (CONN), and Analog & Mixed-Signal (AMS).

Design principle: the LLM only ever identifies (a) which vertical fits the
account and (b) whether a unit volume is disclosed. The dollar math below
is plain Python — deterministic, auditable, and never subject to LLM
arithmetic drift or hallucination.

Every number in BENCHMARKS traces to a `source`. Verticals marked
confidence="needs_research" have no sourced $/unit figure yet — do not
backfill them with a guess; extend this table once real teardown/analyst
data is found, and cite it the same way the sourced entries are cited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Benchmark table
# ---------------------------------------------------------------------------

SEGMENTS = ["power", "ep", "hpc", "conn", "ams"]


@dataclass
class VerticalBenchmark:
    vertical:              str
    unit_basis:            str                    # what the unit volume counts
    semi_content_usd_low:  Optional[float]        # $ per unit, low end (raw USD)
    semi_content_usd_high: Optional[float]        # $ per unit, high end (raw USD)
    segment_split:         dict                    # segment -> fraction, sums to 1.0
    source:                str
    as_of:                 str
    confidence:            str                     # "sourced" | "directional" | "needs_research"


BENCHMARKS: dict[str, VerticalBenchmark] = {

    "auto_ice": VerticalBenchmark(
        vertical="Automotive — ICE / conventional",
        unit_basis="vehicles produced",
        semi_content_usd_low=400, semi_content_usd_high=600,
        segment_split={"power": 0.20, "ep": 0.30, "hpc": 0.05, "conn": 0.20, "ams": 0.25},
        source="AlixPartners, 'Navigating the semiconductor-auto nexus' (2026); "
               "Yole Développement via Electronics360",
        as_of="2026",
        confidence="sourced",
    ),

    "auto_ev": VerticalBenchmark(
        vertical="Automotive — EV / BEV",
        unit_basis="vehicles produced",
        semi_content_usd_low=1200, semi_content_usd_high=2000,
        segment_split={"power": 0.40, "ep": 0.20, "hpc": 0.10, "conn": 0.15, "ams": 0.15},
        source="AlixPartners (2026); IEA Global EV Outlook 2025 via Mordor Intelligence",
        as_of="2026",
        confidence="sourced",
    ),

    "datacenter_ai_server": VerticalBenchmark(
        vertical="Datacenter / HPC — AI server rack",
        unit_basis="AI server racks deployed (compute-tray content only)",
        semi_content_usd_low=1_500_000, semi_content_usd_high=3_500_000,
        segment_split={"power": 0.15, "ep": 0.05, "hpc": 0.55, "conn": 0.20, "ams": 0.05},
        source="Semiconductor Industry Association / Deloitte, "
               "'Powering AI: The Semiconductor Ecosystem at the Foundation of Data Centers' (June 2026)",
        as_of="2026",
        confidence="sourced",
    ),

    "datacenter_general_server": VerticalBenchmark(
        vertical="Datacenter — general-purpose server",
        unit_basis="servers shipped",
        semi_content_usd_low=None, semi_content_usd_high=None,
        segment_split={"power": 0.15, "ep": 0.05, "hpc": 0.45, "conn": 0.25, "ams": 0.10},
        source="NEEDS RESEARCH — no sourced $/unit figure yet for non-AI servers",
        as_of="",
        confidence="needs_research",
    ),

    "telecom_infra": VerticalBenchmark(
        vertical="Telecom infrastructure — base station / RAN",
        unit_basis="base stations / radio units deployed",
        semi_content_usd_low=None, semi_content_usd_high=None,
        segment_split={"power": 0.15, "ep": 0.15, "hpc": 0.10, "conn": 0.45, "ams": 0.15},
        source="NEEDS RESEARCH — pull current Yole / Dell'Oro RAN semiconductor BOM figures",
        as_of="",
        confidence="needs_research",
    ),

    "industrial_automation": VerticalBenchmark(
        vertical="Industrial — factory automation / robotics",
        unit_basis="units produced (PLC, drive, or robot controller)",
        semi_content_usd_low=None, semi_content_usd_high=None,
        segment_split={"power": 0.30, "ep": 0.30, "hpc": 0.05, "conn": 0.15, "ams": 0.20},
        source="NEEDS RESEARCH — segment split is directional only, not yet benchmark-sourced",
        as_of="",
        confidence="directional",
    ),
}


# ---------------------------------------------------------------------------
# Vertical matching (cheap heuristic — the LLM classification step is the
# real source of truth; this just gives it a starting hint / fallback)
# ---------------------------------------------------------------------------

_VERTICAL_KEYWORDS: dict[str, list[str]] = {
    "auto_ev":                    ["electric vehicle", "ev ", "battery electric", "bev", "plug-in hybrid"],
    "auto_ice":                   ["automotive", "vehicle", "ice engine", "internal combustion"],
    "datacenter_ai_server":       ["ai server", "gpu cluster", "ai accelerator", "ai data center", "ai infrastructure"],
    "datacenter_general_server":  ["data center", "datacenter", "server", "cloud infrastructure"],
    "telecom_infra":               ["base station", "ran ", "radio access network", "telecom infrastructure", "5g network"],
    "industrial_automation":      ["factory automation", "industrial robot", "plc ", "motor drive", "industrial control"],
}


def guess_vertical(industry_verticals: list[str], summary: str) -> Optional[str]:
    """
    Cheap keyword fallback for when the LLM classification step doesn't
    return a confident vertical_key. Not a substitute for LLM judgment —
    just prevents losing the estimate entirely on a borderline case.
    """
    text = (" ".join(industry_verticals) + " " + (summary or "")).lower()
    for vertical_key, keywords in _VERTICAL_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return vertical_key
    return None


# ---------------------------------------------------------------------------
# SEC SIC code → vertical hints.
#
# SEC's own filer classification (SIC code), fetched via EDGAR's submissions
# endpoint — an audited, structured fact, not an LLM guess. Deliberately
# NOT a 1:1 mapping to a single vertical_key: SIC codes are coarser than
# our taxonomy. SIC 3711 ("Motor Vehicles & Passenger Car Bodies") covers
# EV and ICE automakers alike, and SIC 3571 ("Electronic Computers") covers
# both AI-server and general-purpose-server makers — SIC classification
# can't make either distinction by itself. Each entry below lists the
# CANDIDATE verticals a code typically implies; llm_enricher.py feeds this
# to the classification prompt as an anchor fact ("SEC classifies this
# filer as X, which usually means one of Y") and still relies on the
# model + search context to pick the actual one, or reject all candidates.
#
# Coverage is a deliberate first pass, not comprehensive — SIC has ~1,000
# codes; only ones plausibly relevant to the current six verticals are
# mapped. Extend as new target companies surface SIC codes not covered
# here, the same way BENCHMARKS itself is meant to grow.
# ---------------------------------------------------------------------------
SIC_VERTICAL_HINTS: dict[str, dict] = {
    "3711": {"description": "Motor Vehicles & Passenger Car Bodies",
             "candidates": ["auto_ice", "auto_ev"]},
    "3714": {"description": "Motor Vehicle Parts & Accessories",
             "candidates": ["auto_ice", "auto_ev"]},
    "3571": {"description": "Electronic Computers",
             "candidates": ["datacenter_ai_server", "datacenter_general_server"]},
    "3572": {"description": "Computer Storage Devices",
             "candidates": ["datacenter_ai_server", "datacenter_general_server"]},
    "3663": {"description": "Radio & TV Broadcasting & Communications Equipment",
             "candidates": ["telecom_infra"]},
    "3669": {"description": "Communications Equipment, NEC",
             "candidates": ["telecom_infra"]},
    "3559": {"description": "Special Industry Machinery, NEC",
             "candidates": ["industrial_automation"]},
    "3625": {"description": "Relays & Industrial Controls",
             "candidates": ["industrial_automation"]},
    "3823": {"description": "Industrial Instruments for Measurement, Display, "
                             "and Control of Process Variables",
             "candidates": ["industrial_automation"]},
}


def sic_vertical_hint(sic_code: Optional[str]) -> Optional[dict]:
    """
    Looks up SIC_VERTICAL_HINTS for a given SIC code. Returns None if the
    code is missing or not one of the (deliberately partial) mapped codes
    — a miss here just means "no SIC-based hint available," not "this
    company doesn't fit any vertical."

    Defensive about formatting: EDGAR sources return SIC codes as plain
    strings, sometimes without leading zeros, so normalize before lookup
    rather than assume one exact format.
    """
    if not sic_code:
        return None
    raw = str(sic_code).strip()
    code = raw.lstrip("0") or "0"
    for candidate in (raw, code, code.zfill(4)):
        if candidate in SIC_VERTICAL_HINTS:
            return SIC_VERTICAL_HINTS[candidate]
    return None


# ---------------------------------------------------------------------------
# Segment SAM allocation (pure math — no LLM involved)
# ---------------------------------------------------------------------------

def estimate_segment_sam(
    vertical_key: Optional[str],
    unit_volume: Optional[float],
    reasoning: Optional[str] = None,
) -> list[dict]:
    """
    Always returns exactly 5 entries (one per segment in SEGMENTS), even
    when we can't compute a number — the "empty" entries carry a
    benchmark_confidence of "needs_research" and a rationale explaining
    why, so downstream viewers can render a clear "no estimate yet" state
    per segment instead of silently omitting it.
    """
    if not vertical_key or vertical_key not in BENCHMARKS:
        return [
            {
                "segment": seg,
                "estimated_sam_low_usd_m": None,
                "estimated_sam_high_usd_m": None,
                "unit_basis": "",
                "benchmark_source": "No matching vertical benchmark",
                "benchmark_confidence": "needs_research",
                "rationale": reasoning or "Account profile did not clearly match a benchmarked vertical.",
            }
            for seg in SEGMENTS
        ]

    b = BENCHMARKS[vertical_key]

    if not unit_volume or b.semi_content_usd_low is None or b.semi_content_usd_high is None:
        return [
            {
                "segment": seg,
                "estimated_sam_low_usd_m": None,
                "estimated_sam_high_usd_m": None,
                "unit_basis": b.unit_basis,
                "benchmark_source": b.source,
                "benchmark_confidence": "needs_research",
                "rationale": (
                    "No disclosed annual unit volume found for this account."
                    if unit_volume is None or unit_volume == 0
                    else "No sourced semiconductor-content benchmark yet for this vertical."
                ),
            }
            for seg in SEGMENTS
        ]

    total_low_usd  = unit_volume * b.semi_content_usd_low
    total_high_usd = unit_volume * b.semi_content_usd_high

    results = []
    for seg in SEGMENTS:
        pct = b.segment_split.get(seg, 0.0)
        results.append({
            "segment": seg,
            "estimated_sam_low_usd_m":  round(total_low_usd  * pct / 1_000_000, 1),
            "estimated_sam_high_usd_m": round(total_high_usd * pct / 1_000_000, 1),
            "unit_basis": f"{unit_volume:,.0f} {b.unit_basis} × {pct:.0%} segment allocation",
            "benchmark_source": b.source,
            "benchmark_confidence": b.confidence,
            "rationale": reasoning,
        })
    return results