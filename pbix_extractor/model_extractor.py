"""
Semantic model extraction from TMSL (DataModelSchema).

Produces the model-side half of the catalogue: tables, columns,
measures with their DAX expressions, relationships with cardinality and
filter direction, hierarchies, calculation groups, RLS roles, and
partition/source-query information.

TMSL shape (abbreviated):
  { "model": {
      "tables": [ { "name", "columns": [...], "measures": [...],
                    "partitions": [...], "hierarchies": [...] } ],
      "relationships": [ { "fromTable","fromColumn","toTable","toColumn",
                           "crossFilteringBehavior","isActive",
                           "fromCardinality","toCardinality" } ],
      "roles": [ { "name", "tablePermissions": [ { "name","filterExpression" } ] } ]
  } }

Only present when the source is a .pbit or a decompiled model — see
container.py for why .pbix cannot supply this offline.
"""
from __future__ import annotations

from typing import Any


def _expr(value: Any) -> str:
    """TMSL expressions are either a string or a list of lines."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return str(value)


def _cardinality(rel: dict) -> str:
    from_card = rel.get("fromCardinality", "many")
    to_card = rel.get("toCardinality", "one")
    return f"{from_card}_to_{to_card}"


def extract_model(tmsl: dict | None) -> dict:
    """Return the model catalogue, or an empty shell if TMSL is absent."""
    if not tmsl:
        return {
            "available": False,
            "tables": [],
            "measures": [],
            "relationships": [],
            "roles": [],
            "calculation_groups": [],
            "expressions": [],
        }

    model = tmsl.get("model", tmsl)
    tables_out: list[dict] = []
    measures_out: list[dict] = []
    calc_groups: list[dict] = []

    for table in model.get("tables", []):
        tname = table.get("name", "")
        is_calc_group = "calculationGroup" in table

        columns = []
        for col in table.get("columns", []):
            columns.append({
                "name": col.get("name"),
                "data_type": col.get("dataType"),
                "type": col.get("type", "data"),          # data | calculated
                "expression": _expr(col.get("expression")),  # calculated columns
                "is_hidden": bool(col.get("isHidden", False)),
                "format_string": col.get("formatString"),
                "summarize_by": col.get("summarizeBy"),
                "sort_by_column": col.get("sortByColumn"),
                "description": col.get("description"),
            })

        for m in table.get("measures", []):
            measure = {
                "name": m.get("name"),
                "table": tname,
                "expression": _expr(m.get("expression")).strip(),
                "format_string": m.get("formatString"),
                "display_folder": m.get("displayFolder"),
                "description": m.get("description"),
                "is_hidden": bool(m.get("isHidden", False)),
            }
            measures_out.append(measure)

        partitions = []
        for pt in table.get("partitions", []):
            src = pt.get("source", {}) or {}
            partitions.append({
                "name": pt.get("name"),
                "mode": pt.get("mode"),                 # import | directQuery | dual
                "source_type": src.get("type"),         # m | query | calculated
                "expression": _expr(src.get("expression") or src.get("query")),
            })

        hierarchies = [
            {
                "name": h.get("name"),
                "levels": [
                    {"name": lv.get("name"), "column": lv.get("column"),
                     "ordinal": lv.get("ordinal")}
                    for lv in sorted(h.get("levels", []), key=lambda x: x.get("ordinal", 0))
                ],
            }
            for h in table.get("hierarchies", [])
        ]

        entry = {
            "name": tname,
            "is_hidden": bool(table.get("isHidden", False)),
            "description": table.get("description"),
            "columns": columns,
            "measure_count": len(table.get("measures", [])),
            "partitions": partitions,
            "hierarchies": hierarchies,
            "storage_mode": partitions[0]["mode"] if partitions else None,
            "is_calculation_group": is_calc_group,
        }
        tables_out.append(entry)

        if is_calc_group:
            cg = table.get("calculationGroup", {})
            calc_groups.append({
                "table": tname,
                "precedence": cg.get("precedence"),
                "items": [
                    {"name": it.get("name"),
                     "expression": _expr(it.get("expression")).strip(),
                     "format_expression": _expr(it.get("formatStringDefinition", {}).get("expression"))}
                    for it in cg.get("calculationItems", [])
                ],
            })

    relationships_out = []
    for rel in model.get("relationships", []):
        relationships_out.append({
            "name": rel.get("name"),
            "from_table": rel.get("fromTable"),
            "from_column": rel.get("fromColumn"),
            "to_table": rel.get("toTable"),
            "to_column": rel.get("toColumn"),
            "cardinality": _cardinality(rel),
            "cross_filter": rel.get("crossFilteringBehavior", "singleDirection"),
            "is_active": rel.get("isActive", True),
            "security_filtering": rel.get("securityFilteringBehavior"),
        })

    roles_out = []
    for role in model.get("roles", []):
        roles_out.append({
            "name": role.get("name"),
            "model_permission": role.get("modelPermission"),
            "table_permissions": [
                {"table": tp.get("name"), "filter_expression": _expr(tp.get("filterExpression")).strip()}
                for tp in role.get("tablePermissions", [])
            ],
        })

    # Shared M expressions (parameters, staged queries). Present in
    # .pbip projects and some templates; carried through so the Power
    # Query layer can be rebuilt from the model when there is no
    # DataMashup part to read.
    expressions_out = [
        {
            "name": e.get("name"),
            "kind": e.get("kind", "m"),
            "expression": _expr(e.get("expression")),
        }
        for e in model.get("expressions", []) or []
    ]

    return {
        "available": True,
        "culture": model.get("culture"),
        "tables": tables_out,
        "measures": measures_out,
        "relationships": relationships_out,
        "roles": roles_out,
        "calculation_groups": calc_groups,
        "expressions": expressions_out,
    }


def detect_role_playing_dimensions(relationships: list[dict]) -> list[dict]:
    """Find dimensions joined more than once to the same fact — the
    classic role-playing case (OrderDate / ShipDate against DimDate).

    Worth surfacing explicitly: inactive relationships plus
    USERELATIONSHIP variants are easy to miss when reading a model, and
    they change what any downstream generator must produce.
    """
    pairs: dict[tuple[str, str], list[dict]] = {}
    for rel in relationships:
        key = (rel["from_table"], rel["to_table"])
        pairs.setdefault(key, []).append(rel)

    findings = []
    for (from_table, to_table), rels in pairs.items():
        if len(rels) > 1:
            findings.append({
                "fact_table": from_table,
                "dimension_table": to_table,
                "relationship_count": len(rels),
                "active": [r["from_column"] for r in rels if r["is_active"]],
                "inactive": [r["from_column"] for r in rels if not r["is_active"]],
            })
    return findings