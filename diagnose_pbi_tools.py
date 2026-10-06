"""
Standalone check for the pbi-tools .pbix recovery path.

Run this BEFORE trusting the full CLI/Streamlit pipeline on a real file
— it exercises pbix_converter.py in isolation, so a failure points
directly at the layer it's in (pbi-tools/PATH, the folder shape
pbi-tools actually produced, or the TMDL parser) rather than getting
buried behind the rest of the extraction pipeline.

Usage, from the project root (the folder containing `pbix_extractor/`):

    python diagnose_pbi_tools.py path\\to\\some_report.pbix

Prints, in order: whether pbi-tools was found at all and where, the
result of actually running it, how many .tmdl files came back, the
resulting table/measure/relationship counts, and finally whether
model_extractor accepts the result cleanly — the same thing the real
pipeline does with it.
"""
from __future__ import annotations

import sys

from pbix_extractor import pbix_converter
from pbix_extractor.model_extractor import extract_model


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python diagnose_pbi_tools.py <report.pbix>")
        return 2

    path = sys.argv[1]

    print("--- Step 1: is pbi-tools visible to Python? ---")
    exe = pbix_converter._pbi_tools_path()
    print(f"available(): {pbix_converter.available()}")
    print(f"resolved exe: {exe or '(not found)'}")
    if not exe:
        print(
            "\nNothing further to test — pbi-tools isn't on PATH and "
            "PBIX_PBITOOLS_PATH isn't set (or points somewhere that isn't "
            "a file). Run `pbi-tools info` in a terminal first to confirm "
            "the tool itself works and can see your Power BI Desktop "
            "install; if that works but this script still can't find it, "
            "the issue is PATH visibility from this Python process "
            "specifically (see note below)."
        )
        return 1

    print("\n--- Step 2: running pbi-tools extract (this can take a while) ---")
    try:
        tmsl = pbix_converter.recover_model(path)
    except pbix_converter.ConversionError as e:
        print(f"FAILED: {e}")
        print(
            "\nIf the message above says 'no .tmdl files', run the same "
            "command manually with a permanent output folder so you can "
            "inspect what pbi-tools actually wrote:\n\n"
            f'    "{exe}" extract "{path}" -extractFolder .\\pbi-debug-out '
            "-modelSerialization Tmdl\n\n"
            "then look for .tmdl files anywhere under pbi-debug-out\\ — if "
            "they exist but under a path this script didn't find, that's a "
            "bug in _collect_tmdl to report back, not a pbi-tools problem."
        )
        return 1

    print("recover_model() returned a TMSL dict without error.")

    print("\n--- Step 3: shape of what came back ---")
    tables = tmsl.get("model", {}).get("tables", [])
    total_measures = sum(len(t.get("measures", [])) for t in tables)
    total_relationships = len(tmsl.get("model", {}).get("relationships", []))
    print(f"tables: {len(tables)}")
    print(f"measures: {total_measures}")
    print(f"relationships: {total_relationships}")
    for t in tables[:5]:
        print(f"  - {t['name']}: {len(t.get('columns', []))} columns, "
              f"{len(t.get('measures', []))} measures")
    if len(tables) > 5:
        print(f"  ... and {len(tables) - 5} more")

    if not tables:
        print(
            "\nZero tables came back — pbi-tools ran and produced .tmdl "
            "files, but tmdl_parser.build_tmsl() didn't find any `table` "
            "nodes in them. Worth pasting one of the .tmdl files here to "
            "check the parser handles this pbi-tools version's exact "
            "formatting."
        )
        return 1

    print("\n--- Step 4: does model_extractor accept it, same as the real pipeline? ---")
    model = extract_model(tmsl)
    print(f"available: {model['available']}")
    print(f"measures parsed: {len(model['measures'])}")
    if model["measures"]:
        sample = model["measures"][0]
        print(f"sample measure: {sample['name']} = {sample['expression'][:80]!r}")

    print(
        "\nAll good — this .pbix should come through the full CLI/Streamlit "
        "pipeline at extraction_tier 'full_model_recovered'."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
