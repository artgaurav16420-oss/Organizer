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


def test_load_history_copy_oserror_handled_gracefully(tmp_path, monkeypatch, capsys):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    sidecar = Path(str(xlsx) + ".history.json")
    sidecar.write_text("{not valid json", encoding="utf-8")

    def mock_copy2(src, dst):
        raise OSError("Permission denied")

    monkeypatch.setattr(fx.shutil, "copy2", mock_copy2)

    assert fx._load_history(xlsx) == []
    assert not Path(str(sidecar) + ".corrupt").exists()
    assert "run history not loaded" in capsys.readouterr().out


def test_load_history_missing_sidecar_returns_empty_quietly(tmp_path, capsys):
    assert fx._load_history(tmp_path / "nope.xlsx") == []
    assert "run history not loaded" not in capsys.readouterr().out


@pytest.mark.parametrize("payload", ["{}", "123", '"string_data"', "true", "45.67"])
def test_load_history_non_list_json_preserves_corrupt_copy(tmp_path, payload):
    xlsx = tmp_path / "organize_fermi_report.xlsx"
    sidecar = Path(str(xlsx) + ".history.json")
    sidecar.write_text(payload, encoding="utf-8")
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


def test_formula_injection_neutralized(tmp_path):
    # PDF-derived text starting with "=" must be stored as text, never as a
    # formula Excel evaluates on open (table rows + dashboard boxes).
    from openpyxl import Workbook, load_workbook

    out = tmp_path / "out"
    out.mkdir()
    path = tmp_path / "organize_fermi_report.xlsx"
    evil = '=HYPERLINK("http://evil.example","click")'
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 1, "roots": 1, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [("F10126106", "F10126109")],
        "chk": [],
        "orphans": [],
        "roots": [],
        "mismatches": [],
        "report_txt": "",
        "names": {"F10126109": evil},
    }

    fx.build_workbook(path, ctx)
    cell = load_workbook(path)["Missing"]["B2"]
    assert cell.value == evil
    assert cell.data_type == "s"

    box = tmp_path / "box.xlsx"
    w2 = Workbook()
    fx._box(w2.active, 1, 1, 3, "=1+1")
    w2.save(box)
    cell2 = load_workbook(box).active["A1"]
    assert cell2.value == "=1+1"
    assert cell2.data_type == "s"


def test_dash_two_tier_banner_print_fit_and_scanned_info_color(tmp_path):
    from openpyxl import load_workbook

    def build(name, ctx):
        path = tmp_path / name
        fx.build_workbook(path, ctx)
        return load_workbook(path)

    base = {
        "output": str(tmp_path / "out"),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 0, "roots": 0, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [], "chk": [], "orphans": [], "roots": [], "mismatches": [],
        "report_txt": "", "names": {},
    }

    # Action tier only: Missing + CHK -> red "Action needed" banner.
    wb = build("act.xlsx", {**base, "missing": [("F10126106", "F10126109")],
                            "chk": ["F10126107__CHK"]})
    dash = wb["Dashboard"]
    assert dash.page_setup.fitToHeight == 1
    vals = [str(c.value) for row in dash.iter_rows() for c in row
            if c.value is not None]
    banner = next(v for v in vals if "Action needed" in v)
    assert "Worth a look" not in banner
    assert "Action" in vals          # per-row status pills
    assert "Info" in vals            # legend documents the blue swatch

    # Review tier only: one scanned PDF -> amber "Worth a look" + blue tab.
    wb2 = build("rev.xlsx", {**base, "scanned": [("F10126108", [1])]})
    vals2 = [str(c.value) for row in wb2["Dashboard"].iter_rows() for c in row
             if c.value is not None]
    assert any("Worth a look" in v and "Action needed" not in v for v in vals2)
    tab = wb2["Scanned"].sheet_properties.tabColor
    assert tab is not None and tab.rgb[-6:].upper() == "2563EB"


def test_no_formula_cells_anywhere_including_used_on_bugs(tmp_path):
    # Fuzz-style: a "=" payload in the USED ON BUGS section (written outside
    # _table) must never be persisted as an Excel formula, and no other sheet
    # may produce one either.
    from openpyxl import load_workbook

    out = tmp_path / "out"
    out.mkdir()
    path = tmp_path / "organize_fermi_report.xlsx"
    evil = "=HYPERLINK(\"http://evil.example\",\"hi\")"
    ctx = {
        "output": str(out),
        "run_mode": "DRY-RUN",
        "run_time": "2026-09-30T12:00:00",
        "counters": {"scanned": 1, "roots": 1, "copies": 0,
                     "cycles": 0, "warnings": 0},
        "missing": [], "chk": [], "orphans": [], "roots": [], "mismatches": [],
        "used_on_bugs": [(evil, evil)],
        "report_txt": "",
        "names": {evil: evil},
    }

    fx.build_workbook(path, ctx)
    wb = load_workbook(path)
    formulas = [(ws.title, c.coordinate)
                for ws in wb.worksheets for row in ws.iter_rows()
                for c in row if c.data_type == "f"]
    assert formulas == []
