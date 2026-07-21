import { useState, useEffect } from "react";

/**
 * Account Research — Record Viewer
 * ---------------------------------
 * This is a VIEWER, not a research runner. Unlike app.jsx (which calls the
 * Claude API + web search directly from the browser), this artifact expects
 * the Python pipeline (run_research.py) to have already produced a JSON
 * record on the server side — including audited SEC EDGAR financials,
 * 10-K narrative extraction, and benchmark-derived segment SAM estimates —
 * and simply imports + displays that JSON. No API calls happen here.
 *
 * Why the split exists: SEC's data.sec.gov API does not support CORS and
 * requires a User-Agent header browsers refuse to set, so EDGAR can only
 * ever be reached from a server/CLI context, never from this sandboxed
 * artifact. This file is the "consume the output" half of that pipeline.
 *
 * EXPECTED JSON CONTRACT (matches models/schema.py's AccountResearchRecord,
 * plus one addition — economics.segment_sam_estimates — described below):
 *
 * {
 *   meta: {
 *     schema_version, record_id, refreshed_at, refresh_cadence,
 *     confidence: { overall, overview, locations, stakeholders, economics },
 *     sources: [{ name, url, fetched_at, fields_populated }]
 *   },
 *   overview: { legal_name, doing_business_as, ticker, ownership_type,
 *     parent_company, founded_year, website, industry_verticals,
 *     employee_count, summary, competitors, key_products },
 *   locations: [{ type, name, city, state_region, country, status, notes }],
 *   stakeholders: [{ name, title, department, location, role_type,
 *     influence_score, notes }],
 *   economics: {
 *     financials: { fiscal_year, revenue_usd_m, revenue_growth_pct,
 *       gross_margin_pct, rd_spend_usd_m, rd_pct_revenue, capex_usd_m,
 *       data_quality },   // data_quality: "audited" | "estimated" | "inferred"
 *     revenue_segments: [{ name, revenue_usd_m, pct_of_total, relevant_to_us }],
 *     semiconductor_profile: { chip_buyer_type, has_own_fab, fab_nodes,
 *       known_chip_suppliers, known_supply_gaps, qualification_cycle },
 *     // NEW — benchmark-derived estimate, distinct from audited financials:
 *     segment_sam_estimates: [{
 *       segment,                 // "power" | "ep" | "hpc" | "conn" | "ams"
 *       estimated_sam_low_usd_m, estimated_sam_high_usd_m,
 *       unit_basis,               // e.g. "per vehicle produced"
 *       benchmark_source,         // e.g. "AlixPartners (2026)"
 *       benchmark_confidence,     // "sourced" | "directional" | "needs_research"
 *       rationale
 *     }]
 *   },
 *   account_intelligence: { opportunity_signals, recommended_next_action,
 *     current_supplier_status },
 *   news: [{ date, headline, summary, source_name, url, tags }]
 * }
 *
 * Records are stored under a DIFFERENT storage key than app.jsx so the two
 * artifacts' data never collide — their record shapes aren't identical.
 */

const STORAGE_KEY = "pipeline_research_records_v1";

const SEGMENT_LABELS = {
  power: "Power",
  ep:    "Embedded Processing",
  hpc:   "High-Performance Compute",
  conn:  "Connectivity",
  ams:   "Analog & Mixed-Signal",
};
const SEGMENT_ORDER = ["power", "ep", "hpc", "conn", "ams"];

const TABS = [
  { key: "overview",     label: "Overview",      icon: "ti-info-circle" },
  { key: "locations",    label: "Locations",     icon: "ti-map-pin"     },
  { key: "stakeholders", label: "Stakeholders",  icon: "ti-users"       },
  { key: "economics",    label: "Economics",     icon: "ti-chart-bar"   },
  { key: "segments",     label: "Segment SAM",   icon: "ti-radar-2"     },
  { key: "intelligence", label: "Intelligence",  icon: "ti-bulb"        },
  { key: "news",         label: "News",          icon: "ti-news"        },
];

// ── CSS — same token system as app.jsx, kept identical on purpose: these
// two artifacts are companion tools used side by side by the same team,
// so visual continuity matters more than novelty here. ──────────────────
const CSS = `
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
  @import url('https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@latest/tabler-icons.min.css');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --font-sans: 'Inter', system-ui, sans-serif;
    --font-mono: 'JetBrains Mono', monospace;
    --radius: 8px;

    --bg:           #0d1117;
    --surface-1:    #161b22;
    --surface-2:    #1c2230;

    --border:         rgba(255,255,255,0.08);
    --border-strong:  rgba(255,255,255,0.16);
    --border-accent:  rgba(56,139,253,0.4);
    --border-success: rgba(46,160,67,0.4);
    --border-warning: rgba(210,153,34,0.4);
    --border-danger:  rgba(248,81,73,0.4);

    --text-primary:   #e6edf3;
    --text-secondary: #8b949e;
    --text-muted:     #484f58;
    --text-accent:    #58a6ff;
    --text-success:   #3fb950;
    --text-warning:   #d29922;
    --text-danger:    #f85149;
    --text-pro:       #a371f7;

    --fill-accent:  #388bfd;
    --fill-success: #2ea043;
    --fill-warning: #9e6a03;
    --fill-danger:  #da3633;

    --bg-accent:   rgba(56,139,253,0.1);
    --bg-success:  rgba(46,160,67,0.1);
    --bg-warning:  rgba(210,153,34,0.1);
    --bg-danger:   rgba(248,81,73,0.1);
    --bg-pro:      rgba(163,113,247,0.1);
    --bg-neutral:  rgba(139,148,158,0.1);
  }

  body { font-family: var(--font-sans); background: var(--bg); color: var(--text-primary); font-size: 14px; line-height: 1.5; height: 100vh; overflow: hidden; }

  textarea, input {
    font-family: var(--font-mono);
    font-size: 12.5px;
    background: var(--surface-1);
    border: 0.5px solid var(--border-strong);
    border-radius: var(--radius);
    color: var(--text-primary);
    padding: 8px 12px;
    outline: none;
    transition: border-color .15s;
  }
  textarea:focus, input:focus { border-color: var(--border-accent); }
  textarea::placeholder, input::placeholder { color: var(--text-muted); font-family: var(--font-sans); }

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
  button.primary { background: var(--fill-accent); border-color: transparent; color: #fff; font-weight: 500; }
  button.primary:hover:not(:disabled) { background: #58a6ff; }

  input[type="file"] { padding: 6px 10px; font-family: var(--font-sans); }

  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 2px; }
`;

// ── Formatters ────────────────────────────────────────────────
const fmtD = iso => { try { return new Date(iso).toLocaleDateString("en-US",{month:"short",day:"numeric",year:"numeric"}); } catch { return "—"; } };
const fmtP = v => v != null ? `${Number(v).toFixed(1)}%` : "—";
const fmtM = v => {
  if (v == null) return "—";
  const n = Number(v);
  return Math.abs(n) >= 1000 ? `$${(n/1000).toFixed(2)}B` : `$${n.toLocaleString()}M`;
};
const fmtRange = (lo, hi) => {
  if (lo == null && hi == null) return "—";
  if (lo == null) return `up to ${fmtM(hi)}`;
  if (hi == null) return `${fmtM(lo)}+`;
  return `${fmtM(lo)} – ${fmtM(hi)}`;
};

// Normalizes the several confidence vocabularies used across the record
// (ConfidenceMap: high/medium/low; DataQuality: audited/estimated/inferred;
// benchmark_confidence: sourced/directional/needs_research) into one
// good/ok/weak color tier so badges stay visually consistent.
const GOOD = new Set(["high","audited","sourced"]);
const OK   = new Set(["medium","estimated","directional"]);
const WEAK = new Set(["low","inferred","needs_research"]);
const tierColor = label => {
  const l = (label||"").toLowerCase();
  if (GOOD.has(l)) return { c: "var(--text-success)", bg: "var(--bg-success)", br: "var(--border-success)" };
  if (OK.has(l))   return { c: "var(--text-warning)", bg: "var(--bg-warning)", br: "var(--border-warning)" };
  if (WEAK.has(l)) return { c: "var(--text-danger)",  bg: "var(--bg-danger)",  br: "var(--border-danger)"  };
  return { c: "var(--text-muted)", bg: "var(--surface-1)", br: "var(--border)" };
};

// Rough data-provenance breakdown for a record: how much of it rests on
// audited filings vs. estimates vs. under-sourced benchmarks. This is the
// one signature visual this viewer is built around — since the entire
// point of the Python pipeline (vs. app.jsx) is knowing which numbers are
// audited and which aren't, that distinction gets a permanent, visible strip
// rather than being buried in per-field badges alone.
function computeProvenance(r) {
  const counts = { good: 0, ok: 0, weak: 0 };
  const bump = label => {
    const l = (label||"").toLowerCase();
    if (GOOD.has(l)) counts.good++;
    else if (OK.has(l)) counts.ok++;
    else if (WEAK.has(l)) counts.weak++;
  };
  bump(r.economics?.financials?.data_quality);
  bump(r.meta?.confidence?.overview);
  bump(r.meta?.confidence?.locations);
  bump(r.meta?.confidence?.stakeholders);
  (r.economics?.segment_sam_estimates||[]).forEach(e => bump(e.benchmark_confidence));
  const total = counts.good + counts.ok + counts.weak;
  if (!total) return null;
  return {
    goodPct: Math.round((counts.good/total)*100),
    okPct:   Math.round((counts.ok/total)*100),
    weakPct: Math.round((counts.weak/total)*100),
  };
}

function ProvenanceBar({ record, compact=false }) {
  const p = computeProvenance(record);
  if (!p) return null;
  return (
    <div style={{display:"flex",flexDirection:"column",gap:4}}>
      {!compact && <p style={{fontSize:10,color:"var(--text-muted)",textTransform:"uppercase",letterSpacing:"0.05em"}}>Data provenance</p>}
      <div style={{display:"flex",height:compact?4:6,borderRadius:99,overflow:"hidden",background:"var(--surface-1)"}}>
        {p.goodPct>0 && <div style={{width:`${p.goodPct}%`,background:"var(--fill-success)"}} title={`Audited/sourced: ${p.goodPct}%`}/>}
        {p.okPct>0   && <div style={{width:`${p.okPct}%`,background:"var(--fill-warning)"}}   title={`Estimated: ${p.okPct}%`}/>}
        {p.weakPct>0 && <div style={{width:`${p.weakPct}%`,background:"var(--fill-danger)"}}   title={`Inferred/needs research: ${p.weakPct}%`}/>}
      </div>
      {!compact && (
        <div style={{display:"flex",gap:10,fontSize:10,color:"var(--text-muted)"}}>
          <span><i className="ti ti-square-filled" style={{color:"var(--text-success)",fontSize:9}}/> Audited/sourced</span>
          <span><i className="ti ti-square-filled" style={{color:"var(--text-warning)",fontSize:9}}/> Estimated</span>
          <span><i className="ti ti-square-filled" style={{color:"var(--text-danger)",fontSize:9}}/> Needs research</span>
        </div>
      )}
    </div>
  );
}

// ── Validation ────────────────────────────────────────────────
function validateRecord(obj) {
  const errors = [];
  if (typeof obj !== "object" || obj === null) return { valid: false, errors: ["Not a JSON object."] };
  if (!obj.meta) errors.push('Missing "meta" section.');
  if (!obj.overview) errors.push('Missing "overview" section.');
  if (obj.overview && !obj.overview.legal_name) errors.push('overview.legal_name is required.');
  if (obj.meta && !obj.meta.record_id) errors.push('meta.record_id is required (used as the unique key).');
  return { valid: errors.length === 0, errors };
}

// ── Sample fixture — lets you try the viewer before the Python pipeline
// produces a real file. Clearly labeled as sample data throughout. ──────
function sampleRecord() {
  const now = new Date().toISOString();
  return {
    meta: {
      schema_version: "1.0.0", record_id: `sample_${Date.now()}`,
      refreshed_at: now, refresh_cadence: "weekly",
      confidence: { overall: "medium", overview: "high", locations: "medium", stakeholders: "medium", economics: "high" },
      sources: [
        { name: "SEC EDGAR 10-K 2025", url: "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany", fetched_at: now, fields_populated: ["economics.financials"] },
        { name: "Company investor relations", url: "https://example.com/ir", fetched_at: now, fields_populated: ["overview"] },
      ],
    },
    overview: {
      legal_name: "Example Mobility Corp (SAMPLE)", ticker: { symbol: "EXMB", exchange: "NASDAQ" },
      ownership_type: "public", founded_year: 1998, website: "https://example.com",
      industry_verticals: ["Automotive", "EV powertrain"],
      employee_count: { value: 42000, approximate: true },
      summary: "Sample record for viewer testing — an EV powertrain OEM used to demonstrate the segment SAM estimate tab.",
      competitors: ["Rival EV Co"], key_products: ["Traction inverters", "Battery packs"],
    },
    locations: [
      { type: "hq", city: "Austin", state_region: "TX", country: "USA", status: "active", notes: "Corporate HQ" },
      { type: "fab", city: "Chandler", state_region: "AZ", country: "USA", status: "ramping", notes: "Power module assembly" },
    ],
    stakeholders: [
      { name: "J. Rivera", title: "VP Procurement", role_type: "economic_buyer", influence_score: 0.9, notes: "Owns supplier qualification budget." },
      { name: "VP Power Electronics (name not found)", title: "VP Power Electronics", role_type: "technical_buyer", influence_score: 0.75, notes: "Evaluates SiC/GaN sourcing." },
    ],
    economics: {
      financials: { fiscal_year: 2025, revenue_usd_m: 8400, revenue_growth_pct: 12.3, gross_margin_pct: 24.1, rd_spend_usd_m: 610, rd_pct_revenue: 7.3, capex_usd_m: 950, data_quality: "audited" },
      revenue_segments: [
        { name: "EV powertrain", revenue_usd_m: 5200, pct_of_total: 62, relevant_to_us: true },
        { name: "Fleet services", revenue_usd_m: 3200, pct_of_total: 38, relevant_to_us: false },
      ],
      semiconductor_profile: {
        chip_buyer_type: "oem_buyer_only", has_own_fab: false,
        known_chip_suppliers: ["Infineon", "onsemi"], known_supply_gaps: ["Automotive-grade SiC MOSFETs"],
        qualification_cycle: "AEC-Q100 qualification opens Q1 2027",
      },
      segment_sam_estimates: [
        { segment: "power", estimated_sam_low_usd_m: 180, estimated_sam_high_usd_m: 300, unit_basis: "per vehicle produced × 40% segment allocation", benchmark_source: "AlixPartners (2026)", benchmark_confidence: "sourced", rationale: "EV traction inverter + OBC + BMS content, applied to ~375k annual unit volume." },
        { segment: "ep", estimated_sam_low_usd_m: 90, estimated_sam_high_usd_m: 150, unit_basis: "per vehicle produced × 20% segment allocation", benchmark_source: "AlixPartners (2026)", benchmark_confidence: "sourced", rationale: "Body/zonal MCUs." },
        { segment: "hpc", estimated_sam_low_usd_m: 45, estimated_sam_high_usd_m: 75, unit_basis: "per vehicle produced × 10% segment allocation", benchmark_source: "AlixPartners (2026)", benchmark_confidence: "sourced", rationale: "ADAS/zonal compute." },
        { segment: "conn", estimated_sam_low_usd_m: 67, estimated_sam_high_usd_m: 113, unit_basis: "per vehicle produced × 15% segment allocation", benchmark_source: "AlixPartners (2026)", benchmark_confidence: "sourced", rationale: "Telematics, V2X, in-cabin networking." },
        { segment: "ams", estimated_sam_low_usd_m: 67, estimated_sam_high_usd_m: 113, unit_basis: "per vehicle produced × 15% segment allocation", benchmark_source: "AlixPartners (2026)", benchmark_confidence: "sourced", rationale: "Current/voltage sensing, signal chain." },
      ],
    },
    account_intelligence: {
      opportunity_signals: [
        { type: "opportunity", description: "New SiC qualification cycle opens Q1 2027 — early engagement window.", urgency: "near_term", source: "10-K risk factors" },
        { type: "risk", description: "Single-sourced on current-gen power modules; incumbent renewal likely mid-2027.", urgency: "long_term", source: null },
      ],
      recommended_next_action: "Engage VP Power Electronics ahead of Q1 2027 qualification window.",
      current_supplier_status: "evaluation",
    },
    news: [
      { date: "2026-05-14", headline: "Example Mobility announces new Arizona plant expansion", summary: "Adds power module assembly capacity.", source_name: "Reuters (sample)", url: "https://example.com/news1", tags: ["fab","capex"] },
    ],
  };
}

// ── Shared UI atoms ───────────────────────────────────────────
function Badge({ children, color="var(--text-secondary)", bg="var(--surface-1)", border }) {
  return <span style={{display:"inline-block",fontSize:11,fontWeight:500,padding:"2px 8px",borderRadius:99,background:bg,color,border:border?`0.5px solid ${border}`:"none",lineHeight:1.6,whiteSpace:"nowrap"}}>{children}</span>;
}

function ConfBadge({ label }) {
  const t = tierColor(label);
  return <Badge color={t.c} bg={t.bg} border={t.br}>{label||"unknown"}</Badge>;
}

function FieldRow({ label, value, mono }) {
  return (
    <div style={{display:"flex",justifyContent:"space-between",alignItems:"baseline",padding:"5px 0",borderBottom:"0.5px solid var(--border)",gap:12}}>
      <span style={{fontSize:12,color:"var(--text-muted)",whiteSpace:"nowrap",flexShrink:0}}>{label}</span>
      <span style={{fontSize:13,color:"var(--text-primary)",textAlign:"right",fontFamily:mono?"var(--font-mono)":undefined,wordBreak:"break-all"}}>{value||"—"}</span>
    </div>
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
        <div style={{width:`${pct}%`,height:"100%",background:fill,borderRadius:2}}/>
      </div>
    </div>
  );
}

function DeleteButton({ onDelete, compact=false }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (!armed) return; const t = setTimeout(()=>setArmed(false),3000); return ()=>clearTimeout(t); }, [armed]);
  return (
    <button
      onClick={(e)=>{ e.stopPropagation(); if (armed) { setArmed(false); onDelete(); } else setArmed(true); }}
      title={armed?"Click again to permanently delete":"Delete this record"}
      style={{fontSize:11,padding:compact?"3px 7px":"4px 10px",display:"flex",alignItems:"center",gap:4,flexShrink:0,
        color:armed?"#fff":"var(--text-muted)", background:armed?"var(--fill-danger)":"transparent", borderColor:armed?"transparent":"var(--border)"}}>
      <i className={`ti ${armed?"ti-alert-triangle":"ti-trash"}`} style={{fontSize:13}}/>
      {!compact && (armed?"Confirm?":"Delete")}
    </button>
  );
}

// ── Section components ────────────────────────────────────────
function OverviewSection({ r }) {
  const ov = r.overview||{};
  return (
    <div style={{display:"grid",gridTemplateColumns:"minmax(0,1fr) minmax(0,1fr)",gap:16}}>
      <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
        <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:10}}>Company details</p>
        <FieldRow label="Legal name" value={ov.legal_name}/>
        <FieldRow label="Ticker" value={ov.ticker?`${ov.ticker.symbol}:${ov.ticker.exchange}`:"Private"} mono/>
        <FieldRow label="Ownership" value={ov.ownership_type}/>
        <FieldRow label="Parent" value={ov.parent_company}/>
        <FieldRow label="Founded" value={ov.founded_year}/>
        <FieldRow label="Employees" value={ov.employee_count?`~${Number(ov.employee_count.value).toLocaleString()}`:null}/>
        <FieldRow label="Website" value={ov.website} mono/>
      </div>
      <div style={{display:"flex",flexDirection:"column",gap:12}}>
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Summary</p>
          <p style={{fontSize:13,lineHeight:1.65,margin:0}}>{ov.summary||"—"}</p>
        </div>
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Industry</p>
          <div style={{display:"flex",flexWrap:"wrap",gap:6,marginBottom:12}}>
            {(ov.industry_verticals||[]).map(v=><Badge key={v}>{v}</Badge>)}
            {!(ov.industry_verticals||[]).length && <span style={{fontSize:13,color:"var(--text-muted)"}}>—</span>}
          </div>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:8}}>Competitors</p>
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
  const locs = r.locations||[];
  const statusMeta = {
    active:{bg:"var(--bg-success)",color:"var(--text-success)"}, ramping:{bg:"var(--bg-warning)",color:"var(--text-warning)"},
    under_construction:{bg:"var(--bg-warning)",color:"var(--text-warning)"}, planned:{bg:"var(--bg-neutral)",color:"var(--text-secondary)"},
    closed:{bg:"var(--surface-1)",color:"var(--text-muted)"},
  };
  return (
    <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,overflow:"hidden"}}>
      {locs.map((loc,i)=>{
        const sm = statusMeta[loc.status]||{bg:"var(--surface-1)",color:"var(--text-muted)"};
        return (
          <div key={i} style={{display:"flex",alignItems:"center",gap:12,padding:"10px 1.25rem",borderBottom:i<locs.length-1?"0.5px solid var(--border)":"none"}}>
            <Badge>{(loc.type||"").toUpperCase().replace(/_/g," ")}</Badge>
            <div style={{flex:1,minWidth:0}}>
              <p style={{fontSize:13,fontWeight:500,margin:0}}>{[loc.name,[loc.city,loc.state_region,loc.country].filter(Boolean).join(", ")].filter(Boolean).join(" — ")}</p>
              {loc.notes && <p style={{fontSize:12,color:"var(--text-muted)",margin:"2px 0 0"}}>{loc.notes}</p>}
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
  const stks = r.stakeholders||[];
  return (
    <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(260px,1fr))",gap:12}}>
      {stks.map((s,i)=>{
        const initials = (s.name||"?").split(" ").filter(w=>!w.includes("(")).slice(0,2).map(w=>w[0]).join("").toUpperCase();
        return (
          <div key={i} style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem",display:"flex",flexDirection:"column",gap:10}}>
            <div style={{display:"flex",alignItems:"center",gap:10}}>
              <div style={{width:36,height:36,borderRadius:"50%",background:"var(--bg-accent)",color:"var(--text-accent)",fontSize:13,fontWeight:500,display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0}}>{initials}</div>
              <div style={{flex:1,minWidth:0}}>
                <p style={{fontSize:13,fontWeight:500,margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{s.name}</p>
                <p style={{fontSize:12,color:"var(--text-muted)",margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{s.title}</p>
              </div>
            </div>
            <Badge>{(s.role_type||"").replace(/_/g," ")}</Badge>
            <InfluenceBar score={s.influence_score||0}/>
            {s.notes && <p style={{fontSize:12,color:"var(--text-secondary)",margin:0,lineHeight:1.5}}>{s.notes}</p>}
          </div>
        );
      })}
      {!stks.length && <p style={{color:"var(--text-muted)",fontSize:13}}>No stakeholders identified.</p>}
    </div>
  );
}

function EconomicsSection({ r }) {
  const f = r.economics?.financials||{};
  const semi = r.economics?.semiconductor_profile||{};
  const segs = r.economics?.revenue_segments||[];
  const metrics = [
    ["Revenue", fmtM(f.revenue_usd_m), `FY${f.fiscal_year||"N/A"}`],
    ["YoY growth", fmtP(f.revenue_growth_pct), ""],
    ["Gross margin", fmtP(f.gross_margin_pct), ""],
    ["R&D spend", fmtM(f.rd_spend_usd_m), f.rd_pct_revenue?fmtP(f.rd_pct_revenue)+" of rev":""],
    ["Capex", fmtM(f.capex_usd_m), ""],
  ];
  return (
    <div style={{display:"flex",flexDirection:"column",gap:16}}>
      <div style={{display:"flex",alignItems:"center",gap:8}}>
        <span style={{fontSize:12,color:"var(--text-muted)"}}>Data quality:</span>
        <ConfBadge label={f.data_quality}/>
      </div>
      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(130px,1fr))",gap:12}}>
        {metrics.map(([label,val,sub])=>(
          <div key={label} style={{background:"var(--surface-1)",borderRadius:"var(--radius)",padding:"0.85rem 1rem"}}>
            <p style={{fontSize:11,color:"var(--text-muted)",margin:"0 0 4px"}}>{label}</p>
            <p style={{fontSize:20,fontWeight:500,margin:0}}>{val}</p>
            {sub && <p style={{fontSize:11,color:"var(--text-secondary)",margin:"2px 0 0"}}>{sub}</p>}
          </div>
        ))}
      </div>
      {segs.length>0 && (
        <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.25rem"}}>
          <p style={{fontSize:12,fontWeight:500,color:"var(--text-secondary)",marginBottom:10}}>Disclosed revenue segments</p>
          {segs.map((seg,i)=>(
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
        <FieldRow label="Buyer type" value={semi.chip_buyer_type}/>
        <FieldRow label="Has own fab" value={semi.has_own_fab!=null?(semi.has_own_fab?"Yes":"No"):null}/>
        <FieldRow label="Known suppliers" value={(semi.known_chip_suppliers||[]).join(", ")||null}/>
        <FieldRow label="Supply gaps" value={(semi.known_supply_gaps||[]).join("; ")||null}/>
        <FieldRow label="Qualification cycle" value={semi.qualification_cycle}/>
      </div>
    </div>
  );
}

// The star feature: benchmark-derived SAM estimates across the five
// segments, always visibly separated from the audited financials above.
function SegmentSAMSection({ r }) {
  const estimates = r.economics?.segment_sam_estimates||[];
  const bySegment = Object.fromEntries(estimates.map(e => [e.segment, e]));

  return (
    <div style={{display:"flex",flexDirection:"column",gap:16}}>
      <div style={{display:"flex",alignItems:"flex-start",gap:10,padding:"10px 14px",background:"var(--bg-warning)",border:"0.5px solid var(--border-warning)",borderRadius:10}}>
        <i className="ti ti-alert-triangle" style={{fontSize:16,color:"var(--text-warning)",flexShrink:0,marginTop:1}}/>
        <p style={{fontSize:12.5,color:"var(--text-primary)",lineHeight:1.55,margin:0}}>
          These are <strong>benchmark-derived estimates</strong>, not figures disclosed by the company.
          They combine this account's audited revenue/COGS with third-party semiconductor-content
          benchmarks per vertical. Never treat these as audited data — check the benchmark confidence
          badge on each card before using a number in a forecast.
        </p>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(220px,1fr))",gap:12}}>
        {SEGMENT_ORDER.map(seg => {
          const e = bySegment[seg];
          return (
            <div key={seg} style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1rem 1.1rem",display:"flex",flexDirection:"column",gap:8}}>
              <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",gap:6}}>
                <p style={{fontSize:12,fontWeight:600,color:"var(--text-secondary)",margin:0}}>{SEGMENT_LABELS[seg]}</p>
                {e ? <ConfBadge label={e.benchmark_confidence}/> : <ConfBadge label="needs_research"/>}
              </div>
              {e ? (
                <>
                  <p style={{fontSize:20,fontWeight:600,margin:0}}>{fmtRange(e.estimated_sam_low_usd_m, e.estimated_sam_high_usd_m)}</p>
                  <p style={{fontSize:11,color:"var(--text-muted)",margin:0}}>{e.unit_basis}</p>
                  {e.rationale && <p style={{fontSize:12,color:"var(--text-secondary)",margin:"4px 0 0",lineHeight:1.5}}>{e.rationale}</p>}
                  <p style={{fontSize:10,color:"var(--text-muted)",margin:"6px 0 0",borderTop:"0.5px solid var(--border)",paddingTop:6}}>Source: {e.benchmark_source||"—"}</p>
                </>
              ) : (
                <p style={{fontSize:12,color:"var(--text-muted)",margin:0,lineHeight:1.5}}>No benchmark sourced for this segment yet — pipeline hasn't computed an estimate.</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function IntelligenceSection({ r }) {
  const intel = r.account_intelligence||{};
  const sigs = intel.opportunity_signals||[];
  const sigMeta = t => ({opportunity:{c:"var(--text-success)",bg:"var(--bg-success)",br:"var(--border-success)"},risk:{c:"var(--text-danger)",bg:"var(--bg-danger)",br:"var(--border-danger)"},watch:{c:"var(--text-warning)",bg:"var(--bg-warning)",br:"var(--border-warning)"}}[t]||{c:"var(--text-secondary)",bg:"var(--surface-1)",br:"var(--border)"});
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
          <p style={{fontSize:13,margin:0,lineHeight:1.55}}>{intel.recommended_next_action}</p>
        </div>
      )}
      <div style={{display:"flex",flexDirection:"column",gap:8}}>
        {sigs.map((sig,i)=>{ const m = sigMeta(sig.type); return (
          <div key={i} style={{background:m.bg,border:`0.5px solid ${m.br}`,borderRadius:12,padding:"10px 14px"}}>
            <div style={{display:"flex",gap:8,marginBottom:6,flexWrap:"wrap"}}>
              <Badge bg={m.bg} color={m.c}>{sig.type}</Badge>
              <Badge>{(sig.urgency||"").replace(/_/g," ")}</Badge>
            </div>
            <p style={{fontSize:13,margin:0,lineHeight:1.55}}>{sig.description}</p>
            {sig.source && <p style={{fontSize:11,color:"var(--text-muted)",margin:"6px 0 0"}}>Source: {sig.source}</p>}
          </div>
        );})}
        {!sigs.length && <p style={{color:"var(--text-muted)",fontSize:13}}>No signals identified.</p>}
      </div>
    </div>
  );
}

function NewsSection({ r }) {
  const items = r.news||[];
  return (
    <div style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,overflow:"hidden"}}>
      {items.map((item,i)=>(
        <div key={i} style={{padding:"12px 1.25rem",borderBottom:i<items.length-1?"0.5px solid var(--border)":"none"}}>
          <div style={{display:"flex",alignItems:"baseline",gap:12,marginBottom:4,flexWrap:"wrap"}}>
            <span style={{fontSize:12,color:"var(--text-muted)",fontFamily:"var(--font-mono)"}}>{item.date}</span>
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

function SourcesPanel({ r, onClose }) {
  const sources = r.meta?.sources||[];
  return (
    <div style={{position:"fixed",inset:0,background:"rgba(0,0,0,0.5)",display:"flex",alignItems:"center",justifyContent:"center",zIndex:1000}} onClick={onClose}>
      <div onClick={e=>e.stopPropagation()} style={{background:"var(--surface-2)",border:"0.5px solid var(--border-strong)",borderRadius:12,padding:"1.25rem",width:520,maxHeight:"70vh",overflowY:"auto"}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:12}}>
          <p style={{fontSize:14,fontWeight:500,margin:0}}>Sources ({sources.length})</p>
          <button onClick={onClose} style={{fontSize:12,padding:"4px 8px"}}>Close</button>
        </div>
        {sources.map((s,i)=>(
          <div key={i} style={{padding:"8px 0",borderBottom:i<sources.length-1?"0.5px solid var(--border)":"none"}}>
            <p style={{fontSize:13,fontWeight:500,margin:"0 0 2px"}}>{s.name}</p>
            <p style={{fontSize:11,color:"var(--text-muted)",margin:"0 0 2px",fontFamily:"var(--font-mono)",wordBreak:"break-all"}}>{s.url}</p>
            <p style={{fontSize:11,color:"var(--text-muted)",margin:0}}>{fmtD(s.fetched_at)} · {(s.fields_populated||[]).join(", ")}</p>
          </div>
        ))}
        {!sources.length && <p style={{fontSize:13,color:"var(--text-muted)"}}>No sources recorded.</p>}
      </div>
    </div>
  );
}

// ── Import panel ──────────────────────────────────────────────
function ImportPanel({ onImport, onCancel }) {
  const [text, setText] = useState("");
  const [error, setError] = useState(null);

  const tryImport = (raw) => {
    let parsed;
    try { parsed = JSON.parse(raw); }
    catch { setError("Could not parse JSON — check for a trailing comma or unescaped quote."); return; }
    const { valid, errors } = validateRecord(parsed);
    if (!valid) { setError(errors.join(" ")); return; }
    setError(null);
    onImport(parsed);
  };

  const handleFile = (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => tryImport(String(reader.result||""));
    reader.onerror = () => setError("Could not read that file.");
    reader.readAsText(file);
  };

  return (
    <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",padding:"2rem",overflowY:"auto"}}>
      <div style={{width:"100%",maxWidth:560}}>
        <i className="ti ti-file-upload" style={{fontSize:28,color:"var(--text-accent)",display:"block",marginBottom:12}}/>
        <h2 style={{fontSize:20,fontWeight:500,margin:"0 0 6px"}}>Import a research record</h2>
        <p style={{fontSize:13,color:"var(--text-muted)",margin:"0 0 20px",lineHeight:1.55}}>
          Paste the JSON produced by <code style={{fontFamily:"var(--font-mono)"}}>run_research.py</code>, or upload the
          <code style={{fontFamily:"var(--font-mono)"}}> .json</code> file from your output directory.
        </p>

        <div style={{marginBottom:14}}>
          <label style={{display:"block",fontSize:12,color:"var(--text-secondary)",marginBottom:6}}>Upload file</label>
          <input type="file" accept=".json,application/json" onChange={handleFile}/>
        </div>

        <div style={{display:"flex",alignItems:"center",gap:10,margin:"14px 0",color:"var(--text-muted)",fontSize:11}}>
          <div style={{flex:1,height:1,background:"var(--border)"}}/>OR PASTE<div style={{flex:1,height:1,background:"var(--border)"}}/>
        </div>

        <textarea
          value={text} onChange={e=>setText(e.target.value)}
          placeholder='{ "meta": { ... }, "overview": { ... }, ... }'
          style={{width:"100%",height:180,resize:"vertical",marginBottom:12}}
        />

        {error && (
          <div style={{padding:"10px 14px",background:"var(--bg-danger)",border:"0.5px solid var(--border-danger)",borderRadius:"var(--radius)",fontSize:12.5,color:"var(--text-danger)",marginBottom:14,lineHeight:1.5}}>{error}</div>
        )}

        <div style={{display:"flex",gap:8}}>
          <button onClick={onCancel} style={{flex:1}}>Cancel</button>
          <button onClick={()=>onImport(sampleRecord())} style={{flex:1}}>Load sample</button>
          <button onClick={()=>tryImport(text)} className="primary" disabled={!text.trim()} style={{flex:2}}>Import JSON</button>
        </div>
      </div>
    </div>
  );
}

// ── Sidebar + Dashboard ───────────────────────────────────────
function Sidebar({ records, activeId, onSelect, onImport, onDelete }) {
  return (
    <div style={{width:220,flexShrink:0,background:"var(--surface-1)",borderRight:"0.5px solid var(--border)",display:"flex",flexDirection:"column",overflow:"hidden"}}>
      <div style={{padding:"1rem",borderBottom:"0.5px solid var(--border)",flexShrink:0}}>
        <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:12}}>
          <i className="ti ti-file-check" style={{fontSize:18,color:"var(--text-accent)"}}/>
          <span style={{fontSize:13,fontWeight:500}}>Record viewer</span>
        </div>
        <button onClick={onImport} className="primary" style={{width:"100%",fontSize:13,display:"flex",alignItems:"center",justifyContent:"center",gap:6}}>
          <i className="ti ti-file-upload" style={{fontSize:14}}/>Import record
        </button>
      </div>
      {records.length>0 && <p style={{fontSize:11,color:"var(--text-muted)",padding:"0.75rem 0.75rem 0.25rem",margin:0}}>Imported ({records.length})</p>}
      <div style={{flex:1,overflowY:"auto",padding:"0.25rem 0.5rem 0.5rem"}}>
        {!records.length && <p style={{fontSize:12,color:"var(--text-muted)",padding:"0.75rem",textAlign:"center",lineHeight:1.5}}>No records imported yet.</p>}
        {records.map(r=>{
          const active = r.meta.record_id===activeId;
          return (
            <div key={r.meta.record_id} onClick={()=>onSelect(r)}
              style={{padding:"8px 10px",borderRadius:"var(--radius)",cursor:"pointer",marginBottom:2,background:active?"var(--surface-2)":"transparent",border:`0.5px solid ${active?"var(--border)":"transparent"}`,display:"flex",alignItems:"center",gap:4}}
              onMouseEnter={e=>{if(!active)e.currentTarget.style.background="var(--surface-2)"}}
              onMouseLeave={e=>{if(!active)e.currentTarget.style.background="transparent"}}>
              <div style={{flex:1,minWidth:0}}>
                <p style={{fontSize:13,fontWeight:500,margin:"0 0 4px",overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{r.overview?.legal_name}</p>
                <ProvenanceBar record={r} compact/>
              </div>
              <DeleteButton compact onDelete={()=>onDelete(r.meta.record_id)}/>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Dashboard({ records, onSelect, onImport, onDelete }) {
  if (!records.length) return (
    <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",gap:16,padding:"4rem 2rem",textAlign:"center"}}>
      <i className="ti ti-file-search" style={{fontSize:48,color:"var(--text-muted)"}}/>
      <div>
        <p style={{fontSize:18,fontWeight:500,margin:"0 0 8px"}}>No records imported yet</p>
        <p style={{fontSize:14,color:"var(--text-muted)",margin:0}}>Import JSON output from the research pipeline to view it here.</p>
      </div>
      <button onClick={onImport} className="primary">Import a record ↗</button>
    </div>
  );
  return (
    <div style={{padding:"1.5rem",display:"grid",gridTemplateColumns:"repeat(auto-fill,minmax(280px,1fr))",gap:16,alignContent:"start",overflowY:"auto",flex:1}}>
      {records.map(r=>{
        const conf = r.meta?.confidence||{};
        const confT = tierColor(conf.overall);
        return (
          <div key={r.meta.record_id} onClick={()=>onSelect(r)}
            style={{background:"var(--surface-2)",border:"0.5px solid var(--border)",borderRadius:12,padding:"1.25rem",cursor:"pointer"}}
            onMouseEnter={e=>e.currentTarget.style.borderColor="var(--border-strong)"}
            onMouseLeave={e=>e.currentTarget.style.borderColor="var(--border)"}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:8,gap:8}}>
              <p style={{fontSize:15,fontWeight:500,margin:0,flex:1,lineHeight:1.3}}>{r.overview?.legal_name}</p>
              <div style={{display:"flex",alignItems:"center",gap:6,flexShrink:0}}>
                <Badge bg={confT.bg} color={confT.c}>{conf.overall||"—"}</Badge>
                <DeleteButton compact onDelete={()=>onDelete(r.meta.record_id)}/>
              </div>
            </div>
            {r.overview?.ticker && <p style={{fontSize:12,color:"var(--text-muted)",margin:"0 0 10px",fontFamily:"var(--font-mono)"}}>{r.overview.ticker.symbol}:{r.overview.ticker.exchange}</p>}
            <div style={{marginBottom:12}}><ProvenanceBar record={r}/></div>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",paddingTop:10,borderTop:"0.5px solid var(--border)"}}>
              <span style={{fontSize:12,color:"var(--text-muted)"}}>{fmtD(r.meta?.refreshed_at)}</span>
              <span style={{fontSize:12,color:"var(--text-muted)"}}>{(r.economics?.segment_sam_estimates||[]).length} segment estimates</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function ResultsView({ record, activeTab, setActiveTab, onBack, onDelete }) {
  const [showSources, setShowSources] = useState(false);
  const ov = record.overview||{};
  const conf = record.meta?.confidence||{};
  const confT = tierColor(conf.overall);
  const content = {
    overview: <OverviewSection r={record}/>, locations: <LocationsSection r={record}/>,
    stakeholders: <StakeholdersSection r={record}/>, economics: <EconomicsSection r={record}/>,
    segments: <SegmentSAMSection r={record}/>, intelligence: <IntelligenceSection r={record}/>,
    news: <NewsSection r={record}/>,
  };
  return (
    <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
      <div style={{padding:"0.75rem 1.5rem",borderBottom:"0.5px solid var(--border)",background:"var(--surface-2)",display:"flex",alignItems:"center",gap:14,flexShrink:0,flexWrap:"wrap"}}>
        <button onClick={onBack} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4,flexShrink:0}}>
          <i className="ti ti-arrow-left" style={{fontSize:13}}/>Back
        </button>
        <div style={{flex:"1 1 220px",minWidth:0}}>
          <h2 style={{fontSize:15,fontWeight:500,margin:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{ov.legal_name}</h2>
          <p style={{fontSize:12,color:"var(--text-muted)",margin:0,fontFamily:"var(--font-mono)"}}>{fmtD(record.meta?.refreshed_at)} · schema v{record.meta?.schema_version||"1.0.0"}</p>
        </div>
        <div style={{width:140,flexShrink:0}}><ProvenanceBar record={record} compact/></div>
        <Badge bg={confT.bg} color={confT.c}>{conf.overall||"medium"} confidence</Badge>
        <button onClick={()=>setShowSources(true)} style={{fontSize:12,padding:"4px 10px",display:"flex",alignItems:"center",gap:4}}>
          <i className="ti ti-list-details" style={{fontSize:13}}/>Sources ({(record.meta?.sources||[]).length})
        </button>
        <DeleteButton onDelete={()=>onDelete(record.meta.record_id)}/>
      </div>
      <div style={{display:"flex",borderBottom:"0.5px solid var(--border)",padding:"0 1.5rem",flexShrink:0,overflowX:"auto"}}>
        {TABS.map(tab=>(
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
      <div style={{flex:1,overflowY:"auto",padding:"1.5rem"}}>{content[activeTab]}</div>
      {showSources && <SourcesPanel r={record} onClose={()=>setShowSources(false)}/>}
    </div>
  );
}

// ── Root app ───────────────────────────────────────────────────
export default function App() {
  const [view, setView] = useState("dashboard");
  const [records, setRecords] = useState([]);
  const [currentRecord, setCurrentRecord] = useState(null);
  const [activeTab, setActiveTab] = useState("overview");
  const [toast, setToast] = useState("");

  useEffect(() => {
    (async () => {
      try {
        const r = await window.storage.get(STORAGE_KEY, true);
        if (r?.value) setRecords(JSON.parse(r.value));
      } catch {}
    })();
  }, []);

  const persist = async (updated) => {
    setRecords(updated);
    try { await window.storage.set(STORAGE_KEY, JSON.stringify(updated), true); } catch {}
  };

  const showToast = (msg) => { setToast(msg); setTimeout(()=>setToast(""),2500); };

  const handleImport = async (parsed) => {
    const key = parsed.overview?.website || parsed.overview?.legal_name || parsed.meta?.record_id;
    const dupe = records.find(r => (r.overview?.website||r.overview?.legal_name) === key);
    const updated = dupe
      ? records.map(r => (r.overview?.website||r.overview?.legal_name)===key ? parsed : r)
      : [parsed, ...records];
    await persist(updated);
    showToast(dupe ? "Updated existing record ✓" : "Record imported ✓");
    setCurrentRecord(parsed); setActiveTab("overview"); setView("results");
  };

  const handleDelete = async (recordId) => {
    const updated = records.filter(r => r.meta.record_id !== recordId);
    await persist(updated);
    showToast("Record deleted");
    if (currentRecord?.meta?.record_id === recordId) { setCurrentRecord(null); setView("dashboard"); }
  };

  return (
    <>
      <style>{CSS}</style>
      <div style={{display:"flex",height:"100vh",overflow:"hidden",fontFamily:"var(--font-sans)"}}>
        <Sidebar
          records={records}
          activeId={view==="results"?currentRecord?.meta?.record_id:null}
          onSelect={r=>{setCurrentRecord(r);setActiveTab("overview");setView("results");}}
          onImport={()=>setView("import")}
          onDelete={handleDelete}
        />
        <div style={{flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}}>
          {view==="dashboard" && <Dashboard records={records} onSelect={r=>{setCurrentRecord(r);setActiveTab("overview");setView("results");}} onImport={()=>setView("import")} onDelete={handleDelete}/>}
          {view==="import" && <ImportPanel onImport={handleImport} onCancel={()=>setView(records.length?"dashboard":"dashboard")}/>}
          {view==="results" && currentRecord && (
            <ResultsView record={currentRecord} activeTab={activeTab} setActiveTab={setActiveTab} onBack={()=>setView("dashboard")} onDelete={handleDelete}/>
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