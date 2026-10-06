"""
Recovering a .pbix's compressed model via a local pbi-tools + Power BI
Desktop install.

container.py explains why a .pbix's `DataModel` part cannot be read
offline: it is a VertiPaq/Xpress9-compressed backup of a live Analysis
Services Tabular database, and nothing except Microsoft's own AS engine
can decode it. That has not changed. What this module does is trade
that constraint for a real, explicit, optional dependency: if
`pbi-tools` is on this machine (and, transitively, Power BI Desktop —
pbi-tools uses Desktop's own bundled AS engine components to do the
decoding), we can shell out to it and read back what it decompiles.

This is NOT a replacement for the "no dependencies, no network, no
Power BI install" core — it is an opt-in escape hatch that no-ops
cleanly everywhere that machine doesn't exist, which today means:
Streamlit Community Cloud, any Linux/macOS host, and any Windows host
without Desktop installed. `available()` is cheap to call and safe to
call unconditionally; callers should always be prepared for it to
return False.

Design choice worth calling out: this only ever recovers the MODEL.
A .pbix's Report/Layout and DataMashup parts are already fully
readable natively (that's what `report_only` tier already gives you),
so there is no need to run pbi-tools' report/mashup output through
anything — only its decompiled `.tmdl` files are used, via the exact
same `tmdl_parser.build_tmsl()` a native .pbip project goes through.
Everything downstream (model_extractor, dax_analyzer, findings,
bridge, exports) stays unaware this ever happened.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from .config import get, get_int, FALSEY
from .tmdl_parser import build_tmsl, TmdlError

DEFAULT_TIMEOUT = 300  # a large model's local AS engine round-trip can take minutes


class ConversionError(Exception):
    """Recovery was attempted and failed.

    Always caught by the caller (container.open_package) and turned
    into a warning, never a hard failure — a .pbix that can't be
    recovered still has its report layer, and that must still come
    back.
    """


def _pbi_tools_path() -> str | None:
    """Locate the pbi-tools executable, or None if it isn't available.

    `PBIX_PBITOOLS_PATH` (env or .env, via config.py — same precedence
    as everything else in this project) takes priority for a custom
    install location; otherwise fall back to PATH.
    """
    override = get("PBIX_PBITOOLS_PATH")
    if override:
        return override if os.path.isfile(override) else None
    return shutil.which("pbi-tools") or shutil.which("pbi-tools.exe")


def available() -> bool:
    """Cheap, side-effect-free check for whether recovery is worth attempting.

    `PBIX_PBITOOLS_ENABLED=0` is the explicit opt-out for a deployment
    that has pbi-tools on PATH but doesn't want this behaviour (e.g. a
    shared box where decompiling should stay a manual, reviewed step).
    """
    if str(get("PBIX_PBITOOLS_ENABLED", "1")).strip().lower() in FALSEY:
        return False
    return _pbi_tools_path() is not None


def _collect_tmdl(root: str) -> dict[str, str]:
    """Every .tmdl file under `root`, keyed by its path relative to root.

    Walking the tree rather than assuming a fixed layout (e.g.
    `Model/tables/*.tmdl`) is deliberate: pbi-tools' own folder
    convention has shifted across versions, and `build_tmsl` does not
    care about paths, only about having every table/relationship/role
    document present.
    """
    documents: dict[str, str] = {}
    for dirpath, _dirs, filenames in os.walk(root):
        for filename in filenames:
            if filename.lower().endswith(".tmdl"):
                full = os.path.join(dirpath, filename)
                rel = os.path.relpath(full, root).replace("\\", "/")
                with open(full, "r", encoding="utf-8-sig") as f:
                    documents[rel] = f.read()
    return documents


def recover_model(pbix_path: str, timeout: int | None = None) -> dict:
    """Decompile `pbix_path` with pbi-tools and return a TMSL model dict.

    Raises ConversionError on any failure: tool missing, non-zero exit,
    timeout, or no .tmdl output (most likely an old pbi-tools defaulting
    to the legacy PbixProj JSON serialization rather than TMDL). The
    caller — container.open_package — decides what to do with that;
    this function never touches report or Power Query parts.
    """
    exe = _pbi_tools_path()
    if not exe:
        raise ConversionError(
            "pbi-tools was not found. Install it from "
            "https://github.com/pbi-tools/pbi-tools/releases (unblock the "
            "zip before extracting — Windows flags downloaded zips), then "
            "either add it to PATH or set PBIX_PBITOOLS_PATH to the full "
            "path of pbi-tools.exe."
        )

    timeout = timeout if timeout is not None else get_int("PBIX_PBITOOLS_TIMEOUT", DEFAULT_TIMEOUT)

    with tempfile.TemporaryDirectory(prefix="pbix-recover-") as out_dir:
        cmd = [exe, "extract", pbix_path,
               "-extractFolder", out_dir,
               "-modelSerialization", "Tmdl"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError as e:
            raise ConversionError(f"Could not run pbi-tools at '{exe}': {e}") from e
        except subprocess.TimeoutExpired as e:
            raise ConversionError(
                f"pbi-tools did not finish within {timeout}s. If this keeps "
                "happening, a stuck local AS engine process may need "
                "clearing (`pbi-tools cache clearAll`), or raise "
                "PBIX_PBITOOLS_TIMEOUT for a genuinely large model."
            ) from e

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ConversionError(
                f"pbi-tools exited with code {result.returncode}: {detail[-800:]}"
            )

        documents = _collect_tmdl(out_dir)
        if not documents:
            raise ConversionError(
                "pbi-tools ran successfully but produced no .tmdl files. This "
                "build may default to the legacy PbixProj JSON serialization "
                "rather than TMDL — try upgrading pbi-tools (1.0.0-rc.3 or "
                "later defaults to TMDL)."
            )

        try:
            return build_tmsl(documents)
        except TmdlError as e:
            raise ConversionError(f"Recovered TMDL but could not parse it: {e}") from e
