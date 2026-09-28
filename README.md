# PBIX Extractor

Extracts metadata, DAX and its business logic, table schemas,
relationships, RLS, Power Query lineage, and **report-wise business
logic** from Power BI `.pbix` / `.pbit` / `.pbip` files. Produces a JSON
catalogue, a browsable HTML index page, an Excel data dictionary, a
client-ready PDF, and inputs for the generation accelerator.

Extraction only — nothing is generated, deployed, or written back.

The extraction core is pure standard library: no dependencies, no
network, no Power BI install, no SSAS. The UI and the export formats add
optional dependencies; the core works without any of them.

```bash
# UI
pip install -r requirements.txt
streamlit run app.py

# CLI
python -m pbix_extractor report.pbit --out output/
python -m pbix_extractor "reports/*.pbix" --out output/ --excel --pdf
python -m pbix_extractor project.pbip --out output/ --explain
```

## The UI

`streamlit run app.py` gives a drop zone for `.pbit`, `.pbix` and
zipped `.pbip` projects, several files at a time. It shows the
extraction tier up front rather than behind a spinner, renders findings,
measures and report structure, and offers every artifact as a download.

Everything it does lives in `pbix_extractor/ui_service.py` rather than in
the Streamlit file, so the same sequence is testable, scriptable and
schedulable without a browser.

## Read this first: `.pbix` cannot give up its data model offline

This is the single constraint that shapes everything below.

| Source | Model storage | What you get |
|---|---|---|
| `.pbip` | TMDL text files, or `model.bim` | **Everything**, from plain text. The best input this tool can take |
| `.pbit` | `DataModelSchema` — TMSL JSON | **Everything**: tables, columns, DAX measures, relationships, RLS, hierarchies |
| `.pbix` | `DataModel` — VertiPaq/Xpress9 compressed | Report layer + Power Query only. **No measures, no relationships.** |

The `.pbix` model part is a proprietary-compressed SSAS backup. No open
tooling reads it, and no agent or model can decode it — it is a format
problem, not an intelligence problem. The extractor does not pretend
otherwise: it reports `extraction_tier: report_only`, states why in
`source.warnings`, and names the three ways to get the model anyway:

1. **Save as `.pbit`** (File → Save as → Power BI template) — easiest, offline
2. **XMLA / DMV** against a Premium workspace or a running Desktop instance
3. **`pbi-tools` decompile** — produces a readable model folder

Anything claiming to read measures straight out of a `.pbix` offline is
either wrong or shelling out to one of those three.

### `.pbip` support

A `.pbip` is a folder of plain text, so it needs no unpacking trick.
Both layouts are handled and both are translated into the shapes the
existing extractors already consume, which is why project support cost
two translators rather than a second pipeline:

| Project part | Formats handled | Translated to |
|---|---|---|
| Semantic model | TMDL (`definition/**/*.tmdl`) or `model.bim` | TMSL — `model_extractor` unchanged |
| Report | PBIR (`definition/pages/**`) or legacy `report.json` | Layout — `report_extractor` unchanged |

Power Query comes from the model's partition sources, since a project
has no `DataMashup` part. Input may be the project folder, the `.pbip`
pointer file, or a **zip of the project** — the last being what a
browser upload can actually provide.

## Outputs

Per input file, into `--out`:

| File | Contents |
|---|---|
| `<name>.catalogue.json` | Everything extracted. Contract: `catalogue.v1` |
| `<name>.index.html` | Self-contained searchable index page |
| `<name>.source_schema.json` | Input for the generation accelerator |
| `<name>.measure_library.json` | Candidate measure library harvested from DAX in active use |
| `<name>.dictionary.xlsx` | Excel data dictionary, with `--excel` |
| `<name>.report.pdf` | Client-ready PDF report, with `--pdf` |

The last two are skipped with `--no-bridge`, and are not produced at
all when the model layer is unavailable — a schema built from nothing
would be worse than no schema.

## What it extracts

**Model** — tables, columns (data type, calculated vs. stored, hidden,
format, sort-by), measures with full DAX, relationships (cardinality,
cross-filter direction, active/inactive), hierarchies, calculation
groups, RLS roles with filter expressions, storage mode per partition.

**DAX business logic** — for every measure, calculated column and RLS
filter: referenced measures/columns/tables, functions used, a pattern
classification (time intelligence, ratio, ranking, filtered
aggregation, iterator, role-playing, parent-child, conditional),
complexity, and a measure-to-measure dependency graph with cycle
detection.

**Report-wise business logic** — pages, visuals, and *which measures
and columns each visual actually binds*, plus report/page/visual-level
filters, bookmarks, theme, hidden pages. This is what the model alone
cannot tell you: not "200 measures exist" but "these 34 are on a page
someone looks at, and these 166 are not".

**Power Query** — M expression per query, transformation steps,
connector type, server/database, and any `Value.NativeQuery` SQL. This
is the physical-source lineage.

## Findings

Cross-layer checks reported in the catalogue and index page:

- Circular measure references
- Measures bound to visuals that don't exist in the model
- Measures used by nothing (excluding dependencies of used measures)
- Bidirectional relationships
- Role-playing dimensions (multiple relationships between two tables,
  with active/inactive columns named)
- Implicit aggregations — raw columns summarised in a visual, i.e.
  business logic living outside the model
- Measures whose DAX could not be fully analysed lexically

## Feeding the generation accelerator

`source_schema.json` matches the shape the generation pipeline already
consumes, so an existing report can seed a regeneration run:

```bash
python -m pbix_extractor legacy.pbit --out out/
cp out/legacy.source_schema.json ../pbi-agentic-accelerator/samples/source_schema.json
cd ../pbi-agentic-accelerator && python orchestrator/pipeline.py
```

Relationships become `declared_foreign_keys`, which the modeling agent
trusts over column-name guessing. Inactive relationships keep their
flag, so role-playing dimensions survive the round trip.

`measure_library.json` is the more valuable direction: rather than
inventing a house DAX library, mine it from measures developers already
wrote and shipped. Only measures actually bound to a visual (plus their
dependencies) are harvested — an unused measure is not evidence of a
standard.

It is emitted as a **candidate**, never merged into `standards.yaml`
automatically. Table names are replaced with `{fact_table}` /
`{date_table}` heuristically and need checking, and promoting a measure
into org standards is a governance decision with an owner, not a side
effect of running a tool.

## Plain-language explanations

`--explain` (CLI) or the sidebar toggle (UI) attaches a business-language
explanation to every measure.

What makes these worth having is not the model doing the writing — it is
the context this tool can hand it, which a model-only catalogue cannot:

- **the dependency chain's DAX**, so `[Margin %]` is not explained blind
  against an unknown `[Total Sales]`
- **where the measure is actually used** — "Executive Summary / Margin %
  card". A page name is a business-context signal, and it is the
  difference between "divides one measure by another" and "the
  exec-level profitability KPI on the summary page"
- the format string, which silently confirms intent (`0.0%` means the
  author meant a ratio), plus display folder and author description

Explanations are produced in **dependency order** — base measures first,
their summaries then passed as context for the measures built on them.
Batching never spans a dependency level, so a dependency is always
explained by an earlier request than its dependant.

Each explanation carries a `summary`, a `detail`, `caveats`, and the
dependencies the model resolved. Where static analysis was unsure
(`confidence: medium` because of `VAR`), anything the model resolved
that the lexer missed is recorded as
`dependencies_missed_by_static_analysis` — a review queue rather than a
silent overwrite of the regex result.

**Caching** is keyed on the DAX itself, not the measure name, so
renaming does not invalidate and editing does. Re-running an unchanged
report costs nothing.

**Transport** is any OpenAI-compatible `/chat/completions` endpoint,
spoken over stdlib `urllib` — no client library. One shape covers
OpenAI, Azure OpenAI, Groq, OpenRouter, Together, and a local Ollama or
LM Studio; only `base_url` changes.

```bash
export PBIX_LLM_BASE_URL=https://api.openai.com/v1
export PBIX_LLM_MODEL=gpt-4o-mini
export PBIX_LLM_API_KEY=sk-...
python -m pbix_extractor report.pbit --out out/ --explain

# nothing leaves the machine
python -m pbix_extractor report.pbit --out out/ --explain \
  --llm-base-url http://localhost:11434/v1 --llm-model llama3.1
```

On sending model metadata to a third party: this is **off unless asked
for**, and when it runs it sends measure names, table and column names,
and DAX expressions. For client work where that is not acceptable, point
`base_url` at a local runtime; nothing else changes. There is
deliberately no "redaction mode", because stripping table names while
still shipping the DAX would offer reassurance rather than protection.

An endpoint failure is never fatal: the measure keeps its static
analysis and gains no explanation, and the reason is reported. A
catalogue that refuses to build because an API was down would be worse
than one with a few blanks.

## Client-ready exports

Three outputs, three audiences.

**`--excel`** — a data dictionary for whoever has to *review* the model.
One sheet per object type, every sheet frozen and filterable, and
annotation columns (`Reviewed by`, `Status`, `Reviewer notes`) shaded
amber so it is obvious which cells are for typing in. A legend on the
Overview sheet says so explicitly. Counts on that sheet are formulas
bound to the real extent of each sheet, so they stay honest if a
reviewer deletes rows.

**`--pdf`** — the document that gets attached to an email. Leads with
findings and plain-language meaning, keeps DAX subordinate to the
explanation, and closes with a **methodology appendix**: what tier the
extraction ran at, that the DAX analysis is lexical, which measures were
therefore reported at reduced confidence, and that the explanations are
a review aid rather than authoritative documentation. This document
travels further than the person who produced it, so it states its own
limits.

**The HTML index** — unchanged in purpose: fast, searchable, for
whoever ran the extraction. Explanations, when present, lead each
measure entry and are searchable, so a business term finds the measure
even when it appears nowhere in the DAX.

## Analysis limits, stated honestly

The DAX analyser is **lexical, not a parser**. It tokenises rather than
building a grammar, because the useful signal — what does this depend
on, what kind of calculation is it — is recoverable from tokens, and a
full DAX grammar is a large piece of work for modest extra value.

Consequences, reported per measure in `analysis.confidence`:

- `high` — direct references, reliable
- `medium` — uses `VAR`; references held in variables aren't resolved
- `low` — dynamic evaluation present; dependency list may be incomplete

The analyser strips comments, string literals and object references
before scanning for functions. That last one matters: a measure named
`Net Revenue (base)` would otherwise read as a call to `REVENUE()`.
There is a test for exactly this.

## Tests

```bash
python tests/test_extractor.py     # 85 checks, no dependencies
python tests/test_extensions.py    # 82 checks: TMDL, .pbip, explanations,
                                   # service layer, exports
```

Export checks skip themselves when openpyxl or reportlab is absent
rather than failing, since both are optional.

Fixtures are generated, not committed, and reproduce the format's real
awkwardness: UTF-16LE with BOM, JSON-inside-JSON strings, DataMashup as
a binary header wrapping a zip, a parenthesised measure name, a
role-playing dimension with an inactive relationship, a circular
measure pair, an implicit aggregation, and a visual bound to a
non-existent measure.

## Layout

```
app.py                 Streamlit UI (thin view over ui_service)
requirements.txt       UI + export dependencies only
pbix_extractor/
  container.py         OPC access, UTF-16LE decoding, tier detection
  pbip_reader.py       .pbip folder/zip -> model + report documents
  tmdl_parser.py       TMDL -> TMSL, so model_extractor is unchanged
  model_extractor.py   TMSL -> tables, measures, relationships, RLS
  dax_analyzer.py      DAX -> dependencies, patterns, dependency graph
  report_extractor.py  Layout/PBIR -> pages, visuals, bindings, filters
  mashup_extractor.py  DataMashup or partitions -> M queries, lineage
  extractor.py         Orchestration + cross-layer findings
  bridge.py            -> source_schema.json, measure_library.json
  index_builder.py     -> self-contained index.html
  ui_service.py        Upload -> every artifact (no Streamlit import)
  cli.py               Batch CLI
  enrich/
    llm_explainer.py   Plain-language explanations (stdlib urllib)
  exports/
    excel_export.py    Excel data dictionary (openpyxl)
    pdf_export.py      Client-ready PDF report (reportlab)
fixtures/
  build_fixtures.py    Generates realistic .pbit / .pbix test files
tests/
  test_extractor.py    85 checks, no deps
  test_extensions.py   82 checks for TMDL, .pbip, explanations, exports
```