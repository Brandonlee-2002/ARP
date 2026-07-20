# Account research pipeline

Automated company research for semiconductor account managers. Given a company name, domain, and optional stock ticker, the pipeline produces a structured JSON record and a human-readable Markdown one-pager covering headquarters, office locations, stakeholder map, financials, and account intelligence signals.

**This project ships as two independent, standalone implementations — you only need one, not both:**

| | Python pipeline (this README, below) | `app.jsx` ([details](#browser-based-interface-appjsx)) |
|---|---|---|
| **Best for** | People comfortable with a terminal | Anyone — no setup required |
| **Runs** | From the command line | In the browser, as a self-contained artifact |
| **Setup** | Install deps, set API keys as env vars | None — open it and go |
| **Output** | JSON + Markdown files on disk | In-app dashboard, editable, downloadable HTML |
| **Good for** | Scripting, batch runs, CRM pipelines | Quick, one-off, interactive research |

They produce the same kind of research output but share no code — pick whichever fits how you want to work.

---

## Changelog

### v1.1.1 — 2026-07-20

**Fixed**
- `llm_enricher.py`'s `_call()` used a plain `json.loads()` with no defense beyond stripping markdown fences. If Claude appended any trailing content after the JSON closed (or occasionally leading preamble beyond a fence), parsing failed outright. Replaced with a balanced-bracket scanner (`_extract_json()`) that finds exactly where the JSON value ends — correctly skipping over brackets and quotes inside string literals — and ignores anything before or after it. Ported from the same fix in `app.jsx`, where this exact failure mode was first observed and reproduced.
- Raised `MAX_TOKENS` from 2048 to 4096 across all six section calls, as a defensive margin against truncation on data-heavy responses (e.g. a company with many locations or stakeholders). Note: this repo's calls are already split one-section-per-call, so the specific *combined-schema* truncation bug found in `app.jsx` (where Overview + Locations shared one 2000-token response) doesn't structurally apply here — this is a precautionary increase, not a confirmed-bug fix like the one above.

### v1.1.0 — 2026-07-16

**Added**
- **People search integration (People Data Labs)** — new optional `extractors/people_search.py` module. When `PDL_API_KEY` is set, a new Stage 3 looks up verified decision-maker contacts (name, title, LinkedIn, email) before LLM enrichment. These verified contacts anchor the stakeholder-extraction prompt — Claude uses their real names/titles instead of inferring purely from search snippets. Fully optional and backward-compatible: if `PDL_API_KEY` is unset, this stage is skipped and behavior is identical to v1.0.0.
- **`verified` field on `Stakeholder`** (schema) — boolean, defaults to `false`. True only when a stakeholder was matched against a structured source (currently PDL) by name, rather than purely LLM-inferred. The match — and any resulting override of `linkedin_url`/`email` — happens in `assembler.py`, not by trusting the LLM's own say-so.
- Verified PDL contacts the LLM fails to surface are still included via a safety net in the assembler, with a conservative default role (`influencer`, 0.4 influence) rather than being silently dropped.
- `meta.sources` now includes a `"People Data Labs"` entry whenever the people-search stage runs.

**Changed**
- Stakeholder confidence scoring: now also reaches `HIGH` when 2+ verified contacts are present, not only when 3+ total (previously unverified-vs-verified counted the same).
- `_render_summary()`: stakeholder lines show a ✓ marker for verified contacts.
- Pipeline stage count/logging: 4 stages → 5 (Web search → EDGAR → People search → LLM enrich → Assemble).

**Fixed**
- `assembler.py` import bug: `EdgarFinancials` was imported from `models.schema` (which never defined it) immediately before being correctly re-imported from `extractors.edgar`. The first import raised `ImportError` on every run before ever reaching the second — pre-existing, unrelated to the people-search work, caught while wiring in the new extractor.

### v1.0.0 — initial release

Four-stage pipeline (web search → EDGAR → LLM enrichment → assemble), JSON + Markdown output, confidence scoring.

---

## Browser-based interface (app.jsx)

The more user-friendly of the two options — no terminal, no installation, no API keys to manage yourself. A parallel, self-contained implementation of this same research capability exists as a single-file React artifact (`app.jsx`). It calls the Claude API directly from the browser using its own `web_search` tool calls, rather than this repo's Python backend (Serper/Brave/SerpAPI + EDGAR). **It does not use `account_research/`** — it's a separate, independent codebase solving the same problem through a different interface, intended for quick, no-install research sessions inside a Claude Project or artifact rather than a scripted/scheduled pipeline run.

### Features

- The same four-stage research flow (overview + locations → economics → stakeholders → account intelligence), rendered as a dashboard of saved accounts with a sidebar and a tabbed results view.
- **Manual editing** — every section except revenue segments and news is editable in place (Edit → Apply changes → Save), with add/remove for locations, stakeholders, and opportunity signals.
- **Delete accounts** — click-to-arm confirmation rather than a native `confirm()` dialog, since those are frequently blocked inside the sandboxed iframe artifacts run in.
- **Re-saving a company you've already researched replaces the old profile** instead of creating a duplicate — matched by domain, not by the internal record ID (which is always freshly generated per run).
- **Download report** — generates a standalone HTML file with a filesystem-safe name derived from company + date; open it and use your browser's Ctrl+P → Save as PDF to get an actual PDF.

### Recent fixes

- **JSON parsing**: `web_search`-enabled responses sometimes have Claude append a trailing sentence after the JSON closes (e.g. "I found these on the investor relations page"). The previous `JSON.parse` + greedy-regex fallback failed outright when this happened. Replaced with a balanced-bracket scanner that finds exactly where the JSON value ends — correctly skipping over brackets and quotes inside string literals — and ignores anything Claude appends afterward.
- **Response truncation**: the Overview and Account Intelligence calls each bundle several schema sections into a single response, but were capped at the same token limit as the smaller calls. Raised individually for those two after both were observed truncating mid-JSON on data-heavy companies.
- **"New research" no longer discards progress mid-run** — clicking it while a pipeline is actively executing now shows the live stage tracker instead of hiding it behind a blank form. (Once a run finishes, the app already force-navigates to the results view regardless of where you're browsing; this only closes the gap during the in-progress window.)
- **PDF download was silently producing an empty, misnamed file** (`undefined.html`, no content) due to an unreachable code path in the report generator. Fixed and verified by actually executing the function, not just checking that it parses.
- **Ticker was accidentally non-editable** in edit mode — grouped in with the (correctly) read-only Domain/Website fields despite having no bearing on how a saved account is matched on re-save. Split into separate Symbol/Exchange fields; clearing both reverts the company to "Private" rather than leaving a hollow object behind.

### Known limitations

- No search/filter box for saved accounts, and no one-click "re-run research for this account" button. Both were prototyped and then removed while troubleshooting an unrelated issue, and haven't been reintroduced since — manual editing was the one feature from that round of work that was kept.
- Revenue segments and the News tab are read-only even in edit mode.
- No timeout or elapsed-time indicator on individual API calls, so a slow-but-working response (e.g. researching a company with a thin public semiconductor footprint) currently looks identical to a hung one.

---

## How it works

The more technical of the two options — runs from a terminal, needs your own API keys, and writes output to disk. Good fit if you're comfortable with the command line and want scripting/batch-run flexibility.

```
Web search (14 queries)
        │
        ├──► LLM extraction ──► Pydantic validation ──► AccountResearchRecord
        │         (6 sections)                                    │
SEC EDGAR ──────────────────────────────────────────►   JSON + Markdown summary
(audited financials, public companies only)         ▲
                                                      │
People Data Labs ─────────────────────────────────────┘
(verified stakeholder contacts, optional — requires PDL_API_KEY)
```

Five stages run in sequence, each independently fault-tolerant:

1. **Web search** — fires 14 targeted queries across topics (overview, locations, stakeholders, financials, semiconductor profile, news) using Serper, Brave Search, or SerpAPI.
2. **SEC EDGAR** — for US-listed companies, fetches audited 10-K / 20-F financials directly from EDGAR's public XBRL API. No API key required.
3. **People search** *(optional)* — if `PDL_API_KEY` is set, looks up verified decision-maker contacts (name, title, LinkedIn, email) via People Data Labs. Skipped entirely if the key isn't set.
4. **LLM enrichment** — Claude makes one focused call per schema section, returning JSON. EDGAR numbers override LLM estimates where both exist; verified PDL contacts anchor the stakeholder prompt where available.
5. **Assemble & validate** — merges all sources into a typed `AccountResearchRecord`, scores confidence per section, and writes output. Verified contacts are matched by name and flagged `verified: true`; any the LLM drops are still included via a safety net.

---

## Output

Each run produces two files in `./output/`:

```
output/
  intel_corporation_20260624_143201.json           # full structured record
  intel_corporation_20260624_143201_summary.md     # one-pager for deal review / CRM
```

The JSON record follows a versioned schema (`v1.1.0`) with these top-level sections:

| Section | What it contains |
|---|---|
| `meta` | Schema version, timestamps, per-section confidence scores, source citations |
| `overview` | Legal name, ticker, founding year, headcount, industry verticals, summary |
| `locations` | HQ, fabs, R&D centers, sales offices — typed and status-tagged |
| `stakeholders` | Decision-makers with role type, influence score, buying relevance notes, and a `verified` flag (true when matched against People Data Labs rather than purely LLM-inferred) |
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
export PDL_API_KEY="your-pdl-key"      # optional — enables verified stakeholder contacts
```

Serper (`serper.dev`) is recommended — roughly $0.001 per query, so a full pipeline run costs under $0.02 in search fees.

`PDL_API_KEY` is optional. [People Data Labs](https://www.peopledatalabs.com) offers a free tier (1,000 records/month, no credit card) that's normally enough for regular use. Without it, the pipeline behaves exactly as in v1.0.0 — stakeholders are inferred entirely from web search snippets.

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
│   ├── people_search.py     # People Data Labs verified contacts (optional, free tier)
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

**Add a new data source** — implement a new extractor in `extractors/`, call it from `pipeline.py` in the run sequence, and pass its output to `assembler.py`. The assembler's merge logic follows the pattern: structured source > LLM estimate > null. `extractors/people_search.py` is a working example of this pattern — see how `pipeline.py` treats it as fully optional (skipped if `PDL_API_KEY` is unset) and how `assembler.py`'s `_build_stakeholders()` merges its output by name.

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