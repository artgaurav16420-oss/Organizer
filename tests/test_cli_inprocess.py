"""In-process cli.main tests: exit codes + dry-run artifacts."""
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


def _minimal_ctx():
    return {
        "counters": {"scanned": 0, "roots": 0, "copies": 0, "cycles": 0,
                     "warnings": 0},
        "missing": [],
        "chk": [],
        "orphans": [],
        "roots": [],
        "used_on_mismatches": [],
        "used_on_bugs": [],
        "titleblock_mismatches": [],
        "scanned": [],
        "watermarks": [],
        "names": {},
    }


def test_refresh_workbook_uses_explicit_run_time(tmp_path, monkeypatch):
    import fermi_report_xlsx as fx

    captured = {}
    monkeypatch.setattr(fx, "refresh_from_run",
                        lambda **kw: captured.update(kw))
    logged = []

    cli_module._refresh_workbook(_minimal_ctx(), tmp_path, None,
                                 "2026-10-08T01:02:03", True, False,
                                 logged.append)

    assert captured["run_time"] == "2026-10-08T01:02:03"


def test_refresh_workbook_logs_exception_type_and_traceback(tmp_path,
                                                            monkeypatch):
    import fermi_report_xlsx as fx

    def boom(**kw):
        raise ValueError("boom")

    monkeypatch.setattr(fx, "refresh_from_run", boom)
    logged = []

    cli_module._refresh_workbook(_minimal_ctx(), tmp_path, None,
                                 "2026-10-08T01:02:03", True, False,
                                 logged.append)

    text = "\n".join(logged)
    assert "ValueError: boom" in text
    assert "Traceback (most recent call last)" in text


def test_cli_main_placement_refused_exits_2(monkeypatch, tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    make_pdf(in_dir / "F10126106.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent"])
    make_pdf(in_dir / "F10126107.pdf", [
        "NAME", "Child part", "USED ON", "F10126106"])
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 SECOND CHILD", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    monkeypatch.setattr(sys, "argv",
                        ["organize_fermi_pdfs.py", str(in_dir),
                         "--output", str(out), "--no-ocr", "--jobs", "1"])

    with pytest.raises(SystemExit) as exc:
        cli_module.main()

    assert exc.value.code == 2
    # Report + workbook are still written before the nonzero exit.
    assert list(out.glob("organize_fermi_pdfs_report_*.txt"))
    assert (out / "organize_fermi_report.xlsx").is_file()
    assert not any(out.rglob("*.pdf"))


def test_cli_main_empty_folder_exits_1(monkeypatch, tmp_path):
    # A directory with no PDFs at all: NoPDFsFoundError -> exit 1 at main()
    # (the runmodes-level raise was tested; the CLI glue was not).
    empty = tmp_path / "in"
    empty.mkdir()
    (empty / "notes.txt").write_text("not a pdf")
    monkeypatch.setattr(sys, "argv", ["organize_fermi_pdfs.py", str(empty)])

    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 1


def test_log_ocr_startup_disabled_line(monkeypatch):
    from fermi_organizer import extraction

    logged = []
    monkeypatch.setattr(extraction.OCR, "enabled", False)
    cli_module._log_ocr_startup(logged.append)
    assert logged == ["OCR: disabled"]


def test_log_ocr_startup_unavailable_line(monkeypatch):
    from fermi_organizer import extraction

    logged = []
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "ensure_tesseract", lambda: None)
    monkeypatch.setattr(extraction.OCR, "reason", "tesseract not found")
    cli_module._log_ocr_startup(logged.append)
    assert logged == ["OCR: unavailable (tesseract not found; "
                      "scanned PDFs stay orphans)"]


def test_log_ocr_startup_enabled_with_version_and_old_warning(monkeypatch):
    from fermi_organizer import extraction

    logged = []
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "ensure_tesseract", lambda: "/x/tesseract")
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "/x/tesseract")
    monkeypatch.setattr(extraction.OCR, "version", "5.5.3")
    cli_module._log_ocr_startup(logged.append)
    assert logged == ["OCR: enabled (tesseract: /x/tesseract, v5.5.3)"]

    logged.clear()
    monkeypatch.setattr(extraction.OCR, "version", "4.1.0")
    cli_module._log_ocr_startup(logged.append)
    assert logged[0] == "OCR: enabled (tesseract: /x/tesseract, v4.1.0)"
    assert "older than v5" in logged[1]


def test_cli_main_locked_output_exits_1(monkeypatch, tmp_path, make_pdf):
    # A live run lock on the output refuses the run at main() with exit 1.
    from fermi_organizer.fsops import RUN_LOCK_NAME

    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_pdf(in_dir / "F10126106.pdf", ["NAME", "Part"])
    out = tmp_path / "out"
    out.mkdir()
    (out / RUN_LOCK_NAME).write_text('{"pid": 1}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["organize_fermi_pdfs.py", str(in_dir),
                         "--output", str(out), "--no-ocr", "--jobs", "1"])

    with pytest.raises(SystemExit) as exc:
        cli_module.main()
    assert exc.value.code == 1
