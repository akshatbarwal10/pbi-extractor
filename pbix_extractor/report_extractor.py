"""
Report layer extraction from Report/Layout.

Layout is the report definition: pages ("sections"), visual containers,
field bindings, filters, bookmarks and themes. Its quirk is nested
JSON-as-string — `config`, `filters`, `query` and `dataTransforms` are
each a JSON document stored inside a string field of the parent JSON,
sometimes two levels deep. Everything here unwraps that carefully and
degrades to a partial result rather than throwing when a visual has an
unexpected shape (visual schemas vary by Power BI version and by custom
visual).

What this yields that the model alone cannot: which measures and
columns are actually USED, on which page, in which visual, under which
filters. That is the report-wise business logic — the difference
between "the model defines 200 measures" and "these 34 are on a page
anyone looks at".
"""
from __future__ import annotations

import json
from typing import Any


def _maybe_json(value: Any) -> Any:
    """Layout stores nested documents as JSON strings; unwrap safely."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _walk(node: Any):
    """Yield every dict in a nested structure."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _field_refs(node: Any) -> list[dict]:
    """Collect field references from a visual's query/config subtree.

    Power BI encodes references as:
      {"Column":  {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Amount"}}
      {"Measure": {"Expression": {"SourceRef": {"Entity": "Sales"}}, "Property": "Total"}}
      {"Aggregation": {"Expression": {"Column": {...}}, "Function": 0}}
    """
    refs: list[dict] = []
    agg_names = {0: "Sum", 1: "Avg", 2: "Min", 3: "Max", 4: "Count",
                 5: "CountNonNull", 6: "Median", 7: "StdDev", 8: "Var"}

    for d in _walk(node):
        for kind in ("Column", "Measure", "HierarchyLevel"):
            ref = d.get(kind)
            if isinstance(ref, dict) and "Property" in ref:
                entity = None
                src = ref.get("Expression", {})
                for inner in _walk(src):
                    if "Entity" in inner:
                        entity = inner["Entity"]
                        break
                refs.append({
                    "kind": "measure" if kind == "Measure" else "column",
                    "table": entity,
                    "name": ref.get("Property"),
                })
        agg = d.get("Aggregation")
        if isinstance(agg, dict):
            fn = agg_names.get(agg.get("Function"), "Agg")
            for inner in _walk(agg.get("Expression", {})):
                col = inner.get("Column")
                if isinstance(col, dict) and "Property" in col:
                    entity = None
                    for e in _walk(col.get("Expression", {})):
                        if "Entity" in e:
                            entity = e["Entity"]
                            break
                    refs.append({
                        "kind": "implicit_measure",
                        "table": entity,
                        "name": col.get("Property"),
                        "aggregation": fn,
                    })

    seen, uniq = set(), []
    for r in refs:
        key = (r["kind"], r.get("table"), r["name"], r.get("aggregation"))
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _parse_filters(raw: Any) -> list[dict]:
    filters = _maybe_json(raw)
    if not isinstance(filters, list):
        return []
    out = []
    for f in filters:
        if not isinstance(f, dict):
            continue
        expr = f.get("expression", {})
        refs = _field_refs(expr)
        target = refs[0] if refs else {}
        out.append({
            "name": f.get("name"),
            "type": f.get("type"),
            "table": target.get("table"),
            "field": target.get("name"),
            "has_condition": bool(f.get("filter")),
            "is_locked": bool(f.get("isLockedInViewMode", False)),
            "is_hidden": bool(f.get("isHiddenInViewMode", False)),
        })
    return out


def _visual_type(config: dict) -> str:
    single = config.get("singleVisual") or {}
    return (
        single.get("visualType")
        or config.get("visualType")
        or (config.get("singleVisualGroup") or {}).get("displayName")
        or "unknown"
    )


def _visual_title(config: dict) -> str | None:
    single = config.get("singleVisual") or {}
    objects = single.get("vcObjects") or single.get("objects") or {}
    title = objects.get("title")
    if isinstance(title, list) and title:
        props = title[0].get("properties", {})
        text = props.get("text", {})
        for d in _walk(text):
            lit = d.get("Literal")
            if isinstance(lit, dict) and "Value" in lit:
                return str(lit["Value"]).strip("'")
    return None


def extract_report(layout: dict | None) -> dict:
    """Return the report catalogue: pages, visuals, bindings, filters."""
    if not layout:
        return {"available": False, "pages": [], "report_filters": [],
                "bookmarks": [], "theme": None, "page_count": 0, "visual_count": 0}

    pages = []
    for section in layout.get("sections", []):
        page_filters = _parse_filters(section.get("filters"))
        visuals = []

        for vc in section.get("visualContainers", []):
            config = _maybe_json(vc.get("config")) or {}
            if not isinstance(config, dict):
                config = {}
            query = _maybe_json(vc.get("query"))
            transforms = _maybe_json(vc.get("dataTransforms"))

            # Field references may live in config, query or transforms
            # depending on version — scan all three.
            refs = _field_refs(config)
            for extra in (query, transforms):
                if extra:
                    for r in _field_refs(extra):
                        if r not in refs:
                            refs.append(r)

            single = config.get("singleVisual") or {}
            visuals.append({
                "id": config.get("name") or vc.get("id"),
                "type": _visual_type(config),
                "title": _visual_title(config),
                "x": vc.get("x"), "y": vc.get("y"),
                "width": vc.get("width"), "height": vc.get("height"),
                "z": vc.get("z"),
                "tab_order": vc.get("tabOrder"),
                "fields": refs,
                "measures_used": sorted({r["name"] for r in refs if r["kind"] == "measure"}),
                "columns_used": sorted({
                    f"{r.get('table')}[{r['name']}]" for r in refs if r["kind"] == "column"
                }),
                "implicit_measures": [
                    f"{r.get('aggregation')}({r.get('table')}[{r['name']}])"
                    for r in refs if r["kind"] == "implicit_measure"
                ],
                "visual_filters": _parse_filters(vc.get("filters")),
                "has_conditional_formatting": "objects" in single and bool(single.get("objects")),
            })

        pages.append({
            "name": section.get("name"),
            "display_name": section.get("displayName"),
            "ordinal": section.get("ordinal"),
            "width": section.get("width"),
            "height": section.get("height"),
            "visibility": (_maybe_json(section.get("config")) or {}).get("visibility"),
            "page_filters": page_filters,
            "visuals": visuals,
            "visual_count": len(visuals),
        })

    pages.sort(key=lambda p: (p.get("ordinal") is None, p.get("ordinal") or 0))

    config = _maybe_json(layout.get("config")) or {}
    bookmarks = []
    for bm in (config.get("bookmarks") or []):
        bookmarks.append({
            "name": bm.get("name"),
            "display_name": bm.get("displayName"),
            "targets_page": (bm.get("explorationState") or {}).get("activeSection"),
        })

    return {
        "available": True,
        "pages": pages,
        "page_count": len(pages),
        "visual_count": sum(p["visual_count"] for p in pages),
        "report_filters": _parse_filters(layout.get("filters")),
        "bookmarks": bookmarks,
        "theme": (config.get("themeCollection") or {}).get("baseTheme", {}).get("name"),
    }


def measure_usage_map(report: dict) -> dict[str, list[dict]]:
    """Where each measure is actually used: measure -> [{page, visual}]."""
    usage: dict[str, list[dict]] = {}
    for page in report.get("pages", []):
        for v in page.get("visuals", []):
            for m in v.get("measures_used", []):
                usage.setdefault(m, []).append({
                    "page": page.get("display_name") or page.get("name"),
                    "visual": v.get("title") or v.get("id"),
                    "visual_type": v.get("type"),
                })
    return usage