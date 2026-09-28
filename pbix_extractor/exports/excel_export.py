"""
Excel data dictionary.

The HTML index page is for whoever ran the extraction. This file is for
everyone else: a data steward who wants to filter 200 measures, mark
which are approved, and hand the sheet back. That review loop is why
every object sheet carries annotation columns and why the workbook is
built to be filtered — freeze panes, autofilter, sane column widths.

Annotation columns are the only cells anyone is expected to type in, so
they are shaded and called out in a legend on the Overview sheet.
Everything else is extracted fact and should be left alone.

Requires openpyxl. Imported lazily by the CLI and UI so the core
package keeps working without it.
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFF")
TITLE_FONT = Font(name=FONT, size=14, bold=True, color="1F3864")
LABEL_FONT = Font(name=FONT, size=10, bold=True)
BODY_FONT = Font(name=FONT, size=10)
MONO_FONT = Font(name="Consolas", size=9)

# Cells a reviewer is meant to fill in.
INPUT_FILL = PatternFill("solid", fgColor="FFF6D8")
INPUT_FONT = Font(name=FONT, size=10, color="7F6000")

SEVERITY_FILL = {
    "error": PatternFill("solid", fgColor="F8D7DA"),
    "warning": PatternFill("solid", fgColor="FFF3CD"),
    "info": PatternFill("solid", fgColor="E7F1FF"),
}

THIN = Side(style="thin", color="D9D9D9")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

ANNOTATION_COLUMNS = ["Reviewed by", "Status", "Reviewer notes"]


def _has_explanations(catalogue: dict) -> bool:
    return any(
        m.get("explanation", {}).get("summary")
        for m in catalogue.get("model", {}).get("measures", []) or []
    )


def _write_header(sheet, headers: list[str], row: int = 1):
    for index, title in enumerate(headers, start=1):
        cell = sheet.cell(row=row, column=index, value=title)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        cell.border = BORDER
    sheet.row_dimensions[row].height = 28


def _write_rows(sheet, rows: list[list], start_row: int = 2,
                wrap_columns: set[int] | None = None,
                mono_columns: set[int] | None = None,
                input_columns: set[int] | None = None):
    wrap_columns = wrap_columns or set()
    mono_columns = mono_columns or set()
    input_columns = input_columns or set()

    for r, values in enumerate(rows, start=start_row):
        for c, value in enumerate(values, start=1):
            cell = sheet.cell(row=r, column=c, value=value)
            cell.border = BORDER
            if c in mono_columns:
                cell.font = MONO_FONT
            elif c in input_columns:
                cell.font = INPUT_FONT
            else:
                cell.font = BODY_FONT
            if c in input_columns:
                cell.fill = INPUT_FILL
            cell.alignment = Alignment(
                vertical="top", wrap_text=c in wrap_columns or c in mono_columns
            )


def _finish(sheet, widths: list[int], row_count: int, freeze: str = "A2"):
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = freeze
    if row_count:
        sheet.auto_filter.ref = (
            f"A1:{get_column_letter(len(widths))}{row_count + 1}"
        )


def _join(values, limit: int = 12) -> str:
    values = [str(v) for v in (values or []) if str(v).strip()]
    if not values:
        return ""
    if len(values) > limit:
        return ", ".join(values[:limit]) + f" (+{len(values) - limit} more)"
    return ", ".join(values)


# ---------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------

def _fill_overview(sheet, catalogue: dict, explained: bool, built: dict[str, int]):
    """Populate the Overview sheet.

    Called *after* every other sheet exists, because the count formulas
    reference them by name and by their real extent. A formula pointing
    at a sheet that was never created evaluates to #REF!, and one
    pointing at an arbitrary large range quietly returns a wrong number
    — both look fine until someone reads the total.
    """
    source = catalogue["source"]
    stats = catalogue["statistics"]

    sheet["A1"] = "Power BI Model Catalogue"
    sheet["A1"].font = TITLE_FONT
    sheet["A2"] = source["file"]
    sheet["A2"].font = Font(name=FONT, size=11, color="5B6472")

    facts = [
        ("Extraction tier", source["extraction_tier"]),
        ("Source format", source.get("source_format", "")),
        ("Extracted at", source["extracted_at"]),
        ("SHA-256", source["sha256"]),
        ("Contract version", catalogue["contract_version"]),
        ("Explanations", "Generated" if explained else "Not generated"),
    ]
    row = 4
    for label, value in facts:
        sheet.cell(row=row, column=1, value=label).font = LABEL_FONT
        sheet.cell(row=row, column=2, value=value).font = BODY_FONT
        row += 1

    # Counts live off the sheets themselves where a sheet exists, so
    # they stay honest if a reviewer deletes rows. Each formula is bound
    # to that sheet's real last row rather than a guessed range.
    row += 1
    sheet.cell(row=row, column=1, value="Model contents").font = TITLE_FONT
    row += 1

    def count_of(sheet_name: str, column: str = "A", fallback: int = 0):
        rows = built.get(sheet_name)
        if not rows:
            return fallback
        reference = f"'{sheet_name}'!{column}2:{column}{rows + 1}"
        return f"=COUNTA({reference})"

    def distinct_count_of(sheet_name: str, column: str = "A", fallback: int = 0):
        rows = built.get(sheet_name)
        if not rows:
            return fallback
        reference = f"'{sheet_name}'!{column}2:{column}{rows + 1}"
        # Standard distinct-count idiom; the &"" guards against blanks
        # producing a divide-by-zero inside the SUMPRODUCT.
        return f'=SUMPRODUCT(({reference}<>"")/COUNTIF({reference},{reference}&""))'

    counts = [
        ("Tables", distinct_count_of("Tables", "A", stats["tables"])),
        ("Columns", count_of("Tables", "B", stats["columns"])),
        ("Measures", count_of("Measures", "A", stats["measures"])),
        ("Relationships", count_of("Relationships", "A", stats["relationships"])),
        ("Report pages", stats["pages"]),
        ("Visuals", count_of("Report usage", "C", stats["visuals"])),
        ("M queries", count_of("Power Query", "A", stats["m_queries"])),
        ("RLS roles", stats["rls_roles"]),
        ("Unused measures", stats["unused_measures"]),
    ]
    for label, value in counts:
        sheet.cell(row=row, column=1, value=label).font = LABEL_FONT
        cell = sheet.cell(row=row, column=2)
        cell.value = value
        cell.font = BODY_FONT
        row += 1

    # Findings tally by severity.
    row += 1
    sheet.cell(row=row, column=1, value="Findings").font = TITLE_FONT
    row += 1
    for severity in ("error", "warning", "info"):
        count = sum(1 for f in catalogue["findings"] if f["severity"] == severity)
        sheet.cell(row=row, column=1, value=severity.title()).font = LABEL_FONT
        cell = sheet.cell(row=row, column=2, value=count)
        cell.font = BODY_FONT
        if count and severity in SEVERITY_FILL:
            cell.fill = SEVERITY_FILL[severity]
        row += 1

    # Legend — which cells a reviewer should touch.
    row += 1
    sheet.cell(row=row, column=1, value="How to use this workbook").font = TITLE_FONT
    row += 1
    legend = [
        "Shaded amber cells are for you to fill in — the 'Reviewed by', 'Status' "
        "and 'Reviewer notes' columns on each sheet.",
        "Every other cell is extracted from the file and should be left as-is.",
        "Example row: Reviewed by = 'A. Patel', Status = 'Approved', "
        "Reviewer notes = 'Matches finance definition of net revenue'.",
        "Suggested Status values: Approved / Needs change / Duplicate / Retire.",
        "Use the filter arrows on each header row to narrow the list.",
    ]
    for line in legend:
        cell = sheet.cell(row=row, column=1, value=line)
        cell.font = BODY_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
        sheet.row_dimensions[row].height = 26
        row += 1

    if source.get("warnings"):
        row += 1
        sheet.cell(row=row, column=1, value="Extraction warnings").font = TITLE_FONT
        row += 1
        for warning in source["warnings"]:
            cell = sheet.cell(row=row, column=1, value=warning)
            cell.font = BODY_FONT
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            sheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
            sheet.row_dimensions[row].height = 40
            row += 1

    sheet.column_dimensions["A"].width = 26
    for letter in "BCDEF":
        sheet.column_dimensions[letter].width = 22
    sheet.sheet_view.showGridLines = False


def _sheet_measures(workbook: Workbook, catalogue: dict, explained: bool) -> int:
    sheet = workbook.create_sheet("Measures")
    headers = ["Measure", "Table", "Display folder", "Format"]
    if explained:
        headers += ["What it means", "Detail", "Caveats"]
    headers += ["DAX", "Patterns", "Complexity", "Confidence",
                "Times used", "Used on", "Depends on"] + ANNOTATION_COLUMNS

    rows = []
    for measure in catalogue["model"]["measures"]:
        analysis = measure.get("analysis", {})
        explanation = measure.get("explanation", {}) or {}
        places = measure.get("used_in", []) or []
        used_on = _join([f"{p.get('page')} / {p.get('visual')}" for p in places], limit=6)

        row = [
            measure["name"],
            measure.get("table"),
            measure.get("display_folder") or "",
            measure.get("format_string") or "",
        ]
        if explained:
            row += [
                explanation.get("summary", ""),
                explanation.get("detail", ""),
                _join(explanation.get("caveats"), limit=6),
            ]
        row += [
            measure.get("expression", ""),
            _join(analysis.get("patterns")),
            analysis.get("complexity", ""),
            analysis.get("confidence", ""),
            measure.get("usage_count", 0),
            used_on,
            _join(analysis.get("references", {}).get("measures")),
            "", "", "",  # annotation columns
        ]
        rows.append(row)

    _write_header(sheet, headers)
    dax_column = headers.index("DAX") + 1
    wrap = {dax_column}
    if explained:
        wrap |= {headers.index("What it means") + 1,
                 headers.index("Detail") + 1,
                 headers.index("Caveats") + 1}
    wrap.add(headers.index("Used on") + 1)
    input_columns = {len(headers) - 2, len(headers) - 1, len(headers)}

    _write_rows(sheet, rows, wrap_columns=wrap,
                mono_columns={dax_column}, input_columns=input_columns)

    widths = [30, 16, 18, 12]
    if explained:
        widths += [46, 52, 34]
    widths += [58, 30, 12, 12, 11, 34, 26, 16, 16, 34]
    _finish(sheet, widths, len(rows))

    # Point out where the confidence column comes from.
    header_cell = sheet.cell(row=1, column=headers.index("Confidence") + 1)
    header_cell.comment = Comment(
        "Confidence of the static DAX analysis.\n"
        "high = direct references\n"
        "medium = uses VAR; variable-held references are not resolved\n"
        "low = dynamic evaluation present; dependency list may be incomplete",
        "pbix-extractor",
    )
    return len(rows)


def _sheet_tables(workbook: Workbook, catalogue: dict) -> int:
    sheet = workbook.create_sheet("Tables")
    headers = ["Table", "Column", "Data type", "Kind", "Hidden", "Format",
               "Storage mode", "Calculated column DAX", "Description"] + ANNOTATION_COLUMNS

    rows = []
    for table in catalogue["model"]["tables"]:
        for column in table["columns"]:
            rows.append([
                table["name"],
                column.get("name"),
                column.get("data_type") or "",
                column.get("type") or "data",
                "Yes" if column.get("is_hidden") else "",
                column.get("format_string") or "",
                table.get("storage_mode") or "",
                column.get("expression") or "",
                column.get("description") or "",
                "", "", "",
            ])

    _write_header(sheet, headers)
    _write_rows(sheet, rows, wrap_columns={8, 9},
                mono_columns={8}, input_columns={10, 11, 12})
    _finish(sheet, [20, 26, 14, 12, 9, 14, 14, 46, 34, 16, 16, 34], len(rows))
    return len(rows)


def _sheet_relationships(workbook: Workbook, catalogue: dict) -> int:
    sheet = workbook.create_sheet("Relationships")
    headers = ["From table", "From column", "To table", "To column",
               "Cardinality", "Cross filter", "Active"] + ANNOTATION_COLUMNS

    rows = [
        [
            r["from_table"], r["from_column"], r["to_table"], r["to_column"],
            r.get("cardinality", ""), r.get("cross_filter", ""),
            "Yes" if r.get("is_active") else "No",
            "", "", "",
        ]
        for r in catalogue["model"]["relationships"]
    ]

    _write_header(sheet, headers)
    _write_rows(sheet, rows, input_columns={8, 9, 10})
    _finish(sheet, [20, 20, 20, 20, 16, 16, 9, 16, 16, 34], len(rows))

    # Inactive relationships are the ones that surprise people.
    for index, relationship in enumerate(catalogue["model"]["relationships"], start=2):
        if not relationship.get("is_active"):
            sheet.cell(row=index, column=7).fill = SEVERITY_FILL["info"]
        if relationship.get("cross_filter") == "bothDirections":
            sheet.cell(row=index, column=6).fill = SEVERITY_FILL["warning"]
    return len(rows)


def _sheet_report(workbook: Workbook, catalogue: dict) -> int:
    sheet = workbook.create_sheet("Report usage")
    headers = ["Page", "Hidden", "Visual", "Visual type", "Measures used",
               "Columns used", "Implicit aggregations", "Visual filters"]

    rows = []
    for page in catalogue["report"]["pages"]:
        page_name = page.get("display_name") or page.get("name")
        hidden = "Yes" if page.get("visibility") == 1 else ""
        for visual in page.get("visuals", []):
            rows.append([
                page_name,
                hidden,
                visual.get("title") or visual.get("id") or "",
                visual.get("type") or "",
                _join(visual.get("measures_used")),
                _join(visual.get("columns_used")),
                _join(visual.get("implicit_measures")),
                _join([f.get("field") for f in visual.get("visual_filters", [])]),
            ])

    _write_header(sheet, headers)
    _write_rows(sheet, rows, wrap_columns={5, 6, 7, 8})
    _finish(sheet, [26, 9, 30, 18, 40, 40, 30, 24], len(rows))
    return len(rows)


def _sheet_power_query(workbook: Workbook, catalogue: dict) -> int:
    sheet = workbook.create_sheet("Power Query")
    headers = ["Query", "Connector", "Server", "Database", "Steps",
               "Step names", "Native SQL"]

    sources = {s["query"]: s for s in catalogue["power_query"].get("sources", [])}
    rows = []
    for query in catalogue["power_query"].get("queries", []):
        source = sources.get(query["name"], {})
        rows.append([
            query.get("name"),
            _join(query.get("connectors")),
            source.get("server") or "",
            source.get("database") or "",
            query.get("step_count", 0),
            _join(query.get("steps"), limit=30),
            query.get("native_query") or "",
        ])

    _write_header(sheet, headers)
    _write_rows(sheet, rows, wrap_columns={6, 7}, mono_columns={7})
    _finish(sheet, [26, 18, 24, 20, 9, 46, 60], len(rows))
    return len(rows)


def _sheet_rls(workbook: Workbook, catalogue: dict) -> int:
    roles = catalogue["model"].get("roles") or []
    if not roles:
        return 0
    sheet = workbook.create_sheet("RLS")
    headers = ["Role", "Model permission", "Table", "Filter expression"] + ANNOTATION_COLUMNS

    rows = []
    for role in roles:
        permissions = role.get("table_permissions") or [{}]
        for permission in permissions:
            rows.append([
                role.get("name"),
                role.get("model_permission") or "",
                permission.get("table") or "",
                permission.get("filter_expression") or "",
                "", "", "",
            ])

    _write_header(sheet, headers)
    _write_rows(sheet, rows, wrap_columns={4}, mono_columns={4}, input_columns={5, 6, 7})
    _finish(sheet, [24, 18, 22, 60, 16, 16, 34], len(rows))
    return len(rows)


def _sheet_findings(workbook: Workbook, catalogue: dict) -> int:
    sheet = workbook.create_sheet("Findings")
    headers = ["Severity", "Category", "Finding", "Detail"] + ANNOTATION_COLUMNS

    rows = []
    for finding in catalogue["findings"]:
        rows.append([
            finding["severity"],
            finding.get("category", ""),
            finding["message"],
            _join(finding.get("detail"), limit=25),
            "", "", "",
        ])

    _write_header(sheet, headers)
    _write_rows(sheet, rows, wrap_columns={3, 4}, input_columns={5, 6, 7})
    _finish(sheet, [12, 14, 60, 60, 16, 16, 34], len(rows))

    for index, finding in enumerate(catalogue["findings"], start=2):
        fill = SEVERITY_FILL.get(finding["severity"])
        if fill:
            sheet.cell(row=index, column=1).fill = fill
    return len(rows)


# ---------------------------------------------------------------------

def _assemble(catalogue: dict) -> Workbook:
    """Build every sheet, then fill Overview against their real sizes."""
    explained = _has_explanations(catalogue)

    workbook = Workbook()
    overview = workbook.active
    overview.title = "Overview"

    built: dict[str, int] = {}
    if catalogue["model"]["available"]:
        built["Measures"] = _sheet_measures(workbook, catalogue, explained)
        built["Tables"] = _sheet_tables(workbook, catalogue)
        built["Relationships"] = _sheet_relationships(workbook, catalogue)
    built["Report usage"] = _sheet_report(workbook, catalogue)
    if catalogue["power_query"].get("queries"):
        built["Power Query"] = _sheet_power_query(workbook, catalogue)
    rls_rows = _sheet_rls(workbook, catalogue)
    if rls_rows:
        built["RLS"] = rls_rows
    built["Findings"] = _sheet_findings(workbook, catalogue)

    _fill_overview(overview, catalogue, explained, built)
    return workbook


def build_workbook(catalogue: dict, path: str) -> str:
    """Write the data dictionary and return the path written."""
    _assemble(catalogue).save(path)
    return path


def build_workbook_bytes(catalogue: dict) -> bytes:
    """In-memory variant, for handing straight to a download button."""
    import io
    buffer = io.BytesIO()
    _assemble(catalogue).save(buffer)
    return buffer.getvalue()