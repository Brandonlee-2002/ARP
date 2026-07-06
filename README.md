# Account research pipeline

Automated company research for semiconductor account managers. Given a company name, domain, and optional stock ticker, the pipeline produces a structured JSON record and a human-readable Markdown one-pager covering headquarters, office locations, stakeholder map, financials, and account intelligence signals.

---

## How it works

```
Web search (14 queries)
        │
        ├──► LLM extraction ──► Pydantic validation ──► AccountResearchRecord
        │         (6 sections)                                    │
SEC EDGAR ──────────────────────────────────────────►   JSON + Markdown summary
(audited financials, public companies only)
```

Four stages run in sequence, each independently fault-tolerant:

1. **Web search** — fires 14 targeted queries across topics (overview, locations, stakeholders, financials, semiconductor profile, news) using Serper, Brave Search, or SerpAPI.
2. **SEC EDGAR** — for US-listed companies, fetches audited 10-K / 20-F financials directly from EDGAR's public XBRL API. No API key required.
3. **LLM enrichment** — Claude makes one focused call per schema section, returning JSON. EDGAR numbers override LLM estimates where both exist.
4. **Assemble & validate** — merges all sources into a typed `AccountResearchRecord`, scores confidence per section, and writes output.

---

## Output

Each run produces two files in `./output/`:

```
output/
  intel_corporation_20260624_143201.json           # full structured record
  intel_corporation_20260624_143201_summary.md     # one-pager for deal review / CRM
```

The JSON record follows a versioned schema (`v1.0.0`) with these top-level sections:

| Section | What it contains |
|---|---|
| `meta` | Schema version, timestamps, per-section confidence scores, source citations |
| `overview` | Legal name, ticker, founding year, headcount, industry verticals, summary |
| `locations` | HQ, fabs, R&D centers, sales offices — typed and status-tagged |
| `stakeholders` | Decision-makers with role type, influence score, and buying relevance notes |
| `economics` | Revenue, margins, R&D spend, capex, segment breakdown, semiconductor profile |
| `account_intelligence` | Opportunity signals, risks, watch items, recommended next action |
| `news` | Up to 10 recent items tagged by topic |

---

## Quickstart

### 1. Install dependencies

```bash
pip install anthropic httpx pydantic
```

### 2. Set credentials

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export SEARCH_API_KEY="your-key-here"
export SEARCH_PROVIDER="serper"        # serper | brave | serpapi
```

Serper (`serper.dev`) is recommended — roughly $0.001 per query, so a full pipeline run costs under $0.02 in search fees.

### 3. Run

```bash
# Public company with EDGAR financials
python run_research.py --company "Intel Corporation" --domain intel.com --ticker INTC

# Foreign issuer (EDGAR lookup skipped; LLM estimates financials from web)
python run_research.py --company "ASML Holding" --domain asml.com --ticker ASML

# Private company (no ticker)
python run_research.py --company "Arm Holdings" --domain arm.com

# Custom output dir and daily refresh cadence
python run_research.py \
  --company "Marvell Technology" \
  --domain marvell.com \
  --ticker MRVL \
  --output ./accounts \
  --cadence daily
```

### 4. Use as a library

```python
import anthropic
from account_research import AccountResearchPipeline

pipeline = AccountResearchPipeline(
    anthropic_client=anthropic.Anthropic(api_key="sk-ant-..."),
    search_api_key="your-key",
    search_provider="serper",
)

record = pipeline.run(
    company_name="Intel Corporation",
    domain="intel.com",
    ticker="INTC",
)

# Access typed fields directly
print(record.overview.legal_name)
print(record.economics.financials.revenue_usd_m)
print(record.meta.confidence.overall)

# Top stakeholder by influence
top = max(record.stakeholders, key=lambda s: s.influence_score)
print(f"{top.name} ({top.role_type})")

# Save to disk
pipeline.save(record, output_dir="./output")
```

---

## CLI reference

```
python run_research.py [options]

Required:
  --company TEXT    Company legal name, e.g. "Intel Corporation"
  --domain TEXT     Company website domain, e.g. intel.com

Optional:
  --ticker TEXT     Stock ticker for EDGAR lookup, e.g. INTC
                    Omit for private companies or foreign issuers
                    not cross-listed on US exchanges.
  --output DIR      Output directory (default: ./output)
  --cadence         daily | weekly | monthly | on-demand (default: weekly)
  --formats TEXT    Comma-separated: json,summary (default: both)
```

---

## Project structure

```
account_research/
├── run_research.py          # CLI entry point
├── requirements.txt
├── __init__.py
│
├── models/
│   └── schema.py            # Pydantic models — AccountResearchRecord and all sub-types
│
├── extractors/
│   ├── web_search.py        # Multi-query web search (Serper / Brave / SerpAPI)
│   ├── edgar.py             # SEC EDGAR XBRL financials (free, no key)
│   └── llm_enricher.py      # Section-by-section Claude extraction
│
└── pipeline/
    ├── pipeline.py          # Orchestrator, output writer, Markdown renderer
    └── assembler.py         # Merge + validate all sources, score confidence
```

---

## Search providers

| Provider | Sign up | Cost per query | Notes |
|---|---|---|---|
| Serper | [serper.dev](https://serper.dev) | ~$0.001 | Recommended. Fast, cheap, reliable. |
| Brave Search | [brave.com/search/api](https://brave.com/search/api) | Free tier available | Good for privacy-conscious deployments. |
| SerpAPI | [serpapi.com](https://serpapi.com) | ~$0.005 | More structured results, higher cost. |

Set `SEARCH_PROVIDER` to `serper`, `brave`, or `serpapi` — the extractor handles the different request formats automatically.

---

## Confidence scoring

Each section in the output record carries a confidence rating (`high`, `medium`, `low`). The overall record confidence is the weakest section's score.

| Score | Meaning |
|---|---|
| `high` | Data comes from audited filings (EDGAR) or multiple corroborating web sources |
| `medium` | Data partially populated, from web snippets only, or LLM-estimated |
| `low` | Section mostly empty or company information is sparse / private |

For private companies, `economics` will typically be `low` or `medium`. Stakeholders sourced only from web search are `medium` by default.

---

## Semiconductor-specific fields

The `economics.semiconductor_profile` section is purpose-built for semiconductor account managers and has no equivalent in generic CRM records:

```json
{
  "chip_buyer_type": "idc",
  "has_own_fab": true,
  "fab_nodes": ["Intel 18A", "Intel 3"],
  "known_chip_suppliers": ["TSMC", "UMC"],
  "known_supply_gaps": ["Advanced packaging", "GaN power"],
  "qualification_cycle": "HBM4 qualification opens H2 2026",
  "annual_chip_spend_est_usd_m": 4200
}
```

`chip_buyer_type` values:

| Value | Meaning |
|---|---|
| `fabless` | Designs chips but uses external foundries entirely |
| `fab_lite` | Has some fabs but sources significant volume externally |
| `idc` | Integrated device manufacturer — designs and fabs own chips |
| `ems` | Electronics manufacturing services — builds others' designs |
| `oem_buyer_only` | Buys finished chips; no chip design capability |

---

## Extending the pipeline

**Add a new data source** — implement a new extractor in `extractors/`, call it from `pipeline.py` in the run sequence, and pass its output to `assembler.py`. The assembler's merge logic follows the pattern: structured source > LLM estimate > null.

**Add a new schema field** — add the field to the relevant Pydantic model in `models/schema.py`, add it to the corresponding LLM prompt in `llm_enricher.py`, and handle it in `assembler.py`.

**Push to CRM** — call `record.model_dump()` after `pipeline.run()` and map the dict to your CRM's API fields. The schema is designed to map cleanly to Salesforce Account + Contact objects.

**Batch multiple accounts** — `AccountResearchPipeline` is stateless; instantiate once and call `run()` in a loop or thread pool.

```python
from concurrent.futures import ThreadPoolExecutor

targets = [
    ("Intel Corporation", "intel.com", "INTC"),
    ("ASML Holding",      "asml.com",  "ASML"),
    ("Marvell Technology","marvell.com","MRVL"),
]

with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [
        pool.submit(pipeline.run, name, domain, ticker)
        for name, domain, ticker in targets
    ]
    records = [f.result() for f in futures]
```

---

## Requirements

- Python 3.11+
- `anthropic >= 0.25.0`
- `httpx >= 0.27.0`
- `pydantic >= 2.0.0`
- A search API key (Serper, Brave, or SerpAPI)
- An Anthropic API key
