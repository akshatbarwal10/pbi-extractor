"""
Client-ready PDF report.

Different audience from both other outputs: the HTML index is for
whoever ran the extraction, the Excel dictionary is for a steward who
will annotate it, and this is the document that gets attached to an
email or handed to an engagement lead. So it leads with findings and
plain-language meaning, and keeps DAX subordinate to the explanation.

The methodology appendix is not filler. This document travels further
than the person who produced it, and a reader needs to know that a
`.pbix` yields no model, that the DAX analysis is lexical, and which
measures were therefore reported at lower confidence. A catalogue that
looks authoritative while quietly omitting 200 measures is the failure
mode worth designing against.

Requires reportlab. Imported lazily so the core package keeps working
without it.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate,
    Paragraph, Spacer, Table, TableStyle, XPreformatted,
)

ACCENT = colors.HexColor("#1F3864")
MUTED = colors.HexColor("#5B6472")
LINE = colors.HexColor("#D9DDE5")
PANEL = colors.HexColor("#F5F7FA")
CODE_BG = colors.HexColor("#F2F3F7")

SEVERITY_COLOR = {
    "error": colors.HexColor("#B3261E"),
    "warning": colors.HexColor("#8A5A00"),
    "info": colors.HexColor("#1F3864"),
}

PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN = 18 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN


def _styles():
    base = getSampleStyleSheet()
    s = {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=24, leading=28, textColor=ACCENT, alignment=TA_LEFT,
            spaceAfter=4,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName="Helvetica",
            fontSize=10.5, leading=15, textColor=MUTED, spaceAfter=18,
        ),
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontName="Helvetica-Bold",
            fontSize=15, leading=19, textColor=ACCENT,
            spaceBefore=16, spaceAfter=8,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=11.5, leading=15, textColor=colors.HexColor("#14181F"),
            spaceBefore=10, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=14, spaceAfter=5,
        ),
        "muted": ParagraphStyle(
            "muted", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=12, textColor=MUTED, spaceAfter=4,
        ),
        "lead": ParagraphStyle(
            "lead", parent=base["Normal"], fontName="Helvetica",
            fontSize=10.5, leading=15.5, spaceAfter=8,
        ),
        "code": ParagraphStyle(
            "code", parent=base["Code"], fontName="Courier",
            fontSize=7.8, leading=10.5, textColor=colors.HexColor("#14181F"),
            backColor=CODE_BG, borderPadding=5, spaceBefore=3, spaceAfter=6,
            leftIndent=0, firstLineIndent=0,
        ),
        "caveat": ParagraphStyle(
            "caveat", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.8, leading=12.5, textColor=colors.HexColor("#8A5A00"),
            leftIndent=9, spaceAfter=2,
        ),
        "detail": ParagraphStyle(
            "detail", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=12.5, textColor=MUTED,
            leftIndent=12, spaceAfter=2,
        ),
    }
    return s


def _p(text, style) -> Paragraph:
    return Paragraph(escape(str(text if text is not None else "")), style)


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 11 * mm, doc.report_title)
    canvas.drawRightString(PAGE_WIDTH - MARGIN, 11 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(MARGIN, 14 * mm, PAGE_WIDTH - MARGIN, 14 * mm)
    canvas.restoreState()


def _kv_table(rows: list[tuple[str, str]], styles, width_split=(48, 0)) -> Table:
    left = width_split[0] * mm
    table = Table(
        [[_p(k, styles["muted"]), _p(v, styles["body"])] for k, v in rows],
        colWidths=[left, CONTENT_WIDTH - left],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _data_table(header: list[str], rows: list[list], widths: list[float], styles) -> Table:
    head = [_p(h, ParagraphStyle("th", parent=styles["muted"],
                                 fontName="Helvetica-Bold",
                                 textColor=colors.white, fontSize=8)) for h in header]
    body = [[_p(cell, styles["muted"]) for cell in row] for row in rows]
    table = Table([head] + body, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, PANEL]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _stat_row(stats: list[tuple[str, str]], styles) -> Table:
    cells = [[
        Paragraph(f'<font size="17" color="#1F3864"><b>{escape(str(value))}</b></font>'
                  f'<br/><font size="7" color="#5B6472">{escape(label.upper())}</font>',
                  styles["body"])
        for label, value in stats
    ]]
    width = CONTENT_WIDTH / len(stats)
    table = Table(cells, colWidths=[width] * len(stats))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("BOX", (0, 0), (-1, -1), 0.4, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return table


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


# ---------------------------------------------------------------------

def _cover(story, catalogue, styles):
    source = catalogue["source"]
    stats = catalogue["statistics"]
    tier = source["extraction_tier"]

    story.append(_p("Power BI Report Catalogue", styles["title"]))
    story.append(_p(source["file"], styles["subtitle"]))

    headline = [
        ("Tables", stats["tables"]),
        ("Measures", stats["measures"]),
        ("Relationships", stats["relationships"]),
        ("Pages", stats["pages"]),
        ("Visuals", stats["visuals"]),
    ]
    story.append(_stat_row(headline, styles))
    story.append(Spacer(1, 12))

    story.append(_kv_table([
        ("Extraction tier", tier),
        ("Source format", source.get("source_format", "")),
        ("Extracted", source["extracted_at"][:19].replace("T", " ") + " UTC"),
        ("Content hash", source["sha256"][:32] + "…"),
        ("Contract", catalogue["contract_version"]),
    ], styles))

    if tier != "full_model":
        story.append(Spacer(1, 10))
        story.append(_callout(
            "Partial extraction",
            "This file is a .pbix, whose data model is stored in a proprietary "
            "compressed format that cannot be read outside Power BI. Measures, "
            "relationships, columns and row-level security are NOT included in "
            "this document. The report layer and Power Query lineage below are "
            "complete. To catalogue the model, supply a .pbit or .pbip instead.",
            styles, SEVERITY_COLOR["warning"]))


def _callout(title, text, styles, color) -> Table:
    # hexval() yields '0xrrggbb'; the inline markup parser needs '#rrggbb'.
    swatch = "#" + color.hexval()[2:]
    inner = [
        [Paragraph(f'<b><font color="{swatch}">{escape(title)}</font></b>',
                   styles["body"])],
        [_p(text, styles["muted"])],
    ]
    table = Table(inner, colWidths=[CONTENT_WIDTH])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PANEL),
        ("LINEBEFORE", (0, 0), (0, -1), 2.5, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (0, 0), 7),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 7),
    ]))
    return table


def _findings(story, catalogue, styles):
    findings = catalogue.get("findings") or []
    story.append(_p("Findings", styles["h1"]))
    if not findings:
        story.append(_p("No findings were raised.", styles["body"]))
        return

    counts = {s: sum(1 for f in findings if f["severity"] == s)
              for s in ("error", "warning", "info")}
    story.append(_p(
        f"{counts['error']} error(s), {counts['warning']} warning(s), "
        f"{counts['info']} informational.", styles["body"]))
    story.append(Spacer(1, 6))

    order = {"error": 0, "warning": 1, "info": 2}
    for finding in sorted(findings, key=lambda f: order.get(f["severity"], 3)):
        color = SEVERITY_COLOR.get(finding["severity"], MUTED)
        block = [_callout(
            f"{finding['severity'].upper()} · {finding.get('category', '')}",
            finding["message"], styles, color)]
        detail = finding.get("detail") or []
        if detail:
            shown = detail[:8]
            lines = "<br/>".join(f"• {escape(str(d))}" for d in shown)
            if len(detail) > len(shown):
                lines += f"<br/>• … and {len(detail) - len(shown)} more"
            # Neutral, not amber — these items belong to findings of every
            # severity, and colouring them all as warnings misreads the page.
            block.append(Paragraph(lines, styles["detail"]))
        block.append(Spacer(1, 7))
        story.append(KeepTogether(block))


def _measures(story, catalogue, styles):
    measures = catalogue["model"].get("measures") or []
    if not measures:
        return
    story.append(PageBreak())
    story.append(_p("Measure catalogue", styles["h1"]))

    explained = sum(1 for m in measures if m.get("explanation", {}).get("summary"))
    used = sum(1 for m in measures if m.get("usage_count", 0) > 0)
    story.append(_p(
        f"{len(measures)} measures, of which {used} are bound to at least one "
        f"visual. {explained} carry a plain-language explanation.",
        styles["body"]))
    story.append(Spacer(1, 6))

    # Used measures first — they are the ones that matter to a reader.
    ordered = sorted(measures, key=lambda m: (-m.get("usage_count", 0), m["name"]))

    for measure in ordered:
        analysis = measure.get("analysis", {}) or {}
        explanation = measure.get("explanation", {}) or {}
        block = []

        usage = measure.get("usage_count", 0)
        meta = f"{measure.get('table')}"
        if measure.get("display_folder"):
            meta += f" · {measure['display_folder']}"
        meta += f" · used {usage}×" if usage else " · not used by any visual"

        block.append(_p(measure["name"], styles["h2"]))
        block.append(_p(meta, styles["muted"]))

        if explanation.get("summary"):
            block.append(_p(explanation["summary"], styles["lead"]))
        if explanation.get("detail"):
            block.append(_p(explanation["detail"], styles["body"]))

        for caveat in explanation.get("caveats") or []:
            block.append(Paragraph(f"⚠ {escape(str(caveat))}", styles["caveat"]))

        dax = _truncate(measure.get("expression", ""), 1400)
        if dax:
            block.append(XPreformatted(escape(dax), styles["code"]))

        tags = []
        if analysis.get("patterns"):
            tags.append("Patterns: " + ", ".join(analysis["patterns"]))
        if analysis.get("complexity"):
            tags.append(f"Complexity: {analysis['complexity']}")
        if analysis.get("confidence") and analysis["confidence"] != "high":
            tags.append(f"Analysis confidence: {analysis['confidence']}")
        if tags:
            block.append(_p(" · ".join(tags), styles["muted"]))

        places = measure.get("used_in") or []
        if places:
            shown = "; ".join(
                f"{p.get('page')} / {p.get('visual')}" for p in places[:5])
            if len(places) > 5:
                shown += f"; +{len(places) - 5} more"
            block.append(_p(f"Used on: {shown}", styles["muted"]))

        block.append(Spacer(1, 9))
        story.append(KeepTogether(block))


def _model_structure(story, catalogue, styles):
    model = catalogue["model"]
    if not model.get("available"):
        return
    story.append(PageBreak())
    story.append(_p("Model structure", styles["h1"]))

    story.append(_p("Tables", styles["h2"]))
    rows = [
        [t["name"], str(len(t["columns"])), str(t.get("measure_count", 0)),
         t.get("storage_mode") or "", "Yes" if t.get("is_hidden") else ""]
        for t in model["tables"]
    ]
    story.append(_data_table(
        ["Table", "Columns", "Measures", "Storage", "Hidden"], rows,
        [CONTENT_WIDTH * 0.40, CONTENT_WIDTH * 0.13, CONTENT_WIDTH * 0.15,
         CONTENT_WIDTH * 0.18, CONTENT_WIDTH * 0.14], styles))

    if model.get("relationships"):
        story.append(Spacer(1, 10))
        story.append(_p("Relationships", styles["h2"]))
        rows = [
            [f"{r['from_table']}[{r['from_column']}]",
             f"{r['to_table']}[{r['to_column']}]",
             r.get("cardinality", ""),
             "Both" if r.get("cross_filter") == "bothDirections" else "Single",
             "Active" if r.get("is_active") else "Inactive"]
            for r in model["relationships"]
        ]
        story.append(_data_table(
            ["From", "To", "Cardinality", "Cross filter", "State"], rows,
            [CONTENT_WIDTH * 0.26, CONTENT_WIDTH * 0.26, CONTENT_WIDTH * 0.19,
             CONTENT_WIDTH * 0.15, CONTENT_WIDTH * 0.14], styles))

    if model.get("roles"):
        story.append(Spacer(1, 10))
        story.append(_p("Row-level security", styles["h2"]))
        for role in model["roles"]:
            story.append(_p(
                f"{role['name']} ({role.get('model_permission') or 'read'})",
                styles["body"]))
            for permission in role.get("table_permissions", []):
                story.append(_p(permission.get("table", ""), styles["muted"]))
                story.append(XPreformatted(
                    escape(_truncate(permission.get("filter_expression", ""), 600)),
                    styles["code"]))


def _report_structure(story, catalogue, styles):
    report = catalogue.get("report") or {}
    pages = report.get("pages") or []
    if not pages:
        return
    story.append(PageBreak())
    story.append(_p("Report structure", styles["h1"]))
    story.append(_p(
        "What each visual actually binds — the business logic that lives in "
        "the report rather than the model.", styles["body"]))

    for page in pages:
        name = page.get("display_name") or page.get("name")
        suffix = " (hidden)" if page.get("visibility") == 1 else ""
        story.append(_p(f"{name}{suffix}", styles["h2"]))

        filters = [f.get("field") for f in page.get("page_filters", []) if f.get("field")]
        if filters:
            story.append(_p("Page filters: " + ", ".join(filters), styles["muted"]))

        rows = []
        for visual in page.get("visuals", []):
            bound = list(visual.get("measures_used") or [])
            bound += list(visual.get("columns_used") or [])
            bound += list(visual.get("implicit_measures") or [])
            rows.append([
                visual.get("title") or visual.get("id") or "",
                visual.get("type") or "",
                _truncate(", ".join(bound), 150) or "—",
            ])
        if rows:
            story.append(_data_table(
                ["Visual", "Type", "Fields bound"], rows,
                [CONTENT_WIDTH * 0.26, CONTENT_WIDTH * 0.18, CONTENT_WIDTH * 0.56],
                styles))
        story.append(Spacer(1, 8))


def _lineage(story, catalogue, styles):
    queries = (catalogue.get("power_query") or {}).get("queries") or []
    if not queries:
        return
    story.append(PageBreak())
    story.append(_p("Data source lineage", styles["h1"]))

    sources = {s["query"]: s for s in catalogue["power_query"].get("sources", [])}
    rows = []
    for query in queries:
        source = sources.get(query["name"], {})
        rows.append([
            query.get("name") or "",
            ", ".join(query.get("connectors") or []) or "—",
            source.get("server") or "—",
            source.get("database") or "—",
            str(query.get("step_count", 0)),
        ])
    story.append(_data_table(
        ["Query", "Connector", "Server", "Database", "Steps"], rows,
        [CONTENT_WIDTH * 0.24, CONTENT_WIDTH * 0.19, CONTENT_WIDTH * 0.24,
         CONTENT_WIDTH * 0.20, CONTENT_WIDTH * 0.13], styles))

    native = [(q["name"], q["native_query"]) for q in queries if q.get("native_query")]
    if native:
        story.append(Spacer(1, 10))
        story.append(_p("Native SQL", styles["h2"]))
        for name, sql in native:
            story.append(_p(name, styles["muted"]))
            story.append(XPreformatted(escape(_truncate(sql, 900)), styles["code"]))


def _methodology(story, catalogue, styles):
    story.append(PageBreak())
    story.append(_p("Methodology and limits", styles["h1"]))

    tier = catalogue["source"]["extraction_tier"]
    story.append(_p("How this was produced", styles["h2"]))
    story.append(_p(
        "Every figure in this document was read directly from the report file. "
        "Nothing was inferred from a data refresh, and no query was run against "
        "any source system.", styles["body"]))

    story.append(_p("Coverage", styles["h2"]))
    if tier == "full_model":
        story.append(_p(
            "The semantic model was fully readable, so measures, relationships, "
            "columns, hierarchies and row-level security are complete.",
            styles["body"]))
    else:
        story.append(_p(
            "The semantic model could not be read from this file type, so the "
            "measure, relationship and column sections are absent rather than "
            "empty. Supply a .pbit or .pbip for full coverage.", styles["body"]))

    story.append(_p("DAX analysis", styles["h2"]))
    story.append(_p(
        "Dependencies and calculation patterns are derived by lexical analysis "
        "of each expression rather than by a full DAX parser. Direct references "
        "are reliable. Expressions using VAR are reported at medium confidence, "
        "because references held in variables are not resolved; expressions "
        "using dynamic evaluation are reported at low confidence.", styles["body"]))

    measures = catalogue["model"].get("measures") or []
    lowered = [m["name"] for m in measures
               if m.get("analysis", {}).get("confidence") in ("medium", "low")]
    if lowered:
        shown = ", ".join(lowered[:18])
        if len(lowered) > 18:
            shown += f", and {len(lowered) - 18} more"
        story.append(_p(
            f"{len(lowered)} measure(s) were analysed at reduced confidence: {shown}.",
            styles["muted"]))

    explained = [m for m in measures if m.get("explanation", {}).get("summary")]
    if explained:
        model_name = explained[0]["explanation"].get("generated_by", "a language model")
        story.append(_p("Plain-language explanations", styles["h2"]))
        story.append(_p(
            f"Measure explanations were generated by {model_name} from the DAX, "
            "its dependency chain, and where each measure is used in the report. "
            "They are an aid to review, not an authoritative statement of "
            "business intent, and should be confirmed by an owner before being "
            "treated as documentation.", styles["body"]))

    story.append(_p("Report layer", styles["h2"]))
    story.append(_p(
        "Visual bindings are read from the report definition. Custom visuals "
        "from AppSource may expose their field bindings in a non-standard shape "
        "and can therefore under-report.", styles["body"]))


# ---------------------------------------------------------------------

def _build(catalogue: dict, target) -> None:
    styles = _styles()
    title = catalogue["source"]["file"]

    doc = BaseDocTemplate(
        target, pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN + 6 * mm,
        title=f"Power BI Catalogue — {title}",
        author="pbix-extractor",
    )
    doc.report_title = title
    frame = Frame(MARGIN, MARGIN + 6 * mm, CONTENT_WIDTH,
                  PAGE_HEIGHT - 2 * MARGIN - 6 * mm, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_footer)])

    story: list = []
    _cover(story, catalogue, styles)
    _findings(story, catalogue, styles)
    _measures(story, catalogue, styles)
    _model_structure(story, catalogue, styles)
    _report_structure(story, catalogue, styles)
    _lineage(story, catalogue, styles)
    _methodology(story, catalogue, styles)

    doc.build(story)


def build_pdf(catalogue: dict, path: str) -> str:
    """Write the client report and return the path written."""
    _build(catalogue, path)
    return path


def build_pdf_bytes(catalogue: dict) -> bytes:
    """In-memory variant, for handing straight to a download button."""
    buffer = io.BytesIO()
    _build(catalogue, buffer)
    return buffer.getvalue()