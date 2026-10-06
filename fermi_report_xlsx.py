"""Live Excel report for the Fermi PDF organizer.

Rebuilt after every run from the run's counters + a live disk scan of the output
folder, so the workbook always shows the current status of the tree.
Sheets: Dashboard, Missing, CHK, Scanned, Watermark, Orphans, Superseded,
Roots, USED ON check, Files (live tree), Run History. Run History survives
rebuilds via a sidecar JSON.

openpyxl is optional; when it is missing the workbook is skipped gracefully.
"""
import json
import os
import shutil
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    _OPENPYXL_OK = True
except ImportError:
    _OPENPYXL_OK = False

from fermi_organizer.config import canonical_stem  # noqa: E402
from fermi_organizer.fsops import scan_output_tree  # noqa: E402

# Colors (RGB hex strings)
DARK = "1F3864"        # header dark blue
NAVY = "2E5AAC"        # accent blue for hyperlinks
RED_F, RED_D = "FFC7CE", "9C0006"      # missing: light red fill, dark red text
AMB_F, AMB_D = "FFEB9C", "9C6500"      # CHK: amber
ORG_F, ORG_D = "FCE4D6", "974706"      # orphans: orange
GREY_F, GREY_D = "E7E6E6", "3F3F3F"    # superseded
GRN_F, GRN_D = "C6EFCE", "276221"      # active/ok: green
STRIPE = "F2F7FB"
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

# Run-history cap: single source for both the sidecar save and the sheet render
# (the two must agree or the saved file and the displayed table drift).
HISTORY_MAX_RUNS = 200


def _hdr(ws, headers, widths):
    """Header row 1 + freeze panes + widths + no gridlines."""
    ws.sheet_view.showGridLines = False
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True, color="FFFFFF", size=11)
        c.fill = PatternFill("solid", fgColor=DARK)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def _fill_row(ws, row, ncols, fill, font_color):
    for col in range(1, ncols + 1):
        c = ws.cell(row=row, column=col)
        c.fill = PatternFill("solid", fgColor=fill)
        c.font = Font(color=font_color)
        c.border = BORDER


def _scan_tree(output):
    """Live disk scan of the output folder -> {'tree','sup','orph'} path lists."""
    return scan_output_tree(output)


def _hist_path(xlsx_path):
    return Path(str(xlsx_path) + ".history.json")


def _load_history(xlsx_path, log=None):
    hist = _hist_path(xlsx_path)
    if not hist.exists():
        # A sidecar that was never written is normal, not a warning.
        return []
    try:
        data = json.loads(hist.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("run history is not a list")
        return data
    except Exception as e:
        (log or print)(f"WARNING: run history not loaded ({hist}): {e}")
        if hist.exists():
            try:
                shutil.copy2(hist, str(hist) + ".corrupt")
            except OSError:
                pass
        return []


def _save_history(xlsx_path, rows, log=None):
    hist = _hist_path(xlsx_path)
    tmp = hist.with_name(hist.name + ".tmp")
    try:
        tmp.write_text(json.dumps(rows[-HISTORY_MAX_RUNS:]), encoding="utf-8")
        os.replace(tmp, hist)
    except OSError as e:
        (log or print)(f"WARNING: run history not saved: {e}")


def _resolve_names(names_raw, live):
    """names_raw + base-stem fallback, for every tree/orphan stem and every key."""
    resolved = {}
    live_stems = [canonical_stem(q.stem) for q in live["tree"] + live["orph"]]
    for stem in set(list(names_raw) + [s for s in live_stems if s]):
        v = names_raw.get(stem)
        if not v:
            v = names_raw.get(stem.split("_")[0], "")
        if v:
            resolved[stem] = v
    return resolved


def build_workbook(path, ctx, log=None):
    """Build/refresh the live workbook at `path`; returns it (None without openpyxl).

    Context keys read (15): output, run_mode, run_time,
    counters{scanned,roots,copies,cycles,warnings}, missing[(stem,val)...],
    chk[stem...], orphans[stem...], roots[(stem,[used_on])...],
    mismatches[(parent,child,actual)...], used_on_bugs[(parent,child)...],
    titleblock_mismatches[(stem,field,filename,tb)...],
    scanned[(stem,[pages])...], watermarks[(stem,evidence)...],
    report_txt, names{stem/val: NAME}.
    cli.py maps the run-context key used_on_mismatches -> mismatches and
    used_on_bugs -> used_on_bugs when it calls refresh_from_run.
    """
    if not _OPENPYXL_OK:
        return None
    path = Path(path)
    wb = Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    missing = wb.create_sheet("Missing")
    chk = wb.create_sheet("CHK")
    scanned_ws = wb.create_sheet("Scanned")
    watermark_ws = wb.create_sheet("Watermark")
    orph = wb.create_sheet("Orphans")
    superseded = wb.create_sheet("Superseded")
    roots_ws = wb.create_sheet("Roots")
    mism_ws = wb.create_sheet("USED ON check")
    tb_ws = wb.create_sheet("Title block check")
    files = wb.create_sheet("Files")
    hist = wb.create_sheet("Run History")

    counters = ctx.get("counters", {})
    live = _scan_tree(ctx["output"])
    missing_pairs = sorted(set(ctx.get("missing", [])))
    names = _resolve_names(ctx.get("names", {}) or {}, live)

    # ---------------- Dashboard ----------------
    _dash_sheet(dash, ctx, counters, live, missing_pairs)

    # ---------------- Missing ----------------
    _missing_sheet(missing, missing_pairs, names)

    # ---------------- CHK ----------------
    _chk_sheet(chk, ctx, names)

    # ---------------- Scanned (image-only PDFs) ----------------
    _scanned_sheet(scanned_ws, ctx, names)

    # ---------------- Watermark ----------------
    _watermark_sheet(watermark_ws, ctx, names)

    # ---------------- Orphans ----------------
    _orphans_sheet(orph, ctx, names)

    # ---------------- Superseded ----------------
    _superseded_sheet(superseded, live)

    # ---------------- Roots (from the last run's log) ----------------
    _roots_sheet(roots_ws, ctx, names)

    # ---------------- USED ON check (BOM vs USED ON consistency) ----------------
    _mismatch_sheet(mism_ws, ctx, names)

    # ---------------- Title block check (filename vs drawing title block) ------
    _titleblock_sheet(tb_ws, ctx, names)

    # ---------------- Files (live tree) ----------------
    _files_sheet(files, ctx, names, live)

    # ---------------- Run History ----------------
    _history_sheet(hist, ctx, counters, missing_pairs, path, log)

    wb.save(path)
    return path


def _dash_sheet(dash, ctx, counters, live, missing_pairs):
    dash.sheet_view.showGridLines = False
    dash["B2"] = "Fermi PDF Organizer - LIVE STATUS"
    dash["B2"].font = Font(bold=True, size=16, color=DARK)
    dash["B3"] = f"Last run: {ctx.get('run_time', '')}   [{ctx.get('run_mode', '')}]"
    dash["B3"].font = Font(color=GREY_D, size=11)

    cards = [
        ("PDF FILES IN TREE (LIVE)", len(live["tree"]), GRN_F, GRN_D),
        ("ROOTS (LAST RUN)", counters.get("roots", "-"), GRN_F, GRN_D),
        ("MISSING - DOWNLOAD FROM TEAMCENTER", len(missing_pairs), RED_F, RED_D),
        ("CHK UNAPPROVED", len(set(ctx.get("chk", []))), AMB_F, AMB_D),
        ("ORPHANS (PARKED ON FULL RUN)", len(live["orph"]), ORG_F, ORG_D),
        ("SUPERSEDED REVISIONS (LIVE)", len(live["sup"]), GREY_F, GREY_D),
        ("EXTRACTION WARNINGS (LAST RUN)", counters.get("warnings", "-"), AMB_F, AMB_D),
        ("CYCLES BROKEN (LAST RUN)", counters.get("cycles", 0), GRN_F, GRN_D),
    ]
    for i, (label, val, f, fc) in enumerate(cards):
        row = 5 + (i // 2) * 3
        col = "B" if i % 2 == 0 else "F"
        dash[f"{col}{row}"] = label
        dash[f"{col}{row}"].font = Font(color=GREY_D, size=10)
        v = dash[f"{col}{row + 1}"]
        v.value = val
        v.font = Font(bold=True, size=18, color=fc)
        v.fill = PatternFill("solid", fgColor=f)
        v.alignment = Alignment(horizontal="center", vertical="center")
        dash.merge_cells(f"{col}{row + 1}:{'E' if i % 2 == 0 else 'H'}{row + 1}")
        dash.row_dimensions[row + 1].height = 26
    for col, w in (("A", 2), ("B", 15), ("C", 6), ("D", 6), ("E", 6),
                   ("F", 15), ("G", 6), ("H", 6), ("I", 6), ("J", 6), ("K", 6)):
        dash.column_dimensions[col].width = w
    navrow = 5 + 4 * 3 + 1
    dash[f"B{navrow}"] = "Open a sheet:"
    dash[f"B{navrow}"].font = Font(bold=True, color=DARK)
    for j, name in enumerate(("Missing", "CHK", "Scanned", "Watermark", "Orphans",
                              "Superseded", "USED ON check", "Title block check",
                              "Files", "Run History"),
                             start=1):
        c = dash.cell(row=navrow, column=2 + j, value=name)
        c.hyperlink = f"#'{name}'!A1"
        c.font = Font(color=NAVY, bold=True, underline="single")
    dash[f"B{navrow + 2}"] = "Rebuilt after every run - always the live status."
    dash[f"B{navrow + 2}"].font = Font(italic=True, color=GREY_D)


def _missing_sheet(missing, missing_pairs, names):
    _hdr(missing, ["Missing FERMI#", "Name", "Referenced by drawing", "Action"],
         (22, 38, 30, 42))
    for r, (stem, val) in enumerate(missing_pairs, start=2):
        missing.cell(row=r, column=1, value=val)
        missing.cell(row=r, column=2, value=names.get(val, "") or "-")
        missing.cell(row=r, column=3, value=stem)
        missing.cell(row=r, column=4, value="Download approved DWG from Teamcenter")
        _fill_row(missing, r, 4, RED_F, RED_D)
    if missing_pairs:
        missing.auto_filter.ref = f"A1:D{len(missing_pairs) + 1}"


def _chk_sheet(chk, ctx, names):
    _hdr(chk, ["Stem (current file)", "Name", "Status"], (30, 38, 52))
    for r, s in enumerate(sorted(set(ctx.get("chk", []))), start=2):
        chk.cell(row=r, column=1, value=s)
        chk.cell(row=r, column=2, value=names.get(s, "") or "-")
        chk.cell(row=r, column=3, value="Unapproved - replace with approved DWG from Teamcenter")
        _fill_row(chk, r, 3, AMB_F, AMB_D)
    tip = len(set(ctx.get("chk", []))) + 3
    chk.cell(row=tip, column=1,
             value="When the approved DWG arrives in an input folder it supersedes the CHK automatically.").font = Font(italic=True, color=GREY_D)


def _scanned_sheet(scanned_ws, ctx, names):
    scanned_data = ctx.get("scanned", [])  # [(stem, [pages]), ...]
    _hdr(scanned_ws, ["Stem (scanned PDF)", "Name", "OCR page(s)"], (30, 44, 18))
    for r, (stem, pages) in enumerate(sorted(scanned_data), start=2):
        if isinstance(pages, (list, tuple)):
            pages_txt = ", ".join(str(p) for p in pages)
        else:
            pages_txt = str(pages)
        scanned_ws.cell(row=r, column=1, value=stem)
        scanned_ws.cell(row=r, column=2, value=names.get(stem, "") or "-")
        scanned_ws.cell(row=r, column=3, value=pages_txt)
        _fill_row(scanned_ws, r, 3, ORG_F, ORG_D)
    if scanned_data:
        scanned_ws.auto_filter.ref = f"A1:C{len(scanned_data) + 1}"
    scanned_ws.cell(row=len(scanned_data) + 3, column=1,
                    value="Image-only (scanned) PDFs whose text was read via OCR.").font = Font(italic=True, color=GREY_D)


def _watermark_sheet(watermark_ws, ctx, names):
    wm_data = ctx.get("watermarks", [])  # [(stem, evidence), ...]
    _hdr(watermark_ws, ["Stem (watermarked PDF)", "Name", "Evidence"], (30, 40, 52))
    for r, (stem, evidence) in enumerate(sorted(wm_data), start=2):
        watermark_ws.cell(row=r, column=1, value=stem)
        watermark_ws.cell(row=r, column=2, value=names.get(stem, "") or "-")
        watermark_ws.cell(row=r, column=3, value=evidence or "-")
        _fill_row(watermark_ws, r, 3, AMB_F, AMB_D)
    if wm_data:
        watermark_ws.auto_filter.ref = f"A1:C{len(wm_data) + 1}"
    watermark_ws.cell(row=len(wm_data) + 3, column=1,
                      value="Best-effort detection: watermark layer, transparent/diagonal/light large text, or watermark keyword.").font = Font(italic=True, color=GREY_D)


def _orphans_sheet(orph, ctx, names):
    _hdr(orph, ["Stem (current file)", "Name", "Note"], (30, 38, 46))
    for r, s in enumerate(sorted(ctx.get("orphans", [])), start=2):
        orph.cell(row=r, column=1, value=s)
        orph.cell(row=r, column=2, value=names.get(s, "") or "-")
        orph.cell(row=r, column=3, value="Parent drawing not seen so far - adopted into tree once it arrives")
        _fill_row(orph, r, 3, ORG_F, ORG_D)
    tip = len(ctx.get("orphans", [])) + 3
    orph.cell(row=tip, column=1,
              value="Orphans are stored under _orphans/ so a future incremental run can adopt them when their parent appears.").font = Font(italic=True, color=GREY_D)


def _superseded_sheet(superseded, live):
    _hdr(superseded, ["File in _superseded/", "Base", "Note"], (44, 18, 42))
    for i, p in enumerate(sorted(live["sup"], key=lambda q: q.name.lower()), start=2):
        superseded.cell(row=i, column=1, value=p.name)
        stem = canonical_stem(p.stem) or p.stem.upper()
        superseded.cell(row=i, column=2, value=stem.split("_")[0])
        superseded.cell(row=i, column=3, value="older revision / unapproved CHK / superseded in place")
        _fill_row(superseded, i, 3, GREY_F, GREY_D)


def _roots_sheet(roots_ws, ctx, names):
    roots_data = ctx.get("roots", [])          # [(stem, [used_on...]), ...]
    _hdr(roots_ws, ["Root drawing", "Name", "USED ON (parent drawings if any)"], (26, 40, 60))
    for r, (stem, used_list) in enumerate(roots_data, start=2):
        n = names.get(stem, "")
        roots_ws.cell(row=r, column=1, value=stem)
        roots_ws.cell(row=r, column=2, value=n)
        roots_ws.cell(row=r, column=3, value=", ".join(sorted(set(used_list))) or "-")
        _fill_row(roots_ws, r, 3, GRN_F, GRN_D)
    if roots_data:
        roots_ws.auto_filter.ref = f"A1:C{len(roots_data) + 1}"
    tip = len(roots_data) + 3
    roots_ws.cell(row=tip, column=1,
                  value="USED ON files belong to parent assemblies - download from Teamcenter if not already in the tree (check the Files sheet).").font = Font(italic=True, color=GREY_D)


def _mismatch_sheet(mism_ws, ctx, names):
    mism_data = ctx.get("mismatches", [])  # [(parent, child, actual_list|str), ...]
    bug_data = ctx.get("used_on_bugs", [])  # [(parent, child), ...]
    _hdr(mism_ws, ["Parent drawing", "Parent name", "Child part", "Child name",
                   "Child USED ON (actual)"], (26, 40, 26, 40, 44))
    for r, (pstem, cstem, actual) in enumerate(mism_data, start=2):
        if isinstance(actual, (list, tuple)):
            actual = ", ".join(actual) if actual else "none"
        mism_ws.cell(row=r, column=1, value=pstem)
        mism_ws.cell(row=r, column=2, value=names.get(pstem, ""))
        mism_ws.cell(row=r, column=3, value=cstem)
        mism_ws.cell(row=r, column=4, value=names.get(cstem, ""))
        mism_ws.cell(row=r, column=5, value=actual or "-")
        _fill_row(mism_ws, r, 5, AMB_F, AMB_D)
    if mism_data:
        mism_ws.auto_filter.ref = f"A1:E{len(mism_data) + 1}"
    mism_ws.cell(row=len(mism_data) + 3, column=1,
                  value="Parent BOM lists this part but the part USED ON omits the parent - verify the USED ON number.").font = Font(italic=True, color=GREY_D)

    # USED ON bugs: children whose USED ON names a parent the parent's BOM
    # does not list. BOM is the only source of truth, so these are data bugs,
    # not placement edges.
    base = len(mism_data) + 6
    mism_ws.cell(row=base, column=1,
                  value=f"USED ON BUGS ({len(bug_data)})").font = Font(bold=True, color=DARK)
    mism_ws.cell(row=base + 1, column=1,
                  value="Child USED ON lists a parent that the parent's BOM does not list - fix the USED ON number.").font = Font(italic=True, color=GREY_D)
    for i, (pstem, cstem) in enumerate(bug_data):
        r = base + 2 + i
        mism_ws.cell(row=r, column=1, value=pstem)
        mism_ws.cell(row=r, column=2, value=names.get(pstem, ""))
        mism_ws.cell(row=r, column=3, value=cstem)
        mism_ws.cell(row=r, column=4, value=names.get(cstem, ""))
        _fill_row(mism_ws, r, 5, ORG_F, ORG_D)


def _titleblock_sheet(tb_ws, ctx, names):
    data = ctx.get("titleblock_mismatches", [])  # [(stem, field, filename, tb)]
    _hdr(tb_ws, ["Stem (current file)", "Name", "Field", "In filename",
                 "In title block"], (30, 34, 12, 26, 26))
    for r, (stem, field, fval, tval) in enumerate(sorted(data), start=2):
        tb_ws.cell(row=r, column=1, value=stem)
        tb_ws.cell(row=r, column=2, value=names.get(stem, "") or "-")
        tb_ws.cell(row=r, column=3, value=field)
        tb_ws.cell(row=r, column=4, value=fval)
        tb_ws.cell(row=r, column=5, value=tval)
        _fill_row(tb_ws, r, 5, AMB_F, AMB_D)
    if data:
        tb_ws.auto_filter.ref = f"A1:E{len(data) + 1}"
    tb_ws.cell(row=len(data) + 3, column=1,
               value="Filename disagrees with the drawing's own title block - verify the filename or re-download from Teamcenter.").font = Font(italic=True, color=GREY_D)


def _files_sheet(files, ctx, names, live):
    chk_set = set(ctx.get("chk", []))
    _hdr(files, ["Drawing (stem)", "Name", "Revision", "Status"], (26, 40, 10, 18))
    rows = []
    for p in sorted(live["tree"], key=lambda q: str(q).lower()):
        stem = canonical_stem(p.stem) or p.stem.upper()
        parts = stem.split("_")
        rev = parts[1] if len(parts) > 1 and parts[1] else "-"
        if stem in chk_set:
            fill_r, fc = AMB_F, AMB_D
            status = "CHK (unapproved)"
        else:
            fill_r, fc = GRN_F, GRN_D
            status = "Active"
        rows.append((stem, names.get(stem, "") or "-", rev, status, fill_r, fc))
    for p in sorted(live["orph"], key=lambda q: str(q).lower()):
        stem = canonical_stem(p.stem) or p.stem.upper()
        parts = stem.split("_")
        rev = parts[1] if len(parts) > 1 and parts[1] else "-"
        rows.append((stem, names.get(stem, "") or "-", rev, "ORPHAN (parked)", ORG_F, ORG_D))
    for r_i, (stem, nm, rev, status_val, fill_r, fc) in enumerate(rows, start=2):
        files.cell(row=r_i, column=1, value=stem)
        files.cell(row=r_i, column=2, value=nm)
        files.cell(row=r_i, column=3, value=rev)
        files.cell(row=r_i, column=4, value=status_val)
        _fill_row(files, r_i, 4, fill_r, fc)
    if rows:
        files.auto_filter.ref = f"A1:D{len(rows) + 1}"


def _history_sheet(hist, ctx, counters, missing_pairs, path, log):
    _hdr(hist, ["Run time", "Mode", "PDFs scanned", "Copies", "Missing", "CHK", "Orphans", "Report (txt)"],
         (20, 26, 13, 10, 10, 8, 10, 46))
    entry = [str(ctx.get("run_time", "")), str(ctx.get("run_mode", "")),
             counters.get("scanned", 0), counters.get("copies", 0),
             len(missing_pairs), len(set(ctx.get("chk", []))),
             len(ctx.get("orphans", [])), ctx.get("report_txt", "")]
    hist_rows = _load_history(path, log) + [entry]
    _save_history(path, hist_rows, log)
    for i, row in enumerate(sorted(hist_rows[-HISTORY_MAX_RUNS:], reverse=True), start=2):
        for j, v in enumerate(row, start=1):
            c = hist.cell(row=i, column=j, value=v)
            c.border = BORDER
            if i % 2 == 1:
                c.fill = PatternFill("solid", fgColor=STRIPE)


def refresh_from_run(output, run_mode, run_time, counters, missing, chk,
                     orphans, report_txt, names=None, roots=None, mismatches=None,
                     used_on_bugs=None, titleblock_mismatches=None, scanned=None,
                     watermarks=None, log=None):
    """Called by the organizer after every run (execute AND dry-run).

    Dry-runs into a fresh --output never create the folder; the workbook is
    still produced, so it creates the output folder if missing (report-only).
    """
    outdir = Path(output)
    outdir.mkdir(parents=True, exist_ok=True)
    xlsx = os.path.join(str(outdir), "organize_fermi_report.xlsx")
    return build_workbook(xlsx, {
        "output": str(output),
        "run_mode": run_mode,
        "run_time": run_time,
        "counters": counters,
        "missing": missing,
        "chk": chk,
        "orphans": orphans,
        "roots": roots or [],
        "mismatches": mismatches or [],
        "used_on_bugs": used_on_bugs or [],
        "titleblock_mismatches": titleblock_mismatches or [],
        "scanned": scanned or [],
        "watermarks": watermarks or [],
        "report_txt": os.path.basename(report_txt or ""),
        "names": names or {},
    }, log=log)
