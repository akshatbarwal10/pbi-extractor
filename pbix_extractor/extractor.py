"""
Extraction orchestrator: assembles the full catalogue from one file.

Output contract: catalogue.v1 — a single JSON document holding
everything recovered, plus explicit provenance about what tier the
extraction ran in and what could not be read. Downstream consumers pin
the contract_version and should reject an unrecognised major version
rather than best-effort parse it.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from .container import open_package, TIER_FULL_MODEL
from .model_extractor import extract_model, detect_role_playing_dimensions
from .report_extractor import extract_report, measure_usage_map
from .mashup_extractor import extract_mashup, extract_from_model
from .dax_analyzer import analyze_dax, build_dependency_graph

CONTRACT_VERSION = "catalogue.v1"


def _file_hash(path: str) -> str:
    """Content hash for provenance.

    A .pbip may be a directory, so hash the tree — each relative path
    followed by its bytes, walked in sorted order for stability.
    """
    h = hashlib.sha256()
    if os.path.isdir(path):
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for filename in sorted(filenames):
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, path).replace("\\", "/")
                h.update(rel.encode("utf-8"))
                try:
                    with open(full, "rb") as f:
                        for chunk in iter(lambda: f.read(1 << 20), b""):
                            h.update(chunk)
                except OSError:
                    continue
        return h.hexdigest()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _size_bytes(path: str) -> int:
    if not os.path.isdir(path):
        return os.path.getsize(path)
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, filename))
            except OSError:
                continue
    return total


def extract(path: str) -> dict:
    """Extract a complete catalogue from a .pbix or .pbit file."""
    pkg = open_package(path)

    model = extract_model(pkg.data_model_schema)
    report = extract_report(pkg.layout)
    mashup = extract_mashup(pkg.raw("DataMashup"))

    # A .pbip carries no DataMashup part — its M lives in the model's
    # partition sources instead. Fall back to those so the Power Query
    # layer is populated for projects rather than silently empty.
    if not mashup.get("available") and model["available"]:
        from_model = extract_from_model(model)
        if from_model.get("available"):
            mashup = from_model

    # --- DAX analysis over every measure -------------------------------
    known = {m["name"] for m in model["measures"]}
    for m in model["measures"]:
        m["analysis"] = analyze_dax(m["expression"], known)

    dependency_graph = build_dependency_graph(model["measures"]) if model["measures"] else {
        "edges": {}, "circular_references": [], "base_measures": [], "top_level_measures": []
    }

    # Calculated columns and RLS filters carry business logic too.
    for table in model["tables"]:
        for col in table["columns"]:
            if col.get("expression"):
                col["analysis"] = analyze_dax(col["expression"], known)
    for role in model["roles"]:
        for tp in role["table_permissions"]:
            if tp.get("filter_expression"):
                tp["analysis"] = analyze_dax(tp["filter_expression"], known)

    # --- cross-layer linkage -------------------------------------------
    usage = measure_usage_map(report)
    for m in model["measures"]:
        places = usage.get(m["name"], [])
        m["used_in"] = places
        m["usage_count"] = len(places)

    referenced_in_report = set(usage.keys())
    unused_measures = sorted(
        m["name"] for m in model["measures"]
        if m["name"] not in referenced_in_report
        and not any(m["name"] in deps for deps in dependency_graph["edges"].values())
    )
    orphan_visual_measures = sorted(referenced_in_report - known) if known else []

    role_playing = detect_role_playing_dimensions(model["relationships"])

    stats = {
        "tables": len(model["tables"]),
        "columns": sum(len(t["columns"]) for t in model["tables"]),
        "calculated_columns": sum(
            1 for t in model["tables"] for c in t["columns"] if c.get("expression")
        ),
        "measures": len(model["measures"]),
        "relationships": len(model["relationships"]),
        "inactive_relationships": sum(1 for r in model["relationships"] if not r["is_active"]),
        "bidirectional_relationships": sum(
            1 for r in model["relationships"] if r["cross_filter"] == "bothDirections"
        ),
        "rls_roles": len(model["roles"]),
        "calculation_groups": len(model["calculation_groups"]),
        "pages": report["page_count"],
        "visuals": report["visual_count"],
        "m_queries": len(mashup.get("queries", [])),
        "unused_measures": len(unused_measures),
    }

    findings = _build_findings(model, report, dependency_graph, role_playing,
                              unused_measures, orphan_visual_measures)

    return {
        "contract_version": CONTRACT_VERSION,
        "source": {
            "file": os.path.basename(path),
            "path": os.path.abspath(path),
            "sha256": _file_hash(path),
            "size_bytes": _size_bytes(path),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extraction_tier": pkg.tier,
            "source_format": "pbip" if pkg.is_project else (
                os.path.splitext(path)[1].lstrip(".").lower() or "unknown"
            ),
            "model_available": model["available"],
            "warnings": pkg.warnings + ([mashup["warning"]] if mashup.get("warning") else []),
        },
        "statistics": stats,
        "model": model,
        "report": report,
        "power_query": mashup,
        "dax_dependency_graph": dependency_graph,
        "measure_usage": usage,
        "analysis": {
            "unused_measures": unused_measures,
            "measures_referenced_but_not_defined": orphan_visual_measures,
            "role_playing_dimensions": role_playing,
            "circular_references": dependency_graph["circular_references"],
        },
        "findings": findings,
    }


def _build_findings(model, report, graph, role_playing, unused, orphans) -> list[dict]:
    """Reviewer-facing observations. Deliberately factual: each states
    what was found and why it matters, without prescribing a fix."""
    out: list[dict] = []

    def add(severity, category, message, detail=None):
        out.append({"severity": severity, "category": category,
                    "message": message, "detail": detail or []})

    if not model["available"]:
        add("info", "coverage",
            "Model layer not extracted — source was a .pbix, whose DataModel part "
            "is compressed and unreadable offline. Report and Power Query layers "
            "were extracted normally.")

    if graph["circular_references"]:
        add("error", "dax",
            f"{len(graph['circular_references'])} circular measure reference(s) detected.",
            [" -> ".join(c) for c in graph["circular_references"]])

    if orphans:
        add("error", "consistency",
            f"{len(orphans)} measure(s) referenced by visuals do not exist in the model.",
            orphans)

    if unused:
        add("info", "hygiene",
            f"{len(unused)} measure(s) are not used by any visual and are not a "
            "dependency of another measure.", unused[:25])

    bidi = [r for r in model["relationships"] if r["cross_filter"] == "bothDirections"]
    if bidi:
        add("warning", "model",
            f"{len(bidi)} bidirectional relationship(s) — a common source of "
            "ambiguous filter paths and slow queries.",
            [f"{r['from_table']}[{r['from_column']}] <-> {r['to_table']}[{r['to_column']}]" for r in bidi])

    if role_playing:
        add("info", "model",
            f"{len(role_playing)} role-playing dimension pattern(s) detected "
            "(multiple relationships between the same two tables).",
            [f"{r['fact_table']} -> {r['dimension_table']} "
             f"(active: {r['active']}, inactive: {r['inactive']})" for r in role_playing])

    complex_measures = [
        m["name"] for m in model["measures"]
        if m.get("analysis", {}).get("complexity") == "complex"
    ]
    if complex_measures:
        add("info", "dax",
            f"{len(complex_measures)} measure(s) classified as complex — "
            "prioritise these for review and documentation.", complex_measures[:25])

    low_conf = [
        m["name"] for m in model["measures"]
        if m.get("analysis", {}).get("confidence") == "low"
    ]
    if low_conf:
        add("warning", "extraction",
            f"{len(low_conf)} measure(s) could not be fully analysed lexically; "
            "their dependency lists may be incomplete.", low_conf)

    implicit = sum(
        len(v.get("implicit_measures", []))
        for p in report.get("pages", []) for v in p.get("visuals", [])
    )
    if implicit:
        add("warning", "report",
            f"{implicit} implicit aggregation(s) used in visuals (a raw column "
            "summarised in the visual rather than an explicit measure). These "
            "carry business logic that lives outside the model.")

    hidden_pages = [
        p.get("display_name") for p in report.get("pages", [])
        if p.get("visibility") == 1
    ]
    if hidden_pages:
        add("info", "report", f"{len(hidden_pages)} hidden page(s).", hidden_pages)

    return out