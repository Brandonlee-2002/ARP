import { useState, useEffect } from "react";

const MODEL = "claude-sonnet-4-6";
const STORAGE_KEY = "acct_research_v1";

// ── CSS variable system ────────────────────────────────────────
const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
  @import url('https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --font-sans: 'Inter', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
    --radius: 8px;

    /* Surfaces */
    --bg:           #0d1117;
    --surface-1:    #161b22;
    --surface-2:    #1c2230;

    /* Borders */
    --border:         rgba(255,255,255,0.08);
    --border-strong:  rgba(255,255,255,0.16);
    --border-accent:  rgba(56,139,253,0.4);
    --border-success: rgba(46,160,67,0.4);
    --border-warning: rgba(210,153,34,0.4);
    --border-danger:  rgba(248,81,73,0.4);

    /* Text */
    --text-primary:   #e6edf3;
    --text-secondary: #8b949e;
    --text-muted:     #484f58;
    --text-accent:    #58a6ff;
    --text-success:   #3fb950;
    --text-warning:   #d29922;
    --text-danger:    #f85149;
    --text-pro:       #a371f7;

    /* Fills (for bars/icons) */
    --fill-accent:  #388bfd;
    --fill-success: #2ea043;
    --fill-warning: #9e6a03;
    --fill-danger:  #da3633;

    /* Tinted backgrounds */
    --bg-accent:   rgba(56,139,253,0.1);
    --bg-success:  rgba(46,160,67,0.1);
    --bg-warning:  rgba(210,153,34,0.1);
    --bg-danger:   rgba(248,81,73,0.1);
    --bg-pro:      rgba(163,113,247,0.1);
    --bg-neutral:  rgba(139,148,158,0.1);
  }

  body {
    font-family: var(--font-sans);
    background: var(--bg);
    color: var(--text-primary);
    font-size: 14px;
    line-height: 1.5;
    height: 100vh;
    overflow: hidden;
  }

  input {
    font-family: var(--font-sans);
    font-size: 13px;
    background: var(--surface-1);
    border: 0.5px solid var(--border-strong);
    border-radius: var(--radius);
    color: var(--text-primary);
    padding: 8px 12px;
    outline: none;
    transition: border-color .15s;
  }
  input:focus { border-color: var(--border-accent); }
  input::placeholder { color: var(--text-muted); }

  button {
    font-family: var(--font-sans);
    font-size: 13px;
    background: var(--surface-2);
    border: 0.5px solid var(--border-strong);
    border-radius: var(--radius);
    color: var(--text-primary);
    padding: 7px 14px;
    cursor: pointer;
    transition: background .1s, border-color .1s;
  }
  button:hover:not(:disabled) { background: var(--surface-1); border-color: var(--border-accent); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary {
    background: var(--fill-accent);
    border-color: transparent;
    color: #fff;
    font-weight: 500;
  }
  button.primary:hover:not(:disabled) { background: #58a6ff; }

  .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }

  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  /* Scrollbars */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 2px; }

  @media print {
    body { background: #fff !important; color: #111 !important; overflow: auto; height: auto; }
    .no-print { display: none !important; }
    .print-area { padding: 24px !important; }
  }
`;

// ── Constants ──────────────────────────────────────────────────
const STAGES = [
  { key: "overview",     label: "Company overview",    icon: "ti-building"  },
  { key: "economics",    label: "Financial data",       icon: "ti-chart-bar" },
  { key: "stakeholders", label: "Stakeholder map",      icon: "ti-users"     },
  { key: "intelligence", label: "Account intelligence", icon: "ti-radar-2"   },
];

const TABS = [
  { key: "overview",     label: "Overview",     icon: "ti-info-circle" },
  { key: "locations",    label: "Locations",    icon: "ti-map-pin"     },
  { key: "stakeholders", label: "Stakeholders", icon: "ti-users"       },
  { key: "economics",    label: "Economics",    icon: "ti-chart-bar"   },
  { key: "intelligence", label: "Intelligence", icon: "ti-radar-2"     },
  { key: "news",         label: "News",         icon: "ti-news"        },
];

// ── Helpers ────────────────────────────────────────────────────
const slug  = s => (s||"").toLowerCase().replace(/[^a-z0-9]+/g,"_").slice(0,40);
// Identifies "the same company" across separate pipeline runs, since each
// run mints a fresh record_id. Used so re-running research on a company you
// already saved replaces the old profile instead of piling up a duplicate.
const normKey = r => (r.domain || r.overview?.website || r.company_name || "")
  .toLowerCase().replace(/^https?:\/\//,"").replace(/\/$/,"").trim();
const fmtD  = iso => { try { return new Date(iso).toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}); } catch { return "—"; } };
const fmtM  = v => v != null ? `$${Number(v).toLocaleString()}M` : "—";
const fmtP  = v => v != null ? `${Number(v).toFixed(1)}%` : "—";
const confC = l => ({high:"var(--text-success)",medium:"var(--text-warning)",low:"var(--text-danger)"}[l]||"var(--text-muted)");
const confB = l => ({high:"var(--bg-success)",medium:"var(--bg-warning)",low:"var(--bg-danger)"}[l]||"var(--surface-1)");
const sigC  = t => ({opportunity:"var(--text-success)",risk:"var(--text-danger)",watch:"var(--text-warning)"}[t]||"var(--text-secondary)");
const sigB  = t => ({opportunity:"var(--bg-success)",risk:"var(--bg-danger)",watch:"var(--bg-warning)"}[t]||"var(--surface-1)");
const sigBr = t => ({opportunity:"var(--border-success)",risk:"var(--border-danger)",watch:"var(--border-warning)"}[t]||"var(--border)");

// ── API ────────────────────────────────────────────────────────
const SYS = `You are a semiconductor sales intelligence extractor. Return ONLY valid JSON. Use null for unknowns. Never fabricate specific numbers. data_quality: "audited"|"estimated"|"inferred".`;

async function apiCall(prompt, webSearch = false) {
  const body = {
    model: MODEL, max_tokens: 2000, system: SYS,
    messages: [{ role: "user", content: prompt }],
  };
  if (webSearch) body.tools = [{ type: "web_search_20250305", name: "web_search" }];
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`API error ${r.status}`);
  const d = await r.json();
  const text = d.content.filter(b => b.type === "text").map(b => b.text).join("");
  const clean = text.replace(/```json\s*/g,"").replace(/```\s*/g,"").trim();
  try { return JSON.parse(clean); } catch {
    const m = clean.match(/(\{[\s\S]*\}|\[[\s\S]*\])/);
    if (m) return JSON.parse(m[0]);
    throw new Error("JSON parse failed");
  }
}

const runOverview = (co, domain, ticker) => apiCall(
`Research "${co}" (domain:${domain}${ticker?", ticker:"+ticker:""}). Return JSON with exactly these two keys:
{"overview":{"legal_name":string,"doing_business_as":string|null,"ticker":{"symbol":string,"exchange":string}|null,"ownership_type":"public"|"private"|"subsidiary"|"jv"|"government","parent_company":string|null,"founded_year":integer|null,"website":string,"industry_verticals":[string],"employee_count":{"value":integer,"approximate":boolean}|null,"summary":"3-4 sentences covering what they do, market position, and relevance to a semiconductor supplier","competitors":[string],"key_products":[string]},"locations":[{"type":"hq"|"regional_hq"|"fab"|"assembly"|"rd_center"|"design_center"|"sales_office","name":string|null,"city":string,"state_region":string|null,"country":string,"country_code":"2-letter ISO","status":"active"|"ramping"|"under_construction"|"planned"|"closed","notes":string|null}]}
HQ first. Max 10 locations. For semiconductor companies distinguish fab sites from offices.`, true);

const runEconomics = (co, ticker) => apiCall(
`Financial and semiconductor data for "${co}"${ticker?" (ticker:"+ticker+")":"(private company)"}. Return JSON:
{"financials":{"fiscal_year":integer|null,"currency":"USD","revenue_usd_m":float|null,"revenue_growth_pct":float|null,"gross_margin_pct":float|null,"operating_margin_pct":float|null,"net_income_usd_m":float|null,"rd_spend_usd_m":float|null,"rd_pct_revenue":float|null,"capex_usd_m":float|null,"market_cap_usd_m":float|null,"data_quality":"audited"|"estimated"|"inferred"},"revenue_segments":[{"name":string,"revenue_usd_m":float|null,"pct_of_total":float,"growth_pct":float|null,"relevant_to_us":boolean}],"semiconductor_profile":{"chip_buyer_type":"fabless"|"fab_lite"|"idc"|"ems"|"oem_buyer_only"|null,"has_own_fab":boolean,"fab_nodes":[string],"known_chip_suppliers":[string],"known_supply_gaps":[string],"design_win_notes":string|null,"qualification_cycle":string|null,"annual_chip_spend_est_usd_m":float|null}}
Revenue in USD millions. Use null for unknowns.`);

const runStakeholders = (co) => apiCall(
`Search for and map 4-6 key stakeholders at "${co}" for a semiconductor account manager. Use web search to find real current executives and leaders — company leadership/investor-relations pages, recent press releases, and LinkedIn search results are good sources. Prefer names you can actually find over inventing plausible-sounding ones; if you can't find a real name for a role, use the role title itself as the "name" (e.g. "VP of Procurement (name not found)") rather than a fabricated person. Return a JSON array only:
[{"name":"Full Name — or Role Title (name not found) if unknown","title":string,"department":string|null,"location":string|null,"role_type":"economic_buyer"|"technical_buyer"|"executive_sponsor"|"champion"|"influencer"|"gatekeeper","influence_score":float 0.0-1.0,"notes":"1-2 sentences on buying role and how to engage"|null}]
economic_buyer=0.8-1.0, technical_buyer=0.6-0.8. Prioritize: procurement VP, CTO, supply chain lead.`, true);

const runIntelligence = (co) => apiCall(
`Semiconductor supplier account intelligence for "${co}". Return JSON:
{"account_intelligence":{"opportunity_signals":[{"type":"opportunity"|"risk"|"watch","description":"1-2 actionable sentences","urgency":"immediate"|"near_term"|"long_term","source":string|null}],"recommended_next_action":string|null,"current_supplier_status":"no_relationship"|"evaluation"|"approved_vendor"|"active_account"|"strategic_partner"},"news":[{"date":"YYYY-MM-DD","headline":string,"summary":string|null,"source_name":string,"url":string|null,"tags":[string]}]}
3-5 signals covering expansion plans, qualification cycles, competitive risks. 5-7 news items.`, true);

// ── Export ─────────────────────────────────────────────────────
function toMarkdown(r) {
  const ov = r.overview||{}, f = r.economics?.financials||{}, semi = r.economics?.semiconductor_profile||{}, intel = r.account_intelligence||{};
  const locs = (r.locations||[]).map(l=>`| ${(l.type||"").toUpperCase()} | ${[l.city,l.state_region,l.country].filter(Boolean).join(", ")} | ${l.status||""} | ${l.notes||""} |`).join("\n");
  const stks = (r.stakeholders||[]).map(s=>`| **${s.name}** | ${s.title} | ${s.role_type?.replace(/_/g," ")} | ${Math.round((s.influence_score||0)*100)}% | ${s.notes||""} |`).join("\n");
  const sigs = (intel.opportunity_signals||[]).map(s=>`- **[${(s.type||"").toUpperCase()}]** ${s.description} *(${(s.urgency||"").replace(/_/g," ")})*`).join("\n");
  const news = (r.news||[]).map(n=>`| ${n.date} | ${n.headline} | ${n.source_name} |`).join("\n");
  return `# Account research: ${ov.legal_name||r.company_name}
*${fmtD(r.meta?.refreshed_at||new Date())} · Confidence: ${r.meta?.confidence?.overall||"medium"} · Schema v1.0.0*

## Overview
- **Ticker:** ${ov.ticker?`${ov.ticker.symbol}:${ov.ticker.exchange}`:"Private"}
- **Domain:** ${r.domain||ov.website||"N/A"}
- **Founded:** ${ov.founded_year||"N/A"} · **Employees:** ${ov.employee_count?`~${ov.employee_count.value?.toLocaleString()}`:"N/A"}
- **Ownership:** ${ov.ownership_type||"N/A"}${ov.parent_company?" · "+ov.parent_company:""}

${ov.summary||""}

## Locations
| Type | Location | Status | Notes |
|---|---|---|---|
${locs||"| — | — | — | — |"}

## Stakeholder map
| Name | Title | Role | Influence | Notes |
|---|---|---|---|---|
${stks||"| — | — | — | — | — |"}

## Economics (FY${f.fiscal_year||"N/A"})
| Revenue | Growth | Gross margin | R&D | Capex |
|---|---|---|---|---|
| ${fmtM(f.revenue_usd_m)} | ${fmtP(f.revenue_growth_pct)} | ${fmtP(f.gross_margin_pct)} | ${fmtM(f.rd_spend_usd_m)} | ${fmtM(f.capex_usd_m)} |

**Semiconductor profile:** ${semi.chip_buyer_type||"N/A"} · Fab: ${semi.has_own_fab?"Yes":"No"} · Nodes: ${(semi.fab_nodes||[]).join(", ")||"N/A"}
Supply gaps: ${(semi.known_supply_gaps||[]).join("; ")||"N/A"}

## Account intelligence
**Status:** ${intel.current_supplier_status||"no_relationship"}
**Next action:** ${intel.recommended_next_action||"N/A"}

${sigs||"No signals identified."}

## Recent news
| Date | Headline | Source |
|---|---|---|
${news||"| — | — | — |"}

---
*Research pipeline v1.0.0*`;
}


function toHtmlReport(r) {
  const ov = r.overview||{}, f = r.economics?.financials||{}, semi = r.economics?.semiconductor_profile||{}, intel = r.account_intelligence||{};
  const esc = s => String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const val = v => `<span style="color:#111">${esc(v||"—")}</span>`;
  const badge = (text, color="#555", bg="#f0f0f0") => `<span style="display:inline-block;font-size:11px;padding:2px 8px;border-radius:99px;background:${bg};color:${color};font-weight:500;white-space:nowrap">${esc(text)}</span>`;
  const sigColor = t => ({opportunity:["#166534","#f0fdf4","#bbf7d0"],risk:["#991b1b","#fff1f1","#fecaca"],watch:["#92400e","#fffbeb","#fde68a"]}[t]||["#555","#f9f9f9","#e5e7eb"]);
  const row = (label, value) => `<tr><td style="padding:5px 8px;font-size:12px;color:#777;white-space:nowrap;border-bottom:1px solid #f0f0f0">${esc(label)}</td><td style="padding:5px 8px;font-size:13px;color:#111;text-align:right;border-bottom:1px solid #f0f0f0">${esc(value||"—")}</td></tr>`;
  const metricCard = (label, value, sub) => `<div style="background:#f9f9f9;border:1px solid #e5e7eb;border-radius:8px;padding:12px 14px"><p style="font-size:11px;color:#777;margin:0 0 4px">${esc(label)}</p><p style="font-size:20px;font-weight:600;margin:0;color:#111">${esc(value)}</p>${sub?`<p style="font-size:11px;color:#555;margin:2px 0 0">${esc(sub)}</p>`:""}</div>`;
  const statusColors = {no_relationship:["#555","#f0f0f0"],evaluation:["#92400e","#fffbeb"],approved_vendor:["#1e40af","#eff6ff"],active_account:["#166534","#f0fdf4"],strategic_partner:["#5b21b6","#f5f3ff"]};
  const [sc,sb] = statusColors[intel.current_supplier_status||"no_relationship"]||["#555","#f0f0f0"];

  const locRows = (r.locations||[]).map(l => {
    const typeColors = {hq:["#5b21b6","#f5f3ff"],fab:["#991b1b","#fff1f1"],rd_center:["#1e40af","#eff6ff"],design_center:["#1e40af","#eff6ff"],sales_office:["#166534","#f0fdf4"],assembly:["#92400e","#fffbeb"],regional_hq:["#5b21b6","#f5f3ff"]};
    const [tc,tb] = typeColors[l.type]||["#555","#f0f0f0"];
    return `<tr>
      <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">${badge((l.type||"").toUpperCase().replace(/_/g," "),tc,tb)}</td>
      <td style="padding:8px 12px;font-size:13px;border-bottom:1px solid #f0f0f0">${esc([l.name,[l.city,l.state_region,l.country].filter(Boolean).join(", ")].filter(Boolean).join(" — "))}</td>
      <td style="padding:8px 12px;font-size:12px;color:#777;border-bottom:1px solid #f0f0f0">${esc(l.notes)}</td>
      <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">${badge(l.status||"")}</td>
    </tr>`;
  }).join("");

  const stkCards = (r.stakeholders||[]).map(s => {
    const initials = (s.name||"?").split(" ").filter(w=>!w.includes("(")).slice(0,2).map(w=>w[0]).join("").toUpperCase();
    const roleColors = {economic_buyer:["#991b1b","#fff1f1"],technical_buyer:["#1e40af","#eff6ff"],executive_sponsor:["#5b21b6","#f5f3ff"],champion:["#166534","#f0fdf4"],influencer:["#92400e","#fffbeb"],gatekeeper:["#555","#f0f0f0"]};
    const [rc,rb] = roleColors[s.role_type||"influencer"]||["#555","#f0f0f0"];
    const pct = Math.round((s.influence_score||0)*100);
    const barColor = pct>=80?"#dc2626":pct>=60?"#d97706":"#2563eb";
    return `<div style="background:#f9f9f9;border:1px solid #e5e7eb;border-radius:10px;padding:14px;break-inside:avoid">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
        <div style="width:34px;height:34px;border-radius:50%;background:#dbeafe;color:#1e40af;font-size:12px;font-weight:600;display:flex;align-items:center;justify-content:center;flex-shrink:0">${initials}</div>
        <div><p style="font-size:13px;font-weight:600;margin:0;color:#111">${esc(s.name)}</p><p style="font-size:12px;color:#777;margin:0">${esc(s.title)}</p></div>
      </div>
      <div style="margin-bottom:8px">${badge((s.role_type||"").replace(/_/g," "),rc,rb)}${s.department?" "+badge(s.department):""}</div>
      <div style="margin-bottom:6px"><div style="display:flex;justify-content:space-between;font-size:11px;color:#777;margin-bottom:3px"><span>Influence</span><span>${pct}%</span></div><div style="height:4px;background:#e5e7eb;border-radius:2px"><div style="width:${pct}%;height:100%;background:${barColor};border-radius:2px"></div></div></div>
      ${s.notes?`<p style="font-size:12px;color:#555;margin:0;line-height:1.5">${esc(s.notes)}</p>`:""}
    </div>`;
  }).join("");

  const sigItems = (intel.opportunity_signals||[]).map(sig => {
    const [sc2,sb2,sbr] = sigColor(sig.type);
    return `<div style="background:${sb2};border:1px solid ${sbr};border-radius:8px;padding:10px 14px;margin-bottom:8px">
      <div style="margin-bottom:6px">${badge(sig.type,sc2,sb2)} ${badge((sig.urgency||"").replace(/_/g," "))}</div>
      <p style="font-size:13px;color:#111;margin:0;line-height:1.55">${esc(sig.description)}</p>
      ${sig.source?`<p style="font-size:11px;color:#777;margin:6px 0 0">Source: ${esc(sig.source)}</p>`:""}
    </div>`;
  }).join("");

  const newsRows = (r.news||[]).map(n => `<tr>
    <td style="padding:8px 12px;font-size:12px;color:#777;white-space:nowrap;border-bottom:1px solid #f0f0f0;font-family:monospace">${esc(n.date)}</td>
    <td style="padding:8px 12px;font-size:13px;font-weight:500;border-bottom:1px solid #f0f0f0">${esc(n.headline)}</td>
    <td style="padding:8px 12px;font-size:12px;color:#777;border-bottom:1px solid #f0f0f0">${esc(n.source_name)}</td>
  </tr>`).join("");

  const metrics = [
    ["Revenue", fmtM(f.revenue_usd_m), `FY${f.fiscal_year||"N/A"}`],
    ["YoY Growth", fmtP(f.revenue_growth_pct), ""],
    ["Gross Margin", fmtP(f.gross_margin_pct), ""],
    ["R&D Spend", fmtM(f.rd_spend_usd_m), f.rd_pct_revenue?fmtP(f.rd_pct_revenue)+" of rev":""],
    ["Capex", fmtM(f.capex_usd_m), ""],
    ["Market Cap", fmtM(f.market_cap_usd_m), ""],
  ];

  return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>${esc(ov.legal_name||r.company_name)} — Account Intelligence</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fff;color:#111;padding:40px;max-width:900px;margin:0 auto;font-size:14px;line-height:1.5}
  h1{font-size:24px;font-weight:700;margin-bottom:4px}
  h2{font-size:15px;font-weight:600;color:#111;margin:28px 0 12px;padding-bottom:6px;border-bottom:2px solid #111}
  .meta{font-size:13px;color:#777;margin-bottom:24px;font-family:monospace}
  .grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .card{background:#f9f9f9;border:1px solid #e5e7eb;border-radius:10px;padding:14px 16px}
  table{width:100%;border-collapse:collapse;background:#f9f9f9;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden}
  th{padding:8px 12px;font-size:11px;font-weight:600;color:#777;text-align:left;text-transform:uppercase;letter-spacing:0.05em;border-bottom:1px solid #e5e7eb;background:#f0f0f0}
  .stk-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
  .footer{margin-top:32px;padding-top:16px;border-top:1px solid #e5e7eb;font-size:11px;color:#aaa;text-align:center}
  @media print{body{padding:20px}h2{page-break-after:avoid}.card{break-inside:avoid}}
</style></head><body>
  <h1>${esc(ov.legal_name||r.company_name)}</h1>
  <p class="meta">${[ov.ticker?ov.ticker.symbol+":"+ov.ticker.exchange:"Private", r.domain||"", `Generated ${fmtD(r.meta?.refreshed_at)}`, `Confidence: ${r.meta?.confidence?.overall||"medium"}`].filter(Boolean).join(" · ")}</p>

  <h2>Overview</h2>
  <div class="grid-2" style="margin-bottom:16px">
    <div class="card">
      <table style="background:transparent;border:none"><tbody>
        ${row("Legal name", ov.legal_name)}
        ${row("Ticker", ov.ticker?`${ov.ticker.symbol}:${ov.ticker.exchange}`:(r.ticker||"Private"))}
        ${row("Domain", r.domain||ov.website)}
        ${row("Ownership", ov.ownership_type)}
        ${row("Parent", ov.parent_company)}
        ${row("Founded", ov.founded_year)}
        ${row("Employees", ov.employee_count?`~${Number(ov.employee_count.value).toLocaleString()}`:null)}
      </tbody></table>
    </div>
    <div class="card"><p style="font-size:13px;line-height:1.65;color:#111">${esc(ov.summary||"—")}</p></div>
  </div>

  <h2>Locations</h2>
  <table><thead><tr><th>Type</th><th>Location</th><th>Notes</th><th>Status</th></tr></thead><tbody>${locRows||`<tr><td colspan="4" style="padding:12px;color:#777;text-align:center">No locations identified</td></tr>`}</tbody></table>

  <h2>Stakeholders</h2>
  <div class="stk-grid">${stkCards||`<p style="color:#777">No stakeholders identified.</p>`}</div>

  <h2>Economics</h2>
  <div class="grid-3" style="margin-bottom:16px">${metrics.map(([l,v,s])=>metricCard(l,v,s)).join("")}</div>
  <div class="card">
    <p style="font-size:12px;font-weight:600;color:#777;margin-bottom:8px">Semiconductor profile</p>
    <table style="background:transparent;border:none"><tbody>
      ${row("Buyer type", semi.chip_buyer_type)}
      ${row("Has own fab", semi.has_own_fab!=null?(semi.has_own_fab?"Yes":"No"):null)}
      ${row("Fab nodes", (semi.fab_nodes||[]).join(", ")||null)}
      ${row("Known suppliers", (semi.known_chip_suppliers||[]).join(", ")||null)}
      ${row("Est. chip spend", semi.annual_chip_spend_est_usd_m?fmtM(semi.annual_chip_spend_est_usd_m):null)}
      ${row("Supply gaps", (semi.known_supply_gaps||[]).join("; ")||null)}
    </tbody></table>
  </div>

  <h2>Account Intelligence</h2>
  <div style="margin-bottom:12px">${badge((intel.current_supplier_status||"no_relationship").replace(/_/g," "),sc,sb)}</div>
  ${intel.recommended_next_action?`<div style="padding:12px 16px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:8px;margin-bottom:12px"><p style="font-size:12px;font-weight:600;color:#1e40af;margin-bottom:4px">Recommended next action</p><p style="font-size:13px;color:#111;margin:0;line-height:1.55">${esc(intel.recommended_next_action)}</p></div>`:""}
  ${sigItems||`<p style="color:#777;font-size:13px">No signals identified.</p>`}

  <h2>Recent News</h2>
  <table><thead><tr><th>Date</th><th>Headline</th><th>Source</th></tr></thead><tbody>${newsRows||`<tr><td colspan="3" style="padding:12px;color:#777;text-align:center">No recent news found</td></tr>`}</tbody></table>

  <div class="footer">Account Research Pipeline v1.0.0 · Generated ${fmtD(r.meta?.refreshed_at||new Date())}</div>
</body></html>`;
}

function openPdfReport(r) {
  const html = toHtmlReport(r);
  // Use a blob URL — works inside sandboxed iframes where window.open("","_blank") is blocked
  const blob = new Blob([html], { type: "text/html" });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement("a"), {
    href:   url,
    target: "_blank",
    rel:    "noopener noreferrer",
  });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // Revoke after a short delay so the new tab has time to load
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return true;
}

// Artifacts run in a sandboxed iframe — no file downloads.
// Export via clipboard copy instead; PDF via browser print dialog.
async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for browsers that block clipboard in iframes
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  }
}

// ── Shared UI atoms ────────────────────────────────────────────
function Badge({ children, color="var(--text-secondary)", bg="var(--surface-1)" }) {
  return <span style={{display:"inline-block",fontSize:11,fontWeight:500,padding:"2px 8px",borderRadius:99,background:bg,color,lineHeight:1.6,whiteSpace:"nowrap"}}>{children}</span>;
}

function DeleteButton({ onDelete, compact=false }) {
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);

  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        if (armed) { setArmed(false); onDelete(); }
        else setArmed(true);
      }}
      title={armed ? "Click again to permanently delete" : "Delete this account"}
      style={{
        fontSize:11, padding: compact ? "3px 7px" : "4px 10px",
        display:"flex", alignItems:"center", gap:4, flexShrink:0,
        color: armed ? "#fff" : "var(--text-muted)",
        background: armed ? "var(--fill-danger)" : "transparent",
        borderColor: armed ? "transparent" : "var(--border)",
      }}
    >
      <i className={`ti ${armed ? "ti-alert-triangle" : "ti-trash"}`} style={{fontSize:13}}/>
      {!compact && (armed ? "Confirm?" : "Delete")}
    </button>
  );
}

function InfluenceBar({ score=0 }) {
  const pct = Math.round(score*100);
  const fill = pct>=80?"var(--fill-danger)":pct>=60?"var(--fill-warning)":"var(--fill-accent)";
  return (
    <div>
      <div style={{display:"flex",justifyContent:"space-between",marginBottom:3}}>
        <span style={{fontSize:11,color:"var(--text-muted)"}}>Influence</span>
        <span style={{fontSize:11,fontWeight:500,color:"var(--text-secondary)"}}>{pct}%</span>
      </div>
      <div style={{height:4,background:"var(--border-strong)",borderRadius:2,overflow:"hidden"}}>
        <div style={{width:`${pct}%`,height:"100%",background:fill,borderRadius:2,transition:"width .5s"}}/>
      </div>
    </div>
  );
}

function FieldRow({ label, value, mono }) {
  return (
    <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",padding:"5px 0",borderBottom:"0.5px solid var(--border)",gap:12}}>
      <span style={{fontSize:12,color:"var(--text-muted)",whiteSpace:"nowrap",flexShrink:0}}>{label}</span>
      <span style={{fontSize:13,color:"var(--text-primary)",textAlign:"right",fontFamily:mono?"var(--font-mono)":undefined,wordBreak:"break-all"}}>{value||"—"}</span>
    </div>
  );
}

// ── Pipeline tracker ───────────────────────────────────────────
function PipelineTracker({ statuses }) {
  return (
    <div style={{display:"flex",alignItems:"flex-start",width:"100%",margin:"2rem 0"}}>
      {STAGES.map((stage,i) => (
        <div key={stage.key} style={{display:"flex",alignItems:"center",flex:i<STAGES.length-1?1:"none"}}>
          <div style={{display:"flex",flexDirection:"column",alignItems:"center",gap:6}}>
            <div style={{
              width:44,height:44,borderRadius:"50%",
              border:`2px solid ${statuses[i]==="done"?"var(--border-success)":statuses[i]==="running"?"var(--border-accent)":statuses[i]==="error"?"var(--border-danger)":"var(--border)"}`,
              background:statuses[i]==="done"?"var(--bg-success)":statuses[i]==="running"?"var(--bg-accent)":statuses[i]==="error"?"var(--bg-danger)":"var(--surface-1)",
              display:"flex",alignItems:"center",justifyContent:"center",
              animation:statuses[i]==="running"?"pulse 1.5s ease-in-out infinite":"none",
              transition:"all .3s",flexShrink:0,
            }}>
              {statuses[i]==="done"  && <i className="ti ti-check" style={{fontSize:20,color:"var(--text-success)"}}/>}
              {statuses[i]==="error" && <i className="ti ti-x"     style={{fontSize:20,color:"var(--text-danger)"}}/>}
              {(statuses[i]==="running"||statuses[i]==="pending") && (
                <i className={`ti ${stage.icon}`} style={{fontSize:20,color:statuses[i]==="running"?"var(--text-accent)":"var(--text-muted)"}}/>
              )}
            </div>
            <div style={{textAlign:"center",width:80}}>
              <p style={{fontSize:11,fontWeight:statuses[i]==="running"?500:400,color:statuses[i]==="done"?"var(--text-success)":statuses[i]==="running"?"var(--text-primary)":"var(--text-muted)",margin:"0 0 2px",lineHeight:1.3}}>{stage.label}</p>
              <p style={{fontSize:10,color:"var(--text-muted)",margin:0}}>
                {statuses[i]==="running"?"Running...":statuses[i]==="done"?"Done":statuses[i]==="error"?"Error":"Pending"}
              </p>
            </div>
          </div>
          {i<STAGES.length-1 && (
            <div style={{flex:1,height:2,background:statuses[i]==="done"?"var(--fill-success)":"var(--border)",margin:"0 4px",marginBottom:32,transition:"background .5s",minWidth:16}}/>
          )}
        </div>
      ))}
    </div>
  );
}

// ── Tab sections ───────────────────────────────────────────────
function OverviewSection({ r }) {
  const ov = r.overview||{};
  return (
    <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(0,1fr)",gap:16}}>
      <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
        <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:10}}>Company details</p>
        <FieldRow label="Legal name"  value={ov.legal_name}/>
        <FieldRow label="DBA"         value={ov.doing_business_as}/>
        <FieldRow label="Ticker"      value={ov.ticker?`${ov.ticker.symbol}:${ov.ticker.exchange}`:(r.ticker||"Private")} mono/>
        <FieldRow label="Ownership"   value={ov.ownership_type}/>
        <FieldRow label="Parent"      value={ov.parent_company}/>
        <FieldRow label="Founded"     value={ov.founded_year}/>
        <FieldRow label="Employees"   value={ov.employee_count?`~${Number(ov.employee_count.value).toLocaleString()}`:null}/>
        <FieldRow label="Domain"      value={r.domain||null} mono/>
        <FieldRow label="Website"     value={ov.website} mono/>
      </div>
      <div style={{display:"flex",flexDirection:"column",gap:12}}>
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Summary</p>
          <p style={{fontSize:13,lineHeight:1.65,color:"var(--text-primary)",margin:0}}>{ov.summary||"—"}</p>
        </div>
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Industry</p>
          <div style={{display:"flex",flexWrap:"wrap",gap:6,marginBottom:12}}>
            {(ov.industry_verticals||[]).map(v=><Badge key={v}>{v}</Badge>)}
            {!(ov.industry_verticals||[]).length && <span style={{fontSize:13,color:"var(--text-muted)"}}>—</span>}
          </div>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Key competitors</p>
          <div style={{display:"flex",flexWrap:"wrap",gap:6}}>
            {(ov.competitors||[]).map(c=><Badge key={c}>{c}</Badge>)}
            {!(ov.competitors||[]).length && <span style={{fontSize:13,color:"var(--text-muted)"}}>—</span>}
          </div>
        </div>
      </div>
    </div>
  );
}

function LocationsSection({ r }) {
  const typeMeta = {
    hq:           {bg:"var(--bg-pro)",    color:"var(--text-pro)"},
    fab:          {bg:"var(--bg-danger)", color:"var(--text-danger)"},
    rd_center:    {bg:"var(--bg-accent)", color:"var(--text-accent)"},
    design_center:{bg:"var(--bg-accent)", color:"var(--text-accent)"},
    sales_office: {bg:"var(--bg-success)",color:"var(--text-success)"},
    assembly:     {bg:"var(--bg-warning)",color:"var(--text-warning)"},
    regional_hq:  {bg:"var(--bg-pro)",   color:"var(--text-pro)"},
  };
  const statusMeta = {
    active:             {bg:"var(--bg-success)",color:"var(--text-success)"},
    ramping:            {bg:"var(--bg-warning)",color:"var(--text-warning)"},
    under_construction: {bg:"var(--bg-warning)",color:"var(--text-warning)"},
    planned:            {bg:"var(--bg-neutral)", color:"var(--text-secondary)"},
    closed:             {bg:"var(--surface-1)",  color:"var(--text-muted)"},
  };
  const locs = r.locations||[];
  return (
    <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,overflow:"hidden"}}>
      {locs.map((loc,i) => {
        const tm = typeMeta[loc.type]||{bg:"var(--surface-1)",color:"var(--text-muted)"};
        const sm = statusMeta[loc.status]||{bg:"var(--surface-1)",color:"var(--text-muted)"};
        return (
          <div key={i} style={{display:"flex",alignItems:"center",gap:12,padding:"10px 1.25rem",borderBottom:i<locs.length-1?"0.5px solid var(--border)":"none"}}>
            <Badge bg={tm.bg} color={tm.color}>{(loc.type||"").toUpperCase().replace(/_/g," ")}</Badge>
            <div style={{flex:1,minWidth:0}}>
              <p style={{fontSize:13,fontWeight:500,margin:0}}>{[loc.name,[loc.city,loc.state_region,loc.country].filter(Boolean).join(", ")].filter(Boolean).join(" — ")}</p>
              {loc.notes && <p style={{fontSize:12,color:"var(--text-muted)",margin:"2px 0 0",lineHeight:1.4}}>{loc.notes}</p>}
            </div>
            <Badge bg={sm.bg} color={sm.color}>{loc.status}</Badge>
          </div>
        );
      })}
      {!locs.length && <p style={{padding:"1rem 1.25rem",color:"var(--text-muted)",fontSize:13}}>No locations identified.</p>}
    </div>
  );
}

function StakeholdersSection({ r }) {
  const roleColor = {economic_buyer:"var(--bg-danger)",technical_buyer:"var(--bg-accent)",executive_sponsor:"var(--bg-pro)",champion:"var(--bg-success)",influencer:"var(--bg-warning)",gatekeeper:"var(--surface-1)"};
  const roleText  = {economic_buyer:"var(--text-danger)",technical_buyer:"var(--text-accent)",executive_sponsor:"var(--text-pro)",champion:"var(--text-success)",influencer:"var(--text-warning)",gatekeeper:"var(--text-secondary)"};
  return (
    <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(260px,1fr))",gap:12}}>
      {(r.stakeholders||[]).map((s,i) => {
        const initials = s.name?.split(" ").filter(w=>!w.includes("(")).slice(0,2).map(w=>w[0]).join("").toUpperCase()||"?";
        const rt = s.role_type||"influencer";
        return (
          <div key={i} style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem",display:"flex",flexDirection:"column",gap:10}}>
            <div style={{display:"flex",alignItems:"center",gap:10}}>
              <div style={{width:36,height:36,borderRadius:"50%",background:"var(--bg-accent)",color:"var(--text-accent)",fontSize:13,fontWeight:500,display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0}}>{initials}</div>
              <div style={{flex:1,minWidth:0}}>
                <p style={{fontSize:13,fontWeight:500,margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{s.name}</p>
                <p style={{fontSize:12,color:"var(--text-muted)",margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{s.title}</p>
              </div>
            </div>
            <div style={{display:"flex",gap:6,flexWrap:"wrap"}}>
              <Badge bg={roleColor[rt]||"var(--surface-1)"} color={roleText[rt]||"var(--text-secondary)"}>{rt.replace(/_/g," ")}</Badge>
              {s.department && <Badge>{s.department}</Badge>}
              {s.location   && <Badge>{s.location}</Badge>}
            </div>
            <InfluenceBar score={s.influence_score||0}/>
            {s.notes && <p style={{fontSize:12,color:"var(--text-secondary)",margin:0,lineHeight:1.5}}>{s.notes}</p>}
          </div>
        );
      })}
      {!(r.stakeholders||[]).length && <p style={{color:"var(--text-muted)",fontSize:13}}>No stakeholders identified.</p>}
    </div>
  );
}

function EconomicsSection({ r }) {
  const f    = r.economics?.financials||{};
  const semi = r.economics?.semiconductor_profile||{};
  const segs = r.economics?.revenue_segments||[];
  const metrics = [
    ["Revenue",     fmtM(f.revenue_usd_m),   `FY${f.fiscal_year||"N/A"}`],
    ["YoY growth",  fmtP(f.revenue_growth_pct),""],
    ["Gross margin",fmtP(f.gross_margin_pct), ""],
    ["R&D spend",   fmtM(f.rd_spend_usd_m),   f.rd_pct_revenue?fmtP(f.rd_pct_revenue)+" of rev":""],
    ["Capex",       fmtM(f.capex_usd_m),      ""],
    ["Market cap",  fmtM(f.market_cap_usd_m), ""],
  ];
  return (
    <div style={{display:"flex",flexDirection:"column",gap:16}}>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))",gap:12}}>
        {metrics.map(([label,val,sub]) => (
          <div key={label} style={{background:"var(--surface-1)",borderRadius:"var(--radius)",padding:"0.85rem 1rem"}}>
            <p style={{fontSize:11,color:"var(--text-muted)",margin:"0 0 4px"}}>{label}</p>
            <p style={{fontSize:20,fontWeight:500,margin:0}}>{val}</p>
            {sub && <p style={{fontSize:11,color:"var(--text-secondary)",margin:"2px 0 0"}}>{sub}</p>}
          </div>
        ))}
      </div>
      {segs.length>0 && (
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:10}}>Revenue segments</p>
          {segs.map((seg,i) => (
            <div key={i} style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"5px 0",borderBottom:i<segs.length-1?"0.5px solid var(--border)":"none",gap:8}}>
              <span style={{fontSize:13,flex:1}}>{seg.name}</span>
              {seg.relevant_to_us && <Badge bg="var(--bg-accent)" color="var(--text-accent)">relevant</Badge>}
              <span style={{fontSize:13,color:"var(--text-secondary)"}}>{fmtM(seg.revenue_usd_m)} ({Number(seg.pct_of_total||0).toFixed(0)}%)</span>
            </div>
          ))}
        </div>
      )}
      <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
        <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:10}}>Semiconductor profile</p>
        <FieldRow label="Buyer type"      value={semi.chip_buyer_type}/>
        <FieldRow label="Has own fab"     value={semi.has_own_fab!=null?(semi.has_own_fab?"Yes":"No"):null}/>
        <FieldRow label="Fab nodes"       value={(semi.fab_nodes||[]).join(", ")||null}/>
        <FieldRow label="Known suppliers" value={(semi.known_chip_suppliers||[]).join(", ")||null}/>
        <FieldRow label="Est. chip spend" value={semi.annual_chip_spend_est_usd_m?fmtM(semi.annual_chip_spend_est_usd_m):null}/>
        {(semi.known_supply_gaps||[]).length>0 && (
          <div style={{marginTop:12}}>
            <p style={{fontSize:12,color:"var(--text-muted)",marginBottom:6}}>Known supply gaps</p>
            <div style={{display:"flex",flexDirection:"column",gap:4}}>
              {semi.known_supply_gaps.map((g,i) => (
                <div key={i} style={{fontSize:13,padding:"4px 10px",background:"var(--bg-warning)",borderRadius:"var(--radius)",color:"var(--text-primary)"}}>{g}</div>
              ))}
            </div>
          </div>
        )}
        {semi.design_win_notes && <p style={{fontSize:13,color:"var(--text-secondary)",marginTop:12,lineHeight:1.55}}>{semi.design_win_notes}</p>}
        {semi.qualification_cycle && (
          <div style={{marginTop:12,padding:"8px 12px",background:"var(--bg-accent)",border:"0.5px solid var(--border-accent)",borderRadius:"var(--radius)"}}>
            <p style={{fontSize:11,color:"var(--text-accent)",fontWeight:500,margin:"0 0 2px"}}>Qualification cycle</p>
            <p style={{fontSize:13,margin:0,color:"var(--text-primary)"}}>{semi.qualification_cycle}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function IntelligenceSection({ r }) {
  const intel = r.account_intelligence||{};
  const sigs  = intel.opportunity_signals||[];
  return (
    <div style={{display:"flex",flexDirection:"column",gap:16}}>
      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:12}}>
        <div style={{background:"var(--surface-1)",borderRadius:"var(--radius)",padding:"0.85rem 1rem"}}>
          <p style={{fontSize:11,color:"var(--text-muted)",margin:"0 0 4px"}}>Supplier status</p>
          <p style={{fontSize:15,fontWeight:500,margin:0,textTransform:"capitalize"}}>{(intel.current_supplier_status||"no_relationship").replace(/_/g," ")}</p>
        </div>
        <div style={{background:"var(--surface-1)",borderRadius:"var(--radius)",padding:"0.85rem 1rem"}}>
          <p style={{fontSize:11,color:"var(--text-muted)",margin:"0 0 4px"}}>Signals identified</p>
          <p style={{fontSize:15,fontWeight:500,margin:0}}>{sigs.length}</p>
        </div>
      </div>
      {intel.recommended_next_action && (
        <div style={{padding:"12px 16px",background:"var(--bg-accent)",border:"0.5px solid var(--border-accent)",borderRadius:12}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-accent)",margin:"0 0 4px"}}>Recommended next action</p>
          <p style={{fontSize:13,color:"var(--text-primary)",margin:0,lineHeight:1.55}}>{intel.recommended_next_action}</p>
        </div>
      )}
      <div style={{display:"flex",flexDirection:"column",gap:8}}>
        {sigs.map((sig,i) => (
          <div key={i} style={{background:sigB(sig.type),border:`0.5px solid ${sigBr(sig.type)}`,borderRadius:12,padding:"10px 14px"}}>
            <div style={{display:"flex",gap:8,marginBottom:6,flexWrap:"wrap"}}>
              <Badge bg={sigB(sig.type)} color={sigC(sig.type)}>{sig.type}</Badge>
              <Badge>{(sig.urgency||"").replace(/_/g," ")}</Badge>
            </div>
            <p style={{fontSize:13,color:"var(--text-primary)",margin:0,lineHeight:1.55}}>{sig.description}</p>
            {sig.source && <p style={{fontSize:11,color:"var(--text-muted)",margin:"6px 0 0"}}>Source: {sig.source}</p>}
          </div>
        ))}
        {!sigs.length && <p style={{color:"var(--text-muted)",fontSize:13}}>No signals identified.</p>}
      </div>
    </div>
  );
}

function NewsSection({ r }) {
  const items = r.news||[];
  return (
    <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,overflow:"hidden"}}>
      {items.map((item,i) => (
        <div key={i} style={{padding:"12px 1.25rem",borderBottom:i<items.length-1?"0.5px solid var(--border)":"none"}}>
          <div style={{display:"flex",alignItems:"baseline",gap:12,marginBottom:4,flexWrap:"wrap"}}>
            <span style={{fontSize:12,color:"var(--text-muted)",flexShrink:0,fontFamily:"var(--font-mono)"}}>{item.date}</span>
            <span style={{fontSize:13,fontWeight:500}}>{item.headline}</span>
          </div>
          {item.summary && <p style={{fontSize:12,color:"var(--text-secondary)",margin:"0 0 6px",lineHeight:1.5}}>{item.summary}</p>}
          <div style={{display:"flex",gap:6,alignItems:"center",flexWrap:"wrap"}}>
            <span style={{fontSize:11,color:"var(--text-muted)"}}>{item.source_name}</span>
            {(item.tags||[]).slice(0,4).map(t=><Badge key={t}>{t}</Badge>)}
          </div>
        </div>
      ))}
      {!items.length && <p style={{padding:"1rem 1.25rem",color:"var(--text-muted)",fontSize:13}}>No recent news found.</p>}
    </div>
  );
}

// ── Sidebar ────────────────────────────────────────────────────
function Sidebar({ records, activeId, onSelect, onNew, onDelete }) {
  return (
    <div className="no-print" style={{width:220,flexShrink:0,background:"var(--surface-1)",borderRight:"0.5px solid var(--border)",display:"flex",flexDirection:"column",overflow:"hidden"}}>
      <div style={{padding:"1rem",borderBottom:"0.5px solid var(--border)",flexShrink:0}}>
        <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12}}>
          <i className="ti ti-radar-2" style={{fontSize:18,color:"var(--text-accent)"}}/>
          <span style={{fontSize:13,fontWeight:500}}>Account research</span>
        </div>
        <button onClick={onNew} className="primary" style={{width:"100%",fontSize:13,display:"flex",alignItems:"center",justifyContent:"center",gap:6}}>
          <i className="ti ti-plus" style={{fontSize:14}}/>New research
        </button>
      </div>
      {records.length>0 && (
        <p style={{fontSize:11,color:"var(--text-muted)",padding:"0.75rem 0.75rem 0.25rem",margin:0}}>Saved accounts ({records.length})</p>
      )}
      <div style={{flex:1,overflowY:"auto",padding:"0.25rem 0.5rem 0.5rem"}}>
        {!records.length && <p style={{fontSize:12,color:"var(--text-muted)",padding:"0.75rem",textAlign:"center",lineHeight:1.5}}>No accounts saved yet.</p>}
        {records.map(r => {
          const active = r.meta.record_id===activeId;
          return (
            <div key={r.meta.record_id} onClick={()=>onSelect(r)}
              style={{padding:"8px 10px",borderRadius:"var(--radius)",cursor:"pointer",marginBottom:2,background:active?"var(--surface-2)":"transparent",border:`0.5px solid ${active?"var(--border)":"transparent"}`,transition:"background .1s",display:"flex",alignItems:"center",gap:4}}
              onMouseEnter={e=>{if(!active)e.currentTarget.style.background="var(--surface-2)"}}
              onMouseLeave={e=>{if(!active)e.currentTarget.style.background="transparent"}}>
              <div style={{flex:1,minWidth:0}}>
                <p style={{fontSize:13,fontWeight:500,margin:"0 0 3px",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{r.overview?.legal_name||r.company_name}</p>
                <div style={{display:"flex",alignItems:"center",gap:5}}>
                  <span style={{width:6,height:6,borderRadius:"50%",background:confC(r.meta?.confidence?.overall),flexShrink:0,display:"inline-block"}}/>
                  <span style={{fontSize:11,color:"var(--text-muted)"}}>{fmtD(r.meta?.refreshed_at)}</span>
                </div>
              </div>
              <DeleteButton compact onDelete={()=>onDelete(r.meta.record_id)}/>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Dashboard ──────────────────────────────────────────────────
function Dashboard({ records, onSelect, onNew, onDelete }) {
  if (!records.length) return (
    <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",gap:16,padding:"4rem 2rem",textAlign:"center"}}>
      <i className="ti ti-building-community" style={{fontSize:48,color:"var(--text-muted)"}}/>
      <div>
        <p style={{fontSize:18,fontWeight:500,margin:"0 0 8px"}}>No accounts researched yet</p>
        <p style={{fontSize:14,color:"var(--text-muted)",margin:0}}>Run the research pipeline on your first target company.</p>
      </div>
      <button onClick={onNew} className="primary">Research a company ↗</button>
    </div>
  );
  return (
    <div style={{padding:"1.5rem",display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(280px,1fr))",gap:16,alignContent:"start",overflowY:"auto",flex:1}}>
      {records.map(r => (
        <div key={r.meta.record_id} onClick={()=>onSelect(r)}
          style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1.25rem",cursor:"pointer",transition:"border-color .15s"}}
          onMouseEnter={e=>e.currentTarget.style.borderColor="var(--border-strong)"}
          onMouseLeave={e=>e.currentTarget.style.borderColor="var(--border)"}>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:6,gap:8}}>
            <p style={{fontSize:15,fontWeight:500,margin:0,flex:1,lineHeight:1.3}}>{r.overview?.legal_name||r.company_name}</p>
            <div style={{display:"flex",alignItems:"center",gap:6,flexShrink:0}}>
              <Badge bg={confB(r.meta?.confidence?.overall)} color={confC(r.meta?.confidence?.overall)}>{r.meta?.confidence?.overall||"—"}</Badge>
              <DeleteButton compact onDelete={()=>onDelete(r.meta.record_id)}/>
            </div>
          </div>
          {r.domain && <p style={{fontSize:12,color:"var(--text-muted)",margin:"0 0 4px",fontFamily:"var(--font-mono)"}}>{r.domain}{r.ticker ? ` · ${r.ticker}` : ""}</p>}
          {r.overview?.parent_company && <p style={{fontSize:12,color:"var(--text-muted)",margin:"0 0 10px"}}>{r.overview.parent_company}</p>}
          <div style={{display:"flex",flexWrap:"wrap",gap:4,marginBottom:14}}>
            {(r.overview?.industry_verticals||[]).slice(0,3).map(v=><Badge key={v}>{v}</Badge>)}
          </div>
          <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",paddingTop:10,borderTop:"0.5px solid var(--border)"}}>
            <span style={{fontSize:12,color:"var(--text-muted)"}}>{fmtD(r.meta?.refreshed_at)}</span>
            <div style={{display:"flex",gap:10}}>
              <span style={{fontSize:12,color:"var(--text-muted)"}}>{(r.locations||[]).length} locations</span>
              <span style={{fontSize:12,color:"var(--text-muted)"}}>{(r.stakeholders||[]).length} stakeholders</span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

// ── New research form ──────────────────────────────────────────
function NewResearchForm({ form, onChange, onSubmit, error, isLoading }) {
  return (
    <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",padding:"2rem",overflowY:"auto"}}>
      <div style={{width:"100%",maxWidth:480}}>
        <i className="ti ti-building-search" style={{fontSize:28,color:"var(--text-accent)",display:"block",marginBottom:12}}/>
        <h2 style={{fontSize:22,fontWeight:500,margin:"0 0 6px"}}>Research a company</h2>
        <p style={{fontSize:14,color:"var(--text-muted)",margin:"0 0 28px"}}>The pipeline runs four stages: company overview, financial data, stakeholder map, and account intelligence.</p>
        <div style={{display:"flex",flexDirection:"column",gap:16,marginBottom:24}}>
          {/* Company name */}
          <div>
            <label style={{display:"block",fontSize:13,color:"var(--text-secondary)",marginBottom:4}}>
              Company name <span style={{color:"var(--text-danger)"}}>*</span>
            </label>
            <input
              placeholder="Intel Corporation"
              value={form.company}
              onChange={e=>onChange({...form,company:e.target.value})}
              style={{width:"100%"}}
            />
          </div>
          {/* Domain */}
          <div>
            <label style={{display:"block",fontSize:13,color:"var(--text-secondary)",marginBottom:4}}>
              Website domain <span style={{color:"var(--text-danger)"}}>*</span>
            </label>
            <div style={{position:"relative"}}>
              <span style={{position:"absolute",left:12,top:"50%",transform:"translateY(-50%)",fontSize:13,color:"var(--text-muted)",pointerEvents:"none",fontFamily:"var(--font-mono)"}}>https://</span>
              <input
                placeholder="intel.com"
                value={form.domain}
                onChange={e=>onChange({...form,domain:e.target.value.replace(/^https?:\/\//,"")})}
                style={{width:"100%",paddingLeft:72,fontFamily:"var(--font-mono)"}}
              />
            </div>
            <p style={{fontSize:11,color:"var(--text-muted)",margin:"4px 0 0"}}>Used to anchor the research to the right company.</p>
          </div>
          {/* Ticker */}
          <div>
            <label style={{display:"block",fontSize:13,color:"var(--text-secondary)",marginBottom:4}}>
              Stock ticker <span style={{color:"var(--text-muted)",fontWeight:400}}>— optional</span>
            </label>
            <input
              placeholder="INTC"
              value={form.ticker}
              onChange={e=>onChange({...form,ticker:e.target.value.toUpperCase()})}
              style={{width:"100%",fontFamily:"var(--font-mono)",textTransform:"uppercase",letterSpacing:"0.05em"}}
            />
            <p style={{fontSize:11,color:"var(--text-muted)",margin:"4px 0 0"}}>Providing a ticker enables audited financial data from public filings.</p>
          </div>
        </div>
        {error && (
          <div style={{padding:"10px 14px",background:"var(--bg-danger)",border:"0.5px solid var(--border-danger)",borderRadius:"var(--radius)",fontSize:13,color:"var(--text-danger)",marginBottom:16}}>{error}</div>
        )}
        <button onClick={onSubmit} disabled={isLoading||!form.company.trim()||!form.domain.trim()} className="primary" style={{width:"100%",fontSize:14,padding:"10px"}}>
          {isLoading?"Starting pipeline…":"Run research ↗"}
        </button>
      </div>
    </div>
  );
}

// ── Pipeline running view ──────────────────────────────────────
function PipelineView({ stageStatuses, currentRecord, log }) {
  const ov = currentRecord?.overview;
  return (
    <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",padding:"2rem",overflowY:"auto"}}>
      <div style={{width:"100%",maxWidth:600}}>
        <h2 style={{fontSize:18,fontWeight:500,margin:"0 0 2px"}}>{ov?.legal_name||currentRecord?.company_name||"Running pipeline…"}</h2>
        <p style={{fontSize:13,color:"var(--text-muted)",margin:"0 0 0"}}>
          {currentRecord?.domain && <span style={{fontFamily:"var(--font-mono)"}}>{currentRecord.domain}</span>}
          {currentRecord?.ticker && <span style={{color:"var(--text-secondary)"}}> · {currentRecord.ticker}</span>}
          {!currentRecord?.domain && "Synthesizing data across four stages"}
        </p>
        <PipelineTracker statuses={stageStatuses}/>
        <div style={{background:"var(--surface-1)",border:"0.5px solid var(--border)",borderRadius:"var(--radius)",padding:"0.75rem 1rem",maxHeight:180,overflowY:"auto"}}>
          {!log.length && <p style={{fontSize:12,color:"var(--text-muted)",margin:0,fontFamily:"var(--font-mono)"}}>Starting…</p>}
          {log.map((entry,i) => <p key={i} style={{fontSize:12,color:"var(--text-muted)",margin:"2px 0",fontFamily:"var(--font-mono)"}}>{entry}</p>)}
        </div>
      </div>
    </div>
  );
}

// ── Results view ───────────────────────────────────────────────
function ResultsView({ record, activeTab, setActiveTab, onBack, onSave, onDelete, onToast }) {
  const ov   = record.overview||{};
  const conf = record.meta?.confidence||{};
  const content = {
    overview:     <OverviewSection     r={record}/>,
    locations:    <LocationsSection    r={record}/>,
    stakeholders: <StakeholdersSection r={record}/>,
    economics:    <EconomicsSection    r={record}/>,
    intelligence: <IntelligenceSection r={record}/>,
    news:         <NewsSection         r={record}/>,
  };
  return (
    <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
      {/* Topbar */}
      <div className="no-print" style={{padding:"0.75rem 1.5rem",borderBottom:"0.5px solid var(--border)",background:"var(--surface-2)",display:"flex",alignItems:"center",gap:10,flexShrink:0,flexWrap:"wrap"}}>
        <button onClick={onBack} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4,flexShrink:0}}>
          <i className="ti ti-arrow-left" style={{fontSize:13}}/>Back
        </button>
        <div style={{flex:1,minWidth:0}}>
          <h2 style={{fontSize:15,fontWeight:500,margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{ov.legal_name||record.company_name}</h2>
          <p style={{fontSize:12,color:"var(--text-muted)",margin:0,fontFamily:"var(--font-mono)"}}>
            {record.domain||""}
            {record.ticker ? ` · ${record.ticker}` : ""}
            {(record.domain||record.ticker) ? " · " : ""}
            {fmtD(record.meta?.refreshed_at)}
          </p>
        </div>
        <div style={{display:"flex",gap:6,alignItems:"center",flexWrap:"wrap",flexShrink:0}}>
          <Badge bg={confB(conf.overall)} color={confC(conf.overall)}>{conf.overall||"medium"} confidence</Badge>
          <button onClick={onSave} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4}}>
            <i className="ti ti-bookmark" style={{fontSize:13}}/>Save
          </button>
          <button onClick={async()=>{await copyToClipboard(toMarkdown(record));onToast("Markdown copied ✓");}} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4}}>
            <i className="ti ti-clipboard" style={{fontSize:13}}/>Copy Markdown
          </button>
          <button onClick={async()=>{await copyToClipboard(JSON.stringify(record,null,2));onToast("JSON copied ✓");}} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4}}>
            <i className="ti ti-clipboard-data" style={{fontSize:13}}/>Copy JSON
          </button>
          <button onClick={()=>{openPdfReport(record);onToast("PDF opened — use Ctrl+P to save");}} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4}}>
            <i className="ti ti-printer" style={{fontSize:13}}/>Save PDF
          </button>
          <DeleteButton onDelete={()=>onDelete(record.meta.record_id)}/>
        </div>
      </div>
      {/* Tabs */}
      <div className="no-print" style={{display:"flex",borderBottom:"0.5px solid var(--border)",padding:"0 1.5rem",flexShrink:0,overflowX:"auto"}}>
        {TABS.map(tab => (
          <button key={tab.key} onClick={()=>setActiveTab(tab.key)} style={{
            fontSize:13,padding:"10px 14px",borderRadius:0,border:"none",
            borderBottom:`2px solid ${activeTab===tab.key?"var(--fill-accent)":"transparent"}`,
            background:"transparent",color:activeTab===tab.key?"var(--text-accent)":"var(--text-secondary)",
            display:"flex",alignItems:"center",gap:6,flexShrink:0,cursor:"pointer",
          }}>
            <i className={`ti ${tab.icon}`} style={{fontSize:14}}/>{tab.label}
          </button>
        ))}
      </div>
      {/* Content */}
      <div className="print-area" style={{flex:1,overflowY:"auto",padding:"1.5rem"}}>{content[activeTab]}</div>
    </div>
  );
}

// ── Root app ───────────────────────────────────────────────────
export default function App() {
  const [view,          setView]          = useState("dashboard");
  const [records,       setRecords]       = useState([]);
  const [currentRecord, setCurrentRecord] = useState(null);
  const [stageStatuses, setStageStatuses] = useState(["pending","pending","pending","pending"]);
  const [activeTab,     setActiveTab]     = useState("overview");
  const [form,          setForm]          = useState({ company:"", domain:"", ticker:"" });
  const [log,           setLog]           = useState([]);
  const [error,         setError]         = useState(null);
  const [isLoading,     setIsLoading]     = useState(false);
  const [toast,         setToast]         = useState("");

  // Load saved records from shared storage on mount
  useEffect(() => {
    (async () => {
      try {
        const r = await window.storage.get(STORAGE_KEY, true);
        if (r?.value) setRecords(JSON.parse(r.value));
      } catch {}
    })();
  }, []);

  const persistRecords = async (updated) => {
    setRecords(updated);
    try { await window.storage.set(STORAGE_KEY, JSON.stringify(updated), true); } catch {}
  };

  const addLog  = msg => setLog(p => [...p, `[${new Date().toLocaleTimeString()}] ${msg}`]);
  const setStage = (i, status) => setStageStatuses(p => { const n=[...p]; n[i]=status; return n; });

  const showToast = (msg) => { setToast(msg); setTimeout(()=>setToast(""),2500); };

  const runPipeline = async () => {
    if (!form.company.trim()||!form.domain.trim()) return;
    setIsLoading(true); setError(null);
    setStageStatuses(["pending","pending","pending","pending"]);
    setLog([]);
    const now = new Date().toISOString();
    let rec = {
      company_name: form.company,
      domain: form.domain.replace(/^https?:\/\//,""),
      ticker: form.ticker||null,
      meta: {
        schema_version:"1.0.0",
        record_id:`${slug(form.company)}_${Date.now()}`,
        created_at:now, refreshed_at:now, refresh_cadence:"weekly",
        confidence:{overall:"medium",overview:"medium",locations:"medium",stakeholders:"medium",economics:"medium"},
        sources:[],
      },
      overview: { legal_name:form.company, website:`https://${form.domain}`, industry_verticals:[], competitors:[], key_products:[] },
      locations:[], stakeholders:[],
      economics: { financials:{}, revenue_segments:[], semiconductor_profile:{} },
      account_intelligence: { opportunity_signals:[], recommended_next_action:null, current_supplier_status:"no_relationship" },
      news:[],
    };
    setCurrentRecord(rec); setView("pipeline");

    // Stage 1 — Overview
    setStage(0,"running"); addLog(`Researching ${form.company} (${form.domain})…`);
    try {
      const d = await runOverview(form.company, form.domain, form.ticker);
      rec = { ...rec, overview:{...rec.overview,...(d.overview||{})}, locations:d.locations||[] };
      rec.meta.confidence.overview  = d.overview?.summary?"high":"medium";
      rec.meta.confidence.locations = (d.locations||[]).length>1?"high":"medium";
      setCurrentRecord({...rec}); setStage(0,"done");
      addLog(`Overview: ${rec.overview.legal_name} · ${rec.locations.length} locations`);
    } catch(e) { setStage(0,"error"); addLog(`Overview error: ${e.message}`); }

    // Stage 2 — Economics
    setStage(1,"running"); addLog(`Extracting financial data${form.ticker?" for "+form.ticker:""}…`);
    try {
      const d = await runEconomics(form.company, form.ticker);
      rec = { ...rec, economics:{ financials:d.financials||{}, revenue_segments:d.revenue_segments||[], semiconductor_profile:d.semiconductor_profile||{} }};
      rec.meta.confidence.economics = d.financials?.data_quality==="audited"?"high":d.financials?.revenue_usd_m?"medium":"low";
      setCurrentRecord({...rec}); setStage(1,"done");
      addLog(`Economics: ${d.financials?.revenue_usd_m?fmtM(d.financials.revenue_usd_m)+" revenue":"estimates only"} · ${d.financials?.data_quality||"inferred"}`);
    } catch(e) { setStage(1,"error"); addLog(`Economics error: ${e.message}`); }

    // Stage 3 — Stakeholders
    setStage(2,"running"); addLog("Mapping stakeholders…");
    try {
      const d = await runStakeholders(form.company);
      const stks = Array.isArray(d)?d:[];
      rec = { ...rec, stakeholders:stks };
      rec.meta.confidence.stakeholders = stks.length>=3?"medium":"low";
      setCurrentRecord({...rec}); setStage(2,"done");
      addLog(`Stakeholders: ${stks.length} decision-makers identified`);
    } catch(e) { setStage(2,"error"); addLog(`Stakeholders error: ${e.message}`); }

    // Stage 4 — Intelligence
    setStage(3,"running"); addLog("Assembling account intelligence…");
    try {
      const d = await runIntelligence(form.company);
      rec = { ...rec, account_intelligence:d.account_intelligence||rec.account_intelligence, news:d.news||[] };
      const lvls = [rec.meta.confidence.overview,rec.meta.confidence.locations,rec.meta.confidence.stakeholders,rec.meta.confidence.economics];
      rec.meta.confidence.overall = lvls.includes("low")?"low":lvls.filter(l=>l==="high").length>=2?"high":"medium";
      rec.meta.refreshed_at = new Date().toISOString();
      setCurrentRecord({...rec}); setStage(3,"done");
      addLog(`Intelligence: ${(d.account_intelligence?.opportunity_signals||[]).length} signals · ${(d.news||[]).length} news items`);
    } catch(e) { setStage(3,"error"); addLog(`Intelligence error: ${e.message}`); }

    addLog("Pipeline complete — review results and save to team.");
    setIsLoading(false); setActiveTab("overview"); setView("results");
  };

  const handleSave = async () => {
    if (!currentRecord) return;
    const key = normKey(currentRecord);
    const supersedes = records.some(r => normKey(r) === key && r.meta.record_id !== currentRecord.meta.record_id);
    const updated = [currentRecord, ...records.filter(r => normKey(r) !== key)];
    await persistRecords(updated);
    showToast(supersedes ? "Updated saved profile ✓" : "Saved to team ✓");
  };

  const handleDelete = async (recordId) => {
    const updated = records.filter(r => r.meta.record_id !== recordId);
    await persistRecords(updated);
    showToast("Account deleted");
    if (currentRecord?.meta?.record_id === recordId) {
      setCurrentRecord(null);
      setView("dashboard");
    }
  };

  return (
    <>
      <style>{CSS}</style>
      <div style={{display:"flex",height:"100vh",overflow:"hidden",fontFamily:"var(--font-sans)"}}>
        <h1 className="sr-only">Account research pipeline — semiconductor sales intelligence</h1>

        <Sidebar
          records={records}
          activeId={view==="results"?currentRecord?.meta?.record_id:null}
          onSelect={r=>{setCurrentRecord(r);setActiveTab("overview");setView("results");}}
          onNew={()=>{setView("new");setError(null);setForm({company:"",domain:"",ticker:""}); }}
          onDelete={handleDelete}
        />

        <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
          {view==="dashboard" && <Dashboard records={records} onSelect={r=>{setCurrentRecord(r);setActiveTab("overview");setView("results");}} onNew={()=>setView("new")} onDelete={handleDelete}/>}
          {view==="new"       && <NewResearchForm form={form} onChange={setForm} onSubmit={runPipeline} error={error} isLoading={isLoading}/>}
          {view==="pipeline"  && <PipelineView stageStatuses={stageStatuses} currentRecord={currentRecord} log={log}/>}
          {view==="results" && currentRecord && (
            <ResultsView record={currentRecord} activeTab={activeTab} setActiveTab={setActiveTab}
              onBack={()=>setView("dashboard")}
              onSave={handleSave}
              onDelete={handleDelete}
              onToast={showToast}/>
          )}
        </div>

        {toast && (
          <div style={{position:"fixed",bottom:24,right:24,padding:"10px 16px",background:"var(--bg-success)",border:"0.5px solid var(--border-success)",borderRadius:"var(--radius)",fontSize:13,color:"var(--text-success)",display:"flex",alignItems:"center",gap:6,zIndex:999}}>
            <i className="ti ti-check"/>{toast}
          </div>
        )}
      </div>
    </>
  );
}