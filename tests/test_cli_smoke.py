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
    assert run_mode_label(False, repair=True) == "REPAIR"
    assert run_mode_label(True, repair=True) == "REPAIR (DRY-RUN)"
    assert args.repair is False
    repair_help = next(a for a in parser._actions if a.dest == "repair").help
    assert "Places nothing new" in repair_help
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


def test_launcher_runs_from_a_foreign_cwd_without_pythonpath(tmp_path, make_pdf):
    # The organize_fermi_pdfs.py sys.path shim is the only thing that makes a
    # foreign-cwd launch resolve the package: run the script file from outside
    # the repo with PYTHONPATH scrubbed and require the full dry-run.
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
    env = {k: v for k, v in os.environ.items() if k.upper() != "PYTHONPATH"}

    proc = subprocess.run(
        [sys.executable, str(ROOT / "organize_fermi_pdfs.py"), str(in_dir),
         "--output", str(out), "--dry-run", "--no-ocr", "--jobs", "1"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(list(out.glob("organize_fermi_pdfs_report_*.txt"))) == 1
    assert (out / "organize_fermi_report.xlsx").is_file()


def test_jobs_ceiling_is_a_usage_error():
    # A dropped decimal point must not ask the spawn pool for a billion
    # workers: --jobs is bounded, and 0/1 keep their meanings.
    import pytest

    parser = build_parser()
    assert parser.parse_args(["in", "--jobs", "0"]).jobs == 0
    assert parser.parse_args(["in", "--jobs", "1"]).jobs == 1
    assert parser.parse_args(["in", "--jobs", "8"]).jobs == 8
    with pytest.raises(SystemExit):
        parser.parse_args(["in", "--jobs", str(10 ** 9)])
    with pytest.raises(SystemExit):
        parser.parse_args(["in", "--jobs", "-2"])


def test_resolve_jobs_caps_a_typo():
    from fermi_organizer.config import MAX_JOBS
    from fermi_organizer.extraction import resolve_jobs

    assert resolve_jobs(0) >= 1
    assert resolve_jobs(1) == 1
    assert resolve_jobs(10 ** 9) == MAX_JOBS
    assert resolve_jobs("abc") >= 1


def test_resolve_jobs_caps_the_auto_count(monkeypatch):
    # The auto path (--jobs 0) must respect MAX_JOBS too: a host reporting
    # thousands of CPUs would otherwise start an oversized pool.
    from fermi_organizer import extraction
    from fermi_organizer.config import MAX_JOBS

    monkeypatch.setattr(extraction.os, "cpu_count", lambda: 4096)
    assert extraction.resolve_jobs(0) == MAX_JOBS
    assert extraction.resolve_jobs(None) == MAX_JOBS
    monkeypatch.setattr(extraction.os, "cpu_count", lambda: 4)
    assert extraction.resolve_jobs(0) == 4
