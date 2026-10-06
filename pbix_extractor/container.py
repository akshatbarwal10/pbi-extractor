"""
PBIX/PBIT container access.

A .pbix and a .pbit are both OPC (zip) packages. What differs is the
model part:

  .pbit  -> "DataModelSchema"  : plain TMSL JSON (UTF-16LE). Fully
                                 readable offline. This is the tier
                                 that yields DAX, relationships, RLS.
  .pbix  -> "DataModel"        : VertiPaq/Xpress9-compressed SSAS
                                 backup. Proprietary. NOT parseable
                                 with open tooling. Report layer and
                                 M queries are still readable.

Parts commonly present:
  Report/Layout         report definition (UTF-16LE JSON)
  DataMashup            nested zip holding Formulas/Section1.m
  Connections           data source connections (JSON)
  Metadata / Settings   authoring metadata
  DiagramLayout         model diagram positions
  Version               format version

Encoding note: Power BI writes most JSON parts as UTF-16LE, frequently
with a BOM, and Layout in particular often carries trailing NUL bytes.
Naive utf-8 decoding is the most common reason a hand-rolled PBIX
reader returns garbage, so decoding is centralised here.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from typing import Any


# Extraction tiers, in descending order of completeness.
TIER_FULL_MODEL = "full_model"        # TMSL available (pbit / decompiled)
TIER_FULL_MODEL_RECOVERED = "full_model_recovered"  # pbix, model recovered via pbi-tools
TIER_REPORT_ONLY = "report_only"      # pbix: report + mashup, no model
TIER_UNKNOWN = "unknown"


class PbixError(Exception):
    pass


def decode_part(raw: bytes) -> str:
    """Decode a Power BI package part to text.

    Tries UTF-16 (with BOM), then UTF-16LE, then UTF-8, and strips the
    trailing NULs that Layout parts frequently carry.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        # Heuristic: UTF-16LE ASCII text has NUL in every odd byte.
        sample = raw[:200]
        nul_ratio = sample.count(0) / max(len(sample), 1)
        if nul_ratio > 0.3:
            text = raw.decode("utf-16-le", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
    return text.replace("\x00", "").strip()


def load_json_part(raw: bytes) -> Any:
    text = decode_part(raw)
    if not text:
        raise PbixError("part decoded to empty text")
    # Some parts have trailing junk after the closing brace.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        end = max(text.rfind("}"), text.rfind("]"))
        if end == -1:
            raise PbixError("part is not JSON")
        return json.loads(text[: end + 1])


@dataclass
class PbixPackage:
    path: str
    parts: dict[str, bytes] = field(default_factory=dict)
    tier: str = TIER_UNKNOWN
    warnings: list[str] = field(default_factory=list)

    # A .pbip project is a folder tree, not an OPC package, so it has no
    # `parts`. Its reader pre-parses the model and report instead and
    # hands them over here, letting the rest of the pipeline stay
    # ignorant of which input format it is looking at.
    is_project: bool = False
    project_model: Any | None = None
    project_report: Any | None = None

    # Populated only for a .pbix whose model was recovered via a local
    # pbi-tools decompile (see pbix_converter.py). Independent of
    # is_project: the report/mashup layers here still come from this
    # package's own `parts` — pbix_converter never touches them, because
    # they were already readable without it.
    recovered_model: Any | None = None
    recovery_error: str | None = None

    # ---- part accessors -------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self.parts

    def raw(self, name: str) -> bytes | None:
        return self.parts.get(name)

    def json_part(self, name: str) -> Any | None:
        raw = self.parts.get(name)
        if raw is None:
            return None
        try:
            return load_json_part(raw)
        except (PbixError, json.JSONDecodeError) as e:
            self.warnings.append(f"Could not parse part '{name}': {e}")
            return None

    @property
    def layout(self) -> Any | None:
        if self.is_project:
            return self.project_report
        for name in ("Report/Layout", "Report\\Layout"):
            if self.has(name):
                return self.json_part(name)
        return None

    @property
    def data_model_schema(self) -> Any | None:
        """TMSL model definition.

        Present in .pbit, absent in .pbix, and synthesised from TMDL for
        a .pbip project — or, when pbi-tools recovered it, for a .pbix.
        """
        if self.recovered_model is not None:
            return self.recovered_model
        if self.is_project:
            return self.project_model
        for name in ("DataModelSchema", "DataModel/DataModelSchema"):
            if self.has(name):
                return self.json_part(name)
        return None


def open_package(path: str) -> PbixPackage:
    """Open a .pbix, .pbit or .pbip and classify what can be extracted.

    A .pbip project takes a different reader entirely (folder tree, not
    a package), so it is detected and delegated here — keeping one entry
    point for every supported input.
    """
    # Late import: pbip_reader depends on this module.
    from .pbip_reader import looks_like_pbip, open_pbip

    if looks_like_pbip(path):
        return open_pbip(path)

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise PbixError(
            f"'{path}' is not a valid PBIX/PBIT package (not a zip archive): {e}"
        ) from e

    pkg = PbixPackage(path=path)
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # Normalise separators so Windows-authored packages match.
            name = info.filename.replace("\\", "/")
            pkg.parts[name] = zf.read(info)

    if pkg.has("DataModelSchema"):
        pkg.tier = TIER_FULL_MODEL
    elif pkg.has("DataModel"):
        pkg.tier = TIER_REPORT_ONLY
        pkg.warnings.append(
            "This is a .pbix: the DataModel part is VertiPaq/Xpress9-compressed "
            "and cannot be read offline. Measures, relationships and column "
            "metadata are NOT available from this file. To extract the model, "
            "either save it as a .pbit (File > Save as > Power BI template), "
            "connect via XMLA/DMV, or decompile with pbi-tools."
        )

        # Optional: recover the model via a locally installed pbi-tools +
        # Power BI Desktop. A no-op everywhere neither exists — this must
        # never become a hard dependency for the common case.
        from . import pbix_converter
        if pbix_converter.available():
            try:
                pkg.recovered_model = pbix_converter.recover_model(path)
                pkg.tier = TIER_FULL_MODEL_RECOVERED
                pkg.warnings.append(
                    "Model recovered from the .pbix via a local pbi-tools "
                    "decompile (Power BI Desktop's own engine was used to read "
                    "the compressed DataModel part). Treat the model layer "
                    "below the same as one sourced from a .pbit."
                )
            except pbix_converter.ConversionError as e:
                pkg.recovery_error = str(e)
                pkg.warnings.append(
                    f"pbi-tools was available and model recovery was attempted, "
                    f"but failed: {e}"
                )
    else:
        pkg.warnings.append(
            "No DataModel or DataModelSchema part found — this may be a "
            "thin report (live connection / DirectQuery to a remote model)."
        )
        pkg.tier = TIER_REPORT_ONLY

    if not any(p.startswith("Report/Layout") for p in pkg.parts):
        pkg.warnings.append("No Report/Layout part found — report layer will be empty.")

    return pkg