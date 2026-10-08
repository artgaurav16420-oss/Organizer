#!/usr/bin/env python3
"""CLI entry point for the Fermi PDF organizer (split-layout version)."""
import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .config import TREE_MAX_DEPTH
from .extraction import (OCR, resolve_jobs)
from .runmodes import run_full, run_incremental, NoPDFsFoundError, RunContext
from .report_glue import names_from_tree


def build_parser():
    parser = argparse.ArgumentParser(
        description="Organize Fermi lab PDF drawings into nested folders based on BOM tables."
    )
    parser.add_argument("folder", help="Folder containing Fermi PDF drawings")
    parser.add_argument("--output", help="Output folder for organized structure (default: same as input)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without copying drawings; report + .xlsx are still "
                             "written (a missing --output folder is created for the workbook only)")
    parser.add_argument("--incremental", action="store_true",
                        help="Process only stems not already in the output tree plus stored _orphans/ (input scan is recursive); existing subfolders stay put except supersede swaps and parent-adoption moves")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback for scanned (image-only) PDFs")
    parser.add_argument("--jobs", type=int, default=0,
                        help="Parallel extraction workers (0=auto = CPU count, 1=serial)")
    parser.add_argument("--rekey-titleblock", action="store_true",
                        help="Trust the title block over the filename: re-key a misnamed text PDF to its "
                             "title-block drawing number (only when that drawing is not already present)")
    return parser


def _log_ocr_startup(log):
    if OCR.enabled and OCR.ensure_tesseract():
        major = int(OCR.version.split(".")[0]) if OCR.version else None
        ver_txt = f", v{OCR.version}" if OCR.version else ""
        log(f"OCR: enabled (tesseract: {OCR.tesseract_path}{ver_txt})")
        if major is not None and major < 5:
            log(f"OCR: WARNING: tesseract v{major} is older than v5; "
                "scanned-page accuracy may suffer")
    else:
        reason = OCR.reason or "tesseract not found"
        log("OCR: disabled" if not OCR.enabled else
            f"OCR: unavailable ({reason}; scanned PDFs stay orphans)")


def run_mode_label(dry_run, incremental):
    if incremental:
        return "INCREMENTAL (DRY-RUN)" if dry_run else "INCREMENTAL"
    return "DRY-RUN" if dry_run else "EXECUTE"


def _refresh_workbook(ctx: RunContext, output, report_path, run_time, dry_run, incremental, log):
    try:
        import fermi_report_xlsx as fx
    except ImportError as e:
        log(f"  WARNING: Excel report skipped (module unavailable: {e})")
        return
    try:
        names = dict(ctx["names"])
        for base, name in names_from_tree(output).items():
            names.setdefault(base, name)
        # Explicit remap: RunContext uses used_on_mismatches, refresh_from_run wants mismatches.
        mismatches: list[tuple[str, str, list[str]]] = ctx["used_on_mismatches"]
        xlsx = fx.refresh_from_run(
            output=str(output), run_mode=run_mode_label(dry_run, incremental),
            run_time=run_time, counters=ctx["counters"], missing=ctx["missing"],
            chk=ctx["chk"], orphans=ctx["orphans"],
            report_txt=report_path if output.is_dir() else None,
            names=names, roots=ctx["roots"],
            mismatches=mismatches,
            used_on_bugs=ctx["used_on_bugs"],
            titleblock_mismatches=ctx["titleblock_mismatches"],
            scanned=ctx["scanned"], watermarks=ctx["watermarks"], log=log)
        if xlsx:
            log(f"  Excel report saved to: {xlsx}")
    except Exception as e:
        log(f"  WARNING: Excel report not updated: {type(e).__name__}: {e}")
        for line in traceback.format_exc().rstrip().splitlines():
            log(f"    {line}")


def main():
    """Parse args, run the full/incremental organizer, write report + workbook."""
    parser = build_parser()
    args = parser.parse_args()
    # Recursion limit: REQUIRED, not a convenience. Three recursive traversals
    # are depth-capped at TREE_MAX_DEPTH=500 (graph.break_cycles.dfs,
    # fsops.place_files.dfs, naming._collect_placements.walk); each nesting
    # level costs more than one Python frame, so the default limit of 1000 is
    # not enough headroom for a degenerate 500-deep input. 10000 ==
    # TREE_MAX_DEPTH * 20. Safe: max() never lowers the existing limit, and
    # every traversal stops at the depth cap regardless.
    sys.setrecursionlimit(max(sys.getrecursionlimit(), TREE_MAX_DEPTH * 20))

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"ERROR: not a directory: {folder}")
        sys.exit(1)

    output = Path(args.output).resolve() if args.output else folder
    if not output.is_dir() and not args.dry_run:
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"ERROR: could not create output folder {output}: {e}")
            sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output / f"organize_fermi_pdfs_report_{timestamp}.txt"
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    run_time = datetime.now().isoformat(timespec="seconds")
    log(f"Fermi PDF organizer - {run_time}")
    log(f"Source folder: {folder}")
    if output != folder:
        log(f"Output folder: {output}")
    OCR.reset(enabled=not args.no_ocr)
    _log_ocr_startup(log)
    if args.incremental and not args.output and (folder / "Output").is_dir():
        log(f"WARNING: {folder / 'Output'} exists. Use --output to target it.")
    if not args.incremental and output == folder:
        log("WARNING: in-place run - organized folders inside the input are re-scanned by later full runs; use --output to keep the tree separate")
    log(f"Mode: {run_mode_label(args.dry_run, args.incremental)}")
    jobs = resolve_jobs(args.jobs)
    log(f"  Extraction workers: {jobs} (override with --jobs N)")
    log("")

    try:
        if args.incremental:
            ctx = run_incremental(folder, output, args.dry_run, log, jobs,
                                  rekey=args.rekey_titleblock)
        else:
            ctx = run_full(folder, output, args.dry_run, log, jobs,
                           rekey=args.rekey_titleblock)
    except NoPDFsFoundError:
        sys.exit(1)

    if ctx.get("placement_refused"):
        log("  WARNING: placement refused - nothing was placed; review the "
            "BOM graph (report + workbook still written)")

    if OCR.enabled and OCR.events:
        log("")
        log("--- OCR (scanned pages) ---")
        for line in OCR.report_lines():
            log(line)

    if output.is_dir():
        try:
            report_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
            log(f"  Report saved to:     {report_path}")
        except OSError as e:
            log(f"  WARNING: report not saved ({report_path}): {e}")
    else:
        log(f"  Report NOT saved (output folder missing): {output}")

    # Live Excel workbook (after every run, dry-run included)
    _refresh_workbook(ctx, output, report_path, run_time, args.dry_run,
                      args.incremental, log)

    # Refusal is a non-success outcome: distinct exit code so scripts notice
    # without reading the log. Report + workbook are already written above.
    if ctx.get("placement_refused"):
        sys.exit(2)


if __name__ == "__main__":
    main()
