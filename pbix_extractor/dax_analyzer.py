"""
DAX analysis: turning expression text into dependency and intent data.

This is a lexical analyser, not a DAX parser. It tokenises expressions
and extracts:

  - referenced measures        [Measure Name]
  - referenced columns         'Table'[Column] or Table[Column]
  - referenced tables
  - DAX functions used
  - a business-logic classification (time intelligence, ratio, ranking,
    filtered aggregation, ...)

Why lexical rather than a full grammar: a complete DAX parser is a
large piece of work and the useful signal here — what does this measure
depend on, and what kind of calculation is it — is recoverable from
tokens. The limits are stated honestly in `confidence`: expressions
using variables heavily, or constructing references dynamically, are
flagged as lower confidence rather than silently reported as complete.

Known lexical hazard (worth keeping): a measure named e.g.
"Net Revenue (base)" must not be read as a call to a REVENUE() function.
Object references are therefore stripped BEFORE function scanning.
"""
from __future__ import annotations

import re

# 'Table'[Column]  or  Table[Column]
COLUMN_REF = re.compile(r"(?:'([^']+)'|(\b[A-Za-z_]\w*))\s*\[([^\]]+)\]")
# [Measure]  — a bracket reference with no table qualifier
MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")
# FUNC(  — an identifier immediately followed by an opening paren
FUNCTION_CALL = re.compile(r"\b([A-Z][A-Z0-9_.]{1,})\s*\(", re.IGNORECASE)
# VAR name =
VAR_DECL = re.compile(r"\bVAR\s+([A-Za-z_]\w*)", re.IGNORECASE)

TIME_INTELLIGENCE = {
    "TOTALYTD", "TOTALQTD", "TOTALMTD", "DATESYTD", "DATESQTD", "DATESMTD",
    "SAMEPERIODLASTYEAR", "PARALLELPERIOD", "DATEADD", "DATESBETWEEN",
    "DATESINPERIOD", "PREVIOUSYEAR", "PREVIOUSQUARTER", "PREVIOUSMONTH",
    "PREVIOUSDAY", "NEXTYEAR", "NEXTQUARTER", "NEXTMONTH", "NEXTDAY",
    "STARTOFYEAR", "ENDOFYEAR", "FIRSTDATE", "LASTDATE", "OPENINGBALANCEYEAR",
    "CLOSINGBALANCEYEAR", "CLOSINGBALANCEMONTH", "CLOSINGBALANCEQUARTER",
}
AGGREGATIONS = {
    "SUM", "SUMX", "AVERAGE", "AVERAGEX", "MIN", "MINX", "MAX", "MAXX",
    "COUNT", "COUNTA", "COUNTX", "COUNTROWS", "DISTINCTCOUNT",
    "DISTINCTCOUNTNOBLANK", "PRODUCT", "PRODUCTX", "MEDIAN", "MEDIANX",
    "PERCENTILE.INC", "PERCENTILE.EXC", "STDEV.P", "STDEV.S", "VAR.P", "VAR.S",
}
RANKING = {"RANKX", "TOPN", "RANK.EQ", "SAMPLE"}
FILTER_FUNCS = {"CALCULATE", "CALCULATETABLE", "FILTER", "ALL", "ALLEXCEPT",
                "ALLSELECTED", "ALLNOBLANKROW", "KEEPFILTERS", "REMOVEFILTERS",
                "USERELATIONSHIP", "CROSSFILTER", "TREATAS"}
RATIO_FUNCS = {"DIVIDE"}
PARENT_CHILD = {"PATH", "PATHITEM", "PATHLENGTH", "PATHCONTAINS"}
# Reserved words that look like function calls after stripping.
KEYWORDS = {"VAR", "RETURN", "IF", "SWITCH", "AND", "OR", "NOT", "IN", "TRUE", "FALSE"}


def _strip_string_literals(expr: str) -> str:
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', expr)


def _strip_comments(expr: str) -> str:
    expr = re.sub(r"//[^\n]*", " ", expr)
    expr = re.sub(r"/\*.*?\*/", " ", expr, flags=re.DOTALL)
    return expr


def analyze_dax(expression: str, known_measures: set[str] | None = None) -> dict:
    """Extract dependencies and classify the calculation."""
    known_measures = known_measures or set()
    raw = expression or ""
    if not raw.strip():
        return {
            "references": {"measures": [], "columns": [], "tables": []},
            "functions": [], "patterns": [], "variables": [],
            "complexity": "empty", "confidence": "high", "notes": [],
        }

    cleaned = _strip_comments(_strip_string_literals(raw))
    notes: list[str] = []

    # --- column references (capture before stripping) ---
    columns, tables = [], set()
    for m in COLUMN_REF.finditer(cleaned):
        table = m.group(1) or m.group(2)
        column = m.group(3)
        columns.append({"table": table, "column": column})
        tables.add(table)

    # Strip qualified column refs so their bracket parts are not later
    # mistaken for measure references.
    without_columns = COLUMN_REF.sub(" ", cleaned)

    measures = []
    for m in MEASURE_REF.finditer(without_columns):
        name = m.group(1).strip()
        if name:
            measures.append(name)

    # --- functions: scan only AFTER every object reference is removed,
    # so a measure called "Net Revenue (base)" is never read as a call.
    without_refs = MEASURE_REF.sub(" ", without_columns)
    functions = set()
    for m in FUNCTION_CALL.finditer(without_refs):
        fn = m.group(1).upper()
        if fn not in KEYWORDS:
            functions.add(fn)

    variables = [m.group(1) for m in VAR_DECL.finditer(cleaned)]

    # --- classification ---
    patterns = []
    if functions & TIME_INTELLIGENCE:
        patterns.append("time_intelligence")
    if functions & RATIO_FUNCS or "/" in without_refs:
        patterns.append("ratio")
    if functions & RANKING:
        patterns.append("ranking")
    if "CALCULATE" in functions or "CALCULATETABLE" in functions:
        patterns.append("filtered_aggregation")
    if functions & {"ALL", "ALLEXCEPT", "ALLSELECTED", "REMOVEFILTERS"}:
        patterns.append("percent_of_total_candidate")
    if "USERELATIONSHIP" in functions:
        patterns.append("role_playing_dimension")
    if functions & PARENT_CHILD:
        patterns.append("parent_child_hierarchy")
    if functions & {"SUMX", "AVERAGEX", "MAXX", "MINX", "COUNTX", "RANKX", "PRODUCTX", "MEDIANX"}:
        patterns.append("iterator")
    if "SWITCH" in without_refs.upper() or re.search(r"\bIF\s*\(", without_refs, re.I):
        patterns.append("conditional_logic")
    if functions & AGGREGATIONS and not patterns:
        patterns.append("simple_aggregation")
    if measures and not (functions & AGGREGATIONS):
        patterns.append("composed_from_measures")

    # --- complexity ---
    length = len(raw)
    depth = raw.count("(")
    if length < 60 and depth <= 2:
        complexity = "simple"
    elif length < 300 and depth <= 8:
        complexity = "moderate"
    else:
        complexity = "complex"

    # --- confidence in the lexical read ---
    confidence = "high"
    if variables:
        confidence = "medium"
        notes.append(
            f"Uses {len(variables)} VAR declaration(s); variable-held references "
            "are not resolved by lexical analysis."
        )
    if re.search(r"\b(INDIRECT|EVALUATE)\b", cleaned, re.I):
        confidence = "low"
        notes.append("Contains dynamic evaluation; dependency list may be incomplete.")
    unresolved = [m for m in measures if known_measures and m not in known_measures]
    if unresolved:
        notes.append(
            f"References not matched to a known measure (may be columns or "
            f"variables): {', '.join(sorted(set(unresolved))[:5])}"
        )

    # Deduplicate columns preserving order
    seen, uniq_cols = set(), []
    for c in columns:
        key = (c["table"], c["column"])
        if key not in seen:
            seen.add(key)
            uniq_cols.append(c)

    return {
        "references": {
            "measures": sorted(set(measures)),
            "columns": uniq_cols,
            "tables": sorted(tables),
        },
        "functions": sorted(functions),
        "patterns": patterns,
        "variables": variables,
        "complexity": complexity,
        "confidence": confidence,
        "notes": notes,
    }


def build_dependency_graph(measures: list[dict]) -> dict:
    """Measure-to-measure dependency edges, plus circular-reference detection."""
    names = {m["name"] for m in measures}
    edges: dict[str, list[str]] = {}

    for m in measures:
        analysis = m.get("analysis") or analyze_dax(m.get("expression", ""), names)
        deps = [d for d in analysis["references"]["measures"] if d in names]
        edges[m["name"]] = sorted(set(deps))

    # Detect cycles with an iterative DFS (no recursion limit risk).
    cycles: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}

    for start in edges:
        if color[start] != WHITE:
            continue
        stack = [(start, iter(edges[start]))]
        path = [start]
        color[start] = GREY
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if color.get(child, BLACK) == GREY:
                    idx = path.index(child) if child in path else 0
                    cycle = path[idx:] + [child]
                    if cycle not in cycles:
                        cycles.append(cycle)
                elif color.get(child, BLACK) == WHITE:
                    color[child] = GREY
                    path.append(child)
                    stack.append((child, iter(edges[child])))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()
                if path:
                    path.pop()

    roots = [n for n, d in edges.items() if not d]
    leaves = [n for n in edges if not any(n in d for d in edges.values())]

    return {
        "edges": edges,
        "circular_references": cycles,
        "base_measures": sorted(roots),
        "top_level_measures": sorted(leaves),
    }