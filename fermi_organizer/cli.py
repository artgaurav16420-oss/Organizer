#!/usr/bin/env python3
"""CLI entry point for the Fermi PDF organizer (split-layout version)."""
import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .config import MAX_JOBS
from .extraction import OCR, resolve_jobs
from .fsops import (
    LockLostError,
    RunLockedError,
    abandon_run_lock,
    release_run_lock,
    run_lock_lost,
)
from .report_glue import names_from_tree
from .runmodes import (
    NoPDFsFoundError,
    RunContext,
    run_full,
    run_incremental,
    run_repair,
)


def _jobs_arg(raw):
    """argparse type for --jobs: an int in 0..MAX_JOBS.

    0 keeps its meaning (auto = CPU count) and 1 stays serial. A value above
    the ceiling is a usage error rather than a silent clamp: a dropped decimal
    point ('--jobs 1000000000') is a typo the operator wants to see.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"invalid int value: {raw!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (0 = auto, 1 = serial)")
    if value > MAX_JOBS:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_JOBS}")
    return value


def build_parser():
    """Build and return the argparse parser for the Fermi PDF organizer CLI."""
    parser = argparse.ArgumentParser(
        description="Organize Fermi lab PDF drawings into nested folders based on BOM tables."
    )
    parser.add_argument("folder", help="Folder containing Fermi PDF drawings")
    parser.add_argument("--output", help="Output folder for organized structure; use an isolated "
                                "folder such as \"<input>/Output\" for repeat runs (default: same as input)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without copying drawings; report + .xlsx are still "
                             "written (a missing --output folder is created for the workbook only)")
    parser.add_argument("--incremental", action="store_true",
                        help="Process only stems not already in the output tree plus stored "
                             "_orphans/ (input scan is recursive); existing subfolders stay put "
                             "except supersede swaps and parent-adoption moves")
    parser.add_argument("--repair", action="store_true",
                        help="Re-apply the current decisions to an existing output tree given as "
                             "FOLDER: rename folders, re-home misplaced ones, adopt wrongly parked "
                             "_orphans/, promote wrongly archived _superseded/ revisions, drop "
                             "verified duplicates. Places nothing new; reads no source folder")
    parser.add_argument("--no-ocr", action="store_true", help="Disable OCR fallback for scanned (image-only) PDFs")
    parser.add_argument("--jobs", type=_jobs_arg, default=0,
                        help=f"Parallel extraction workers (0=auto = CPU count, 1=serial, "
                             f"max {MAX_JOBS})")
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


def run_mode_label(dry_run, incremental=False, repair=False):
    """Return the short mode label for logging/workbook based on run flags."""
    if repair:
        return "REPAIR (DRY-RUN)" if dry_run else "REPAIR"
    if incremental:
        return "INCREMENTAL (DRY-RUN)" if dry_run else "INCREMENTAL"
    return "DRY-RUN" if dry_run else "EXECUTE"


def _refresh_workbook(ctx: RunContext, output, report_path, run_time, dry_run, incremental, log,
                      repair=False):
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
            output=str(output), run_mode=run_mode_label(dry_run, incremental, repair),
            run_time=run_time, counters=ctx["counters"], missing=ctx["missing"],
            chk=ctx["chk"], orphans=ctx["orphans"],
            report_txt=report_path if output.is_dir() else None,
            names=names, roots=ctx["roots"],
            mismatches=mismatches,
            used_on_bugs=ctx["used_on_bugs"],
            titleblock_mismatches=ctx["titleblock_mismatches"],
            scanned=ctx["scanned"], watermarks=ctx["watermarks"], log=log,
            placement_refused=ctx.get("placement_refused", False),
            skipped_roots=ctx.get("skipped_roots", []))
        if xlsx:
            log(f"  Excel report saved to: {xlsx}")
    except Exception as e:
        log(f"  WARNING: Excel report not updated: {type(e).__name__}: {e}")
        for line in traceback.format_exc().rstrip().splitlines():
            log(f"    {line}")


def _parse_args(argv=None):
    """(parser, parsed args). parser is returned so flag conflicts can
    call parser.error and keep argparse's usage text."""
    parser = build_parser()
    return parser, parser.parse_args(argv)


def _resolve_folders(parser, args):
    """Validated (folder, output); exits 1 on a bad path, usage error on a
    contradictory flag combination."""
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"ERROR: not a directory: {folder}", file=sys.stderr)
        sys.exit(1)

    if args.repair:
        if args.incremental:
            parser.error("--repair and --incremental cannot be combined: --repair works on an "
                         "existing tree, --incremental brings in new PDFs")
        if args.output:
            parser.error("--repair takes the tree to repair as FOLDER; drop --output")
        if args.rekey_titleblock:
            parser.error("--repair does not re-key drawings; run --rekey-titleblock with a "
                         "normal run")

    output = Path(args.output).resolve() if args.output else folder
    if not output.is_dir() and not args.dry_run:
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"ERROR: could not create output folder {output}: {e}",
                  file=sys.stderr)
            sys.exit(1)
    return folder, output


def _log_run_header(log, args, folder, output, run_time, jobs):
    """Startup block: paths, mode, worker count and the operator warnings.

    Order is load-bearing: tests/test_golden_log.py pins the dry-run log
    byte-for-byte.
    """
    log(f"Fermi PDF organizer - {run_time}")
    log(f"Source folder: {folder}")
    if output != folder:
        log(f"Output folder: {output}")
    OCR.reset(enabled=not args.no_ocr)
    _log_ocr_startup(log)
    if args.incremental and not args.output and (folder / "Output").is_dir():
        log(f"WARNING: {folder / 'Output'} exists. Use --output to target it.")
    if not args.incremental and not args.repair and output == folder:
        log("WARNING: in-place run - organized folders inside the input "
            "are re-scanned by later full runs; use --output to keep the "
            "tree separate")
    if os.name == "nt" and str(output).startswith("\\"):
        log("WARNING: output is on a network share (UNC path): the run lock "
            "is not reliable there - coordinate runs manually, never run two "
            "against this output (mapped drive letters to a share are no "
            "safer, they just cannot be detected - see Known limits)")
    log(f"Mode: {run_mode_label(args.dry_run, args.incremental, args.repair)}")
    log(f"  Extraction workers: {jobs} (override with --jobs N)")
    log("")


def _dispatch_mode(args, folder, output, jobs, log):
    """Run the selected mode; returns (ctx, held lock path)."""
    if args.repair:
        return run_repair(folder, dry_run=args.dry_run, log=log,
                          jobs=jobs, hold_lock=True)
    if args.incremental:
        return run_incremental(folder, output, args.dry_run, log, jobs,
                               rekey=args.rekey_titleblock, hold_lock=True)
    return run_full(folder, output, args.dry_run, log, jobs,
                    rekey=args.rekey_titleblock, hold_lock=True)


def _write_artifacts(ctx, args, output, report_path, run_time, log, log_lines):
    """Post-work: refusal warnings, the OCR block, the .txt report, workbook."""
    skipped = ctx.get("skipped_roots", [])
    if skipped:
        names = ", ".join(r for r, _ in skipped[:5])
        more = f" (+{len(skipped) - 5} more)" if len(skipped) > 5 else ""
        verb = "would be placed" if args.dry_run else "was placed"
        log("  WARNING: placement partially refused - "
            f"{len(skipped)} oversized root(s) skipped ({names}{more}); "
            f"the rest {verb} (bounded orphan/archive copies are still "
            "handled); review the BOM graph (report + workbook still "
            "written)")
    elif ctx.get("placement_refused"):
        log("  WARNING: placement refused - folder placement skipped (bounded "
            "orphan/archive copies still ran); review the "
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

    _refresh_workbook(ctx, output, report_path, run_time, args.dry_run,
                      args.incremental, log, repair=args.repair)


def _refusal_exit_code(ctx):
    """2 when the run placed nothing (or repair needs review), else 0."""
    if ctx.get("placement_refused") or ctx.get("repair_required"):
        return 2
    return 0


def main(argv=None):
    """Parse args, run the selected mode, write the report + live workbook."""
    parser, args = _parse_args(argv)
    folder, output = _resolve_folders(parser, args)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = output / f"organize_fermi_pdfs_report_{timestamp}.txt"
    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    run_time = datetime.now().isoformat(timespec="seconds")
    jobs = resolve_jobs(args.jobs)
    _log_run_header(log, args, folder, output, run_time, jobs)

    try:
        ctx, held = _dispatch_mode(args, folder, output, jobs, log)
    except NoPDFsFoundError:
        sys.exit(1)
    except LockLostError as e:
        # Stolen mid-run: the tree may hold partial writes, hence exit 2
        # (same as placement-refused) rather than the clean-refusal exit 1.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except RunLockedError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # The lock spans the shared-file post-work too - but re-verify it first:
    # a steal during the run leaves the report/workbook writes to the other
    # run. Discard post-work (never clobber) and exit 2 like a mid-run loss.
    if run_lock_lost(held):
        log("  ERROR: run lock lost during the run (stolen while paused?) - "
            "discarding report/workbook writes to avoid clobbering the other run")
        abandon_run_lock(held)
        sys.exit(2)

    try:
        _write_artifacts(ctx, args, output, report_path, run_time, log, log_lines)
    finally:
        # The lock spans the shared-file post-work too: report + workbook are
        # read-modify-write files, so a second run must not enter until they
        # are done. run_* acquired it; only the CLI holder releases it here.
        release_run_lock(held)

    # Refusal is a non-success outcome: distinct exit code so scripts notice
    # without reading the log. Report + workbook are already written above.
    code = _refusal_exit_code(ctx)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
