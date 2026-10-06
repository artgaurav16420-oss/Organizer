"""Workbook glue: tree-derived names; the .txt log is no longer parsed."""
from pathlib import Path

import fermi_organizer.report_glue as report_glue
from fermi_organizer.report_glue import names_from_tree


def test_names_from_tree_reads_base_name_folders(tmp_path):
    (tmp_path / "F10126106 Assembly bracket").mkdir()
    (tmp_path / "F10126106 Assembly bracket" / "F10126107 Child bracket").mkdir()
    assert names_from_tree(tmp_path) == {
        "F10126106": "Assembly bracket",
        "F10126107": "Child bracket",
    }


def test_log_parsing_helpers_removed():
    assert not hasattr(report_glue, "_parse_summary")
    assert not hasattr(report_glue, "_collect_xlsx_ctx")


def test_no_prior_report_fallback_in_source():
    src = Path(report_glue.__file__).read_text(encoding="utf-8")
    assert "organize_fermi_pdfs_report" not in src
