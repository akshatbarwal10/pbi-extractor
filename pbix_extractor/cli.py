"""
CLI for the PBIX extractor.

    python -m pbix_extractor report.pbit --out output/
    python -m pbix_extractor report.pbix --out output/ --no-bridge
    python -m pbix_extractor *.pbit --out output/ --quiet

Writes per file:
    <name>.catalogue.json     full extraction (contract: catalogue.v1)
    <name>.index.html         self-contained browsable index page
    <name>.source_schema.json input for the generation accelerator
    <name>.measure_library.json  candidate harvested measure library
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from .extractor import extract
from .index_builder import build_index
from .bridge import to_source_schema, harvest_measure_library
from .container import PbixError


def _write(path: str, content: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def process(path: str, out_dir: str, bridge: bool, intent: str | None, quiet: bool,
            explain: bool = False, excel: bool = False, pdf: bool = False,
            llm_model: str | None = None, llm_base_url: str | None = None) -> dict:
    catalogue = extract(path)
    stem = os.path.splitext(os.path.basename(path.rstrip("/\\")))[0]
    written = []
    notes = []

    # Explanations run before anything is rendered, so the index page,
    # workbook and PDF all carry them.
    if explain:
        try:
            from .enrich.llm_explainer import LLMConfig, explain_measures
            overrides = {}
            if llm_model:
                overrides["model"] = llm_model
            if llm_base_url:
                overrides["base_url"] = llm_base_url
            stats = explain_measures(catalogue, LLMConfig.from_env(**overrides))
            notes.append(
                f"explanations: {stats['explained']} generated, "
                f"{stats['cached']} cached, {stats['failed']} failed"
            )
            notes += [f"! {e}" for e in stats.get("errors", [])]
        except Exception as e:  # noqa: BLE001 — never lose the catalogue
            notes.append(f"! explanations failed: {type(e).__name__}: {e}")

    cat_path = os.path.join(out_dir, f"{stem}.catalogue.json")
    _write(cat_path, json.dumps(catalogue, indent=2))
    written.append(cat_path)

    idx_path = os.path.join(out_dir, f"{stem}.index.html")
    _write(idx_path, build_index(catalogue))
    written.append(idx_path)

    if bridge and catalogue["model"]["available"]:
        schema_path = os.path.join(out_dir, f"{stem}.source_schema.json")
        _write(schema_path, json.dumps(to_source_schema(catalogue, intent), indent=2))
        written.append(schema_path)

        lib_path = os.path.join(out_dir, f"{stem}.measure_library.json")
        _write(lib_path, json.dumps(harvest_measure_library(catalogue), indent=2))
        written.append(lib_path)

    if excel:
        try:
            from .exports.excel_export import build_workbook
            xlsx_path = os.path.join(out_dir, f"{stem}.dictionary.xlsx")
            os.makedirs(out_dir or ".", exist_ok=True)
            build_workbook(catalogue, xlsx_path)
            written.append(xlsx_path)
        except ImportError:
            notes.append("! Excel export needs openpyxl (pip install openpyxl)")
        except Exception as e:  # noqa: BLE001
            notes.append(f"! Excel export failed: {type(e).__name__}: {e}")

    if pdf:
        try:
            from .exports.pdf_export import build_pdf
            pdf_path = os.path.join(out_dir, f"{stem}.report.pdf")
            os.makedirs(out_dir or ".", exist_ok=True)
            build_pdf(catalogue, pdf_path)
            written.append(pdf_path)
        except ImportError:
            notes.append("! PDF export needs reportlab (pip install reportlab)")
        except Exception as e:  # noqa: BLE001
            notes.append(f"! PDF export failed: {type(e).__name__}: {e}")

    if not quiet:
        s = catalogue["statistics"]
        print(f"\n{os.path.basename(path)}  [{catalogue['source']['extraction_tier']}]")
        print(f"  {s['tables']} tables · {s['columns']} columns · {s['measures']} measures "
              f"· {s['relationships']} relationships")
        print(f"  {s['pages']} pages · {s['visuals']} visuals · {s['m_queries']} M queries "
              f"· {s['rls_roles']} RLS roles")
        for w in catalogue["source"]["warnings"]:
            print(f"  ! {w}")
        errors = [f for f in catalogue["findings"] if f["severity"] == "error"]
        warnings = [f for f in catalogue["findings"] if f["severity"] == "warning"]
        if errors or warnings:
            print(f"  findings: {len(errors)} error(s), {len(warnings)} warning(s)")
            for f in errors + warnings:
                print(f"    [{f['severity']}] {f['message']}")
        for n in notes:
            print(f"  {n}")
        for w in written:
            print(f"  -> {w}")

    return catalogue


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="pbix_extractor",
        description="Extract metadata, DAX, schema, relationships and report "
                    "business logic from Power BI .pbix/.pbit files.",
    )
    ap.add_argument("files", nargs="+", help=".pbix / .pbit paths (globs allowed)")
    ap.add_argument("--out", default="output", help="output directory (default: output)")
    ap.add_argument("--no-bridge", action="store_true",
                    help="skip source_schema.json and measure_library.json")
    ap.add_argument("--intent", default=None,
                    help="report intent text to embed in the generated source schema")
    ap.add_argument("--quiet", action="store_true")

    ap.add_argument("--excel", action="store_true",
                    help="also write an Excel data dictionary (needs openpyxl)")
    ap.add_argument("--pdf", action="store_true",
                    help="also write a client-ready PDF report (needs reportlab)")

    llm = ap.add_argument_group("plain-language explanations")
    llm.add_argument("--explain", action="store_true",
                     help="explain each measure with an LLM. Sends measure names, "
                          "table/column names and DAX to the configured endpoint.")
    llm.add_argument("--llm-model", default=None,
                     help="model name (env: PBIX_LLM_MODEL)")
    llm.add_argument("--llm-base-url", default=None,
                     help="OpenAI-compatible base URL, e.g. "
                          "http://localhost:11434/v1 for a local runtime "
                          "(env: PBIX_LLM_BASE_URL). API key: PBIX_LLM_API_KEY")

    args = ap.parse_args(argv)

    paths = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        paths.extend(matched if matched else [pattern])

    if not paths:
        print("No input files matched.", file=sys.stderr)
        return 2

    failures = 0
    for path in paths:
        # A .pbip project is a directory, so `isfile` is not the test.
        if not os.path.exists(path):
            print(f"! Not found: {path}", file=sys.stderr)
            failures += 1
            continue
        try:
            process(path, args.out, not args.no_bridge, args.intent, args.quiet,
                    explain=args.explain, excel=args.excel, pdf=args.pdf,
                    llm_model=args.llm_model, llm_base_url=args.llm_base_url)
        except PbixError as e:
            print(f"! {os.path.basename(path)}: {e}", file=sys.stderr)
            failures += 1
        except Exception as e:  # noqa: BLE001 — report and continue the batch
            print(f"! {os.path.basename(path)}: unexpected {type(e).__name__}: {e}",
                  file=sys.stderr)
            failures += 1

    if not args.quiet:
        print(f"\nProcessed {len(paths) - failures}/{len(paths)} file(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())