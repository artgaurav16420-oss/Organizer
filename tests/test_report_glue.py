"""Workbook glue: tree-derived names; the .txt log is no longer parsed."""
import os
import subprocess
from pathlib import Path

import pytest

import fermi_organizer.report_glue as report_glue
from fermi_organizer.report_glue import names_from_tree


@pytest.mark.skipif(os.name != "nt", reason="NTFS junctions are Windows-only")
def test_names_from_tree_ignores_a_linked_subtree(tmp_path):
    # The walk must not *enter* a junction: skipping its own name still let
    # names from outside the output tree reach the workbook fallback.
    out = tmp_path / "out"
    (out / "F10126106 Parent").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "F10126199 Foreign name").mkdir(parents=True)

    link = out / "F10126106 Parent" / "linked"
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                          capture_output=True)
    if proc.returncode != 0:
        pytest.skip("junction creation not permitted")
    try:
        assert names_from_tree(out) == {"F10126106": "Parent"}
    finally:
        os.rmdir(link)


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
