"""
Power Query (M) extraction from the DataMashup part.

DataMashup is itself a container: a small binary header followed by an
embedded zip whose Formulas/Section1.m holds the M script for every
query in the file. Parsing it gives the physical-source lineage the
model alone does not state — which server, database and object each
table actually came from, and what transformation steps were applied.

The binary header length varies by Power BI version, so rather than
assume an offset, this scans for the PK zip signature.
"""
from __future__ import annotations

import io
import re
import zipfile

from .container import decode_part

# shared expression  Name = let ... in ...;
QUERY_BLOCK = re.compile(
    r"shared\s+(?:#\"([^\"]+)\"|([A-Za-z_]\w*))\s*=\s*(.*?);\s*(?=shared\s|\Z)",
    re.DOTALL,
)
STEP = re.compile(r"^\s*(?:#\"([^\"]+)\"|([A-Za-z_]\w*))\s*=\s*(.+?),?\s*$", re.MULTILINE)

SOURCE_FUNCS = {
    "Sql.Database": "SQL Server",
    "Sql.Databases": "SQL Server",
    "Snowflake.Databases": "Snowflake",
    "PostgreSQL.Database": "PostgreSQL",
    "Oracle.Database": "Oracle",
    "MySQL.Database": "MySQL",
    "Excel.Workbook": "Excel",
    "Csv.Document": "CSV",
    "Web.Contents": "Web/API",
    "SharePoint.Files": "SharePoint",
    "AzureStorage.Blobs": "Azure Blob",
    "Databricks.Catalogs": "Databricks",
    "Lakehouse.Contents": "Fabric Lakehouse",
    "PowerPlatform.Dataflows": "Dataflow",
    "Odbc.DataSource": "ODBC",
    "Json.Document": "JSON",
}


def _find_embedded_zip(raw: bytes) -> bytes | None:
    idx = raw.find(b"PK\x03\x04")
    return raw[idx:] if idx != -1 else None


def _analyze_query(name: str, body: str) -> tuple[dict, list[dict]]:
    """Analyse one M query body. Returns (query_entry, source_entries).

    Shared by both input routes: the DataMashup part in a .pbix/.pbit,
    and TMDL partition expressions in a .pbip, which carry the same M
    but arrive without any container around them.
    """
    body = (body or "").strip()

    steps = []
    for s in STEP.finditer(body):
        step_name = s.group(1) or s.group(2)
        if step_name and step_name.lower() not in ("let", "in"):
            steps.append(step_name)

    detected, sources = [], []
    for func, label in SOURCE_FUNCS.items():
        if func in body:
            detected.append(label)
            server = re.search(re.escape(func) + r'\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?', body)
            sources.append({
                "query": name,
                "connector": label,
                "server": server.group(1) if server else None,
                "database": server.group(2) if server and server.lastindex and server.lastindex >= 2 else None,
            })

    native = re.search(r'Value\.NativeQuery\s*\([^,]+,\s*"((?:[^"\\]|\\.)*)"', body, re.DOTALL)
    native_sql = native.group(1).replace('\\"', '"') if native else None

    # Native SQL is also commonly passed through the *options record* of
    # a connector call (the "Advanced options -> SQL statement" box)
    # rather than Value.NativeQuery. Missing that would leave a large
    # class of hand-written SQL out of the lineage entirely.
    if not native_sql:
        option = re.search(r'\[\s*Query\s*=\s*"((?:[^"\\]|\\.)*)"', body, re.DOTALL)
        if option:
            native_sql = option.group(1).replace('\\"', '"')

    entry = {
        "name": name,
        "connectors": sorted(set(detected)),
        "step_count": len(steps),
        "steps": steps,
        "native_query": native_sql,
        "is_parameter": body.startswith("#\"") and "meta [IsParameterQuery" in body,
        "expression": body if len(body) < 8000 else body[:8000] + "\n/* truncated */",
    }
    return entry, sources


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    seen, uniq = set(), []
    for s in sources:
        key = (s["connector"], s["server"], s["database"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq


def extract_from_model(model: dict) -> dict:
    """Build the M catalogue from TMSL/TMDL partitions.

    A .pbip has no DataMashup part — its M lives in each table's
    partition source, plus any shared `expressions` (parameters and
    staged queries). Reading it from there keeps the Power Query section
    populated for projects instead of silently empty.
    """
    queries, sources = [], []

    for table in model.get("tables", []):
        for partition in table.get("partitions", []):
            # Accept both the extracted shape (expression hoisted) and
            # raw TMSL (expression nested under `source`).
            body = partition.get("expression") or ""
            if not body:
                body = (partition.get("source") or {}).get("expression") or ""
            if not body.strip():
                continue
            entry, found = _analyze_query(partition.get("name") or table.get("name"), body)
            entry["table"] = table.get("name")
            entry["mode"] = partition.get("mode")
            queries.append(entry)
            sources.extend(found)

    for shared in model.get("expressions", []) or []:
        body = shared.get("expression") or ""
        if not body.strip():
            continue
        entry, found = _analyze_query(shared.get("name"), body)
        entry["is_shared_expression"] = True
        queries.append(entry)
        sources.extend(found)

    if not queries:
        return {"available": False, "queries": [], "sources": []}

    return {
        "available": True,
        "queries": queries,
        "sources": _dedupe_sources(sources),
        "origin": "model_partitions",
    }


def extract_mashup(raw: bytes | None) -> dict:
    """Return the M query catalogue from a DataMashup part."""
    if not raw:
        return {"available": False, "queries": [], "sources": []}

    inner = _find_embedded_zip(raw)
    if inner is None:
        return {"available": False, "queries": [], "sources": [],
                "warning": "DataMashup present but no embedded package found."}

    section_text = None
    try:
        with zipfile.ZipFile(io.BytesIO(inner)) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/").endswith("Section1.m"):
                    section_text = decode_part(zf.read(name))
                    break
    except zipfile.BadZipFile:
        return {"available": False, "queries": [], "sources": [],
                "warning": "DataMashup embedded package is not readable."}

    if not section_text:
        return {"available": False, "queries": [], "sources": [],
                "warning": "No Section1.m found inside DataMashup."}

    queries, sources = [], []
    for m in QUERY_BLOCK.finditer(section_text):
        name = m.group(1) or m.group(2)
        entry, found = _analyze_query(name, m.group(3))
        queries.append(entry)
        sources.extend(found)

    return {"available": True, "queries": queries, "sources": _dedupe_sources(sources)}