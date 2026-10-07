"""Workbook builder + history sidecar tests."""
import json
from pathlib import Path

import pytest

import fermi_report_xlsx as fx

pytestmark = pytest.mark.skipif(not fx._OPENPYXL_OK,
                                reason="openpyxl not installed")


def test_load_history_corrupt_json_preserves_corrupt_copy(tmp_path, capsys):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    sidecar = Path(str(xlsx) + ".history.json")
    sidecar.write_text("{not valid json", encoding="utf-8")

    assert fx._load_history(xlsx) == []
    assert Path(str(sidecar) + ".corrupt").exists()
    assert sidecar.exists()
    assert "run history not loaded" in capsys.readouterr().out


def test_load_history_missing_sidecar_returns_empty_quietly(tmp_path, capsys):
    assert fx._load_history(tmp_path / "nope.xlsx") == []
    assert "run history not loaded" not in capsys.readouterr().out


def test_load_history_non_list_json_preserves_corrupt_copy(tmp_path):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    sidecar = Path(str(xlsx) + ".history.json")
    sidecar.write_text("{}", encoding="utf-8")
    logged = []

    assert fx._load_history(xlsx, logged.append) == []
    assert Path(str(sidecar) + ".corrupt").exists()
    assert "run history is not a list" in "\n".join(logged)


def test_save_history_oserror_warns_without_crash(tmp_path, monkeypatch):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    logged = []

    def boom(src, dst):
        raise OSError("locked")

    monkeypatch.setattr(fx.os, "replace", boom)
    fx._save_history(xlsx, [["run", 1]], logged.append)

    assert "run history not saved" in "\n".join(logged)


def test_save_history_roundtrip_and_cap(tmp_path):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    rows = [["run", i] for i in range(250)]

    fx._save_history(xlsx, rows)

    loaded = fx._load_history(xlsx)
    assert len(loaded) == 200
    assert loaded == rows[-200:]
    assert not Path(str(xlsx) + ".history.json.tmp").exists()


def test_build_workbook_creates_expected_sheets(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = tmp_path / "organize_fermi_report.xlsx"
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 1, "roots": 1, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [],
        "chk": [],
        "orphans": [],
        "roots": [],
        "mismatches": [],
        "report_txt": "",
        "names": {},
    }

    result = fx.build_workbook(path, ctx)

    assert result == path
    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert wb.sheetnames == ["Dashboard", "Missing", "CHK", "Scanned",
                             "Watermark", "Orphans", "Superseded", "Roots",
                             "USED ON check", "Title block check", "Files",
                             "Run History"]
    history = json.loads(Path(str(path) + ".history.json").read_text(encoding="utf-8"))
    assert len(history) == 1
    assert history[0][0] == "2026-09-30T12:00:00"


def test_build_workbook_sheet_cells(tmp_path):
    out = tmp_path / "out"
    (out / "F10126106 Assembly").mkdir(parents=True)
    (out / "F10126106 Assembly" / "F10126106.pdf").write_bytes(b"%PDF")
    path = tmp_path / "organize_fermi_report.xlsx"
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 3, "roots": 1, "copies": 1,
                     "cycles": 0, "warnings": 0},
        "missing": [("F10126106", "F10126109")],
        "chk": ["F10126107__CHK"],
        "orphans": ["F10126108"],
        "roots": [("F10126106", ["F10126100"])],
        "mismatches": [("F10126106", "F10126107", ["F10126100"])],
        "titleblock_mismatches": [("F10126107__CHK", "revision", "-", "B")],
        "report_txt": "report.txt",
        "names": {"F10126109": "Child nine"},
    }

    fx.build_workbook(path, ctx)

    from openpyxl import load_workbook
    wb = load_workbook(path)
    assert wb["Dashboard"]["B5"].value == "PDF FILES IN TREE (LIVE)"
    assert wb["Dashboard"]["B6"].value == 1
    assert wb["Missing"]["A2"].value == "F10126109"
    assert wb["Missing"]["B2"].value == "Child nine"
    assert wb["CHK"]["A2"].value == "F10126107__CHK"
    assert wb["Orphans"]["A2"].value == "F10126108"
    assert wb["Roots"]["C2"].value == "F10126100"
    assert wb["USED ON check"]["E2"].value == "F10126100"
    assert wb["Title block check"]["A2"].value == "F10126107__CHK"
    assert wb["Title block check"]["C2"].value == "revision"
    assert wb["Title block check"]["D2"].value == "-"
    assert wb["Title block check"]["E2"].value == "B"
    assert wb["Files"]["A2"].value == "F10126106"
    hist = wb["Run History"]
    assert hist["A2"].value == "2026-09-30T12:00:00"
    assert hist["C2"].value == 3
    assert hist["E2"].value == 1
    assert hist["H2"].value == "report.txt"


def test_build_workbook_scanned_and_watermark_sheets(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = tmp_path / "organize_fermi_report.xlsx"
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 2, "roots": 0, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [],
        "chk": [],
        "orphans": [],
        "roots": [],
        "mismatches": [],
        "scanned": [("F10126108", [1])],
        "watermarks": [("F10126109", "watermark text: PRELIMINARY")],
        "report_txt": "",
        "names": {"F10126108": "Loose part", "F10126109": "Stamped part"},
    }

    fx.build_workbook(path, ctx)

    from openpyxl import load_workbook
    wb = load_workbook(path)
    scanned = wb["Scanned"]
    assert scanned["A2"].value == "F10126108"
    assert scanned["B2"].value == "Loose part"
    assert scanned["C2"].value == "1"
    wm = wb["Watermark"]
    assert wm["A2"].value == "F10126109"
    assert wm["B2"].value == "Stamped part"
    assert wm["C2"].value == "watermark text: PRELIMINARY"


def test_build_workbook_lists_used_on_bugs(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = tmp_path / "organize_fermi_report.xlsx"
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 1, "roots": 1, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [],
        "chk": [],
        "orphans": [],
        "roots": [],
        "mismatches": [],
        "used_on_bugs": [("F10126108", "F10126107")],
        "report_txt": "",
        "names": {},
    }

    fx.build_workbook(path, ctx)

    from openpyxl import load_workbook
    ws = load_workbook(path)["USED ON check"]
    col_a = [c.value for c in ws["A"]]
    col_c = [c.value for c in ws["C"]]
    assert "USED ON BUGS (1)" in col_a
    assert "F10126108" in col_a
    assert "F10126107" in col_c


def test_resolve_names_exact_match_and_fallback():
    live = {
        "tree": [Path("F10126106_REV_A.pdf"), Path("F10126107.pdf")],
        "orph": [Path("F10126108_B.pdf")],
    }
    names_raw = {
        "F10126106": "Base Bracket",
        "F10126107": "Direct Name",
        "F10126108_B": "Specific Orphan Rev",
        "F10126109": "Standalone Extra",
    }

    resolved = fx._resolve_names(names_raw, live)

    assert resolved == {
        "F10126106_REV_A": "Base Bracket",
        "F10126106": "Base Bracket",
        "F10126107": "Direct Name",
        "F10126108_B": "Specific Orphan Rev",
        "F10126109": "Standalone Extra",
    }


def test_resolve_names_exact_match_takes_precedence():
    live = {
        "tree": [Path("F10126106_REV_A.pdf")],
        "orph": [],
    }
    names_raw = {
        "F10126106": "Base Bracket",
        "F10126106_REV_A": "Specific Rev Bracket",
    }

    resolved = fx._resolve_names(names_raw, live)

    assert resolved["F10126106_REV_A"] == "Specific Rev Bracket"
    assert resolved["F10126106"] == "Base Bracket"


def test_resolve_names_falsy_value_triggers_fallback():
    live = {
        "tree": [Path("F10126106_REV_A.pdf")],
        "orph": [],
    }
    names_raw = {
        "F10126106_REV_A": "",
        "F10126106": "Base Bracket",
    }

    resolved = fx._resolve_names(names_raw, live)

    assert resolved["F10126106_REV_A"] == "Base Bracket"


def test_resolve_names_unresolved_and_empty_inputs():
    live = {
        "tree": [Path("F10126106.pdf"), Path("invalid_file.txt")],
        "orph": [],
    }
    names_raw = {}

    resolved = fx._resolve_names(names_raw, live)
    assert resolved == {}

    empty_resolved = fx._resolve_names({}, {"tree": [], "orph": []})
    assert empty_resolved == {}
