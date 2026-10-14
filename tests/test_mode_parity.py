"""Full vs incremental mode parity.

The standing operator requirement is that the two run modes report the same
things for the same tree. The workbook is rebuilt from the run context every
run, so a field that is computed differently per mode silently changes the
report. These tests pin the reported decisions to a single implementation.

Excluded by design (see docs/adr/0001-shared-run-mode-decisions.md):
  * counters / summary lines - inherently delta-oriented for incremental
    ("New PDFs scanned", "PDF copies written") vs whole-tree for full.
  * `scanned` - run-scoped OCR events, not a tree property.
  * `chk` - full reports CHK across the whole input; incremental reports CHK
    arrivals only (the Teamcenter "download these" list for this batch).
  * `orphans` - full derives them from the input classification, incremental
    from a live `_orphans/` scan (which also sees earlier runs' parks).
"""
from fermi_organizer.runmodes import run_full, run_incremental


def _decision_sets(ctx):
    return {
        "roots": sorted(ctx["roots"]),
        "missing": sorted(ctx["missing"]),
        "used_on_bugs": sorted(ctx["used_on_bugs"]),
        "used_on_mismatches": sorted(ctx["used_on_mismatches"]),
        "titleblock_mismatches": sorted(ctx["titleblock_mismatches"]),
        "watermarks": sorted(ctx["watermarks"]),
    }


def _write_fixture(make_pdf, in_dir):
    # Root with two children, one of them named through an OCR misread that
    # snaps (F0156241 -> F10156241).
    make_pdf(in_dir / "F10126106.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "F0156241 SNAPPED PART",
        "NAME", "Parent"])
    # Child whose USED ON omits its BOM parent -> USED ON mismatch.
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part"])
    # Snapped-to target.
    make_pdf(in_dir / "F10156241.pdf", ["NAME", "Snapped target"])
    # Parentless part claiming a parent whose BOM does not list it -> bug.
    make_pdf(in_dir / "F10126111.pdf", ["NAME", "Bugged part", "USED ON", "F10126106"])
    # Parentless BOM-less standalone -> parked orphan, never a root.
    make_pdf(in_dir / "F10126108.pdf", ["NAME", "Standalone part"])


def test_full_and_incremental_report_same_decisions(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_fixture(make_pdf, in_dir)

    # Populate a tree, then add one more PDF so the incremental run has work
    # (with nothing new it short-circuits and never builds the merged view).
    out = tmp_path / "out"
    run_full(in_dir, out, False, lambda m: None, jobs=1)
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126108 STANDALONE PART", "NAME", "New assembly"])

    incr = run_incremental(in_dir, out, False, lambda m: None, jobs=1)
    # Reference: a full run over the SAME final input, into its own tree.
    full = run_full(in_dir, tmp_path / "out_full", False, lambda m: None, jobs=1)

    assert _decision_sets(incr) == _decision_sets(full)

    # ...and the sets are non-trivial, so parity is not vacuous.
    assert full["roots"] == [("F10126106", []), ("F10126109", [])]
    assert full["used_on_mismatches"] == [("F10126106", "F10126107", []),
                                          ("F10126106", "F10156241", []),
                                          ("F10126109", "F10126108", [])]
    assert full["used_on_bugs"] == [("F10126106", "F10126111")]
    assert full["missing"] == []
