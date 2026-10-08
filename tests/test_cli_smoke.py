"""Subprocess smoke tests: launcher -> CLI, and clean module imports."""
import os
import subprocess
import sys
from pathlib import Path

from fermi_organizer.cli import build_parser, run_mode_label

ROOT = Path(__file__).resolve().parent.parent


def test_import_after_pymupdf_preimport_emits_no_deprecation():
    # The legacy `fitz` shim printed its deprecation notice through the
    # stdout handle pymupdf captured at ITS import time - so the print
    # leaked whenever pymupdf was imported before fermi_organizer.extraction
    # (the old redirect_stdout buffer only covered the lucky order). Pin
    # exactly that ordering: pymupdf first, then extraction.
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        [sys.executable, "-c",
         "import pymupdf; import fermi_organizer.extraction"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    combined = proc.stdout + proc.stderr
    assert "deprecated" not in combined, combined


def test_build_parser_and_run_mode_label():
    parser = build_parser()
    args = parser.parse_args(["in", "--dry-run", "--no-ocr", "--jobs", "2"])
    assert args.folder == "in"
    assert args.dry_run and args.no_ocr and not args.incremental
    assert args.jobs == 2
    assert run_mode_label(True, False) == "DRY-RUN"
    assert run_mode_label(False, False) == "EXECUTE"
    assert run_mode_label(True, True) == "INCREMENTAL (DRY-RUN)"
    assert run_mode_label(False, True) == "INCREMENTAL"
    # Help must not promise "no folders": the workbook creates the output
    # folder, and the report/workbook are still written in dry-run.
    dry_help = next(a for a in parser._actions if a.dest == "dry_run").help
    assert "workbook" in dry_help
    assert "without copying drawings" in dry_help


def test_cli_dry_run_smoke(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    make_pdf(in_dir / "F10126106.pdf", [
        "FERMI PART LIST",
        "F10126107 CHILD PART",
        "NAME",
        "Parent",
    ])
    make_pdf(in_dir / "F10126107.pdf", [
        "NAME",
        "Child part",
        "USED ON",
        "F10126106",
    ])

    proc = subprocess.run(
        [sys.executable, str(ROOT / "organize_fermi_pdfs.py"), str(in_dir),
         "--output", str(out), "--dry-run", "--no-ocr", "--jobs", "1"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    reports = list(out.glob("organize_fermi_pdfs_report_*.txt"))
    assert len(reports) == 1
    assert "Mode: DRY-RUN" in reports[0].read_text(encoding="utf-8")
    assert (out / "organize_fermi_report.xlsx").is_file()
    assert not list(out.rglob("*.pdf"))
