"""
Test suite. Runs against generated fixtures — no real PBIX needed, no
network, no external deps.

    python tests/test_extractor.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "fixtures"))

from build_fixtures import build_pbit, build_pbix           # noqa: E402
from pbix_extractor.extractor import extract                # noqa: E402
from pbix_extractor.dax_analyzer import analyze_dax, build_dependency_graph  # noqa: E402
from pbix_extractor.container import decode_part, open_package, PbixError    # noqa: E402
from pbix_extractor.bridge import to_source_schema, harvest_measure_library  # noqa: E402
from pbix_extractor.index_builder import build_index        # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


FIX = os.path.join(HERE, "_tmp")
os.makedirs(FIX, exist_ok=True)
PBIT = os.path.join(FIX, "T.pbit")
PBIX = os.path.join(FIX, "T.pbix")
build_pbit(PBIT)
build_pbix(PBIX)

# ---------------------------------------------------------------- encoding
check("decodes UTF-16LE with BOM",
      decode_part(b"\xff\xfe" + '{"a":1}'.encode("utf-16-le")) == '{"a":1}')
check("decodes UTF-16LE without BOM",
      decode_part('{"a":1}'.encode("utf-16-le")) == '{"a":1}')
check("decodes plain UTF-8", decode_part(b'{"a":1}') == '{"a":1}')
check("strips trailing NULs", decode_part(b'{"a":1}\x00\x00') == '{"a":1}')

try:
    open_package(os.path.join(FIX, "nonexistent.pbix"))
    check("rejects missing file", False)
except (PbixError, FileNotFoundError):
    check("rejects missing file", True)

bad = os.path.join(FIX, "bad.pbix")
open(bad, "wb").write(b"this is not a zip")
try:
    open_package(bad)
    check("rejects non-zip with clear error", False)
except PbixError as e:
    check("rejects non-zip with clear error", "not a valid" in str(e))

# ---------------------------------------------------------------- pbit tier
cat = extract(PBIT)
check("pbit reaches full_model tier", cat["source"]["extraction_tier"] == "full_model")
check("model marked available", cat["model"]["available"])
check("extracts 4 tables", cat["statistics"]["tables"] == 4, str(cat["statistics"]))
check("extracts 13 measures", cat["statistics"]["measures"] == 13)
check("extracts 4 relationships", cat["statistics"]["relationships"] == 4)
check("extracts 3 pages", cat["statistics"]["pages"] == 3)
check("extracts 6 visuals", cat["statistics"]["visuals"] == 6)
check("extracts 3 M queries", cat["statistics"]["m_queries"] == 3)
check("extracts 1 RLS role", cat["statistics"]["rls_roles"] == 1)
check("detects calculated column",
      cat["statistics"]["calculated_columns"] == 1)
check("detects inactive relationship",
      cat["statistics"]["inactive_relationships"] == 1)
check("detects bidirectional relationship",
      cat["statistics"]["bidirectional_relationships"] == 1)

# ---------------------------------------------------- DAX lexical hazards
m = {x["name"]: x for x in cat["model"]["measures"]}
check("parenthesised measure name is not read as a function",
      "REVENUE" not in m["Net Revenue (base)"]["analysis"]["functions"],
      str(m["Net Revenue (base)"]["analysis"]["functions"]))
check("simple aggregation classified",
      "simple_aggregation" in m["Total Sales"]["analysis"]["patterns"])
check("time intelligence classified",
      "time_intelligence" in m["Sales YTD"]["analysis"]["patterns"])
check("ratio classified", "ratio" in m["Margin %"]["analysis"]["patterns"])
check("ranking classified", "ranking" in m["Product Rank"]["analysis"]["patterns"])
check("USERELATIONSHIP flagged as role-playing",
      "role_playing_dimension" in m["Sales by Ship Date"]["analysis"]["patterns"])
check("measure-to-measure dependency resolved",
      set(m["Margin %"]["analysis"]["references"]["measures"]) == {"Total Sales", "Total Cost"})
check("column reference resolved",
      {"table": "FactSales", "column": "NetAmount"} in m["Total Sales"]["analysis"]["references"]["columns"])
check("VAR usage lowers confidence to medium",
      m["Top Product Share"]["analysis"]["confidence"] == "medium")
check("VAR names captured",
      len(m["Top Product Share"]["analysis"]["variables"]) == 3)

# comment / string-literal stripping
a = analyze_dax('// SUM(fake) \n CALCULATE([X], Y[Z] = "SUM(alsoFake)")')
check("comments excluded from function scan", "SUM" not in a["functions"])
check("string literals excluded from function scan", "CALCULATE" in a["functions"])
check("empty expression handled", analyze_dax("")["complexity"] == "empty")

# ------------------------------------------------------------ graph checks
check("circular reference detected",
      len(cat["analysis"]["circular_references"]) == 1,
      str(cat["analysis"]["circular_references"]))
check("cycle names both measures",
      set(cat["analysis"]["circular_references"][0]) == {"Circular A", "Circular B"})
check("no false cycle on a clean chain",
      build_dependency_graph([
          {"name": "A", "expression": "SUM(T[x])"},
          {"name": "B", "expression": "[A] * 2"},
          {"name": "C", "expression": "[B] + [A]"},
      ])["circular_references"] == [])

# ----------------------------------------------------- cross-layer linkage
check("orphan measure detected",
      cat["analysis"]["measures_referenced_but_not_defined"] == ["Forecast Variance"])
check("unused measure detected",
      "Legacy Discount Total" in cat["analysis"]["unused_measures"])
check("measure used by a dependency is not called unused",
      "Total Cost" not in cat["analysis"]["unused_measures"])
check("usage count reflects visual bindings",
      m["Total Sales"]["usage_count"] == 3, str(m["Total Sales"]["usage_count"]))
check("role-playing dimension detected",
      len(cat["analysis"]["role_playing_dimensions"]) == 1)
check("role-playing names active and inactive columns",
      cat["analysis"]["role_playing_dimensions"][0]["inactive"] == ["ShipDateKey"])

# ------------------------------------------------------------ report layer
pages = {p["display_name"]: p for p in cat["report"]["pages"]}
check("page order preserved", list(pages) == ["Executive Summary", "Product Detail", "QA Scratch"])
check("hidden page detected", pages["QA Scratch"]["visibility"] == 1)
check("visual title extracted",
      any(v["title"] == "Sales Trend" for v in pages["Executive Summary"]["visuals"]))
check("implicit aggregation captured",
      any(v["implicit_measures"] for v in pages["Executive Summary"]["visuals"]))
check("page-level filter captured", len(pages["Product Detail"]["page_filters"]) == 1)
check("locked filter flag read", pages["Product Detail"]["page_filters"][0]["is_locked"])
check("visual-level filter captured",
      any(v["visual_filters"] for v in pages["Product Detail"]["visuals"]))
check("report-level filter captured", len(cat["report"]["report_filters"]) == 1)
check("bookmark captured", len(cat["report"]["bookmarks"]) == 1)
check("theme captured", cat["report"]["theme"] == "CY24SU06")

# -------------------------------------------------------------- power query
q = {x["name"]: x for x in cat["power_query"]["queries"]}
check("all M queries parsed", set(q) == {"FactSales", "DimCustomer", "DimProduct"})
check("SQL Server connector detected", "SQL Server" in q["FactSales"]["connectors"])
check("Snowflake connector detected", "Snowflake" in q["DimProduct"]["connectors"])
check("M steps parsed", q["FactSales"]["step_count"] >= 4, str(q["FactSales"]["step_count"]))
check("native query extracted",
      q["DimProduct"]["native_query"] and "DIM_PRODUCT" in q["DimProduct"]["native_query"])
check("source server captured",
      any(s["server"] == "dw-prod.internal" for s in cat["power_query"]["sources"]))

# --------------------------------------------------------------- RLS
role = cat["model"]["roles"][0]
check("RLS role extracted", role["name"] == "RegionalManager")
check("RLS filter expression extracted", "USERPRINCIPALNAME" in role["table_permissions"][0]["filter_expression"])
check("RLS filter analysed", "analysis" in role["table_permissions"][0])

# --------------------------------------------------------- pbix degradation
pcat = extract(PBIX)
check("pbix degrades to report_only", pcat["source"]["extraction_tier"] == "report_only")
check("pbix model marked unavailable", pcat["model"]["available"] is False)
check("pbix explains why model is missing",
      any("Xpress9" in w for w in pcat["source"]["warnings"]))
check("pbix warning names the remedy",
      any(".pbit" in w and "XMLA" in w for w in pcat["source"]["warnings"]))
check("pbix still extracts report layer", pcat["statistics"]["pages"] == 3)
check("pbix still extracts M queries", pcat["statistics"]["m_queries"] == 3)
check("pbix records a coverage finding",
      any(f["category"] == "coverage" for f in pcat["findings"]))
check("pbix does not report phantom measures", pcat["statistics"]["measures"] == 0)

# ------------------------------------------------------------------ bridge
schema = to_source_schema(cat)
check("bridge emits all tables", len(schema["tables"]) == 4)
check("bridge emits declared FKs", len(schema["declared_foreign_keys"]) == 4)
check("bridge preserves inactive FK flag",
      any(not f["is_active"] for f in schema["declared_foreign_keys"]))
cols = {(t["name"], c["name"]) for t in schema["tables"] for c in t["columns"]}
dangling = [
    (f[t], f[c]) for f in schema["declared_foreign_keys"]
    for t, c in (("fk_table", "fk_column"), ("pk_table", "pk_column"))
    if (f[t], f[c]) not in cols
]
check("bridge has no dangling FK endpoints", not dangling, str(dangling))
check("bridge marks primary keys",
      sum(1 for t in schema["tables"] for c in t["columns"] if c.get("key")) == 3)
check("bridge carries provenance hash", len(schema["provenance"]["sha256"]) == 64)

try:
    to_source_schema(pcat)
    check("bridge refuses a model-less catalogue", False)
except ValueError as e:
    check("bridge refuses a model-less catalogue", "not extracted" in str(e))

lib = harvest_measure_library(cat)
names = {x["name"] for x in lib["measure_library"]}
check("library harvests used measures", "Total Sales" in names)
check("library includes dependency of a used measure", "Total Cost" in names)
check("library excludes unused measures", "Legacy Discount Total" not in names)
check("library parameterises the fact table",
      "{fact_table}" in next(x for x in lib["measure_library"] if x["name"] == "Total Sales")["dax"])
check("library parameterises the date table",
      "{date_table}" in next(x for x in lib["measure_library"] if x["name"] == "Sales YTD")["dax"])
check("library retains original DAX",
      next(x for x in lib["measure_library"] if x["name"] == "Total Sales")["original_dax"]
      == "SUM(FactSales[NetAmount])")

# ------------------------------------------------------------- index page
html = build_index(cat)
check("index page is self-contained", "http://" not in html and "https://" not in html.split("xmlns")[0])
check("index page includes measures", "Net Revenue (base)" in html)
check("index page includes findings", "circular measure reference" in html)
check("index renders for a model-less catalogue", len(build_index(pcat)) > 2000)
check("index escapes HTML in names",
      "&lt;" in build_index({**cat, "model": {**cat["model"], "measures": [
          {"name": "<script>x</script>", "table": "T", "expression": "1",
           "usage_count": 0, "used_in": [], "analysis": analyze_dax("1")}]}}))

# ----------------------------------------------------------------- report
passed = sum(1 for _, c, _ in results if c)
for name, cond, detail in results:
    print(f"{'PASS' if cond else 'FAIL'}: {name}" + (f"   -> {detail}" if not cond else ""))
print(f"\n{passed}/{len(results)} checks passed")

import shutil
shutil.rmtree(FIX, ignore_errors=True)
sys.exit(0 if passed == len(results) else 1)
