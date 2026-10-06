"""
PBIX Extractor — drag-and-drop UI.

    pip install -r requirements.txt
    streamlit run app.py

Deliberately a thin view. Everything it does lives in
`pbix_extractor.ui_service`, so the sequence can be tested, scripted and
scheduled without a browser, and this file stays readable.

Measure explanations are configured in `.env` (see `.env.example`), not
here: which model to call is an operator decision, and the people using
this just want explanations to arrive with their results.
"""
from __future__ import annotations

from html import escape

import streamlit as st

from pbix_extractor import config, pbix_converter, ui_service
from pbix_extractor.container import PbixError

st.set_page_config(
    page_title="PBIX Extractor",
    page_icon="◱",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACCENT = "#1F3864"

st.markdown(f"""
<style>
  .block-container {{ padding-top: 2.2rem; max-width: 1180px; }}
  .stat-card {{
    background: rgba(128,128,128,0.06); border: 1px solid rgba(128,128,128,0.22);
    border-radius: 10px; padding: 14px 16px; text-align: center;
  }}
  .stat-num {{ font-size: 26px; font-weight: 700; color: {ACCENT}; line-height: 1.1; }}
  .stat-lbl {{ font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase;
               opacity: .68; margin-top: 2px; }}
  .dax {{ font-family: ui-monospace, Consolas, monospace; font-size: 12px;
          background: rgba(128,128,128,0.10); padding: 9px 11px;
          border-radius: 6px; white-space: pre-wrap; word-break: break-word; }}
  div[data-testid="stFileUploaderDropzone"] {{ padding: 1.6rem 1rem; }}
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------------
# Sidebar — outputs only. Everything else is configuration, not a choice.
# ---------------------------------------------------------------------

with st.sidebar:
    st.markdown("### Outputs")
    want_excel = st.checkbox("Excel data dictionary", value=True)
    want_pdf = st.checkbox("PDF client report", value=True)

    st.markdown("---")
    st.markdown("### Accepted files")
    if pbix_converter.available():
        st.markdown(
            "- **`.pbit`** — full model\n"
            "- **`.pbip`** (zip the project folder) — full model\n"
            "- **`.pbix`** — full model, recovered locally via pbi-tools"
        )
        st.caption(
            "This deployment has pbi-tools + Power BI Desktop available, so a "
            "`.pbix`'s compressed data model is decompiled locally before "
            "extraction. Nothing leaves this machine to do it."
        )
    else:
        st.markdown(
            "- **`.pbit`** — full model\n"
            "- **`.pbip`** (zip the project folder) — full model\n"
            "- **`.pbix`** — report layer only"
        )
        st.caption(
            "A `.pbix` stores its data model in a compressed format that cannot "
            "be read outside Power BI, so no tool can recover measures from one "
            "offline on this deployment. Save as `.pbit` or use a `.pbip` "
            "project for full coverage."
        )

    # Explanations are an operator setting, read from `.env` — reported
    # here so it is clear whether they are on, never editable in the browser.
    st.markdown("---")
    st.caption(f"Measure explanations: {config.describe()}")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

st.title("PBIX Extractor")
st.caption(
    "Model, DAX business logic, report-wise usage and Power Query lineage "
    "from a Power BI file — extraction only, nothing is deployed or written back."
)

uploads = st.file_uploader(
    "Drop a .pbit, .pbix, or a zipped .pbip project",
    type=["pbit", "pbix", "pbip", "zip"],
    accept_multiple_files=True,
)


def render_stats(artifacts):
    stats = artifacts.headline_stats()
    for column, (label, value) in zip(st.columns(len(stats)), stats):
        column.markdown(
            f'<div class="stat-card"><div class="stat-num">{escape(str(value))}</div>'
            f'<div class="stat-lbl">{escape(label)}</div></div>',
            unsafe_allow_html=True,
        )


def render_findings(artifacts):
    grouped = artifacts.findings_by_severity()
    if not sum(len(v) for v in grouped.values()):
        st.success("No findings raised.")
        return

    st.markdown(
        f"**{len(grouped['error'])} error · {len(grouped['warning'])} warning · "
        f"{len(grouped['info'])} info**"
    )
    render = {"error": st.error, "warning": st.warning, "info": st.info}
    for severity in ("error", "warning", "info"):
        for finding in grouped[severity]:
            body = f"**{finding.get('category', '')}** — {finding['message']}"
            detail = finding.get("detail") or []
            if detail:
                shown = detail[:8]
                body += "\n\n" + "\n".join(f"- `{d}`" for d in shown)
                if len(detail) > len(shown):
                    body += f"\n- … and {len(detail) - len(shown)} more"
            render[severity](body)


def render_measures(artifacts, slot: int):
    measures = artifacts.catalogue["model"]["measures"]
    if not measures:
        return
    ordered = sorted(measures, key=lambda m: (-m.get("usage_count", 0), m["name"]))

    # Keys are scoped by upload position, not by file name: uploading two
    # files with the same name would otherwise collide and crash.
    query = st.text_input("Filter measures", "", key=f"measure-filter-{slot}")
    if query:
        needle = query.lower()
        ordered = [
            m for m in ordered
            if needle in m["name"].lower()
            or needle in (m.get("expression") or "").lower()
            or needle in (m.get("explanation", {}).get("summary") or "").lower()
        ]
        st.caption(f"{len(ordered)} of {len(measures)} measures match.")

    for measure in ordered[:60]:
        usage = measure.get("usage_count", 0)
        badge = f"used {usage}×" if usage else "unused"
        with st.expander(f"{measure['name']}  ·  {badge}", expanded=False):
            explanation = measure.get("explanation") or {}
            if explanation.get("summary"):
                st.markdown(f"**{explanation['summary']}**")
                if explanation.get("detail"):
                    st.write(explanation["detail"])
                for caveat in explanation.get("caveats") or []:
                    st.warning(caveat, icon="⚠")
                missed = explanation.get("dependencies_missed_by_static_analysis")
                if missed:
                    st.info(
                        "Static analysis did not resolve these references "
                        f"(held in VAR): {', '.join(missed)}",
                        icon="🔎",
                    )

            # DAX comes from an uploaded file, so it must be escaped before
            # going anywhere near unsafe_allow_html — a measure containing
            # `<` would otherwise break the page or inject markup.
            st.markdown(
                f'<div class="dax">{escape(measure.get("expression") or "")}</div>',
                unsafe_allow_html=True,
            )

            analysis = measure.get("analysis", {})
            meta = [
                f"Table: `{measure.get('table')}`",
                f"Complexity: {analysis.get('complexity', '—')}",
                f"Confidence: {analysis.get('confidence', '—')}",
            ]
            if analysis.get("patterns"):
                meta.append("Patterns: " + ", ".join(analysis["patterns"]))
            st.caption(" · ".join(meta))

            places = measure.get("used_in") or []
            if places:
                st.caption("Used on: " + "; ".join(
                    f"{p.get('page')} / {p.get('visual')}" for p in places[:6]))

    if len(ordered) > 60:
        st.caption(
            f"Showing 60 of {len(ordered)}. Download the HTML or Excel "
            "catalogue for the full list."
        )


def render_report(artifacts):
    pages = artifacts.catalogue["report"]["pages"]
    if not pages:
        st.info("No report layer was found in this file.")
        return
    for page in pages:
        name = page.get("display_name") or page.get("name")
        hidden = " · hidden" if page.get("visibility") == 1 else ""
        st.markdown(f"**{name}**{hidden} — {page.get('visual_count', 0)} visuals")

        filters = [f.get("field") for f in page.get("page_filters", []) if f.get("field")]
        if filters:
            st.caption("Page filters: " + ", ".join(filters))

        rows = [
            {
                "Visual": v.get("title") or v.get("id"),
                "Type": v.get("type"),
                "Measures": ", ".join(v.get("measures_used") or []),
                "Columns": ", ".join(v.get("columns_used") or []),
                "Implicit": ", ".join(v.get("implicit_measures") or []),
            }
            for v in page.get("visuals", [])
        ]
        if rows:
            st.dataframe(rows, hide_index=True)


def render_sources(artifacts):
    """Power Query lineage and RLS — the physical side of the model.

    This replaced an inline preview of the HTML catalogue, which tried to
    embed a full self-contained document in an iframe. That fought the
    host page and only duplicated a download; this shows data that is
    otherwise not visible in the UI at all.
    """
    power_query = artifacts.catalogue.get("power_query") or {}
    queries = power_query.get("queries") or []

    if queries:
        origin = power_query.get("origin")
        if origin == "model_partitions":
            st.caption(
                "Read from the model's partition sources — a .pbip project "
                "has no DataMashup part."
            )
        sources = {s["query"]: s for s in power_query.get("sources", [])}
        st.dataframe(
            [
                {
                    "Query": q.get("name"),
                    "Connector": ", ".join(q.get("connectors") or []) or "—",
                    "Server": sources.get(q["name"], {}).get("server") or "—",
                    "Database": sources.get(q["name"], {}).get("database") or "—",
                    "Steps": q.get("step_count", 0),
                }
                for q in queries
            ],
            hide_index=True,
        )

        native = [(q["name"], q["native_query"]) for q in queries if q.get("native_query")]
        if native:
            st.markdown("**Native SQL**")
            for name, sql in native:
                st.caption(name)
                st.code(sql, language="sql")
    else:
        st.info("No Power Query definitions were found in this file.")

    roles = artifacts.catalogue["model"].get("roles") or []
    if roles:
        st.markdown("**Row-level security**")
        for role in roles:
            st.markdown(
                f"`{role['name']}` — {role.get('model_permission') or 'read'}")
            for permission in role.get("table_permissions", []):
                st.caption(permission.get("table", ""))
                st.code(permission.get("filter_expression", ""), language="text")


def render_downloads(artifacts, slot: int):
    items = artifacts.downloads()
    for row_start in range(0, len(items), 3):
        row = items[row_start:row_start + 3]
        for column, (label, filename, payload, mime) in zip(st.columns(3), row):
            column.download_button(
                label, data=payload, file_name=filename, mime=mime,
                use_container_width=True,
                key=f"download-{slot}-{row_start}-{label}",
            )


def render(artifacts, slot: int):
    level, headline, detail = artifacts.tier_message()
    {"success": st.success, "warning": st.warning, "info": st.info}[level](
        f"**{headline}** — {detail}"
    )

    for warning in artifacts.catalogue["source"].get("warnings", []):
        st.caption(f"⚠ {warning}")
    for error in artifacts.errors:
        st.error(error)

    stats = artifacts.explain_stats
    if stats:
        line = (
            f"Explanations: {stats.get('explained', 0)} generated, "
            f"{stats.get('cached', 0)} from cache, {stats.get('failed', 0)} failed"
        )
        if stats.get("model"):
            line += f" · {stats['model']}"
        st.caption(line)

    st.markdown("### ")
    render_stats(artifacts)

    st.markdown("### Download")
    render_downloads(artifacts, slot)

    findings_tab, measures_tab, report_tab, sources_tab = st.tabs(
        ["Findings", "Measures", "Report", "Data sources"]
    )
    with findings_tab:
        render_findings(artifacts)
    with measures_tab:
        if artifacts.model_available:
            render_measures(artifacts, slot)
        else:
            st.info(
                "No measures available — this file is a .pbix. Save the report "
                "as a .pbit, or use a .pbip project, to catalogue the model."
            )
    with report_tab:
        render_report(artifacts)
    with sources_tab:
        render_sources(artifacts)


if not uploads:
    st.info(
        "Drop a file above to begin. Extraction runs locally on this machine — "
        "nothing is uploaded to a third party."
        + (" Measure explanations are generated by the configured model."
           if config.explanations_enabled() else "")
    )
else:
    explain = config.explanations_enabled()

    for slot, upload in enumerate(uploads):
        problem = ui_service.validate_upload(upload.name)
        if problem:
            st.error(problem)
            continue

        st.markdown(f"## {upload.name}")
        status = st.status(f"Extracting {upload.name}…", expanded=True)

        def report_progress(batch, total, names, _status=status):
            _status.write(f"Explaining measures — batch {batch} of {total}")

        try:
            with status:
                artifacts = ui_service.process_upload(
                    upload.name, upload.getvalue(),
                    explain=explain,
                    want_excel=want_excel, want_pdf=want_pdf,
                    progress=report_progress if explain else None,
                )
            status.update(label=f"{upload.name} — done", state="complete",
                          expanded=False)
        except PbixError as e:
            status.update(label=f"{upload.name} — failed", state="error")
            st.error(str(e))
            continue
        except Exception as e:  # noqa: BLE001
            status.update(label=f"{upload.name} — failed", state="error")
            st.error(f"Unexpected {type(e).__name__}: {e}")
            continue

        render(artifacts, slot)
        st.divider()