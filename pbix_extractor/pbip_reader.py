"""
.pbip (Power BI Project) reader.

A .pbip is not a package — it is a folder tree of plain text, which is
exactly why it is the best input this tool can receive:

    MyReport.pbip                      pointer file (small JSON)
    MyReport.SemanticModel/
        definition/                    TMDL flavour (current default)
            model.tmdl
            relationships.tmdl
            tables/Sales.tmdl
            roles/Manager.tmdl
        model.bim                      TMSL flavour (older projects)
    MyReport.Report/
        report.json                    legacy report definition
        definition/                    PBIR flavour (current default)
            pages/pages.json
            pages/<id>/page.json
            pages/<id>/visuals/<id>/visual.json

Both model flavours and both report flavours are handled, and both are
normalised into the shapes the existing extractors already consume:

    TMDL  -> TMSL dict      (model_extractor.extract_model unchanged)
    PBIR  -> Layout dict    (report_extractor.extract_report unchanged)

So .pbip support costs two translators, not a second pipeline.

Input can be a directory, a .pbip pointer file, or a .zip of the
project — the last being what a browser upload realistically provides,
since a drop zone receives files rather than folders.
"""
from __future__ import annotations

import io
import json
import os
import zipfile

from .container import PbixError, PbixPackage, TIER_FULL_MODEL, TIER_REPORT_ONLY, decode_part
from .tmdl_parser import build_tmsl, TmdlError

PBIP_MARKERS = (".semanticmodel/", ".report/", ".pbip")


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _read_tree(root: str) -> dict[str, bytes]:
    """Walk a directory into {relative_path: bytes}."""
    files: dict[str, bytes] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = _norm(os.path.relpath(full, root))
            try:
                with open(full, "rb") as f:
                    files[rel] = f.read()
            except OSError:
                continue
    return files


def _read_zip(path: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            files[_norm(info.filename)] = zf.read(info)
    return files


def looks_like_pbip(path: str) -> bool:
    """True when this path is a .pbip project rather than a package."""
    if os.path.isdir(path):
        return True
    lower = path.lower()
    if lower.endswith(".pbip"):
        return True
    if lower.endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = [_norm(n).lower() for n in zf.namelist()[:400]]
        except zipfile.BadZipFile:
            return False
        return any(any(marker in n for marker in PBIP_MARKERS) for n in names)
    return False


def _strip_common_prefix(files: dict[str, bytes]) -> dict[str, bytes]:
    """Drop a single wrapping folder, which zipping a project adds."""
    if not files:
        return files
    tops = {name.split("/", 1)[0] for name in files if "/" in name}
    if len(tops) != 1:
        return files
    top = tops.pop()
    # Only strip when the marker folders sit *inside* it.
    inner = [n[len(top) + 1:] for n in files if n.startswith(top + "/")]
    if not any(any(m in ("/" + n.lower()) for m in PBIP_MARKERS) for n in inner):
        return files
    return {
        n[len(top) + 1:]: data
        for n, data in files.items()
        if n.startswith(top + "/")
    }


# ---------------------------------------------------------------------
# Model: TMDL or model.bim -> TMSL
# ---------------------------------------------------------------------

def _load_model(files: dict[str, bytes], warnings: list[str]) -> dict | None:
    # model.bim is already TMSL — nothing to translate.
    for name, data in files.items():
        if name.lower().endswith("model.bim"):
            try:
                return json.loads(decode_part(data))
            except (json.JSONDecodeError, ValueError) as e:
                warnings.append(f"Found model.bim but could not parse it: {e}")

    # TMDL: every .tmdl under a SemanticModel definition folder.
    documents: dict[str, str] = {}
    for name, data in files.items():
        lower = name.lower()
        if not lower.endswith(".tmdl"):
            continue
        if ".semanticmodel/" not in lower and "/definition/" not in lower \
                and not lower.startswith("definition/"):
            continue
        try:
            documents[name] = decode_part(data)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Could not decode '{name}': {e}")

    if not documents:
        return None

    try:
        tmsl = build_tmsl(documents)
    except TmdlError as e:
        warnings.append(
            f"TMDL parsing failed, so the model layer is unavailable: {e}"
        )
        return None

    if not tmsl["model"]["tables"]:
        warnings.append(
            f"Parsed {len(documents)} TMDL file(s) but found no tables — the "
            "project layout may differ from the expected "
            "'<name>.SemanticModel/definition/' structure."
        )
        return None
    return tmsl


# ---------------------------------------------------------------------
# Report: PBIR folder -> Layout dict
# ---------------------------------------------------------------------

def _pbir_filters(filter_config) -> list:
    """PBIR `filterConfig` -> the legacy `filters` list shape."""
    if isinstance(filter_config, str):
        try:
            filter_config = json.loads(filter_config)
        except json.JSONDecodeError:
            return []
    if not isinstance(filter_config, dict):
        return []
    out = []
    for f in filter_config.get("filters", []) or []:
        if not isinstance(f, dict):
            continue
        # PBIR names it `field`; the legacy shape calls it `expression`.
        out.append({
            "name": f.get("name"),
            "type": f.get("type"),
            "expression": f.get("field") or f.get("expression") or {},
            "filter": f.get("filter"),
            "isLockedInViewMode": f.get("isLockedInViewMode", False),
            "isHiddenInViewMode": f.get("isHiddenInViewMode", False),
        })
    return out


def _load_pbir_report(files: dict[str, bytes], warnings: list[str]) -> dict | None:
    """Assemble PBIR page/visual files into one Layout-shaped dict."""
    page_files = {
        name: data for name, data in files.items()
        if name.lower().endswith("/page.json") and "/pages/" in name.lower()
    }
    if not page_files:
        return None

    def _json(data: bytes):
        try:
            return json.loads(decode_part(data))
        except (json.JSONDecodeError, ValueError):
            return None

    # Page order comes from pages.json when present.
    order: list[str] = []
    for name, data in files.items():
        if name.lower().endswith("/pages.json"):
            meta = _json(data) or {}
            order = [p for p in (meta.get("pageOrder") or []) if isinstance(p, str)]
            break

    sections = []
    for page_path, page_data in sorted(page_files.items()):
        page = _json(page_data)
        if not isinstance(page, dict):
            warnings.append(f"Could not parse page definition '{page_path}'.")
            continue

        page_dir = page_path.rsplit("/", 1)[0]
        page_name = page.get("name") or page_dir.rsplit("/", 1)[-1]

        containers = []
        prefix = page_dir + "/visuals/"
        for vpath, vdata in sorted(files.items()):
            if not vpath.startswith(prefix) or not vpath.lower().endswith("/visual.json"):
                continue
            visual = _json(vdata)
            if not isinstance(visual, dict):
                warnings.append(f"Could not parse visual definition '{vpath}'.")
                continue

            spec = visual.get("visual") or {}
            position = visual.get("position") or {}
            containers.append({
                "id": visual.get("name"),
                "x": position.get("x"), "y": position.get("y"),
                "z": position.get("z"),
                "width": position.get("width"), "height": position.get("height"),
                "tabOrder": position.get("tabOrder"),
                # Hand `extract_report` the shape it already understands.
                "config": {
                    "name": visual.get("name"),
                    "singleVisual": {
                        "visualType": spec.get("visualType"),
                        "objects": spec.get("objects") or {},
                        "vcObjects": spec.get("visualContainerObjects") or {},
                    },
                },
                "query": spec.get("query"),
                "dataTransforms": spec.get("columnProperties"),
                "filters": _pbir_filters(visual.get("filterConfig")),
            })

        visibility = page.get("visibility")
        sections.append({
            "name": page_name,
            "displayName": page.get("displayName") or page.get("name"),
            "ordinal": (order.index(page_name) if page_name in order
                        else page.get("ordinal")),
            "width": page.get("width"),
            "height": page.get("height"),
            "config": {
                # Legacy encodes hidden as visibility == 1.
                "visibility": 1 if visibility == "HiddenInViewMode" else 0
            },
            "filters": [],
            "_pbir_page_filters": _pbir_filters(page.get("filterConfig")),
            "visualContainers": containers,
        })

    # Page-level filters use the same key the legacy parser reads.
    for section in sections:
        section["filters"] = section.pop("_pbir_page_filters", [])

    report_filters = []
    for name, data in files.items():
        if name.lower().endswith("/report.json") and "/definition/" in name.lower():
            meta = _json(data) or {}
            report_filters = _pbir_filters(meta.get("filterConfig"))
            break

    return {"sections": sections, "filters": report_filters, "config": {}}


def _load_legacy_report(files: dict[str, bytes]) -> dict | None:
    """Older .pbip projects keep a single report.json in Layout shape."""
    for name, data in files.items():
        lower = name.lower()
        if lower.endswith("report.json") and "/definition/" not in lower:
            try:
                parsed = json.loads(decode_part(data))
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict) and "sections" in parsed:
                return parsed
    return None


# ---------------------------------------------------------------------

def open_pbip(path: str) -> PbixPackage:
    """Open a .pbip project and return it in `PbixPackage` clothing.

    The returned package carries pre-parsed model and report documents
    rather than raw parts, so the rest of the pipeline treats a project
    exactly like a template file.
    """
    if os.path.isdir(path):
        files = _read_tree(path)
    elif path.lower().endswith(".zip"):
        try:
            files = _strip_common_prefix(_read_zip(path))
        except zipfile.BadZipFile as e:
            raise PbixError(f"'{path}' is not a readable zip: {e}") from e
    elif path.lower().endswith(".pbip"):
        # Pointer file — the real content sits in sibling folders.
        parent = os.path.dirname(os.path.abspath(path)) or "."
        files = _read_tree(parent)
    else:
        raise PbixError(f"'{path}' does not look like a .pbip project.")

    if not files:
        raise PbixError(f"No files found in '{path}'.")

    pkg = PbixPackage(path=path)
    pkg.is_project = True

    tmsl = _load_model(files, pkg.warnings)
    report = _load_pbir_report(files, pkg.warnings) or _load_legacy_report(files)

    pkg.project_model = tmsl
    pkg.project_report = report

    if tmsl:
        pkg.tier = TIER_FULL_MODEL
    else:
        pkg.tier = TIER_REPORT_ONLY
        pkg.warnings.append(
            "No semantic model found in this project. Expected either "
            "'<name>.SemanticModel/definition/*.tmdl' or "
            "'<name>.SemanticModel/model.bim'. If you zipped the project, "
            "make sure the .SemanticModel folder is included."
        )

    if not report:
        pkg.warnings.append(
            "No report definition found — report-layer analysis "
            "(which measures are actually used on a page) is unavailable."
        )

    return pkg