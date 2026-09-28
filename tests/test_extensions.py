"""
Checks for the added layers: TMDL, .pbip, explanations, exports, service.

Run alongside the original suite:

    python tests/test_extractor.py
    python tests/test_extensions.py

The core checks need no third-party package. Export checks are skipped
when openpyxl or reportlab is absent rather than failing, because those
are optional dependencies of an otherwise stdlib-only package.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pbix_extractor import ui_service  # noqa: E402
from pbix_extractor.enrich import llm_explainer as L  # noqa: E402
from pbix_extractor.extractor import extract  # noqa: E402
from pbix_extractor.pbip_reader import looks_like_pbip  # noqa: E402
from pbix_extractor.tmdl_parser import build_tmsl, parse_tmdl  # noqa: E402

PASSED = 0
FAILED = 0


def check(condition, label):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS: {label}")
    else:
        FAILED += 1
        print(f"FAIL: {label}")


# ---------------------------------------------------------------------
# TMDL parser
# ---------------------------------------------------------------------

TABLE_TMDL = """/// Sales fact
table FactSales
\tisHidden

\tcolumn SalesKey
\t\tdataType: int64
\t\tisHidden

\tcolumn 'Margin Amount' = FactSales[Net] - FactSales[Cost]
\t\tdataType: decimal

\tmeasure 'Total Sales' = SUM(FactSales[Net])
\t\tformatString: #,##0
\t\tdisplayFolder: Core

\t/// Share of the top product
\tmeasure 'Top Share' =
\t\t\t\tVAR Top = TOPN(1, VALUES(Product[Name]), [Total Sales])
\t\t\t\tRETURN DIVIDE(Top, 1)
\t\tformatString: 0.0%
\t\tdisplayFolder: Analysis

\thierarchy Calendar
\t\tlevel Year
\t\t\tcolumn: Year
\t\tlevel Month
\t\t\tcolumn: MonthName

\tpartition FactSales = m
\t\tmode: import
\t\tsource =
\t\t\t\tlet
\t\t\t\t\tSource = Sql.Database("srv", "db")
\t\t\t\tin
\t\t\t\t\tSource
"""

REL_TMDL = """relationship aaa
\tfromColumn: FactSales.OrderDate
\ttoColumn: DimDate.Date

relationship bbb
\tisActive: false
\tcrossFilteringBehavior: bothDirections
\tfromColumn: FactSales.ShipDate
\ttoColumn: 'Dim Date'.Date
"""

ROLE_TMDL = """role Manager
\tmodelPermission: read

\ttablePermission DimCustomer = DimCustomer[Region] = USERPRINCIPALNAME()
"""


def test_tmdl():
    tmsl = build_tmsl({
        "tables/FactSales.tmdl": TABLE_TMDL,
        "relationships.tmdl": REL_TMDL,
        "roles/Manager.tmdl": ROLE_TMDL,
        "model.tmdl": "model Model\n\tculture: en-US\n",
    })
    model = tmsl["model"]
    table = model["tables"][0]

    check(model["culture"] == "en-US", "tmdl reads model culture")
    check(table["name"] == "FactSales", "tmdl reads table name")
    check(table["isHidden"] is True, "tmdl reads a bare boolean property")
    check(table["description"] == "Sales fact", "tmdl attaches /// description")

    columns = {c["name"]: c for c in table["columns"]}
    check(columns["SalesKey"]["dataType"] == "int64", "tmdl reads column data type")
    check(columns["SalesKey"]["isHidden"] is True, "tmdl reads column boolean")
    check(columns["Margin Amount"]["type"] == "calculated",
          "tmdl marks a column with an expression as calculated")
    check("FactSales[Net]" in columns["Margin Amount"]["expression"],
          "tmdl keeps the calculated column expression")

    measures = {m["name"]: m for m in table["measures"]}
    check(measures["Total Sales"]["expression"] == "SUM(FactSales[Net])",
          "tmdl reads an inline measure expression")
    check(measures["Total Sales"]["formatString"] == "#,##0",
          "tmdl reads a property after an inline expression")
    # The regression that motivated block-end handling: properties sitting
    # at a shallower indent than a multi-line expression were swallowed.
    check(measures["Top Share"]["formatString"] == "0.0%",
          "tmdl keeps properties that follow a multi-line expression")
    check(measures["Top Share"]["displayFolder"] == "Analysis",
          "tmdl keeps every property after a multi-line expression")
    check("VAR Top" in measures["Top Share"]["expression"]
          and "RETURN" in measures["Top Share"]["expression"],
          "tmdl keeps the whole multi-line expression")
    check("formatString" not in measures["Top Share"]["expression"],
          "tmdl does not leak a property into the expression body")
    check(measures["Top Share"]["description"] == "Share of the top product",
          "tmdl attaches a description to a measure")

    partition = table["partitions"][0]
    check(partition["mode"] == "import", "tmdl reads partition mode")
    check(partition["source"]["type"] == "m", "tmdl reads partition source type")
    check("Sql.Database" in partition["source"]["expression"],
          "tmdl reads a multi-line M partition source")

    hierarchy = table["hierarchies"][0]
    check([lv["name"] for lv in hierarchy["levels"]] == ["Year", "Month"],
          "tmdl reads hierarchy levels in order")
    check(hierarchy["levels"][0]["column"] == "Year",
          "tmdl reads a level's column property, not a nested column object")

    relationships = model["relationships"]
    check(relationships[0]["fromTable"] == "FactSales"
          and relationships[0]["fromColumn"] == "OrderDate",
          "tmdl splits a relationship column reference")
    check(relationships[1]["isActive"] is False, "tmdl reads isActive: false")
    check(relationships[1]["crossFilteringBehavior"] == "bothDirections",
          "tmdl reads cross-filter direction")
    check(relationships[1]["toTable"] == "Dim Date",
          "tmdl unquotes a table name containing a space")

    role = model["roles"][0]
    check(role["tablePermissions"][0]["filterExpression"]
          == "DimCustomer[Region] = USERPRINCIPALNAME()",
          "tmdl keeps an RLS expression containing its own '='")

    check(parse_tmdl("")[:] == [], "tmdl tolerates an empty document")
    check(build_tmsl({})["model"]["tables"] == [],
          "tmdl tolerates no documents at all")


# ---------------------------------------------------------------------
# .pbip
# ---------------------------------------------------------------------

def _build_pbip(root: str):
    def write(rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    write("P.pbip", json.dumps({"version": "1.0"}))
    write("P.SemanticModel/definition/model.tmdl", "model Model\n\tculture: en-US\n")
    write("P.SemanticModel/definition/tables/FactSales.tmdl", TABLE_TMDL)
    write("P.SemanticModel/definition/relationships.tmdl", REL_TMDL)
    write("P.SemanticModel/definition/roles/Manager.tmdl", ROLE_TMDL)

    def field(kind, entity, prop):
        return {kind: {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}

    write("P.Report/definition/pages/pages.json",
          json.dumps({"pageOrder": ["p1"]}))
    write("P.Report/definition/pages/p1/page.json", json.dumps({
        "name": "p1", "displayName": "Exec", "visibility": "HiddenInViewMode",
        "filterConfig": {"filters": [
            {"name": "f", "type": "Categorical",
             "field": field("Column", "DimProduct", "Category")}]},
    }))
    write("P.Report/definition/pages/p1/visuals/v1/visual.json", json.dumps({
        "name": "v1",
        "position": {"x": 1, "y": 2, "width": 3, "height": 4},
        "visual": {
            "visualType": "card",
            "query": {"queryState": {"Values": {"projections": [
                {"field": field("Measure", "FactSales", "Total Sales")}]}}},
            "objects": {"title": [{"properties": {
                "text": {"expr": {"Literal": {"Value": "'Sales card'"}}}}}]},
        },
    }))


def test_pbip():
    directory = tempfile.mkdtemp(prefix="pbip-test-")
    try:
        _build_pbip(directory)
        check(looks_like_pbip(directory), "a project directory is detected as .pbip")

        catalogue = extract(directory)
        check(catalogue["source"]["source_format"] == "pbip",
              "pbip is recorded as the source format")
        check(catalogue["source"]["extraction_tier"] == "full_model",
              "a pbip with TMDL reaches the full_model tier")
        check(catalogue["statistics"]["measures"] == 2,
              "pbip measures reach the catalogue")
        check(catalogue["statistics"]["relationships"] == 2,
              "pbip relationships reach the catalogue")
        check(catalogue["statistics"]["rls_roles"] == 1,
              "pbip RLS roles reach the catalogue")

        page = catalogue["report"]["pages"][0]
        check(page["display_name"] == "Exec", "PBIR page display name is read")
        check(page["visibility"] == 1,
              "PBIR HiddenInViewMode maps to the legacy hidden flag")
        check([f["field"] for f in page["page_filters"]] == ["Category"],
              "PBIR filterConfig maps onto the legacy filter shape")

        visual = page["visuals"][0]
        check(visual["title"] == "Sales card", "PBIR visual title is read")
        check(visual["type"] == "card", "PBIR visual type is read")
        check(visual["measures_used"] == ["Total Sales"],
              "PBIR projections resolve to bound measures")

        used = {m["name"]: m["usage_count"] for m in catalogue["model"]["measures"]}
        check(used.get("Total Sales") == 1,
              "cross-layer usage links a PBIR visual to a TMDL measure")

        # M comes from partitions, since a project has no DataMashup part.
        check(catalogue["power_query"]["available"],
              "power query is rebuilt from pbip partitions")
        check(catalogue["power_query"].get("origin") == "model_partitions",
              "power query records that it came from partitions")

        # Same project, zipped — the path a browser upload takes.
        archive = os.path.join(directory, "..", "proj.zip")
        archive = os.path.abspath(archive)
        with zipfile.ZipFile(archive, "w") as zf:
            for dirpath, _dirs, files in os.walk(directory):
                for name in files:
                    full = os.path.join(dirpath, name)
                    zf.write(full, os.path.relpath(full, directory))
        try:
            check(looks_like_pbip(archive), "a zipped project is detected as .pbip")
            zipped = extract(archive)
            check(zipped["statistics"]["measures"] == 2,
                  "a zipped project extracts identically")
        finally:
            os.remove(archive)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------

def _prompt_of(payload: dict) -> str:
    """Pull the user prompt out of either provider's request shape."""
    if "contents" in payload:
        return payload["contents"][0]["parts"][0]["text"]
    return payload["messages"][1]["content"]


def _stub_transport(record: list, provider: str = "openai"):
    def fake(config, payload, headers=None):
        prompt = _prompt_of(payload)
        record.append(prompt)
        names = json.loads(prompt.split("names: ")[1].split("\n")[0])
        answer = json.dumps({
            name: {"summary": f"Summary of {name}", "detail": "Detail",
                   "caveats": [], "dependencies": []}
            for name in names
        })
        if provider == "gemini":
            return {"candidates": [{"content": {"parts": [{"text": answer}]}}]}
        return {"choices": [{"message": {"content": answer}}]}
    return fake


def test_explanations():
    directory = tempfile.mkdtemp(prefix="pbip-llm-")
    original = L._post
    try:
        _build_pbip(directory)
        catalogue = extract(directory)

        prompts: list = []
        L._post = _stub_transport(prompts)
        config = L.LLMConfig(api_key="x", model="test-model", batch_size=1)

        stats = L.explain_measures(catalogue, config, L.ExplanationCache(None))
        check(stats["explained"] == 2, "every measure gets an explanation")
        check(stats["failed"] == 0, "no measure fails on a healthy endpoint")

        measures = {m["name"]: m for m in catalogue["model"]["measures"]}
        check(measures["Total Sales"]["explanation"]["summary"]
              == "Summary of Total Sales", "the explanation lands on the measure")

        # Dependency ordering, and the dependant's prompt carrying the
        # dependency's already-written summary.
        order = [json.loads(p.split("names: ")[1].split("\n")[0])[0] for p in prompts]
        check(order.index("Total Sales") < order.index("Top Share"),
              "base measures are explained before their dependants")
        top_prompt = [p for p in prompts if "### Measure: Top Share" in p][0]
        check("Depends on these measures:" in top_prompt,
              "the dependency chain is included in the prompt")
        check("Summary of Total Sales" in top_prompt,
              "an earlier explanation is reused as context")
        check("Exec / Sales card" in prompts[0] or "Exec" in prompts[0],
              "report usage context is included in the prompt")

        # Caching: a second run over a fresh catalogue makes no calls.
        with tempfile.TemporaryDirectory() as cache_dir:
            cache_path = os.path.join(cache_dir, "cache.json")
            fresh = extract(directory)
            L.explain_measures(fresh, config, L.ExplanationCache(cache_path))
            prompts.clear()
            again = extract(directory)
            stats2 = L.explain_measures(again, config, L.ExplanationCache(cache_path))
            check(len(prompts) == 0 and stats2["cached"] == 2,
                  "a cached run makes no requests")

        # Fenced JSON is common from chat models and must still parse.
        L._post = lambda c, p, h=None: {"choices": [{"message": {"content":
            "```json\n" + json.dumps({
                n: {"summary": "s", "detail": "d", "caveats": [], "dependencies": []}
                for n in json.loads(_prompt_of(p)
                                    .split("names: ")[1].split("\n")[0])
            }) + "\n```"}}]}
        fenced = extract(directory)
        check(L.explain_measures(fenced, config, L.ExplanationCache(None))["explained"] == 2,
              "markdown-fenced JSON is parsed")

        # An outage must not cost the catalogue.
        def boom(config_, payload, headers=None):
            raise L.ExplainerError("endpoint down")
        L._post = boom
        broken = extract(directory)
        stats3 = L.explain_measures(broken, config, L.ExplanationCache(None))
        check(stats3["failed"] == 2 and stats3["errors"],
              "a failing endpoint is reported, not raised")
        check(all("explanation" not in m for m in broken["model"]["measures"]),
              "a failed explanation leaves the measure untouched")
        check(broken["statistics"]["measures"] == 2,
              "the catalogue survives an explanation failure")
    finally:
        L._post = original
        shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------------
# Configuration and the Gemini provider
# ---------------------------------------------------------------------

def test_config():
    from pbix_extractor import config as C

    directory = tempfile.mkdtemp(prefix="pbix-env-")
    saved_env = {k: os.environ.get(k) for k in
                 ("PBIX_ENV_FILE", "GEMINI_MODEL", "PBIX_EXPLAIN", "GEMINI_API_KEY")}
    try:
        env_path = os.path.join(directory, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(
                "# comment line\n"
                "\n"
                "GEMINI_API_KEY=AQ.testkey123   # trailing comment\n"
                'GEMINI_MODEL="gemini-flash-lite-latest"\n'
                "GEMINI_RPM=15\n"
                "export PBIX_LLM_BATCH_SIZE=6\n"
                'QUOTED_HASH="val#ue"\n'
            )
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ["PBIX_ENV_FILE"] = env_path
        C.reload()

        settings = C.llm_settings()
        check(settings["provider"] == "gemini",
              "a GEMINI_API_KEY selects the gemini provider")
        check(settings["api_key"] == "AQ.testkey123",
              "a trailing # comment is stripped from a value")
        check(settings["model"] == "gemini-flash-lite-latest",
              "quotes are stripped from a value")
        check(settings["rpm"] == 15, "GEMINI_RPM is read as an int")
        check(settings["batch_size"] == 6, "an `export ` prefix is tolerated")
        check(C.get("QUOTED_HASH") == "val#ue",
              "a # inside a quoted value is preserved")
        check(C.explanations_enabled(), "a configured key enables explanations")
        check("AQ.testkey" not in C.describe(),
              "the status line never contains the key")

        os.environ["GEMINI_MODEL"] = "gemini-2.5-pro"
        check(C.llm_settings()["model"] == "gemini-2.5-pro",
              "a real environment variable overrides .env")
        del os.environ["GEMINI_MODEL"]

        os.environ["PBIX_EXPLAIN"] = "false"
        check(C.explanations_enabled() is False,
              "PBIX_EXPLAIN=false turns explanations off without removing the key")
        del os.environ["PBIX_EXPLAIN"]

        # An absent .env must not raise — it just means no explanations.
        os.environ["PBIX_ENV_FILE"] = os.path.join(directory, "nope.env")
        C.reload()
        check(C.parse_env_file(os.path.join(directory, "nope.env")) == {},
              "a missing .env parses as empty rather than raising")
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        C.reload()
        shutil.rmtree(directory, ignore_errors=True)


def test_gemini_provider():
    config = L.LLMConfig(api_key="k", provider="gemini",
                         model="gemini-flash-lite-latest",
                         base_url="https://generativelanguage.googleapis.com/v1beta",
                         rpm=15)

    check(config.endpoint.endswith(
        "/models/gemini-flash-lite-latest:generateContent"),
        "the gemini endpoint embeds the model name")

    body, headers = L._build_request(config, "SYS", "USER")
    check(headers.get("x-goog-api-key") == "k",
          "gemini authenticates with x-goog-api-key")
    check("Authorization" not in headers,
          "no bearer header is sent to gemini")
    check(body["system_instruction"]["parts"][0]["text"] == "SYS",
          "the system prompt maps to system_instruction")
    check(body["generationConfig"]["responseMimeType"] == "application/json",
          "gemini is asked to guarantee JSON")

    openai_config = L.LLMConfig(api_key="k", provider="openai")
    body, headers = L._build_request(openai_config, "SYS", "USER")
    check(headers.get("Authorization") == "Bearer k",
          "the openai dialect still uses a bearer token")
    check(body["messages"][0]["content"] == "SYS",
          "the openai dialect still uses a messages array")

    answer = json.dumps({"A": {"summary": "s"}})
    check(L._content(
        {"candidates": [{"content": {"parts": [{"text": answer}]}}]},
        "gemini") == answer, "a gemini response body is read")

    # No text must raise a useful error rather than falling through to a
    # misleading "did not return parseable JSON".
    for payload, needle, label in [
        ({"candidates": [{"finishReason": "SAFETY"}]}, "SAFETY",
         "a filtered gemini response names the finish reason"),
        ({"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}]},
         "BATCH_SIZE", "a truncated gemini response suggests a smaller batch"),
        ({"promptFeedback": {"blockReason": "OTHER"}}, "OTHER",
         "a blocked prompt names the block reason"),
        ({}, "no candidates", "an empty gemini response raises clearly"),
    ]:
        try:
            L._content(payload, "gemini")
            check(False, label)
        except L.ExplainerError as e:
            check(needle in str(e), label)

    # End to end through the gemini request shape.
    directory = tempfile.mkdtemp(prefix="pbix-gem-")
    original = L._post
    try:
        _build_pbip(directory)
        catalogue = extract(directory)
        prompts: list = []
        L._post = _stub_transport(prompts, provider="gemini")
        stats = L.explain_measures(catalogue, config, L.ExplanationCache(None))
        check(stats["explained"] == 2, "explanations work over the gemini dialect")
        check("gemini" in stats["model"], "the provider is reported in the stats")
    finally:
        L._post = original
        shutil.rmtree(directory, ignore_errors=True)


def test_rate_limiter():
    limiter = L.RateLimiter(per_minute=0)
    start = time.monotonic()
    for _ in range(50):
        limiter.wait()
    check(time.monotonic() - start < 0.2,
          "rpm=0 applies no limiting")

    limiter = L.RateLimiter(per_minute=3)
    start = time.monotonic()
    for _ in range(3):
        limiter.wait()
    check(time.monotonic() - start < 0.2,
          "calls under the cap are not delayed")

    # A fourth call with the window full must wait for the oldest to age out.
    limiter = L.RateLimiter(per_minute=2)
    now = time.monotonic()
    limiter._times = [now - 59.0, now - 58.5]
    waited = limiter.wait()
    check(0.8 < waited < 2.0,
          "a call over the cap waits for the window to clear")


# ---------------------------------------------------------------------
# Service layer and exports
# ---------------------------------------------------------------------

def test_service():
    directory = tempfile.mkdtemp(prefix="pbip-svc-")
    try:
        _build_pbip(directory)
        archive = os.path.join(tempfile.gettempdir(), "svc-proj.zip")
        with zipfile.ZipFile(archive, "w") as zf:
            for dirpath, _dirs, files in os.walk(directory):
                for name in files:
                    full = os.path.join(dirpath, name)
                    zf.write(full, os.path.relpath(full, directory))

        try:
            with open(archive, "rb") as f:
                payload = f.read()

            artifacts = ui_service.process_upload(
                "MyProject.zip", payload,
                want_excel=False, want_pdf=False,
            )
            check(artifacts.stem == "MyProject", "the upload name sets the output stem")
            check(artifacts.tier == "full_model", "a zipped project reaches full_model")
            check(artifacts.model_available, "the model is reported as available")
            check(artifacts.tier_message()[0] == "success",
                  "full_model maps to a success banner")
            check(artifacts.index_html.startswith("<!DOCTYPE html>"),
                  "the index page is produced")
            check(artifacts.source_schema_json and artifacts.measure_library_json,
                  "bridge outputs are produced when the model is available")

            names = [d[1] for d in artifacts.downloads()]
            check("MyProject.index.html" in names, "the HTML download is offered")
            check("MyProject.catalogue.json" in names, "the JSON download is offered")
            check(all(isinstance(d[2], bytes) for d in artifacts.downloads()),
                  "every download is bytes, ready for a download button")

            check(ui_service.validate_upload("notes.docx") is not None,
                  "an unsupported extension is rejected")
            check(ui_service.validate_upload("report.pbit") is None,
                  "a .pbit is accepted")
        finally:
            os.remove(archive)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_degraded_pbix():
    """A .pbix must degrade honestly rather than look empty."""
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "fixtures", "SalesAnalytics.pbix")
    if not os.path.exists(fixture):
        print("SKIP: .pbix fixture not built (run fixtures/build_fixtures.py)")
        return
    with open(fixture, "rb") as f:
        artifacts = ui_service.process_upload(
            "SalesAnalytics.pbix", f.read(), want_excel=False, want_pdf=False)
    check(artifacts.tier == "report_only", "a .pbix is reported as report_only")
    check(artifacts.tier_message()[0] == "warning",
          "report_only maps to a warning banner, not a success")
    check("save the report as a .pbit" in artifacts.tier_message()[2].lower()
          or ".pbit" in artifacts.tier_message()[2],
          "the banner names the way to get the model")
    check(artifacts.source_schema_json is None,
          "no source schema is invented from a missing model")
    check("Source schema (JSON)" not in [d[0] for d in artifacts.downloads()],
          "bridge downloads are withheld when the model is unavailable")


def test_exports():
    try:
        from pbix_extractor.exports.excel_export import build_workbook_bytes
    except ImportError:
        print("SKIP: openpyxl not installed — Excel checks skipped")
        build_workbook_bytes = None

    try:
        from pbix_extractor.exports.pdf_export import build_pdf_bytes
    except ImportError:
        print("SKIP: reportlab not installed — PDF checks skipped")
        build_pdf_bytes = None

    if not build_workbook_bytes and not build_pdf_bytes:
        return

    directory = tempfile.mkdtemp(prefix="pbip-exp-")
    try:
        _build_pbip(directory)
        catalogue = extract(directory)

        if build_workbook_bytes:
            data = build_workbook_bytes(catalogue)
            check(data[:2] == b"PK", "the workbook is a valid zip container")
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp.write(data)
                path = tmp.name
            try:
                from openpyxl import load_workbook
                workbook = load_workbook(path)
                check("Overview" in workbook.sheetnames, "the workbook has an Overview")
                check("Measures" in workbook.sheetnames, "the workbook has Measures")
                sheet = workbook["Measures"]
                headers = [c.value for c in sheet[1]]
                check("DAX" in headers, "the measures sheet carries the DAX")
                check("Reviewer notes" in headers,
                      "annotation columns are present for a reviewer")
                check(sheet.max_row == 3, "one row per measure")
                check(sheet.auto_filter.ref is not None,
                      "the measures sheet is filterable")
                check(sheet.freeze_panes == "A2", "the header row is frozen")
            finally:
                os.unlink(path)

        if build_pdf_bytes:
            pdf = build_pdf_bytes(catalogue)
            check(pdf[:5] == b"%PDF-", "the PDF has a PDF header")
            check(len(pdf) > 3000, "the PDF has real content")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    test_tmdl()
    test_pbip()
    test_explanations()
    test_config()
    test_gemini_provider()
    test_rate_limiter()
    test_service()
    test_degraded_pbix()
    test_exports()

    print(f"\n{PASSED}/{PASSED + FAILED} checks passed")
    sys.exit(1 if FAILED else 0)