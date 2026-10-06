"""
Everything the UI does, minus the UI.

The Streamlit app is a view: it collects an upload, calls into here, and
renders what comes back. All the work — writing the upload somewhere the
extractor can open it, unwrapping a zipped .pbip, running extraction,
optionally explaining measures, and rendering the four downloadable
artifacts — lives in this module so it can be tested without a browser.

It also means the same entry point serves a future CLI, API or scheduled
job without reimplementing the sequence.
"""
from __future__ import annotations

import os
import tempfile
import zipfile
from dataclasses import dataclass, field

from .extractor import extract
from .index_builder import build_index
from .bridge import to_source_schema, harvest_measure_library
from .container import PbixError

SUPPORTED_SUFFIXES = (".pbit", ".pbix", ".pbip", ".zip")

TIER_MESSAGES = {
    "full_model": (
        "success",
        "Full model extracted",
        "Measures, relationships, columns, hierarchies and row-level security "
        "were all readable from this file.",
    ),
    "full_model_recovered": (
        "success",
        "Full model recovered from .pbix",
        "This file's compressed data model was decompiled locally via "
        "pbi-tools and Power BI Desktop's own engine — nothing left this "
        "machine. Measures, relationships, columns and RLS below are as "
        "complete as they would be from a .pbit.",
    ),
    "report_only": (
        "warning",
        "Report layer only — no semantic model",
        "This file's data model is stored in a compressed format that cannot be "
        "read outside Power BI, so measures, relationships and column metadata "
        "are unavailable. The report layer and Power Query lineage below are "
        "complete. To catalogue the model, save the report as a .pbit "
        "(File → Save as → Power BI template) or use a .pbip project.",
    ),
}


@dataclass
class Artifacts:
    """Everything produced for one input, ready to hand to a download."""
    stem: str
    catalogue: dict
    index_html: str
    catalogue_json: str
    source_schema_json: str | None = None
    measure_library_json: str | None = None
    excel_bytes: bytes | None = None
    pdf_bytes: bytes | None = None
    explain_stats: dict | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def tier(self) -> str:
        return self.catalogue["source"]["extraction_tier"]

    @property
    def model_available(self) -> bool:
        return bool(self.catalogue["model"]["available"])

    def tier_message(self) -> tuple[str, str, str]:
        return TIER_MESSAGES.get(
            self.tier,
            ("info", "Extraction complete", "See warnings below for coverage."),
        )

    def headline_stats(self) -> list[tuple[str, int]]:
        s = self.catalogue["statistics"]
        return [
            ("Tables", s["tables"]),
            ("Measures", s["measures"]),
            ("Relationships", s["relationships"]),
            ("Pages", s["pages"]),
            ("Visuals", s["visuals"]),
            ("M queries", s["m_queries"]),
        ]

    def findings_by_severity(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {"error": [], "warning": [], "info": []}
        for finding in self.catalogue.get("findings", []):
            grouped.setdefault(finding["severity"], []).append(finding)
        return grouped

    def downloads(self) -> list[tuple[str, str, bytes, str]]:
        """(label, filename, payload, mime) for each available artifact."""
        items: list[tuple[str, str, bytes, str]] = [
            ("Interactive HTML catalogue", f"{self.stem}.index.html",
             self.index_html.encode("utf-8"), "text/html"),
        ]
        if self.excel_bytes:
            items.append((
                "Excel data dictionary", f"{self.stem}.dictionary.xlsx",
                self.excel_bytes,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ))
        if self.pdf_bytes:
            items.append(("PDF client report", f"{self.stem}.report.pdf",
                          self.pdf_bytes, "application/pdf"))
        items.append(("Full catalogue (JSON)", f"{self.stem}.catalogue.json",
                      self.catalogue_json.encode("utf-8"), "application/json"))
        if self.source_schema_json:
            items.append(("Source schema (JSON)", f"{self.stem}.source_schema.json",
                          self.source_schema_json.encode("utf-8"), "application/json"))
        if self.measure_library_json:
            items.append(("Measure library (JSON)", f"{self.stem}.measure_library.json",
                          self.measure_library_json.encode("utf-8"), "application/json"))
        return items


def stage_upload(name: str, data: bytes, directory: str) -> str:
    """Write an uploaded file where the extractor can open it.

    A zipped .pbip is expanded, because the extractor wants a project
    tree and a browser can only hand over a file. Anything else is
    written through untouched.
    """
    safe = os.path.basename(name) or "upload"
    path = os.path.join(directory, safe)
    with open(path, "wb") as f:
        f.write(data)

    if safe.lower().endswith(".zip"):
        # A zip is either a zipped .pbip project or something unusable;
        # open_package detects which, so only expand when it is a project.
        from .pbip_reader import looks_like_pbip
        if looks_like_pbip(path):
            target = os.path.join(directory, "project")
            os.makedirs(target, exist_ok=True)
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # Guard against path traversal in a crafted archive.
                    rel = os.path.normpath(info.filename.replace("\\", "/"))
                    if rel.startswith("..") or os.path.isabs(rel):
                        continue
                    destination = os.path.join(target, rel)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    with open(destination, "wb") as out:
                        out.write(zf.read(info))
            return target
    return path


def _stem_for(name: str) -> str:
    stem = os.path.basename(name)
    for suffix in SUPPORTED_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem or "report"


def process(
    path: str,
    stem: str | None = None,
    explain: bool | None = None,
    llm_config=None,
    cache=None,
    want_excel: bool = True,
    want_pdf: bool = True,
    report_intent: str | None = None,
    progress=None,
) -> Artifacts:
    """Run the full pipeline for one input and return every artifact.

    Optional stages degrade rather than raise: if explanations fail or an
    export dependency is missing, the rest still comes back with the
    reason recorded in `errors`. Only the extraction itself is allowed to
    fail hard, because without it there is nothing to show.
    """
    import json

    from . import config as settings

    catalogue = extract(path)
    stem = stem or _stem_for(path)
    result = Artifacts(stem=stem, catalogue=catalogue, index_html="", catalogue_json="")

    # Explanations follow the deployment's configuration unless a caller
    # overrides explicitly, so the UI never has to decide.
    if explain is None:
        explain = settings.explanations_enabled()

    if explain and catalogue["model"]["available"]:
        try:
            from .enrich.llm_explainer import LLMConfig, explain_measures
            result.explain_stats = explain_measures(
                catalogue, llm_config or LLMConfig.from_env(), cache, progress=progress
            )
            for message in result.explain_stats.get("errors", []):
                result.errors.append(f"Explanations: {message}")
        except Exception as e:  # noqa: BLE001 — never lose the catalogue
            result.errors.append(f"Explanations failed: {type(e).__name__}: {e}")

    result.index_html = build_index(catalogue)
    result.catalogue_json = json.dumps(catalogue, indent=2)

    if catalogue["model"]["available"]:
        try:
            result.source_schema_json = json.dumps(
                to_source_schema(catalogue, report_intent), indent=2)
            result.measure_library_json = json.dumps(
                harvest_measure_library(catalogue), indent=2)
        except ValueError as e:
            result.errors.append(f"Bridge outputs unavailable: {e}")

    if want_excel:
        try:
            from .exports.excel_export import build_workbook_bytes
            result.excel_bytes = build_workbook_bytes(catalogue)
        except ImportError:
            result.errors.append(
                "Excel export needs openpyxl — install it with "
                "`pip install openpyxl`.")
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"Excel export failed: {type(e).__name__}: {e}")

    if want_pdf:
        try:
            from .exports.pdf_export import build_pdf_bytes
            result.pdf_bytes = build_pdf_bytes(catalogue)
        except ImportError:
            result.errors.append(
                "PDF export needs reportlab — install it with "
                "`pip install reportlab`.")
        except Exception as e:  # noqa: BLE001
            result.errors.append(f"PDF export failed: {type(e).__name__}: {e}")

    return result


def process_upload(name: str, data: bytes, **kwargs) -> Artifacts:
    """Stage an uploaded file into a temp directory, then process it."""
    with tempfile.TemporaryDirectory(prefix="pbix-extractor-") as directory:
        path = stage_upload(name, data, directory)
        return process(path, stem=_stem_for(name), **kwargs)


def validate_upload(name: str) -> str | None:
    """Return an error message if this file type cannot be handled."""
    lower = (name or "").lower()
    if not lower.endswith(SUPPORTED_SUFFIXES):
        return (
            f"'{name}' is not a supported file. Upload a .pbit, .pbix, or a "
            ".pbip project zipped up (.zip)."
        )
    return None


__all__ = [
    "Artifacts", "PbixError", "SUPPORTED_SUFFIXES",
    "process", "process_upload", "stage_upload", "validate_upload",
]