"""Full vs incremental mode parity.

The standing operator requirement is that the two run modes report the same
things for the same tree. The workbook is rebuilt from the run context every
run, so a field that is computed differently per mode silently changes the
report. These tests pin the reported decisions to a single implementation.

Excluded by design (see docs/adr/0001-shared-run-mode-decisions.md):
  * counters / summary lines - inherently delta-oriented for incremental
    ("New PDFs scanned", "PDF copies written") vs whole-tree for full.
  * `scanned` - run-scoped OCR events, not a tree property.
  * `orphans` - full derives them from the input classification, incremental
    from a live `_orphans/` scan (which also sees earlier runs' parks).
"""
from fermi_organizer.runmodes import run_full, run_incremental


def _decision_sets(ctx):
    return {
        "roots": sorted(ctx["roots"]),
        "missing": sorted(ctx["missing"]),
        "chk": sorted(ctx["chk"]),
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
    # Unapproved CHK with no approved twin -> stays live in the tree, so both
    # modes must report it (and stop once an approved revision arrives).
    make_pdf(in_dir / "F10126113__CHK.pdf", [
        "FERMI PART LIST", "F10126108 STANDALONE PART", "NAME", "Unapproved"])


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
    assert full["roots"] == [("F10126106", []), ("F10126109", []),
                             ("F10126113__CHK", [])]
    assert full["chk"] == ["F10126113__CHK"]
    assert full["used_on_mismatches"] == [("F10126106", "F10126107", []),
                                          ("F10126106", "F10156241", []),
                                          ("F10126109", "F10126108", []),
                                          ("F10126113__CHK", "F10126108", [])]
    assert full["used_on_bugs"] == [("F10126106", "F10126111")]
    assert full["missing"] == []


def _live_paths(root):
    """Live PDFs of a tree, as relative paths (system folders excluded)."""
    found = set()
    for path in root.rglob("*.pdf"):
        rel = path.relative_to(root)
        if rel.parts[0].startswith("_"):
            continue
        found.add(str(rel).lower())
    return found


_LONG_A = "ASSEMBLY OF THE LONG NAMED THING FOR PATH LENGTH TESTING ALPHA BRAVO CHA"
_LONG_B = "WELDMENT OF THE LONG NAMED THING FOR PATH LENGTH TESTING CHARLIE DELTA E"
_LONG_C = "PROCUREMENT ASSEMBLY OF THE LONG NAMED THING FOR PATH LENGTH TESTING FOXT"


def _write_long_batch_base(make_pdf, in_dir):
    make_pdf(in_dir / "F10127001.pdf", [
        "FERMI PART LIST", "F10127002 CHILD", "NAME", _LONG_A])
    make_pdf(in_dir / "F10127002.pdf", ["NAME", _LONG_B])


def _write_long_batch_deeper(make_pdf, in_dir):
    make_pdf(in_dir / "F10127003.pdf", [
        "FERMI PART LIST", "F10127001 CHILD", "NAME", _LONG_C])


def test_folder_names_do_not_depend_on_batch_order(tmp_path, make_pdf):
    # The same drawings must give the same tree whichever order the batches
    # arrive in. Folder names are shortened to fit the path limit, and how much
    # is needed depends on how deep a drawing ends up. Naming each folder from
    # the placement that happened to create it first - and adopting organized
    # children under the name they already carried on disk - left two trees
    # built from the same drawings in opposite orders with differently spelled
    # folders, and skipped the deepest copies outright.
    logged_a = []
    out_a = tmp_path / "order_a"
    in_a = tmp_path / "in_a"
    in_a.mkdir()
    _write_long_batch_base(make_pdf, in_a)
    run_full(in_a, out_a, False, logged_a.append, jobs=1)
    _write_long_batch_deeper(make_pdf, in_a)
    run_incremental(in_a, out_a, False, logged_a.append, jobs=1)

    logged_b = []
    out_b = tmp_path / "order_b"
    in_b = tmp_path / "in_b"
    in_b.mkdir()
    _write_long_batch_base(make_pdf, in_b)
    _write_long_batch_deeper(make_pdf, in_b)
    run_full(in_b, out_b, False, logged_b.append, jobs=1)

    paths_a = _live_paths(out_a)
    assert paths_a == _live_paths(out_b)
    # Non-vacuous: all three drawings are live in both, and nothing was dropped
    # for being too long (the deepest copies used to be skipped).
    assert len(paths_a) == 3
    assert not any("path too long" in line for line in logged_a + logged_b)
