# Account research pipeline

Automated company research for semiconductor account managers. Given a company name, domain, and optional stock ticker, the pipeline produces a structured JSON record and a human-readable Markdown one-pager covering headquarters, office locations, stakeholder map, financials, semiconductor segment SAM estimates, and account intelligence signals.

**This project ships as two independent, standalone implementations — you only need one, not both:**

| | Python pipeline (this README, below) | `app.jsx` ([details](#browser-based-interface-appjsx)) |
|---|---|---|
| **Best for** | People comfortable with a terminal | Anyone — no setup required |
| **Runs** | From the command line | In the browser, as a self-contained artifact |
| **Setup** | Install deps, set API keys as env vars | None — open it and go |
| **Output** | JSON + Markdown files on disk | In-app dashboard, editable, downloadable HTML |
| **Good for** | Scripting, batch runs, CRM pipelines | Quick, one-off, interactive research |

They produce the same kind of research output but share no code — pick whichever fits how you want to work.

There's also a third, smaller artifact — `research_record_viewer.jsx` — which isn't an alternative to either of the above but a companion to the Python pipeline: see [Companion viewer artifact](#companion-viewer-artifact-research_record_viewerjsx).

---

## Changelog

### v1.3.0 — 2026-07-22

**Added — `patent_search.py` wired into the pipeline as Stage 5 of 7**
- New optional stage (requires `USPTO_API_KEY`; skipped cleanly if unset, same pattern as PDL/FCC). Runs after FCC facility lookup, before LLM enrichment.
- **Stakeholder handling deliberately weaker than PDL, by design**: a patent inventor match on an existing stakeholder appends a corroborating note (patent count, segments, most recent grant date) — it never sets `verified=True` (reserved for PDL's current-employment signal) and, unlike PDL, an unmatched patent inventor is **never** added as a new stakeholder record. A patent proves someone invented something at some point, not that they still work there; presenting a possibly-years-stale inventor as an active contact would overclaim currency the data doesn't support. Their value surfaces instead through the signal below.
- Patent inventors also anchor the stakeholder-extraction LLM prompt as candidates, with framing explicitly distinct from the PDL block: the model is told a patent inventor may only become a stakeholder if the source text *also* corroborates current employment — not from the patent list alone.
- Grounding pass (`llm_enricher.py`) extended to treat a patent-inventor name as grounded even if it doesn't appear in web-search text, same reasoning as the earlier PDL grounding fix.
- **New deterministic `account_intelligence` signal**: one `watch`-type `OpportunitySignal` per segment with patent activity (e.g. "14 granted patent(s) in Embedded Processing..."), built from `segment_patent_activity()`'s exact counts in plain Python — not summarized by the LLM, so there's no risk of the model restating a number we already have exactly right. Explicitly labeled as a raw count, not a trend (no year-over-year baseline is computed).
- `meta.sources` gets a `"USPTO Open Data Portal (granted patents)"` entry when inventors are found (same "found, not merely ran" caveat as the PDL/FCC sources — still an open follow-up, not fixed here).

**Verified before merging, not just read-through**
- `_match_patent_inventor`'s name matching (exact + surname fallback, case-insensitive) tested in isolation against real inventor names from the Intel dataset, including a case-difference match, a surname-only fallback match, and confirming a `(name not found)` placeholder never matches.
- The stakeholder note-formatting string tested against expected output.
- `segment_patent_activity()`'s dedup-by-patent-number confirmed with a synthetic multi-co-inventor patent — a shared patent contributes to the segment count once, not once per co-inventor. This matters in practice: Intel's real patents frequently have 4–5 co-inventors each, so without this dedup the signal's counts would be substantially inflated.

### v1.2.5 — 2026-07-22

**Fixed — every extractor, not just patent_search.py**
- All five HTTP-calling extractors (`edgar.py`, `web_search.py`, `people_search.py`, `fcc_search.py`, `patent_search.py`) failed with `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain` on any network that does TLS inspection (corporate proxies like Zscaler, Palo Alto) — confirmed on a real corporate network, not theoretical. Root cause: `httpx` verifies against its own bundled `certifi` CA list by default, which has no knowledge of a corporate root CA that PowerShell/the OS already trusts. Every `httpx.Client(...)` construction (and the three direct `httpx.get`/`httpx.post` calls in `web_search.py`, which don't use a persistent client) now passes `verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`, which validates against the OS certificate store instead — the same approach `pip` itself has used by default since v24.2. See [A note on corporate networks](#a-note-on-corporate-networks).
- New dependency: `truststore >= 0.9.0`, added to `requirements.txt`. Not optional — every extractor now requires it at import time.

**Verified**
- `patent_search.py` re-run end-to-end against real USPTO data after this fix and the earlier pagination/CPC-whitespace/postal-vs-residence fixes: 426 distinct Intel Corporation inventors returned over a 5-year window, with segment tags (`conn`, `ep`, `hpc`, `ams`) correctly appearing on CPC-matched patents and non-US locations (France, Israel) correctly showing no state. This is the first real-scale validation of the extractor, not just the earlier 2-patent spot check.
- **Gotcha found and resolved during this verification**: `pip_system_certs` (recommended earlier as a zero-code-change stopgap before this fix existed) and this code's own explicit `truststore.SSLContext(...)` usage conflict when both are installed — `pip_system_certs` does its own global `truststore`-based patching, and the two collide into `RecursionError: maximum recursion depth exceeded`. This is a known interaction (matches a currently-open `pip-system-certs` issue describing the identical symptom and fix). Resolution: `pip uninstall pip_system_certs` once this pipeline's own fix is in place — the global patch is redundant and actively harmful once the code handles it directly. Re-ran the exact same 426-inventor test afterward to confirm identical output with only `truststore` installed, confirming the fix doesn't depend on the stopgap package at all.

### v1.2.4 — 2026-07-22

**Fixed — `extractors/patent_search.py`, all confirmed against a real live response (Intel Corporation search)**
- **Request body was rejected with 400 on every query, including the simplest possible one.** Root cause: `limit`/`offset` were sent as top-level JSON keys. USPTO's actual schema (confirmed against their published `ODP-API-Query-Spec.pdf`) requires them nested under `"pagination": {"offset": ..., "limit": ...}` — there is no partial credit here, a top-level `limit` key invalidates the whole request. `sort` also needed to be an array of `{"field", "order"}` objects, not the bare string previously sent.
- **CPC segment tagging was silently broken for every patent.** Real CPC codes come back with inconsistent internal whitespace (`"G06F   9/30032"`, three spaces; `"H04N  21/438"`, two spaces), so `CPC_SEGMENT_MAP` prefix matching (e.g. `"G06F9"`) never matched anything. Fixed by normalizing (stripping all whitespace) on both sides before comparing — verified against the real response that a G06F-classified patent now correctly tags `ep`.
- **`_parse_inventor_location` was written but never actually called** — `_aggregate_by_inventor` left every inventor's city/state/country as `None` with a comment admitting the gap. Now wired through end-to-end: `_parse_record` builds structured per-inventor dicts (name + location) instead of bare name strings, and the aggregator carries location through.
- **Confirmed a real, not hypothetical, postal-vs-residence mismatch**: one inventor in the test data has a "postal" (correspondence) address in Chandler, AZ but a "residence" address in Gilbert, AZ. `_parse_inventor_location` now explicitly prefers `residence` — the stronger signal for where someone actually is — and this was verified to resolve correctly against that exact example, not just written to look plausible.
- Root-caused a `403 Forbidden` that had nothing to do with the API key: USPTO's ODP sits behind AWS WAF, which blocks based on User-Agent *before* the API key is ever checked (confirmed via AWS's own docs on WAF evaluation order). PowerShell's and `httpx`'s default User-Agent strings both trigger this. `PatentSearchExtractor`'s client now always sends an ordinary browser User-Agent.

**Verified, not just read-through**: the parser fixes above were run against the exact real API response obtained during debugging (reconstructed as test input), not just checked by eye — confirmed correct inventor/location/segment output before being called done.

**Still not wired into `pipeline.py`** — the extractor itself is now confirmed working end-to-end; wiring it in as a stage (stakeholder-candidate feed + `account_intelligence` patent-activity signal, per the original design) is the next step whenever you're ready for it.

### v1.2.3 — 2026-07-22

**Fixed**
- **USPTO ODP calls were silently blocked by AWS WAF based on User-Agent, regardless of API key validity.** USPTO's ODP sits behind AWS WAF, which evaluates its own rules (including User-Agent-based bot detection) *before* the API key/authorizer is ever checked. `httpx`'s default `User-Agent` header (`python-httpx/x.y.z`) reads as a script rather than a browser and got hard-blocked with a bare `{"message": "Forbidden"}` — indistinguishable from an invalid or unauthorized key. Confirmed via manual testing: the identical request succeeded once a normal browser User-Agent was set, and 403'd every time with any script-like default. `PatentSearchExtractor`'s `httpx.Client` now sets an ordinary browser User-Agent by default. This cost real debugging time chasing key/account-authorization theories before the actual cause (a firewall rule unrelated to authorization) was found — worth remembering for any future extractor added against USPTO or similarly WAF-fronted infrastructure.
- Documented in `patent_search.py`'s module docstring, including the reminder not to strip the User-Agent header back out as a "cleanup."

### v1.2.2 — 2026-07-21

**Added**
- **FCC facility lookup wired in as Stage 4** (of 6 total now) — always runs,
  no API key required. Queries FCC's Socrata-hosted equipment authorization
  grantee registrations (`extractors/fcc_search.py`) for the target company
  and merges results into `locations`: a facility whose city matches an
  existing (LLM-derived) location gets a corroborating note rather than a
  duplicate entry; one with no match gets added as a new `Location` with
  `type` defaulted to `design_center` and an explicit note that the type is
  *inferred, not confirmed* — FCC data verifies a registered address for
  RF-emitting products, not what the facility actually is.
- `_score_confidence` now reaches `HIGH` on locations when even a single
  FCC-corroborated address is present, not just on raw location count —
  same reasoning EDGAR already gets for `economics` (an independent
  government record outweighs having more LLM-inferred entries).
- `meta.sources` gets an `"FCC Equipment Authorization (grantee
  registrations)"` entry whenever facilities are found (see known gap below).
- Optional `FCC_APP_TOKEN` env var — raises the Socrata rate limit; not
  required for normal use.

**Known gap carried over from this addition**
- The FCC source-record addition only fires when facilities were actually
  *found*, not merely whenever the stage *ran* — for the (common) case of a
  company with no RF/wireless products, no source entry gets added, even
  though the lookup did happen. This mirrors an identical, pre-existing
  inconsistency in the PDL source-record logic. Fixing both properly means
  threading an explicit "did this stage run" boolean through from
  `pipeline.py` into `assembler.py` for both stages — left as a follow-up
  rather than fixed asymmetrically for just one.

**Not yet wired**
- `extractors/patent_search.py` is now confirmed working against a real
  USPTO key (see v1.2.3 above) but still not connected to `pipeline.py` —
  field-name verification against a live response is the next step before
  wiring it in as a stage.

### v1.2.1 — 2026-07-20

This entry closes out work that earlier entries described but hadn't actually landed in code yet — see each bullet for what changed vs. what was already true.

**Fixed**
- **`assembler.py` reintroduced the exact `EdgarFinancials` double-import bug that v1.1.0 already fixed once** — a bad edit re-added `EdgarFinancials` to the `from ..models.schema import (...)` block (it's never been defined there; it lives in `extractors/edgar.py`), which would have broken every run again. Removed, with a comment in the file now explaining why it must stay removed.
- **PDL-anchored stakeholder names were incorrectly getting wiped by the v1.2.0 grounding pass.** The grounding check verifies a name appears in source text before trusting it — but a name the LLM correctly copied from a verified PDL contact often won't appear anywhere in the raw web-search snippets at all, since it came from the PDL block in the prompt, not from search text. `extractors/llm_enricher.py`'s `_extract_stakeholders` now includes the PDL contact names in the grounding source text, so a genuinely PDL-sourced name is recognized as grounded instead of being replaced with a placeholder.
- Ported the balanced-bracket JSON scanner (`_extract_json`) from `app.jsx` into `llm_enricher.py`'s `_call()`, replacing the plain `json.loads()` + fence-stripping that v1.1.1 documented as fixed but that was never actually implemented here. Same fix as `app.jsx`: finds exactly where the JSON value ends, skipping over brackets/quotes inside string literals, ignoring anything Claude appends afterward.
- Raised `MAX_TOKENS` from 2048 to 4096 in `llm_enricher.py` (v1.1.1 also documented this without it being applied).

**Added**
- **The actual PDL merge logic** in `assembler.py`'s `_build_stakeholders()` — matching v1.1.0's description, now implemented: LLM-extracted stakeholders are matched against PDL contacts by normalized name (exact match, then surname fallback); a match sets `verified=True` and overrides `linkedin_url`/`email` from the structured PDL record rather than trusting whatever the LLM inferred (which in practice is always `null`, since the prompt never asks it to guess contact details). Any verified PDL contact the LLM never surfaced at all is still added, with a conservative default role (`influencer`, 0.4 influence) rather than silently dropped.
- Stakeholder confidence scoring now actually reaches `HIGH` on 2+ verified contacts (previously just described in the v1.1.0 changelog, not implemented in `_score_confidence`).
- `pipeline.py` now actually instantiates and calls `PeopleSearchExtractor` as Stage 3 — `from_env()` reads `PDL_API_KEY` (optional) and skips the stage cleanly if unset. `meta.sources` gets a `"People Data Labs"` entry whenever the stage runs, whether or not it finds contacts.
- `_render_summary()` shows a ✓ marker next to verified stakeholder names and a "(N verified ✓)" note in the stakeholder-map header.

### v1.2.0 — 2026-07-20

**Added**
- **Segment SAM estimates** — new `economics.segment_sam_estimates` field and `extractors/segment_benchmarks.py` module. Estimates semiconductor SAM for a target account across five product segments — Power, Embedded Processing (EP), High-Performance Compute (HPC), Connectivity (CONN), and Analog & Mixed-Signal (AMS) — for **accounts being sold TO** (customers/OEMs), not semiconductor suppliers, who'd report this taxonomy directly in their own 10-K segment footnotes.
- Deliberately split in two so no dollar math is left to the model: an LLM call in `llm_enricher.py` identifies which benchmarked vertical fits the account and whether a disclosed annual unit volume exists (explicitly instructed to return `null` rather than invent one); `segment_benchmarks.py` then does the actual multiplication — unit volume × $/unit benchmark × segment split — in plain Python. Every record always gets exactly 5 entries, even when unestimable; those carry `benchmark_confidence: "needs_research"` and a `null` range rather than being silently omitted.
- Benchmark coverage as of this release: `auto_ice`, `auto_ev`, and `datacenter_ai_server` are `sourced` (AlixPartners 2026, IEA Global EV Outlook 2025, and the SIA/Deloitte "Powering AI" report, respectively); `datacenter_general_server` and `telecom_infra` are `needs_research`; `industrial_automation` is `directional`. See [Segment SAM estimates](#segment-sam-estimates-economicssegment_sam_estimates) below.
- **`extractors/grounding.py`** — post-extraction checks that re-verify the highest-risk LLM outputs against actual source text, rather than trusting the model's own compliance with its "don't fabricate" instructions. Applied to three fields: stakeholder names (replaced with a role-title placeholder if ungrounded), the segment-SAM classification's disclosed unit volume (discarded if ungrounded), and news item URLs (item dropped entirely if ungrounded). See [Grounding safeguards](#grounding-safeguards).
- Stakeholder extraction prompt now explicitly instructs the model to use a role-title placeholder (e.g. `"VP of Procurement (name not found)"`) instead of inventing a plausible-sounding name — matching the instruction `app.jsx` already had. The grounding pass re-checks this regardless of what the model reports.
- `research_record_viewer.jsx` — a separate, minimal React artifact that imports this pipeline's JSON output and renders it, including a dedicated Segment SAM tab with a persistent audited-vs-estimated provenance indicator. See [Companion viewer artifact](#companion-viewer-artifact-research_record_viewerjsx).

**Changed**
- Schema bumped to `v1.2.0`. Note: this was done in parallel with — and would otherwise collide with — the `v1.1.0` bump from the People Data Labs work above. This release supersedes both; a record tagged `schema_version: "1.1.0"` from the PDL-only change predates `segment_sam_estimates` and the grounding pass, and should be treated as an earlier generation of the same schema line, not a divergent one.
- `economics` confidence scoring is explicitly unaffected by `segment_sam_estimates` — see [Confidence scoring](#confidence-scoring).

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

- The same five-stage research flow (overview + locations → economics → stakeholders → subsidiaries & M&A → account intelligence), rendered as a dashboard of saved accounts with a sidebar and a tabbed results view.
- **Manual editing** — every section except revenue segments and news is editable in place (Edit → Apply changes → Save), with add/remove for locations, stakeholders, subsidiaries, acquisitions, and opportunity signals.
- **Delete accounts** — click-to-arm confirmation rather than a native `confirm()` dialog, since those are frequently blocked inside the sandboxed iframe artifacts run in.
- **Re-saving a company you've already researched replaces the old profile** instead of creating a duplicate — matched by domain, not by the internal record ID (which is always freshly generated per run).
- **Download report** — generates a standalone HTML file and triggers a real browser download (not a same-tab open, which sandboxed artifacts can't do) with a filesystem-safe name derived from company + date; open it and use your browser's Ctrl+P → Save as PDF to get an actual PDF.

Note: `app.jsx` does not currently have a segment SAM tab, the grounding checks, or the `verified` stakeholder flag added on the Python side — those exist in the Python pipeline and in `research_record_viewer.jsx`, not here. `app.jsx` remains fully independent, by design, so this isn't a gap to close so much as a scope boundary to be aware of.

### Recent fixes

- **JSON parsing**: `web_search`-enabled responses sometimes have Claude append a trailing sentence after the JSON closes (e.g. "I found these on the investor relations page"). The previous `JSON.parse` + greedy-regex fallback failed outright when this happened. Replaced with a balanced-bracket scanner that finds exactly where the JSON value ends — correctly skipping over brackets and quotes inside string literals — and ignores anything Claude appends afterward.
- **Response truncation**: the Overview, Account Intelligence, Economics, and Subsidiaries calls each bundle several schema sections into a single response, but were capped at the same token limit as the smaller calls. Raised individually for those after each was observed (or was likely) truncating mid-JSON on data-heavy companies.
- **Economics showed stale data even when other sections had current figures** — `runEconomics` never had `web_search` enabled, unlike Overview/Stakeholders/Intelligence, so it answered purely from Claude's training-data memory. For a company with fast-changing financials (e.g. a private company mid-growth), this produced internally inconsistent reports: the Overview summary and Intelligence signals would cite a recent year's figures (found via search), while the structured Revenue/margin/capex metrics silently reflected an older, better-known number. Enabled `web_search` for this call too, and the prompt now explicitly directs it to prioritize the latest available figures over older, more widely-cited ones, and to mark private-company estimates as `"estimated"` rather than defaulting toward `"audited"`.
- **"New research" no longer discards progress mid-run** — clicking it while a pipeline is actively executing now shows the live stage tracker instead of hiding it behind a blank form. (Once a run finishes, the app already force-navigates to the results view regardless of where you're browsing; this only closes the gap during the in-progress window.)
- **PDF download was silently producing an empty, misnamed file** (`undefined.html`, no content) due to an unreachable code path in the report generator. Fixed and verified by actually executing the function, not just checking that it parses. The download now also uses a real filesystem-safe filename derived from the company name and date, and forces an actual file download rather than relying on a sandboxed `target="_blank"`, which silently degrades to a generic `download.html` in some browsers.
- **Ticker was accidentally non-editable** in edit mode — grouped in with the (correctly) read-only Domain/Website fields despite having no bearing on how a saved account is matched on re-save. Split into separate Symbol/Exchange fields; clearing both reverts the company to "Private" rather than leaving a hollow object behind.

### Known limitations

- No search/filter box for saved accounts, and no one-click "re-run research for this account" button. Both were prototyped and then removed while troubleshooting an unrelated issue, and haven't been reintroduced since — manual editing was the one feature from that round of work that was kept.
- Revenue segments and the News tab are read-only even in edit mode.
- No timeout or elapsed-time indicator on individual API calls, so a slow-but-working response (e.g. researching a company with a thin public semiconductor footprint) currently looks identical to a hung one.

---

## Companion viewer artifact (research_record_viewer.jsx)

A separate, smaller React artifact — not an alternative to `app.jsx`, and not part of `account_research/`. Where `app.jsx` runs its own live research via the Claude API and web search, `research_record_viewer.jsx` does no research and makes no API calls at all: it imports the JSON this Python pipeline produces (paste, file upload, or a built-in sample fixture) and renders it.

It exists because the Python pipeline is the only place that can reach SEC EDGAR at all — `data.sec.gov` doesn't support CORS and requires a `User-Agent` header browsers refuse to let JavaScript set, so no browser-based artifact (including `app.jsx`) can ever fetch audited EDGAR data directly. This viewer is the "read the Python pipeline's output" half of that split.

Its one feature beyond what `app.jsx` shows is a dedicated **Segment SAM tab**, plus a persistent provenance bar (audited/sourced vs. estimated vs. needs-research, color-coded) so a benchmark-derived segment estimate can never be visually mistaken for the audited financials sitting next to it. It does not yet surface the `verified` stakeholder flag — a reasonable next addition, not yet built. See the comment header in that file for the exact JSON contract it expects.

---

## How it works

The more technical of the two options — runs from a terminal, needs your own API keys, and writes output to disk. Good fit if you're comfortable with the command line and want scripting/batch-run flexibility.

```
Web search (14 queries)
        │
        ├──► LLM extraction ──► Grounding checks ──► Pydantic validation ──► AccountResearchRecord
        │    (6 sections +          (extractors/                                     │
        │    segment SAM            grounding.py)                                    │
        │    classification,             ▲                                           │
        │    PDL- and patent-            │                                           │
        │    anchored)                   │                                           │
SEC EDGAR ──────────────────────────────┤──────────────────────────►   JSON + Markdown summary
(audited financials)                    │                          ▲
                                         │                          │
People Data Labs ─────────────────────────┤ (verified contacts,     │
(optional — PDL_API_KEY)                  │  matched by name)       │
                                           │                         │
FCC grantee registrations ─────────────────┤ (facility corroboration │
(always runs, no key needed)               │  by city match)         │
                                            │                         │
USPTO Open Data Portal ──────────────────────┤ (inventor corroboration +
(optional — USPTO_API_KEY)                   │  deterministic segment-
                                              │  activity signal)
Segment benchmarks (segment_benchmarks.py) ──┘
(pure Python SAM math — no LLM involved)
```

Seven stages run in sequence, each independently fault-tolerant:

1. **Web search** — fires 14 targeted queries across topics (overview, locations, stakeholders, financials, semiconductor profile, news) using Serper, Brave Search, or SerpAPI.
2. **SEC EDGAR** — for US-listed companies, fetches audited 10-K / 20-F financials directly from EDGAR's public XBRL API. No API key required.
3. **People search** *(optional)* — if `PDL_API_KEY` is set, looks up verified decision-maker contacts (name, title, LinkedIn, email) via People Data Labs. Skipped entirely if the key isn't set.
4. **FCC facility lookup** *(always runs, no key required)* — queries FCC equipment authorization grantee registrations for the target company and merges results into `locations`: a city match with an existing (LLM-derived) location gets a corroborating note; no match gets added as a new, explicitly-caveated `Location` entry. Only surfaces anything for companies with RF/wireless products.
5. **Patent search** *(optional)* — if `USPTO_API_KEY` is set, looks up granted patents and their named inventors, tagged by CPC classification into the five segments. Treated more conservatively than PDL: a matched inventor only ever corroborates an existing stakeholder with a note (patent count, segments, most recent grant) — it never sets `verified: true` and never adds a new stakeholder record for an unmatched inventor, since a patent proves inventorship at some point in time, not current employment. Segment-level patent-activity counts also feed a deterministic `account_intelligence` signal, computed in plain Python from exact counts rather than summarized by the LLM.
6. **LLM enrichment** — Claude makes one focused call per schema section, returning JSON. EDGAR numbers override LLM estimates where both exist; verified PDL contacts and known patent inventors are both listed in the stakeholder prompt (with different framing — PDL contacts are presented as confirmed current employees, patent inventors are explicitly flagged as needing corroboration) so Claude prefers real names/titles over inference. A dedicated call also classifies the account's semiconductor end-market vertical and looks for a disclosed unit volume — but never invents one; the actual segment SAM dollar math happens afterward in plain Python (`segment_benchmarks.py`), never inside the LLM call. A grounding pass (`extractors/grounding.py`) then re-checks stakeholder names (against the search snippets, the PDL contact list, *and* the patent inventor list — a name only present in one of those lists is still grounded), the disclosed unit volume, and news URLs, discarding or flagging anything that doesn't trace back to something real.
7. **Assemble & validate** — merges all sources into a typed `AccountResearchRecord`, scores confidence per section, and writes output. LLM-extracted stakeholders are matched against PDL contacts by normalized name (exact, then surname fallback); a match sets `verified: true` and lets the structured PDL contact info override anything the LLM inferred. Any verified PDL contact the LLM never surfaced is still added via a safety net rather than dropped — patent inventors deliberately do **not** get this same safety-net treatment (see Stage 5).

---

## Output

Each run produces two files in `./output/`:

```
output/
  intel_corporation_20260624_143201.json           # full structured record
  intel_corporation_20260624_143201_summary.md     # one-pager for deal review / CRM
```

The JSON record follows a versioned schema (`v1.2.0`) with these top-level sections:

| Section | What it contains |
|---|---|
| `meta` | Schema version, timestamps, per-section confidence scores, source citations |
| `overview` | Legal name, ticker, founding year, headcount, industry verticals, summary |
| `locations` | HQ, fabs, R&D centers, sales offices — typed and status-tagged |
| `stakeholders` | Decision-makers with role type, influence score, buying relevance notes, and a `verified` flag (true when matched against People Data Labs rather than purely LLM-inferred) |
| `economics` | Revenue, margins, R&D spend, capex, disclosed segment breakdown, semiconductor profile, and benchmark-derived segment SAM estimates (Power/EP/HPC/CONN/AMS) |
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
export FCC_APP_TOKEN="your-token"      # optional — raises the FCC Socrata rate limit; not required
export USPTO_API_KEY="your-uspto-key"  # optional — enables patent inventor lookup + segment activity signals
```

Serper (`serper.dev`) is recommended — roughly $0.001 per query, so a full pipeline run costs under $0.02 in search fees.

`PDL_API_KEY` is optional. [People Data Labs](https://www.peopledatalabs.com) offers a free tier (1,000 records/month, no credit card) that's normally enough for regular use. Without it, the pipeline behaves exactly as in v1.0.0 — stakeholders are inferred entirely from web search snippets (and grounding-checked, as of v1.2.0).

`USPTO_API_KEY` is also optional and free, but requires more setup than the others: a USPTO.gov account verified through ID.me (see `extractors/patent_search.py`'s module docstring, or the pipeline's earlier troubleshooting notes, for the full registration flow). Without it, patent-inventor lookup and the segment patent-activity signal are simply skipped — everything else behaves the same.

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

# Segment SAM estimates — always 5 entries (power/ep/hpc/conn/ams), even
# when a given segment couldn't be estimated (benchmark_confidence will
# read "needs_research" and low/high will be null in that case)
for est in record.economics.segment_sam_estimates:
    print(est.segment, est.estimated_sam_low_usd_m, est.estimated_sam_high_usd_m, est.benchmark_confidence)

# Verified stakeholders (matched against People Data Labs)
verified = [s for s in record.stakeholders if s.verified]
print(f"{len(verified)} of {len(record.stakeholders)} stakeholders verified")

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
│   ├── web_search.py            # Multi-query web search (Serper / Brave / SerpAPI)
│   ├── edgar.py                  # SEC EDGAR XBRL financials (free, no key)
│   ├── people_search.py         # People Data Labs verified contacts (optional, free tier)
│   ├── fcc_search.py             # FCC equipment authorization facility lookup (free, no key)
│   ├── patent_search.py         # USPTO granted-patent inventor + segment activity lookup (optional, free)
│   ├── llm_enricher.py          # Section-by-section Claude extraction
│   ├── segment_benchmarks.py    # Power/EP/HPC/CONN/AMS content benchmarks + SAM math
│   └── grounding.py              # Post-extraction checks that a claim traces to source text
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

For private companies, `economics` will typically be `low` or `medium`. Stakeholder confidence reaches `high` when either 3+ stakeholders are present OR 2+ are `verified` (PDL-matched) — a smaller set of confirmed contacts counts at least as much as a larger set of purely LLM-inferred ones.

`segment_sam_estimates` uses a **separate** confidence vocabulary — `benchmark_confidence` (`sourced` / `directional` / `needs_research`) — and deliberately does **not** factor into the `economics` confidence score above. A segment SAM estimate is never "audited" data; conflating the two would overstate how well-founded the economics section is.

---

## Grounding safeguards

Pydantic validation (in `assembler.py`) checks that LLM output has the right **shape** — it says nothing about whether the content is **true**. A well-formed, schema-valid stakeholder name, disclosed unit volume, or news URL can still be entirely invented by the model.

`extractors/grounding.py` adds a narrow, literal check on top of shape validation for the three fields most prone to confident fabrication:

| Field | Check | What happens if ungrounded |
|---|---|---|
| Stakeholder `name` | Must appear (or closely match) in the stakeholder source snippets **or** the verified PDL contact list **or** the known patent inventor list | Replaced with a role-title placeholder, e.g. `"VP of Procurement (name not found)"` |
| `annual_unit_volume` (segment SAM classification) | The digit sequence must appear in the source text | Discarded (set to `null`); the record gets a `needs_research` segment SAM estimate instead of a fabricated one |
| News item `url` | Must match a URL actually returned by that account's "news"-topic search queries | The entire news item is dropped |

This is deliberately **not** a semantic entailment check — it won't catch a model subtly misreading a real sentence. It only catches outright invention: a name, number, or citation that doesn't trace back to anything actually retrieved (or, for stakeholder names, actually supplied as a verified contact or known patent inventor). Every relevant prompt in `llm_enricher.py` also carries an explicit "do not fabricate — use null or a placeholder instead" instruction, but that instruction is a first line of defense, not a guarantee — the grounding pass runs regardless of whether the model says it complied.

**Relationship to the `verified` (PDL) flag, and where patent inventors sit relative to both:** these are three *different* trust signals that don't all carry the same weight. `verified: true` means the name was matched against a structured, current-employment-oriented source (People Data Labs) in `assembler.py` — the strongest signal, since it targets *currently works here*. Passing the grounding check in `llm_enricher.py` means the name appears somewhere in the web-search snippets, the PDL contact list, *or* the patent inventor list handed to the model — a much weaker claim: it means the name wasn't invented, not that the person is currently employed there. A patent-matched stakeholder gets a corroborating note (patent count, segments, grant date) but **never** `verified: true` — a patent proves inventorship at some point in time, which is a meaningfully different (and weaker) claim than PDL's current-employment signal, and conflating the two would overstate how current a patent-sourced name is. Treat the three in this order of strength: `verified` (PDL) > grounded-via-web-text > grounded-via-patent-only.

None of this eliminates hallucination risk entirely. Narrative fields (the `summary`, `opportunity_signals` descriptions, `qualification_cycle` text) aren't grounding-checked, since there's no simple literal test for whether a paraphrase is faithful to its source. Treat every field in a `medium` or `low` confidence record — and every `needs_research`/`directional` segment SAM estimate — as something to verify before it goes into a customer-facing deliverable.

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

### Segment SAM estimates (`economics.segment_sam_estimates`)

For **target accounts you sell chips to** (as opposed to semiconductor suppliers themselves), a customer's own 10-K never breaks spend down by chip product category — that taxonomy belongs to suppliers, not buyers. `segment_sam_estimates` fills that gap with a **benchmark-derived estimate**, not a disclosed figure:

```json
{
  "segment": "power",
  "estimated_sam_low_usd_m": 180.0,
  "estimated_sam_high_usd_m": 300.0,
  "unit_basis": "375,000 vehicles produced × 40% segment allocation",
  "benchmark_source": "AlixPartners, 'Navigating the semiconductor-auto nexus' (2026)",
  "benchmark_confidence": "sourced",
  "rationale": "EV traction inverter + OBC + BMS content."
}
```

How it's computed (see `extractors/segment_benchmarks.py`):

1. The LLM identifies which benchmarked vertical (`auto_ice`, `auto_ev`, `datacenter_ai_server`, etc. — see `BENCHMARKS` in that file) fits the account, and looks for a disclosed annual unit volume. It's explicitly instructed to return `null` rather than invent a number — `annual_unit_volume` is `null` far more often than not.
2. A grounding check (see [Grounding safeguards](#grounding-safeguards)) re-verifies that any returned unit volume actually appears in the source text before it's trusted.
3. If both a vertical and a grounded unit volume are found, `estimate_segment_sam()` — pure Python, no LLM — multiplies unit volume × the vertical's $/unit benchmark range × each segment's allocation percentage.
4. Every record always gets exactly 5 entries (`power`, `ep`, `hpc`, `conn`, `ams`), even when a number couldn't be computed — in that case `estimated_sam_low_usd_m`/`high` are `null` and `benchmark_confidence` reads `needs_research`, so downstream viewers (including `research_record_viewer.jsx`) can render an explicit "no estimate yet" state per segment instead of silently omitting it.

**Benchmark coverage as of this schema version:**

| Vertical | Confidence | Notes |
|---|---|---|
| `auto_ice` | `sourced` | AlixPartners (2026); Yole via Electronics360 |
| `auto_ev` | `sourced` | AlixPartners (2026); IEA Global EV Outlook 2025 |
| `datacenter_ai_server` | `sourced` | SIA/Deloitte "Powering AI" report (June 2026) |
| `datacenter_general_server` | `needs_research` | No sourced $/unit figure yet for non-AI servers |
| `telecom_infra` | `needs_research` | Needs current Yole/Dell'Oro RAN BOM data |
| `industrial_automation` | `directional` | Segment split is a reasonable guess, not yet benchmark-sourced |

Extend `BENCHMARKS` in `extractors/segment_benchmarks.py` as better sourcing becomes available for the `needs_research`/`directional` verticals — always cite the source alongside the number, the same way the `sourced` entries do.

---

## Extending the pipeline

**Add a new data source** — implement a new extractor in `extractors/`, call it from `pipeline.py` in the run sequence, and pass its output to `assembler.py`. The assembler's merge logic follows the pattern: structured source > LLM estimate > null. `extractors/people_search.py` is a working example of this pattern — see `pipeline.py`'s Stage 3 (fully optional, skipped if `PDL_API_KEY` is unset) and `assembler.py`'s `_build_stakeholders()`, which matches PDL contacts by name, overrides LLM-inferred contact fields with structured data on a match, and adds any unmatched verified contact via a safety net.

**Add a new schema field** — add the field to the relevant Pydantic model in `models/schema.py`, add it to the corresponding LLM prompt in `llm_enricher.py`, and handle it in `assembler.py`.

**Add or refine a segment SAM benchmark** — add/edit an entry in `BENCHMARKS` in `extractors/segment_benchmarks.py` with a real, dated source; no other file needs to change.

**Add a grounding check for a new high-risk field** — add a function to `extractors/grounding.py` following the pattern of `text_contains_claim`/`number_grounded`/`is_url_grounded`, then call it in `llm_enricher.py` right after the relevant `_call()` returns, before the value is merged into the section dict. If the field can legitimately be sourced from something other than raw web text (as stakeholder names can from PDL), make sure the grounding source text includes that other source too — see `_extract_stakeholders` for the pattern.

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

## A note on corporate networks

If you're running this from inside a company network with TLS inspection (Zscaler, Palo Alto, Netskope, and similar corporate proxies all do this), you may hit this on first run:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate in certificate chain
```

This isn't a bug in the pipeline's requests — it's a mismatch between two different certificate stores. Your OS (and anything that uses it, like PowerShell's `Invoke-RestMethod`) already trusts your company's internal root certificate, because IT pushed it out. Python's `httpx` doesn't use the OS store by default — it ships its own separate, bundled CA list (`certifi`) that has never heard of your company's certificate, so it correctly refuses to trust what looks like a self-signed certificate sitting in the middle of the connection.

As of v1.2.5, every extractor in this pipeline passes `verify=truststore.SSLContext(...)` to its `httpx` calls, which checks against the OS certificate store instead — the same fix `pip` itself adopted by default starting in v24.2. This should resolve the error with no further configuration, since your OS almost certainly already trusts the corporate root your IT team installed.

If you hit this error on a *different* library this pipeline doesn't control (e.g. while debugging with `curl.exe`, or in some other tool entirely), the fastest one-command fix for local testing is `pip install pip_system_certs`, which patches Python globally to use the OS store for anything using standard SSL contexts — not required for this pipeline itself once on v1.2.5, but useful to know about for adjacent tooling.

**Do not install `pip_system_certs` alongside running this pipeline.** If you used it as a stopgap before v1.2.5 existed, uninstall it now (`pip uninstall pip_system_certs`) — running it together with this code's own `truststore.SSLContext(...)` usage causes `RecursionError: maximum recursion depth exceeded`, since both patch SSL context creation in ways that collide. `truststore` alone (already in `requirements.txt`) is all this pipeline needs.

---

## Requirements

- Python 3.11+
- `anthropic >= 0.25.0`
- `httpx >= 0.27.0`
- `pydantic >= 2.0.0`
- `truststore >= 0.9.0` — required, not optional; see [A note on corporate networks](#a-note-on-corporate-networks) below
- A search API key (Serper, Brave, or SerpAPI)
- An Anthropic API key
- `PDL_API_KEY` (optional) — for verified stakeholder contacts via People Data Labs