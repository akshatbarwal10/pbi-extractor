"""
Index page: a self-contained, searchable HTML catalogue of one file's
extracted contents.

Single HTML file, no external assets and no network calls, so it can be
committed to a repo, attached to a ticket, or opened from a share
without anything else being installed. Everything is inlined.
"""
from __future__ import annotations

import html
import json


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else ""


SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def build_index(catalogue: dict) -> str:
    src = catalogue["source"]
    stats = catalogue["statistics"]
    model = catalogue["model"]
    report = catalogue["report"]
    pq = catalogue["power_query"]
    findings = sorted(catalogue["findings"], key=lambda f: SEVERITY_ORDER.get(f["severity"], 3))

    # ---- stat cards ----
    cards = [
        ("Tables", stats["tables"]), ("Columns", stats["columns"]),
        ("Measures", stats["measures"]), ("Relationships", stats["relationships"]),
        ("Pages", stats["pages"]), ("Visuals", stats["visuals"]),
        ("M Queries", stats["m_queries"]), ("RLS Roles", stats["rls_roles"]),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="num">{v}</div><div class="lbl">{_esc(k)}</div></div>'
        for k, v in cards
    )

    # ---- warnings banner ----
    banner = ""
    if src["warnings"]:
        items = "".join(f"<li>{_esc(w)}</li>" for w in src["warnings"])
        banner = f'<div class="banner"><strong>Extraction notes</strong><ul>{items}</ul></div>'

    # ---- findings ----
    findings_html = ""
    for f in findings:
        detail = ""
        if f["detail"]:
            lis = "".join(f"<li><code>{_esc(d)}</code></li>" for d in f["detail"])
            detail = f"<ul class='detail'>{lis}</ul>"
        findings_html += (
            f'<div class="finding {f["severity"]}">'
            f'<span class="sev">{f["severity"]}</span>'
            f'<span class="cat">{_esc(f["category"])}</span>'
            f'<div class="msg">{_esc(f["message"])}{detail}</div></div>'
        )
    if not findings_html:
        findings_html = '<p class="muted">No findings.</p>'

    # ---- measures ----
    measures_html = ""
    for m in sorted(model["measures"], key=lambda x: (-x["usage_count"], x["name"])):
        a = m.get("analysis", {})
        pats = "".join(f'<span class="tag">{_esc(p)}</span>' for p in a.get("patterns", []))
        deps = a.get("references", {}).get("measures", [])
        cols = a.get("references", {}).get("columns", [])
        dep_html = ""
        if deps:
            dep_html += "<div class='dep'>Depends on measures: " + ", ".join(
                f"<code>[{_esc(d)}]</code>" for d in deps) + "</div>"
        if cols:
            dep_html += "<div class='dep'>Columns: " + ", ".join(
                f"<code>{_esc(c['table'])}[{_esc(c['column'])}]</code>" for c in cols[:8]) + "</div>"
        used = m.get("used_in", [])
        used_html = (
            "<div class='dep'>Used in: " + ", ".join(
                f"{_esc(u['page'])} / {_esc(u['visual'])}" for u in used[:6]) + "</div>"
            if used else "<div class='dep muted'>Not used in any visual</div>"
        )
        note_html = ""
        if a.get("notes"):
            note_html = "<div class='note'>" + " ".join(_esc(n) for n in a["notes"]) + "</div>"

        # Plain-language explanation, when one was generated. It leads the
        # entry because the meaning is what a reader wants first; the DAX
        # stays below as the evidence for it.
        ex = m.get("explanation") or {}
        explain_html = ""
        if ex.get("summary"):
            explain_html = f'<div class="explain"><strong>{_esc(ex["summary"])}</strong>'
            if ex.get("detail"):
                explain_html += f'<div class="explain-detail">{_esc(ex["detail"])}</div>'
            for caveat in ex.get("caveats") or []:
                explain_html += f'<div class="caveat">{_esc(caveat)}</div>'
            missed = ex.get("dependencies_missed_by_static_analysis")
            if missed:
                explain_html += (
                    "<div class='caveat'>References not resolved by static "
                    "analysis (held in VAR): "
                    + ", ".join(f"<code>[{_esc(d)}]</code>" for d in missed)
                    + "</div>"
                )
            explain_html += "</div>"

        # Explanations are searchable too, so a business term finds the
        # measure even when it appears nowhere in the DAX.
        haystack = " ".join([
            m["name"], m["expression"],
            ex.get("summary", ""), ex.get("detail", ""),
        ]).lower()

        measures_html += (
            f'<div class="item" data-search="{_esc(haystack)}">'
            f'<div class="item-head"><strong>{_esc(m["name"])}</strong>'
            f'<span class="meta">{_esc(m["table"])}'
            f'{" · " + _esc(m.get("display_folder")) if m.get("display_folder") else ""}'
            f' · used {m["usage_count"]}×</span></div>'
            f'{explain_html}'
            f'<div class="tags">{pats}<span class="tag c">{_esc(a.get("complexity"))}</span></div>'
            f'<pre>{_esc(m["expression"])}</pre>{dep_html}{used_html}{note_html}</div>'
        )
    if not measures_html:
        measures_html = '<p class="muted">No measures extracted (model layer unavailable).</p>'

    # ---- tables ----
    tables_html = ""
    for t in model["tables"]:
        rows = "".join(
            f'<tr><td>{_esc(c["name"])}</td><td>{_esc(c.get("data_type"))}</td>'
            f'<td>{"calculated" if c.get("expression") else _esc(c.get("type", "data"))}</td>'
            f'<td>{"yes" if c.get("is_hidden") else ""}</td></tr>'
            for c in t["columns"]
        )
        parts = []
        if t.get("storage_mode"):
            parts.append(_esc(t["storage_mode"]))
        if t.get("is_calculation_group"):
            parts.append("calculation group")
        meta = " · ".join(parts)
        hier = ""
        if t.get("hierarchies"):
            hier = "<div class='dep'>Hierarchies: " + ", ".join(
                f"<code>{_esc(h['name'])}</code>" for h in t["hierarchies"]) + "</div>"
        tables_html += (
            f'<div class="item" data-search="{_esc(t["name"].lower())}">'
            f'<div class="item-head"><strong>{_esc(t["name"])}</strong>'
            f'<span class="meta">{len(t["columns"])} cols · {t["measure_count"]} measures'
            f'{" · " + meta if meta else ""}</span></div>{hier}'
            f'<table><thead><tr><th>Column</th><th>Type</th><th>Kind</th><th>Hidden</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    if not tables_html:
        tables_html = '<p class="muted">No tables extracted (model layer unavailable).</p>'

    # ---- relationships ----
    rel_rows = "".join(
        f'<tr><td>{_esc(r["from_table"])}[{_esc(r["from_column"])}]</td>'
        f'<td>{_esc(r["to_table"])}[{_esc(r["to_column"])}]</td>'
        f'<td>{_esc(r["cardinality"])}</td><td>{_esc(r["cross_filter"])}</td>'
        f'<td>{"active" if r["is_active"] else "<em>inactive</em>"}</td></tr>'
        for r in model["relationships"]
    )
    rel_html = (
        f'<table class="full"><thead><tr><th>From</th><th>To</th><th>Cardinality</th>'
        f'<th>Cross filter</th><th>State</th></tr></thead><tbody>{rel_rows}</tbody></table>'
        if rel_rows else '<p class="muted">No relationships extracted.</p>'
    )

    # ---- report pages ----
    pages_html = ""
    for p in report["pages"]:
        vis = ""
        for v in p["visuals"]:
            fields = ", ".join(
                [f"[{_esc(x)}]" for x in v["measures_used"]] +
                [f"<code>{_esc(x)}</code>" for x in v["columns_used"][:6]] +
                [f"<em>{_esc(x)}</em>" for x in v["implicit_measures"][:4]]
            ) or "<span class='muted'>no bound fields</span>"
            vfilters = (
                f"<div class='dep'>Filters: " + ", ".join(
                    f"{_esc(fl.get('table'))}[{_esc(fl.get('field'))}]" for fl in v["visual_filters"]) + "</div>"
                if v["visual_filters"] else ""
            )
            vis += (
                f'<div class="visual"><div><strong>{_esc(v["title"] or v["type"])}</strong>'
                f'<span class="meta">{_esc(v["type"])}</span></div>'
                f'<div class="fields">{fields}</div>{vfilters}</div>'
            )
        pfilters = (
            "<div class='dep'>Page filters: " + ", ".join(
                f"{_esc(fl.get('table'))}[{_esc(fl.get('field'))}]" for fl in p["page_filters"]) + "</div>"
            if p["page_filters"] else ""
        )
        hidden = " <span class='tag'>hidden</span>" if p.get("visibility") == 1 else ""
        pages_html += (
            f'<div class="item" data-search="{_esc((p.get("display_name") or "").lower())}">'
            f'<div class="item-head"><strong>{_esc(p.get("display_name") or p.get("name"))}</strong>{hidden}'
            f'<span class="meta">{p["visual_count"]} visuals</span></div>{pfilters}{vis}</div>'
        )
    if not pages_html:
        pages_html = '<p class="muted">No report pages extracted.</p>'

    # ---- power query ----
    pq_html = ""
    for q in pq.get("queries", []):
        conn = "".join(f'<span class="tag">{_esc(c)}</span>' for c in q["connectors"])
        native = f'<pre>{_esc(q["native_query"])}</pre>' if q.get("native_query") else ""
        steps = ", ".join(_esc(s) for s in q["steps"][:12])
        pq_html += (
            f'<div class="item" data-search="{_esc(q["name"].lower())}">'
            f'<div class="item-head"><strong>{_esc(q["name"])}</strong>'
            f'<span class="meta">{q["step_count"]} steps</span></div>'
            f'<div class="tags">{conn}</div>'
            f'<div class="dep">Steps: {steps}</div>{native}</div>'
        )
    if not pq_html:
        pq_html = '<p class="muted">No Power Query definitions extracted.</p>'

    # ---- RLS ----
    rls_html = ""
    for r in model["roles"]:
        perms = "".join(
            f'<div class="dep"><code>{_esc(tp["table"])}</code>'
            f'<pre>{_esc(tp["filter_expression"])}</pre></div>'
            for tp in r["table_permissions"]
        )
        rls_html += (
            f'<div class="item"><div class="item-head"><strong>{_esc(r["name"])}</strong>'
            f'<span class="meta">{_esc(r.get("model_permission"))}</span></div>{perms}</div>'
        )
    if not rls_html:
        rls_html = '<p class="muted">No row-level security roles defined.</p>'

    graph_json = json.dumps(catalogue["dax_dependency_graph"]["edges"], indent=2)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>PBIX Catalogue — {_esc(src['file'])}</title>
<style>
:root {{
  --bg:#ffffff; --fg:#14181f; --muted:#5b6472; --line:#e3e6ec;
  --panel:#f7f8fa; --accent:#1F3864; --code:#f2f3f7;
  --err:#b3261e; --warn:#8a5a00; --info:#1F3864;
  box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#14181f; --fg:#e8eaee; --muted:#9aa4b2; --line:#2a303a;
    --panel:#1b2029; --accent:#8fb0e8; --code:#1e232c;
    --err:#f2b8b5; --warn:#e8c98a; --info:#8fb0e8;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#14181f; --fg:#e8eaee; --muted:#9aa4b2; --line:#2a303a;
  --panel:#1b2029; --accent:#8fb0e8; --code:#1e232c;
  --err:#f2b8b5; --warn:#e8c98a; --info:#8fb0e8;
}}
*,*::before,*::after {{ box-sizing:inherit; }}
html {{ scroll-padding-top:env(safe-area-inset-top,0px); }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1100px; margin:0 auto; padding:24px 18px 64px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
h2 {{ font-size:18px; margin:36px 0 12px; padding-bottom:6px; border-bottom:1px solid var(--line); }}
.sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; word-break:break-all; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(110px,1fr)); gap:10px; margin:18px 0; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; text-align:center; }}
.card .num {{ font-size:26px; font-weight:700; color:var(--accent); }}
.card .lbl {{ font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px; }}
.banner {{ background:var(--panel); border-left:3px solid var(--accent); border-radius:6px; padding:12px 14px; margin:16px 0; font-size:13px; }}
.banner ul {{ margin:6px 0 0; padding-left:18px; }}
nav {{ position:sticky; top:env(safe-area-inset-top,0px); background:var(--bg); border-bottom:1px solid var(--line);
  padding:10px 0; margin-bottom:8px; overflow-x:auto; white-space:nowrap; z-index:5; }}
nav a {{ color:var(--accent); text-decoration:none; font-size:13px; font-weight:600; margin-right:16px; }}
#q {{ width:100%; padding:10px 12px; border:1px solid var(--line); border-radius:8px;
  background:var(--panel); color:var(--fg); font-size:14px; margin:8px 0 4px; }}
.item {{ border:1px solid var(--line); border-radius:8px; padding:12px 14px; margin-bottom:10px; background:var(--panel); }}
.item-head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:8px; }}
.meta {{ color:var(--muted); font-size:12px; margin-left:auto; }}
.explain {{ background:var(--panel); border-left:3px solid var(--accent); border-radius:0 6px 6px 0;
  padding:9px 12px; margin:8px 0 4px; font-size:13.5px; line-height:1.5; }}
.explain-detail {{ margin-top:5px; color:var(--muted); font-size:12.5px; font-weight:400; }}
.caveat {{ margin-top:6px; font-size:12.5px; color:var(--warn); }}
.caveat::before {{ content:"⚠ "; }}
.tags {{ margin:6px 0; }}
.tag {{ display:inline-block; background:var(--code); border:1px solid var(--line); border-radius:12px;
  padding:1px 9px; font-size:11px; color:var(--muted); margin:2px 4px 2px 0; }}
.tag.c {{ font-weight:600; }}
pre {{ background:var(--code); border:1px solid var(--line); border-radius:6px; padding:10px;
  overflow-x:auto; font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace; margin:8px 0; white-space:pre-wrap; word-break:break-word; }}
code {{ background:var(--code); border-radius:4px; padding:1px 5px; font:12px ui-monospace,Consolas,monospace; }}
.dep {{ font-size:12.5px; color:var(--muted); margin-top:5px; }}
.note {{ font-size:12px; color:var(--warn); margin-top:5px; }}
.muted {{ color:var(--muted); }}
table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:13px; display:block; overflow-x:auto; }}
table.full {{ display:table; }}
th,td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.4px; }}
.visual {{ border-left:2px solid var(--line); padding:6px 0 6px 12px; margin:8px 0 0 4px; }}
.fields {{ font-size:12.5px; color:var(--muted); margin-top:3px; }}
.finding {{ border:1px solid var(--line); border-left-width:3px; border-radius:6px;
  padding:10px 12px; margin-bottom:8px; background:var(--panel); }}
.finding.error {{ border-left-color:var(--err); }}
.finding.warning {{ border-left-color:var(--warn); }}
.finding.info {{ border-left-color:var(--info); }}
.sev {{ font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.6px; margin-right:8px; }}
.finding.error .sev {{ color:var(--err); }} .finding.warning .sev {{ color:var(--warn); }}
.finding.info .sev {{ color:var(--info); }}
.cat {{ font-size:11px; color:var(--muted); }}
.msg {{ margin-top:4px; }}
ul.detail {{ margin:6px 0 0; padding-left:18px; font-size:12.5px; color:var(--muted); }}
details summary {{ cursor:pointer; color:var(--accent); font-size:13px; font-weight:600; }}
</style></head><body><div class="wrap">

<h1>{_esc(src['file'])}</h1>
<div class="sub">Extraction tier: <strong>{_esc(src['extraction_tier'])}</strong> ·
{_esc(src['extracted_at'])} · sha256 {_esc(src['sha256'][:16])}… ·
contract {_esc(catalogue['contract_version'])}</div>

{banner}
<div class="cards">{cards_html}</div>

<nav>
<a href="#findings">Findings</a><a href="#tables">Tables</a><a href="#measures">Measures</a>
<a href="#rels">Relationships</a><a href="#report">Report</a><a href="#pq">Power Query</a>
<a href="#rls">RLS</a><a href="#graph">Dependencies</a>
</nav>

<input id="q" type="search" placeholder="Filter tables, measures, pages, queries…" autocomplete="off">

<h2 id="findings">Findings</h2>{findings_html}
<h2 id="tables">Tables &amp; Columns</h2><div class="searchable">{tables_html}</div>
<h2 id="measures">Measures &amp; Business Logic</h2><div class="searchable">{measures_html}</div>
<h2 id="rels">Relationships</h2>{rel_html}
<h2 id="report">Report Pages &amp; Visuals</h2><div class="searchable">{pages_html}</div>
<h2 id="pq">Power Query (M)</h2><div class="searchable">{pq_html}</div>
<h2 id="rls">Row-Level Security</h2>{rls_html}
<h2 id="graph">Measure Dependency Graph</h2>
<details><summary>Show dependency edges</summary><pre>{_esc(graph_json)}</pre></details>

<script>
document.getElementById('q').addEventListener('input', function(e) {{
  var term = e.target.value.toLowerCase().trim();
  document.querySelectorAll('.searchable .item').forEach(function(el) {{
    var hay = el.getAttribute('data-search') || el.textContent.toLowerCase();
    el.style.display = (!term || hay.indexOf(term) !== -1) ? '' : 'none';
  }});
}});
</script>
</div></body></html>"""