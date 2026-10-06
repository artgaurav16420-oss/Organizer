"""In-process cli.main tests (T-023): exit codes + dry-run artifacts."""
import sys

import pytest

from fermi_organizer import cli as cli_module


def test_cli_main_missing_folder_exits_1(monkeypatch, tmp_path):
    missing = tmp_path / "nope"
    monkeypatch.setattr(sys, "argv",
                        ["organize_fermi_pdfs.py", str(missing)])
    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 1


def test_cli_main_dry_run_writes_report_and_xlsx(monkeypatch, tmp_path, make_pdf):
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
    monkeypatch.setattr(sys, "argv",
                        ["organize_fermi_pdfs.py", str(in_dir),
                         "--output", str(out), "--dry-run",
                         "--no-ocr", "--jobs", "1"])
    assert cli_module.main() is None

    reports = list(out.glob("organize_fermi_pdfs_report_*.txt"))
    assert len(reports) == 1
    assert "Mode: DRY-RUN" in reports[0].read_text(encoding="utf-8")
    assert (out / "organize_fermi_report.xlsx").is_file()
    assert not list(out.rglob("*.pdf"))
