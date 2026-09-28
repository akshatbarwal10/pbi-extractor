"""
Bridge to the generation accelerator.

Two outputs, because the extracted catalogue feeds the other project in
two different ways:

1. source_schema.json — the shape the generation pipeline already
   consumes (tables/columns/declared_foreign_keys/report_intent). This
   lets an existing PBIX seed a regeneration run.

2. standards.yaml fragment — a measure library harvested from DAX that
   real developers already wrote and shipped. This is the more valuable
   direction: rather than inventing a house measure library, mine it
   from the reports the business already trusts.

Design note: the harvested library is emitted as a *candidate* file,
never written over a live standards.yaml. Promoting a measure into the
organisation's standards is a governance decision with an owner, not a
side effect of running an extractor.
"""
from __future__ import annotations

import re
from collections import Counter

# TMSL data types -> the small vocabulary the generation pipeline uses.
TYPE_MAP = {
    "int64": "int", "double": "float", "decimal": "decimal",
    "string": "nvarchar", "dateTime": "datetime", "boolean": "int",
    "binary": "nvarchar", "variant": "nvarchar",
}


def to_source_schema(catalogue: dict, report_intent: str | None = None) -> dict:
    """Emit source_schema.json for the generation accelerator."""
    model = catalogue["model"]
    if not model["available"]:
        raise ValueError(
            "Cannot build a source schema: the model layer was not extracted. "
            "Supply a .pbit, or extract via XMLA, and retry."
        )

    # Columns participating in a relationship must survive the hidden
    # filter: key columns are conventionally hidden, and dropping them
    # would leave foreign keys pointing at columns not in the schema.
    relationship_columns: set[tuple[str, str]] = set()
    for r in model["relationships"]:
        relationship_columns.add((r["from_table"], r["from_column"]))
        relationship_columns.add((r["to_table"], r["to_column"]))

    tables = []
    for t in model["tables"]:
        if t.get("is_calculation_group"):
            continue
        columns = []
        for c in t["columns"]:
            is_key_column = (t["name"], c["name"]) in relationship_columns
            if c.get("is_hidden") and not is_key_column:
                continue
            columns.append({
                "name": c["name"],
                "type": TYPE_MAP.get(c.get("data_type"), "nvarchar"),
            })
        if columns:
            tables.append({"name": t["name"], "columns": columns})

    # Relationships become declared foreign keys, which the generation
    # pipeline trusts over column-name guessing.
    fks = [
        {
            "fk_table": r["from_table"], "fk_column": r["from_column"],
            "pk_table": r["to_table"], "pk_column": r["to_column"],
            "is_active": r["is_active"],
        }
        for r in model["relationships"]
    ]

    # Mark primary keys: the "one" side of each relationship.
    pk_by_table: dict[str, set[str]] = {}
    for r in model["relationships"]:
        pk_by_table.setdefault(r["to_table"], set()).add(r["to_column"])
    for t in tables:
        for c in t["columns"]:
            if c["name"] in pk_by_table.get(t["name"], set()):
                c["key"] = True

    if not report_intent:
        pages = [p.get("display_name") or p.get("name") for p in catalogue["report"]["pages"]]
        report_intent = (
            f"Rebuild of '{catalogue['source']['file']}'. "
            f"Pages: {', '.join(filter(None, pages))}." if pages
            else f"Rebuild of '{catalogue['source']['file']}'."
        )

    return {
        "source_system": f"pbix:{catalogue['source']['file']}",
        "tables": tables,
        "declared_foreign_keys": fks,
        "report_intent": report_intent,
        "provenance": {
            "extracted_from": catalogue["source"]["file"],
            "sha256": catalogue["source"]["sha256"],
            "contract_version": catalogue["contract_version"],
        },
    }


def _parameterise(expression: str, fact_tables: set[str], date_tables: set[str]) -> str:
    """Replace concrete table names with generation-pipeline placeholders."""
    out = expression
    for t in sorted(date_tables, key=len, reverse=True):
        out = re.sub(rf"'{re.escape(t)}'|\b{re.escape(t)}\b", "'{date_table}'", out)
    for t in sorted(fact_tables, key=len, reverse=True):
        out = re.sub(rf"'{re.escape(t)}'|\b{re.escape(t)}\b", "'{fact_table}'", out)
    return out


def harvest_measure_library(catalogue: dict, min_usage: int = 1) -> dict:
    """Mine a candidate measure library from measures already in use.

    Only measures actually bound to a visual (or depended on by one that
    is) are harvested — an unused measure is not evidence of a house
    standard, it is more likely leftover.
    """
    model = catalogue["model"]
    if not model["available"]:
        return {"measure_library": [], "note": "Model layer unavailable."}

    graph = catalogue["dax_dependency_graph"]["edges"]
    used_directly = {m["name"] for m in model["measures"] if m["usage_count"] >= min_usage}
    # Include dependencies of used measures.
    needed = set(used_directly)
    frontier = list(used_directly)
    while frontier:
        current = frontier.pop()
        for dep in graph.get(current, []):
            if dep not in needed:
                needed.add(dep)
                frontier.append(dep)

    # Identify the tables to parameterise.
    rel_targets = Counter(r["to_table"] for r in model["relationships"])
    fact_tables = {r["from_table"] for r in model["relationships"]}
    date_tables = {
        t["name"] for t in model["tables"]
        if any(c.get("data_type") == "dateTime" for c in t["columns"])
        and t["name"] in rel_targets
    }

    library = []
    for m in model["measures"]:
        if m["name"] not in needed:
            continue
        analysis = m.get("analysis", {})
        library.append({
            "name": m["name"],
            "dax": _parameterise(m["expression"], fact_tables, date_tables),
            "original_dax": m["expression"],
            "format_string": m.get("format_string"),
            "display_folder": m.get("display_folder"),
            "patterns": analysis.get("patterns", []),
            "complexity": analysis.get("complexity"),
            "usage_count": m["usage_count"],
            "source_table": m["table"],
        })

    library.sort(key=lambda x: (-x["usage_count"], x["name"]))

    pattern_counts = Counter(p for item in library for p in item["patterns"])
    return {
        "measure_library": library,
        "harvested_from": catalogue["source"]["file"],
        "pattern_summary": dict(pattern_counts.most_common()),
        "note": (
            "Candidate library harvested from measures in active use. "
            "Review before promoting into standards.yaml — placeholders "
            "{fact_table} and {date_table} were substituted heuristically "
            "and should be checked."
        ),
    }