"""End-to-end run_full / run_incremental on generated PDFs (tmp dirs only)."""
import re

import pymupdf as fitz
import pytest

from pathlib import Path
from unittest.mock import patch

from fermi_organizer import extraction, fsops
from fermi_organizer.runmodes import (run_full, run_incremental, NoPDFsFoundError,
                                      _ensure_lock, _swap_revision_files, _log_summary,
                                      _log_unplaced, _titleblock_mismatches)
from fermi_organizer.fsops import (RunLockedError, LockLostError, release_run_lock,
                                   abandon_run_lock, acquire_run_lock)


def test_run_full_hold_lock_spans_caller_post_work(tmp_path, make_pdf):
    # FIX-203: with hold_lock the run returns (ctx, lock) and keeps the tree
    # locked past return, so the caller's report/workbook writes cannot
    # interleave with a second run; default behavior is unchanged.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_pdf(in_dir / "F10126106.pdf", ["NAME", "Part"])
    out = tmp_path / "out"
    out.mkdir()

    ctx, lock = run_full(in_dir, out, True, lambda m: None, jobs=1,
                         hold_lock=True)

    assert isinstance(ctx, dict) and lock is not None and lock.is_file()
    with pytest.raises(RunLockedError):
        run_full(in_dir, out, True, lambda m: None, jobs=1)
    release_run_lock(lock)
    ctx2 = run_full(in_dir, out, True, lambda m: None, jobs=1)
    assert isinstance(ctx2, dict)


def test_ensure_lock_passes_when_held_or_unlocked(tmp_path):
    # No loss flagged: silent pass, including the unlocked (None) run.
    out = tmp_path / "out"
    out.mkdir()
    logged = []
    lock = acquire_run_lock(out, False, logged.append)
    try:
        assert _ensure_lock(lock, logged.append) is None
        assert _ensure_lock(None, logged.append) is None
        assert logged == []
    finally:
        release_run_lock(lock)


def test_ensure_lock_aborts_lost_run(tmp_path):
    # Flagged loss: LockLostError (a RunLockedError) with an ERROR line.
    out = tmp_path / "out"
    out.mkdir()
    logged = []
    lock = acquire_run_lock(out, False, logged.append)
    try:
        fsops._heartbeats[str(lock)][2].set()  # heartbeat observed a steal
        with pytest.raises(LockLostError):
            _ensure_lock(lock, logged.append)
        assert any("ERROR" in m and "lock lost" in m for m in logged)
    finally:
        abandon_run_lock(lock)


def test_run_wrappers_abandon_stealer_lock_on_loss(tmp_path, monkeypatch):
    # A mid-run LockLostError must stop the heartbeat WITHOUT unlinking:
    # the lock file belongs to the stealer now.
    import fermi_organizer.runmodes as runmodes

    out = tmp_path / "out"
    out.mkdir()

    def fake_inner(*a, **k):
        lock = k.get("lock")
        fsops._heartbeats[str(lock)][2].set()
        from fermi_organizer.runmodes import _ensure_lock as ensure
        ensure(lock, lambda m: None)

    for runner, inner_name in ((run_full, "_run_full_inner"),
                               (run_incremental, "_run_incremental_inner")):
        monkeypatch.setattr(runmodes, inner_name, fake_inner)
        with pytest.raises(LockLostError):
            runner(tmp_path, out, True, lambda m: None, jobs=1)
        assert (out / ".fermi_organizer.lock").is_file()
        assert str(out / ".fermi_organizer.lock") not in fsops._heartbeats
        # Reset for the next iteration (content is still ours here).
        release_run_lock(out / ".fermi_organizer.lock")


def test_swap_revision_files_aborts_on_lost_lock(tmp_path):
    # Pre-swap check: a stolen lock raises before os.replace, so the tree is
    # untouched (old revision in place, nothing archived, no staging left).
    from fermi_organizer.runmodes import _swap_revision_files

    out = tmp_path / "out"
    tree = out / "F10126106 Assembly"
    tree.mkdir(parents=True)
    old = tree / "F10126106.pdf"
    old.write_bytes(b"old-rev")
    new = tmp_path / "F10126106_B.pdf"
    new.write_bytes(b"new-rev")
    sup = out / "_superseded"
    sup.mkdir()

    logged = []
    lock = acquire_run_lock(out, False, logged.append)
    try:
        fsops._heartbeats[str(lock)][2].set()  # stolen before the swap
        with pytest.raises(LockLostError):
            _swap_revision_files([old], new, sup, out, False, logged.append,
                                 lock=lock)
        assert any("lock lost" in m for m in logged)
    finally:
        abandon_run_lock(lock)
    assert old.read_bytes() == b"old-rev"
    assert list(sup.glob("*.pdf")) == []
    assert list(tree.glob("*.supersede_tmp.*")) == []


def test_swap_revision_files_sees_file_steal(tmp_path):
    # Synchronous detection on the swap path: replaced lock content (no tick,
    # no flag) aborts before os.replace with the tree untouched.
    from fermi_organizer.runmodes import _swap_revision_files

    out = tmp_path / "out"
    tree = out / "F10126106 Assembly"
    tree.mkdir(parents=True)
    old = tree / "F10126106.pdf"
    old.write_bytes(b"old-rev")
    new = tmp_path / "F10126106_B.pdf"
    new.write_bytes(b"new-rev")
    sup = out / "_superseded"
    sup.mkdir()

    logged = []
    lock = acquire_run_lock(out, False, logged.append)
    try:
        lock.write_bytes(b'{"pid": 999999, "host": "stealer"}')
        with pytest.raises(LockLostError):
            _swap_revision_files([old], new, sup, out, False, logged.append,
                                 lock=lock)
        assert any("lock lost" in m for m in logged)
    finally:
        abandon_run_lock(lock)
    assert old.read_bytes() == b"old-rev"
    assert list(sup.glob("*.pdf")) == []
    assert list(tree.glob("*.supersede_tmp.*")) == []


def _write_parent(make_pdf, folder, name="Test Parent"):
    return make_pdf(folder / "F10126106.pdf", [
        "FERMI PART LIST",
        "F10126107 CHILD PART",
        "NAME",
        name,
    ])


def _write_child(make_pdf, folder, name="Child part"):
    return make_pdf(folder / "F10126107.pdf", [
        "NAME",
        name,
        "USED ON",
        "F10126106",
    ])


def _write_standalone(make_pdf, folder, name="Standalone part"):
    return make_pdf(folder / "F10126108.pdf", ["NAME", name])


def test_run_full_empty_folder_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    logged = []
    with pytest.raises(NoPDFsFoundError):
        run_full(empty, tmp_path / "out", True, logged.append, jobs=1)
    assert "No PDFs found in folder." in logged


def test_run_incremental_empty_folder_raises(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    logged = []
    with pytest.raises(NoPDFsFoundError):
        run_incremental(empty, tmp_path / "out", True, logged.append, jobs=1)
    assert "No PDFs found in folder." in logged


def test_run_full_dry_run_plans_without_copying(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)

    logged = []
    run_full(in_dir, out, True, logged.append, jobs=1)
    text = "\n".join(logged)

    assert "Found 3 active PDF(s)" in text
    assert "F10126106 Test Parent" in text
    assert "F10126107 Child part" in text
    assert "_orphans" in text
    assert "Would store 1 orphan(s) in _orphans/" in text
    assert "  F10126106 [Test Parent]" in text
    assert re.search(r"PDF copies written:\s+3", text)
    assert not out.exists() or not list(out.rglob("*.pdf"))


def test_run_full_execute_builds_tree_and_orphans(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)

    logged = []
    run_full(in_dir, out, False, logged.append, jobs=1)

    root_folder = out / "F10126106 Test Parent"
    assert (root_folder / "F10126106.pdf").is_file()
    assert (root_folder / "F10126107 Child part" / "F10126107.pdf").is_file()
    assert (out / "_orphans" / "F10126108.pdf").is_file()

    text = "\n".join(logged)
    assert re.search(r"PDFs scanned:\s+3", text)
    assert re.search(r"^  Root assemblies:\s+1", text, re.M)
    assert re.search(r"BOM edges kept:\s+1", text)
    assert re.search(r"Cycles broken:\s+0", text)
    assert re.search(r"PDF copies written:\s+3", text)
    assert "Stored 1 orphan(s) in _orphans/" in text


def test_run_incremental_adopts_stored_orphan(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)
    assert (out / "_orphans" / "F10126108.pdf").is_file()

    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST",
        "F10126108 STANDALONE PART",
        "NAME",
        "New assembly",
    ])

    logged = []
    run_incremental(in_dir, out, False, logged.append, jobs=1)

    new_root = out / "F10126109 New assembly"
    assert (new_root / "F10126109.pdf").is_file()
    assert (new_root / "F10126108 Standalone part" / "F10126108.pdf").is_file()
    assert not (out / "_orphans" / "F10126108.pdf").exists()

    assert list(out.rglob("F10126106.pdf")) == \
        [out / "F10126106 Test Parent" / "F10126106.pdf"]
    assert len(list(out.rglob("F10126107.pdf"))) == 1

    text = "\n".join(logged)
    assert re.search(r"New PDFs scanned:\s+2", text)
    assert re.search(r"PDF copies written:\s+2", text)
    assert "Retired 1 orphan copy(ies) from _orphans/ (adopted into tree)" in text


def _write_organized_newer_rev(make_pdf, out):
    folder = out / "F10126109 B Assy"
    folder.mkdir(parents=True)
    return make_pdf(folder / "F10126109_B.pdf", ["NAME", "Rev B assembly"])


def test_run_incremental_failed_archive_keeps_parked_orphan(tmp_path, make_pdf,
                                                            monkeypatch):
    # The parked _orphans/ PDF is the only copy of the older revision; the
    # top-level arrival supersedes the organized _B copy. A failed archive
    # copy must not retire the parked copy (regression: intent-based retire
    # deleted the last copy).
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_organized_newer_rev(make_pdf, out)
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    parked = make_pdf(orphans_dir / "F10126109.pdf", ["NAME", "Old rev"])
    make_pdf(in_dir / "F10126109.pdf", ["NAME", "Old rev"])
    make_pdf(in_dir / "F10126150.pdf", ["NAME", "Unrelated new part"])

    real_copy2 = fsops.shutil.copy2

    def fake_copy2(s, d):
        if Path(d).name == "F10126109.pdf":
            raise OSError("locked")
        return real_copy2(s, d)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []
    run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert parked.is_file()
    assert not (out / "_superseded" / "F10126109.pdf").exists()
    assert "WARNING: F10126109: copy failed" in "\n".join(logged)


def test_run_incremental_archived_superseded_orphan_retires(tmp_path, make_pdf):
    # Control for the failed-archive case: once the archive copy lands, the
    # parked copy is still retired (the normal flow is preserved).
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_organized_newer_rev(make_pdf, out)
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    parked = make_pdf(orphans_dir / "F10126109.pdf", ["NAME", "Old rev"])
    make_pdf(in_dir / "F10126109.pdf", ["NAME", "Old rev"])
    make_pdf(in_dir / "F10126150.pdf", ["NAME", "Unrelated new part"])

    logged = []
    run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert (out / "_superseded" / "F10126109.pdf").is_file()
    assert not parked.exists()
    assert "Retired 1 orphan copy(ies)" in "\n".join(logged)


def test_run_incremental_no_new_pdf_retires_archived_parked_orphan(tmp_path, make_pdf):
    # The only candidate is a parked orphan that is older than the organized
    # _B revision: it gets archived, then the no-new fast path must still
    # retire the parked copy instead of leaving it reported forever.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_organized_newer_rev(make_pdf, out)
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    parked = make_pdf(orphans_dir / "F10126109.pdf", ["NAME", "Old rev"])
    make_pdf(in_dir / "F10126109.pdf", ["NAME", "Old rev"])

    logged = []
    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert (out / "_superseded" / "F10126109.pdf").is_file()
    assert not parked.exists()
    assert "Retired 1 orphan copy(ies)" in "\n".join(logged)
    assert ctx["orphans"] == []


def test_run_incremental_dry_run_reports_planned_orphan_retirement(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)
    assert (out / "_orphans" / "F10126108.pdf").is_file()

    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST",
        "F10126108 STANDALONE PART",
        "NAME",
        "New assembly",
    ])

    logged = []
    run_incremental(in_dir, out, True, logged.append, jobs=1)

    text = "\n".join(logged)
    assert "[DRY-RUN] retire orphan copy:" in text
    assert "F10126108" in text
    # Dry-run touches nothing on disk.
    assert (out / "_orphans" / "F10126108.pdf").is_file()
    assert not (out / "F10126109 New assembly").exists()


def test_run_full_returns_structured_ctx(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)

    ctx = run_full(in_dir, out, True, lambda msg: None, jobs=1)

    assert set(ctx) == {"counters", "missing", "chk", "orphans", "roots",
                        "used_on_mismatches", "used_on_bugs",
                        "titleblock_mismatches", "scanned", "watermarks",
                        "names", "placement_refused", "skipped_roots"}
    assert ctx["counters"] == {"scanned": 3, "roots": 1, "copies": 3,
                               "cycles": 0, "warnings": 0}
    assert ctx["skipped_roots"] == []
    assert ctx["missing"] == []
    assert ctx["chk"] == []
    assert ctx["orphans"] == ["F10126108"]
    assert ctx["roots"] == [("F10126106", [])]
    assert ctx["used_on_mismatches"] == []
    assert ctx["used_on_bugs"] == []
    assert ctx["names"]["F10126106"] == "Test Parent"
    assert ctx["names"]["F10126107"] == "Child part"
    assert ctx["names"]["F10126108"] == "Standalone part"


def test_run_full_warnings_count_is_structured(tmp_path, make_pdf, monkeypatch):
    # Extraction issues are counted from the scan results, not by matching
    # log text: each page-capped PDF contributes exactly one warning.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    monkeypatch.setattr(extraction, "MAX_PDF_PAGES", 0)

    ctx = run_full(in_dir, out, True, lambda msg: None, jobs=1)

    assert ctx["counters"]["warnings"] == 2


def test_run_incremental_warnings_count_is_structured(tmp_path, make_pdf,
                                                      monkeypatch):
    # Organized scan + new-PDF scan each contribute their own issues.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_organized_newer_rev(make_pdf, out)
    _write_standalone(make_pdf, in_dir)
    monkeypatch.setattr(extraction, "MAX_PDF_PAGES", 0)

    ctx = run_incremental(in_dir, out, False, lambda msg: None, jobs=1)

    assert ctx["counters"]["warnings"] == 2


def test_run_incremental_returns_structured_ctx(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST",
        "F10126108 STANDALONE PART",
        "NAME",
        "New assembly",
    ])

    ctx = run_incremental(in_dir, out, False, lambda msg: None, jobs=1)

    assert set(ctx) == {"counters", "missing", "chk", "orphans", "roots",
                        "used_on_mismatches", "used_on_bugs",
                        "titleblock_mismatches", "scanned", "watermarks",
                        "names", "placement_refused", "skipped_roots"}
    assert ctx["counters"] == {"scanned": 2, "roots": 1, "copies": 2,
                               "cycles": 0, "warnings": 0}
    assert ctx["skipped_roots"] == []
    assert ctx["missing"] == []
    assert ctx["chk"] == []
    assert ctx["orphans"] == []
    assert ctx["roots"] == [("F10126109", [])]
    assert ctx["used_on_mismatches"] == [("F10126109", "F10126108", [])]
    assert ctx["used_on_bugs"] == []
    assert ctx["names"]["F10126108"] == "Standalone part"
    assert ctx["names"]["F10126109"] == "New assembly"


def test_run_incremental_per_run_cap_skips_only_oversized_roots(
        tmp_path, make_pdf, monkeypatch):
    # The cap is per run, not per place_files call, and it degrades
    # gracefully: two roots with two copies each exceed a cap of 3, so the
    # largest (stem order breaks the tie) is skipped and reported while the
    # other is still placed.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 SECOND CHILD", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert ctx["skipped_roots"] == [("F10126106", 2)]
    # The kept root landed in the tree; the skipped one never did.
    assert (out / "F10126109 Parent two" / "F10126109.pdf").is_file()
    assert (out / "F10126109 Parent two" / "F10126110 Second child"
            / "F10126110.pdf").is_file()
    assert not list(out.rglob("F10126106*.pdf"))
    text = "\n".join(logged)
    assert "placement refused for 1 oversized root(s), placing 1 of 2" in text
    assert "skipped oversized root F10126106 (2 copies planned)" in text
    assert "Roots skipped (oversized): 1" in text


def test_run_incremental_refused_before_supersede_swaps(tmp_path, make_pdf,
                                                         monkeypatch):
    # The cap is checked before _incremental_supersede: a refused batch must
    # not have swapped revisions or moved folders already. The refusal is
    # partial now (the kept root is still placed), but swaps stay skipped
    # while anything is over cap.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    organized_dir = out / "F10126106 Old parent"
    organized_dir.mkdir(parents=True)
    make_pdf(organized_dir / "F10126106.pdf", ["NAME", "Old parent"])
    # Input: the superseding revision A plus two roots (2 copies each).
    make_pdf(in_dir / "F10126106_A.pdf", ["NAME", "Old parent"])
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 CHILD PART", "NAME", "Parent one"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Child one"])
    make_pdf(in_dir / "F10126111.pdf", [
        "FERMI PART LIST", "F10126112 CHILD PART", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126112.pdf", ["NAME", "Child two"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert ctx["skipped_roots"] == [("F10126109", 2)]
    # No swap, no archive: the old organized copy stands. Bounded orphan
    # parking still runs under a partial refusal, so the superseding
    # revision waits in _orphans/ for a later run to swap it in.
    assert (organized_dir / "F10126106.pdf").is_file()
    assert (out / "_orphans" / "F10126106_A.pdf").is_file()
    assert not (out / "_superseded").exists()
    # The dropped root never landed, but the kept one still placed.
    assert not (out / "F10126109 Parent one").exists()
    assert (out / "F10126111 Parent two" / "F10126111.pdf").is_file()
    assert "supersede skipped" in "\n".join(logged)


def test_run_incremental_refused_dry_run_skips_orphan_retirement(tmp_path,
                                                                 make_pdf,
                                                                 monkeypatch):
    # A skipped adopter must not log orphan retirement for an adoption that
    # never happened (the dry-run planned set excludes skipped roots). Here
    # the adopter loses the cap tie-break, so the parked copy stays put.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    make_pdf(orphans_dir / "F10126108.pdf", ["NAME", "Parked part"])
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126108 PARKED PART", "NAME", "Adopter"])
    make_pdf(in_dir / "F10126110.pdf", [
        "FERMI PART LIST", "F10126111 CHILD PART", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126111.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 2)
    logged = []

    ctx = run_incremental(in_dir, out, True, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert "[DRY-RUN] retire orphan copy" not in "\n".join(logged)
    assert (orphans_dir / "F10126108.pdf").is_file()


def test_run_full_cap_skips_only_oversized_roots(tmp_path, make_pdf,
                                                   monkeypatch):
    # Full mode degrades the same way: the over-cap root is skipped and
    # reported while the rest of the tree is still built.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 SECOND CHILD", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    ctx = run_full(in_dir, out, True, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert ctx["skipped_roots"] == [("F10126106", 2)]
    text = "\n".join(logged)
    assert "placement refused for 1 oversized root(s), placing 1 of 2" in text
    assert "Roots skipped (oversized): 1" in text


def test_run_full_skipped_root_exclusive_child_reported_unplaced(
        tmp_path, make_pdf, monkeypatch):
    # Reachability for the unplaced report uses kept roots only: a child
    # exclusive to a skipped root is never copied, so it must surface in
    # "Unplaced PDFs" instead of vanishing from the report.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 SECOND CHILD", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    run_full(in_dir, out, True, logged.append, jobs=1)

    text = "\n".join(logged)
    # The skipped root itself plus its exclusive child: both PDFs are
    # missing from the tree, while the kept subtree is placed.
    assert "--- Unplaced PDFs (2) ---" in text
    block = text.split("--- Unplaced PDFs (2) ---")[1]
    assert "\n  F10126106\n" in block
    assert "\n  F10126107\n" in block
    assert "F10126110" not in block


def test_run_incremental_partial_dry_run_credits_kept_placements(
        tmp_path, make_pdf, monkeypatch):
    # Partial refusal still reports adoptions under kept roots: the adopter
    # wins the cap tie-break here, so its orphan adoption is planned.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    make_pdf(orphans_dir / "F10126108.pdf", ["NAME", "Parked part"])
    make_pdf(in_dir / "F10126113.pdf", [
        "FERMI PART LIST", "F10126108 PARKED PART", "NAME", "Adopter"])
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126110 CHILD PART", "NAME", "Parent two"])
    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Second child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    ctx = run_incremental(in_dir, out, True, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert ctx["skipped_roots"] == [("F10126109", 2)]
    assert "[DRY-RUN] retire orphan copy" in "\n".join(logged)
    assert (orphans_dir / "F10126108.pdf").is_file()


def test_run_incremental_full_refusal_places_and_parks_nothing(
        tmp_path, make_pdf, monkeypatch):
    # Cap below every root's count: all placeable roots drop, so nothing is
    # placed and standalone roots are not even parked - the old full-refusal
    # shape is preserved (only bounded archive copies may run).
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 1)
    logged = []

    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert ctx["placement_refused"] is True
    assert ctx["skipped_roots"] == [("F10126106", 2)]
    assert ctx["roots"] == []
    assert ctx["counters"]["roots"] == 0
    assert not any(out.rglob("*.pdf"))
    text = "\n".join(logged)
    assert "placing 0 of 1" in text
    assert "New graph roots:      0" in text


def test_run_incremental_precheck_ignores_phantom_cycle_fanout(
        tmp_path, make_pdf, monkeypatch):
    # A two-cycle explodes the unbroken path count past any small cap, but
    # the pre-check fits the cycle-broken graph (like placement does), so a
    # healthy cyclic pair is placed instead of refused. The break is logged
    # once, by the merged-graph pass - the silent pre-break adds no lines.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Cyclic parent"])
    make_pdf(in_dir / "F10126107.pdf", [
        "FERMI PART LIST", "F10126106 PARENT PART", "NAME", "Cyclic child"])
    monkeypatch.setattr("fermi_organizer.fsops.MAX_PLANNED_COPIES", 3)
    logged = []

    ctx = run_incremental(in_dir, out, True, logged.append, jobs=1)

    assert ctx["placement_refused"] is False
    assert ctx["skipped_roots"] == []
    assert ctx["counters"]["cycles"] == 1
    text = "\n".join(logged)
    assert text.count("CYCLE BREAK") == 1
    assert "supersede skipped" not in text


def test_run_full_rerun_excludes_own_output_tree(tmp_path, make_pdf):
    # A second full run against the same in-input output folder must not
    # re-index the previously placed tree as input (any custom name, not
    # just top-level "Output/"): no duplicate-stem noise, same plan.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = in_dir / "Organizer Output"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda m: None, jobs=1)

    logged = []
    ctx = run_full(in_dir, out, True, logged.append, jobs=1)

    text = "\n".join(logged)
    assert "duplicate stem(s)" not in text
    assert "under the output tree" in text
    assert ctx["counters"]["copies"] == 2
    assert ctx["skipped_roots"] == []


def test_run_incremental_no_new_pdfs_returns_ctx(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    ctx = run_incremental(in_dir, out, False, lambda msg: None, jobs=1)

    assert ctx["counters"] == {"scanned": 0, "roots": 0, "copies": 0,
                               "cycles": 0, "warnings": 0}
    assert ctx["missing"] == []
    assert ctx["orphans"] == []
    assert ctx["roots"] == []
    assert ctx["used_on_mismatches"] == []
    assert ctx["used_on_bugs"] == []
    assert ctx["names"] == {}


def test_run_full_watermarked_new_revision_is_used(tmp_path, make_pdf):
    # Full run: clean B and watermarked C in one input folder. Revision
    # ranking picks C (the watermark rule is same-revision only).
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106_B.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Part B"])
    make_pdf(in_dir / "F10126106_C.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Part C",
              "PRELIMINARY"])

    ctx = run_full(in_dir, out, False, lambda msg: None, jobs=1)

    live = [p for p in out.rglob("F10126106*.pdf")
            if p.parent.name != "_superseded"]
    assert len(live) == 1
    assert live[0].name == "F10126106_C.pdf"
    assert (out / "_superseded" / "F10126106_B.pdf").is_file()
    assert ctx["watermarks"] == [("F10126106_C", "watermark text: PRELIMINARY")]


def test_run_incremental_watermarked_new_revision_is_used(tmp_path, make_pdf):
    # A newer revision (C) that is watermarked still supersedes the clean B:
    # the watermark rule only chooses between copies of the SAME revision.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106_B.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Part B"])
    run_full(in_dir, out, False, lambda msg: None, jobs=1)
    assert (out / "F10126106 Part B" / "F10126106_B.pdf").is_file()

    make_pdf(in_dir / "F10126106_C.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Part C",
              "PRELIMINARY"])
    ctx = run_incremental(in_dir, out, False, lambda msg: None, jobs=1)

    live = [p for p in out.rglob("F10126106*.pdf")
            if p.parent.name != "_superseded"]
    assert len(live) == 1
    assert live[0].name == "F10126106_C.pdf"          # watermarked C used
    assert (out / "_superseded" / "F10126106_B.pdf").is_file()
    assert ctx["watermarks"] == [("F10126106_C", "watermark text: PRELIMINARY")]


def test_run_full_superseded_revision(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Base part"])
    make_pdf(in_dir / "F10126107_A.pdf", ["NAME", "Revision A part"])

    logged = []
    run_full(in_dir, out, False, logged.append, jobs=1)
    text = "\n".join(logged)

    assert "Found 1 superseded revision(s) - will be placed in _superseded/" in text
    assert (out / "_superseded" / "F10126107.pdf").is_file()
    assert sorted(p.name for p in (out / "_superseded").iterdir()) == \
        ["F10126107.pdf"]

    live = [p for p in out.rglob("F10126107*.pdf")
            if p.parent.name != "_superseded"]
    assert len(live) == 1
    assert live[0].name == "F10126107_A.pdf"


def test_run_full_coincident_used_on_is_not_a_bug(tmp_path, make_pdf):
    # Parent BOM lists the child AND the child's USED ON names the parent:
    # consistent, so no USED ON bug and no extra placement edge.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)

    logged = []
    ctx = run_full(in_dir, tmp_path / "out", True, logged.append, jobs=1)

    assert ctx["used_on_bugs"] == []
    assert "USED ON bugs" not in "\n".join(logged)


def test_run_full_used_on_bug_is_reported_not_placed(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    # P's BOM lists C; C's USED ON names Q; Q's BOM lists R (Q is a root).
    # C's USED ON (Q) is a bug: Q's BOM does not list C, so C is placed only
    # under P (BOM) and the USED ON mismatch is reported.
    make_pdf(in_dir / "F10126106.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent P"])
    make_pdf(in_dir / "F10126107.pdf",
             ["NAME", "Child C", "USED ON", "F10126108"])
    make_pdf(in_dir / "F10126108.pdf",
             ["FERMI PART LIST", "F10126109 LEAF PART", "NAME", "Parent Q"])
    make_pdf(in_dir / "F10126109.pdf", ["NAME", "Leaf R"])

    logged = []
    ctx = run_full(in_dir, tmp_path / "out", True, logged.append, jobs=1)
    text = "\n".join(logged)

    assert ctx["used_on_bugs"] == [("F10126108", "F10126107")]
    assert "USED ON bugs (1)" in text
    assert "F10126107: USED ON F10126108 but F10126108 BOM does not list it" in text


def test_run_full_reports_watermark(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_pdf(in_dir / "F10126106.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent",
              "PRELIMINARY"])
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part"])

    ctx = run_full(in_dir, tmp_path / "out", True, lambda msg: None, jobs=1)

    assert ctx["watermarks"] == [("F10126106", "watermark text: PRELIMINARY")]


def test_run_full_reports_scanned_pdf(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    doc = fitz.open()
    doc.new_page()
    doc.save(str(in_dir / "F10126108.pdf"))
    doc.close()

    ctx = run_full(in_dir, tmp_path / "out", True, lambda msg: None, jobs=1)

    assert ("F10126108", [1]) in ctx["scanned"]
    assert ctx["watermarks"] == []


def test_run_full_prefers_clean_same_revision_duplicate(tmp_path, make_pdf):
    # Same stem in two subfolders: the shallowest (a_wm, watermarked) wins the
    # index, but the clean copy must be used in the tree and the watermarked
    # one archived in _superseded/.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a_wm").mkdir()
    (in_dir / "z_clean").mkdir()
    wm = make_pdf(in_dir / "a_wm" / "F10126106.pdf",
                  ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent",
                   "PRELIMINARY"])
    clean = make_pdf(in_dir / "z_clean" / "F10126106.pdf",
                     ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent"])
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part"])

    out = tmp_path / "out"
    ctx = run_full(in_dir, out, False, lambda msg: None, jobs=1)

    placed = out / "F10126106 Parent" / "F10126106.pdf"
    assert placed.is_file()
    assert placed.read_bytes() == clean.read_bytes()
    archived = out / "_superseded" / "F10126106.pdf"
    assert archived.is_file()
    assert archived.read_bytes() == wm.read_bytes()
    assert ctx["watermarks"] == []


def test_run_full_prefers_text_copy_over_scanned_duplicate(tmp_path, make_pdf):
    # Both copies clean; the shallowest is image-only (blank = scanned). The
    # text-layer twin must win so BOM/NAME are extracted exactly - with OCR
    # off the scanned copy would yield no BOM and orphan the whole subtree.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a_scan").mkdir()
    (in_dir / "z_text").mkdir()
    make_pdf(in_dir / "a_scan" / "F10126106.pdf", [])
    text = make_pdf(in_dir / "z_text" / "F10126106.pdf",
                    ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Test Parent"])
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part", "USED ON", "F10126106"])

    out = tmp_path / "out"
    logged = []
    ctx = run_full(in_dir, out, False, logged.append, jobs=1)

    placed = out / "F10126106 Test Parent" / "F10126106.pdf"
    assert placed.is_file()
    assert placed.read_bytes() == text.read_bytes()
    # BOM came from the text twin: the child is placed, not orphaned.
    assert (out / "F10126106 Test Parent" / "F10126107 Child part" /
            "F10126107.pdf").is_file()
    # Losing clean copies stay ignored in the input - never archived.
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert not (out / "_orphans" / "F10126106.pdf").exists()
    assert ctx["watermarks"] == []
    assert "replaced by text-layer" in "\n".join(logged)


def test_run_full_watermark_clean_beats_text_layer(tmp_path, make_pdf):
    # Decision: watermark-clean dominates. The shallowest is a watermarked
    # text-layer copy, the loser is a clean scanned copy - clean wins even
    # though the text layer would extract better. Regression pin: a future
    # text-first ranking must not flip this.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a_wm_text").mkdir()
    (in_dir / "z_scan").mkdir()
    wm_text = make_pdf(in_dir / "a_wm_text" / "F10126106.pdf",
                       ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent",
                        "PRELIMINARY"])
    scanned = make_pdf(in_dir / "z_scan" / "F10126106.pdf", [])

    out = tmp_path / "out"
    logged = []
    ctx = run_full(in_dir, out, False, logged.append, jobs=1)

    # Watermarked text copy archived; clean scanned copy chosen - it has no
    # extractable BOM (OCR off in tests), so it parks in _orphans/.
    archived = out / "_superseded" / "F10126106.pdf"
    assert archived.is_file()
    assert archived.read_bytes() == wm_text.read_bytes()
    parked = out / "_orphans" / "F10126106.pdf"
    assert parked.is_file()
    assert parked.read_bytes() == scanned.read_bytes()
    assert not (out / "F10126106 Parent").exists()
    assert ctx["watermarks"] == []
    assert "replaced by non-watermarked" in "\n".join(logged)


def test_run_full_all_watermarked_prefers_text_over_scanned(tmp_path, make_pdf):
    # Nuance: NO clean copy exists - both are watermarked. Text-layer still
    # beats scanned within the watermarked tier (rank: watermark > text >
    # shallowest), and nothing is archived because no clean copy exists.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a_scan_wm").mkdir()
    (in_dir / "z_text_wm").mkdir()
    make_pdf(in_dir / "a_scan_wm" / "F10126106.pdf", ["PRELIMINARY"])
    text_wm = make_pdf(in_dir / "z_text_wm" / "F10126106.pdf",
                       ["FERMI PART LIST", "F10126107 CHILD PART", "NAME",
                        "Test Parent", "PRELIMINARY"])
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part", "USED ON", "F10126106"])

    out = tmp_path / "out"
    logged = []
    ctx = run_full(in_dir, out, False, logged.append, jobs=1)

    placed = out / "F10126106 Test Parent" / "F10126106.pdf"
    assert placed.is_file()
    assert placed.read_bytes() == text_wm.read_bytes()
    # All copies watermarked: best-ranked one used as-is, nothing archived.
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert ctx["watermarks"] == [("F10126106", "watermark text: PRELIMINARY")]
    assert "replaced by text-layer" in "\n".join(logged)


def test_run_full_uses_watermarked_when_no_clean_copy(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a_wm").mkdir()
    (in_dir / "b_wm").mkdir()
    wm1 = make_pdf(in_dir / "a_wm" / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent",
                    "PRELIMINARY"])
    make_pdf(in_dir / "b_wm" / "F10126106.pdf",
             ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent",
              "PRELIMINARY"])
    make_pdf(in_dir / "F10126107.pdf", ["NAME", "Child part"])

    out = tmp_path / "out"
    ctx = run_full(in_dir, out, False, lambda msg: None, jobs=1)

    placed = out / "F10126106 Parent" / "F10126106.pdf"
    assert placed.is_file()
    assert placed.read_bytes() == wm1.read_bytes()
    # No clean copy exists, so the watermarked one is used as-is and the
    # redundant duplicate is not archived.
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert ctx["watermarks"] == [("F10126106", "watermark text: PRELIMINARY")]


def test_run_incremental_prefers_clean_same_revision_duplicate(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    (in_dir / "a_wm").mkdir()
    (in_dir / "z_clean").mkdir()
    wm = make_pdf(in_dir / "a_wm" / "F10126108.pdf",
                  ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "New assy",
                   "PRELIMINARY"])
    clean = make_pdf(in_dir / "z_clean" / "F10126108.pdf",
                     ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "New assy"])

    ctx = run_incremental(in_dir, out, False, lambda msg: None, jobs=1)

    placed = out / "F10126108 New assy" / "F10126108.pdf"
    assert placed.is_file()
    assert placed.read_bytes() == clean.read_bytes()
    archived = out / "_superseded" / "F10126108.pdf"
    assert archived.is_file()
    assert archived.read_bytes() == wm.read_bytes()
    assert ctx["watermarks"] == []


def test_run_incremental_prefers_text_copy_over_scanned_duplicate(tmp_path, make_pdf):
    # Same resolution runs pre-extraction in incremental mode: the shallow
    # scanned copy must not source the new root's BOM.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    (in_dir / "a_scan").mkdir()
    (in_dir / "z_text").mkdir()
    make_pdf(in_dir / "a_scan" / "F10126110.pdf", [])
    text = make_pdf(in_dir / "z_text" / "F10126110.pdf",
                    ["FERMI PART LIST", "F10126111 NEW CHILD PART", "NAME",
                     "New assembly section"])
    make_pdf(in_dir / "F10126111.pdf", ["NAME", "Second child part"])

    logged = []
    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    root = out / "F10126110 New assembly section"
    assert (root / "F10126110.pdf").is_file()
    assert (root / "F10126110.pdf").read_bytes() == text.read_bytes()
    assert (root / "F10126111 Second child part" / "F10126111.pdf").is_file()
    assert not (out / "_orphans" / "F10126110.pdf").exists()
    assert not (out / "_superseded" / "F10126110.pdf").exists()
    assert "replaced by text-layer" in "\n".join(logged)
    assert ctx["watermarks"] == []


def test_run_incremental_parks_standalone_new_pdf(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    make_pdf(in_dir / "F10126110.pdf", ["NAME", "Loose part"])

    logged = []
    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert (out / "_orphans" / "F10126110.pdf").is_file()
    assert "F10126110" in ctx["orphans"]
    assert "storing in _orphans/" in "\n".join(logged)


def test_swap_revision_counts_only_actual_writes(tmp_path):
    new_pdf = tmp_path / "F10126107_A.pdf"
    new_pdf.write_bytes(b"new")

    # Archive target absent -> archive copy + replacement copy = 2.
    out1 = tmp_path / "out1"
    (out1 / "_superseded").mkdir(parents=True)
    old1 = out1 / "F10126107 Base part" / "F10126107.pdf"
    old1.parent.mkdir()
    old1.write_bytes(b"old")
    _moved, copies = _swap_revision_files([old1], new_pdf, out1 / "_superseded",
                                          out1, False, lambda msg: None)
    assert copies == 2
    assert (out1 / "_superseded" / "F10126107.pdf").read_bytes() == b"old"

    # Archive target already present with identical bytes -> only the
    # replacement copy is written (different bytes suffix instead: C-001).
    out2 = tmp_path / "out2"
    sup2 = out2 / "_superseded"
    sup2.mkdir(parents=True)
    (sup2 / "F10126107.pdf").write_bytes(b"old")
    old2 = out2 / "F10126107 Base part" / "F10126107.pdf"
    old2.parent.mkdir()
    old2.write_bytes(b"old")
    _moved, copies = _swap_revision_files([old2], new_pdf, sup2,
                                          out2, False, lambda msg: None)
    assert copies == 1
    assert (old2.parent / "F10126107_A.pdf").read_bytes() == b"new"
    assert (sup2 / "F10126107.pdf").read_bytes() == b"old"


def test_run_full_processes_hyphen_named_standalone(tmp_path, make_pdf):
    # Real-world export names use hyphens ('F10126108---DWG1'); the canonical
    # stem must reach the context and the parked orphan copy.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126108---DWG1.pdf", ["NAME", "Hyphen part"])

    ctx = run_full(in_dir, out, False, lambda msg: None, jobs=1)

    assert (out / "_orphans" / "F10126108---DWG1.pdf").is_file()
    assert ctx["orphans"] == ["F10126108___DWG1"]


def test_run_full_reports_titleblock_mismatches(tmp_path, make_pdf):
    # The drawing's own title block is authoritative: flag stale filename
    # revisions, misnamed files (wrong drawing number), and wrong titles.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106_A_DWG1.pdf", [
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "B",
        "NUMBER",
        "F10126107",
        "SHEET 1 OF 1",
    ])
    make_pdf(in_dir / "F10126108 KIT, SOMETHING ELSE.pdf", [
        "NAME",
        "Completely Different",
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "-",
        "NUMBER",
        "F10126108",
        "SHEET 1 OF 1",
    ])

    logged = []
    ctx = run_full(in_dir, out, True, logged.append, jobs=1)

    mism = ctx["titleblock_mismatches"]
    assert ("F10126106_A_DWG1", "number", "F10126106", "F10126107") in mism
    assert ("F10126106_A_DWG1", "revision", "A", "B") in mism
    assert ("F10126108", "name", "KIT, SOMETHING ELSE",
            "Completely Different") in mism
    text = "\n".join(logged)
    assert "Title block check (3 mismatch(es))" in text
    assert "F10126106_A_DWG1: revision: filename 'A', title block 'B'" in text


def test_rekey_misnamed_pure_rules():
    from fermi_organizer.runmodes import _rekey_misnamed

    def run(index, titleblocks, stems):
        logged = []
        out = _rekey_misnamed(index, {}, {}, titleblocks, stems, logged.append)
        return out, logged

    # Misnamed text PDF: re-keyed to the title-block number, adopting its rev.
    index = {"F10126106___DWG1": "a.pdf"}
    tbs = {"F10126106___DWG1": ("F10126106___DWG1", "F10126107", "B", None, False)}
    (index2, _bom, _wm, tbs2, rekeyed), logged = run(index, tbs, set(index))
    assert index2 == {"F10126107_B_DWG1": "a.pdf"}
    assert rekeyed == {"F10126106___DWG1": "F10126107_B_DWG1"}
    assert tbs2["F10126107_B_DWG1"][1] == "F10126107"

    # Scanned PDFs are never re-keyed (OCR number reads are not trusted).
    index = {"F10126106___DWG1": "a.pdf"}
    tbs = {"F10126106___DWG1": ("F10126106___DWG1", "F10126107", "B", None, True)}
    (index2, _b, _w, _t, rekeyed), logged = run(index, tbs, set(index))
    assert index2 == {"F10126106___DWG1": "a.pdf"} and rekeyed == {}

    # The title-block drawing already exists -> keep the filename (report only).
    index = {"F10126106___DWG1": "a.pdf", "F10126107": "b.pdf"}
    tbs = {"F10126106___DWG1": ("F10126106___DWG1", "F10126107", "B", None, False)}
    (index2, _b, _w, _t, rekeyed), logged = run(index, tbs, set(index))
    assert index2 == index and rekeyed == {}
    assert any("keeping filename" in m for m in logged)


def test_run_full_rekey_titleblock_places_under_real_number(tmp_path, make_pdf):
    # The title block is authoritative when --rekey-titleblock is set: the
    # misnamed file is placed under its real drawing number.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106_A_DWG1.pdf", [
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "B",
        "NUMBER",
        "F10126107",
        "SHEET 1 OF 1",
    ])

    logged = []
    ctx = run_full(in_dir, out, False, logged.append, jobs=1, rekey=True)

    assert "misnamed - re-keyed to F10126107_B_DWG1" in "\n".join(logged)
    assert any("F10126107_B_DWG1" in p.name for p in out.rglob("*.pdf"))
    assert not any("F10126106" in p.name for p in out.rglob("*.pdf"))
    # The number/revision disagreements are resolved by the re-key.
    assert not any(m[0] == "F10126106_A_DWG1" for m in ctx["titleblock_mismatches"])


def test_run_incremental_rekey_titleblock_places_under_real_number(tmp_path, make_pdf):
    # The incremental path must wire rekey=True the same way: a NEW misnamed
    # text PDF arriving into an existing tree is re-keyed before extraction
    # and placed (renamed) under its title-block number.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    make_pdf(in_dir / "F10126109_A_DWG1.pdf", [
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "B",
        "NUMBER",
        "F10126110",
        "SHEET 1 OF 1",
    ])

    logged = []
    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1, rekey=True)

    text = "\n".join(logged)
    assert "misnamed - re-keyed to F10126110_B_DWG1" in text
    assert any("F10126110_B_DWG1" in p.name for p in out.rglob("*.pdf"))
    assert not any("F10126109" in p.name for p in out.rglob("*.pdf"))
    assert not any(m[0] == "F10126109_A_DWG1" for m in ctx["titleblock_mismatches"])


def test_log_summary():
    logged = []
    rows = [
        "--- Summary ---",
        "  PDFs scanned:        5",
        "  Root assemblies:     1",
        "  PDF copies written:  5",
    ]
    _log_summary(rows, logged.append)
    assert logged == rows

    empty_logged = []
    _log_summary([], empty_logged.append)
    assert empty_logged == []

    gen_logged = []
    _log_summary((r for r in ["row1", "row2"]), gen_logged.append)
    assert gen_logged == ["row1", "row2"]

    mixed_logged = []
    _log_summary(["Header", 123, ("Tuple", 1)], mixed_logged.append)
    assert mixed_logged == ["Header", 123, ("Tuple", 1)]


def test_log_unplaced_none_unplaced():
    logged = []
    roots = ["F10126106"]
    children = {"F10126106": {"F10126107"}}
    index = {"F10126106": "p1.pdf", "F10126107": "p2.pdf", "F10126108": "p3.pdf"}
    orphans = ["F10126108"]

    _log_unplaced(roots, children, index, orphans, logged.append)
    assert logged == []


def test_log_unplaced_with_unplaced_items():
    logged = []
    roots = ["F10126106"]
    children = {"F10126106": {"F10126107"}}
    index = {
        "F10126106": "p1.pdf",
        "F10126107": "p2.pdf",
        "F10126108": "p3.pdf",
        "F10126109": "p4.pdf",
        "F10126110": "p5.pdf",
    }
    orphans = ["F10126108"]

    _log_unplaced(roots, children, index, orphans, logged.append)

    text = "\n".join(logged)
    assert "--- Unplaced PDFs (2) ---" in text
    assert "  F10126109" in text
    assert "  F10126110" in text


def test_log_unplaced_handles_multiple_unplaced_sorted():
    logged = []
    roots = []
    children = {}
    index = {"F10126110": "a.pdf", "F10126108": "b.pdf", "F10126109": "c.pdf"}
    orphans = []

    _log_unplaced(roots, children, index, orphans, logged.append)

    assert logged == [
        "--- Unplaced PDFs (3) ---",
        "  F10126108",
        "  F10126109",
        "  F10126110",
        "",
    ]

def _write_organized_nested_child(make_pdf, out):
    # Organized tree: parent folder holding its PDF plus a nested child
    # folder (the shared-leaf shape: F10128174 under F10187622).
    parent_dir = out / "F10126106 Old parent"
    parent_dir.mkdir(parents=True)
    make_pdf(parent_dir / "F10126106.pdf", ["NAME", "Old parent"])
    child_dir = parent_dir / "F10126107 Child"
    child_dir.mkdir()
    make_pdf(child_dir / "F10126107.pdf", ["NAME", "Child part"])
    return parent_dir, child_dir


def test_place_above_copies_nested_shared_child_to_every_claimant(
        tmp_path, make_pdf):
    # A child nested under a live parent must be COPIED, not moved: the
    # source parent keeps its copy and every new claimant gets one (the
    # F10128174 adoption that hollowed out F10187622 moved instead).
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    parent_dir, child_dir = _write_organized_nested_child(make_pdf, out)
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Higher one"])
    make_pdf(in_dir / "F10126111.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Higher two"])
    logged = []

    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert ctx["placement_refused"] is False
    # Source intact, both claimants served.
    assert (child_dir / "F10126107.pdf").is_file()
    assert (out / "F10126109 Higher one" / "F10126107 Child"
            / "F10126107.pdf").is_file()
    assert (out / "F10126111 Higher two" / "F10126107 Child"
            / "F10126107.pdf").is_file()
    assert "already moved, skipping" not in "\n".join(logged)


def test_place_above_moves_top_level_root_adoption(tmp_path, make_pdf):
    # A top-level organized root claimed by a new parent is still re-homed
    # (moved): leaving the old root folder behind would strand a stale root.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    root_dir = out / "F10126107 Child"
    root_dir.mkdir()
    make_pdf(root_dir / "F10126107.pdf", ["NAME", "Child part"])
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Higher assembly"])
    logged = []

    ctx = run_incremental(in_dir, out, False, logged.append, jobs=1)

    assert ctx["placement_refused"] is False
    assert not root_dir.exists()
    assert (out / "F10126109 Higher assembly" / "F10126107 Child"
            / "F10126107.pdf").is_file()


def test_place_above_organized_children_mkdir_oserror(tmp_path, make_pdf):
    # Test uncaught OSError handling when creating target_folder in _place_above_organized_children.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    # Add a new higher-level assembly referencing the organized child F10126107.
    make_pdf(in_dir / "F10126109.pdf", [
        "FERMI PART LIST",
        "F10126107 CHILD PART",
        "NAME",
        "Higher assembly",
    ])

    logged = []
    original_mkdir = Path.mkdir

    def mock_mkdir(self, *args, **kwargs):
        if "F10126109" in str(self):
            raise OSError("Mocked permission error")
        return original_mkdir(self, *args, **kwargs)

    with patch.object(Path, "mkdir", autospec=True, side_effect=mock_mkdir):
        run_incremental(in_dir, out, False, logged.append, jobs=1)

    text = "\n".join(logged)
    assert "WARNING: F10126109: mkdir failed" in text
    assert "skipping child moves" in text


def test_titleblock_mismatches_pure_rules():
    logged = []

    # 1. Empty titleblocks dict -> returns empty list, logs nothing
    res = _titleblock_mismatches({}, logged.append)
    assert res == []
    assert logged == []

    # 2. Perfect match -> returns empty list, logs nothing
    tb_match = {
        "F10126106_A_BRACKET": (
            "F10126106_A BRACKET ASSEMBLY",
            "F10126106",
            "A",
            "BRACKET ASSEMBLY",
            False,
        )
    }
    res = _titleblock_mismatches(tb_match, logged.append)
    assert res == []
    assert logged == []

    # 3. Number mismatch only
    tb_num = {
        "F10126106_A": (
            "F10126106_A",
            "F10126107",
            "A",
            "SOME PART",
            False,
        )
    }
    res = _titleblock_mismatches(tb_num, logged.append)
    assert res == [("F10126106_A", "number", "F10126106", "F10126107")]
    assert any("Title block check (1 mismatch(es))" in line for line in logged)
    assert any("F10126106_A: number: filename 'F10126106', title block 'F10126107'" in line for line in logged)

    # 4. Revision mismatches:
    # 4a. Filename revision 'A', title block revision 'B'
    tb_rev1 = {
        "F10126106_A": (
            "F10126106_A",
            "F10126106",
            "B",
            None,
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_rev1, logged.append)
    assert res == [("F10126106_A", "revision", "A", "B")]

    # 4b. Filename no revision, title block revision 'A'
    tb_rev2 = {
        "F10126106": (
            "F10126106",
            "F10126106",
            "A",
            None,
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_rev2, logged.append)
    assert res == [("F10126106", "revision", "-", "A")]

    # 4c. Filename revision 'A', title block revision '-'
    tb_rev3 = {
        "F10126106_A": (
            "F10126106_A",
            "F10126106",
            "-",
            None,
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_rev3, logged.append)
    assert res == [("F10126106_A", "revision", "A", "-")]

    # 4d. Title block revision is None -> no revision mismatch
    tb_rev4 = {
        "F10126106_A": (
            "F10126106_A",
            "F10126106",
            None,
            None,
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_rev4, logged.append)
    assert res == []

    # 4e. Both dash / empty revision -> no mismatch
    tb_rev5 = {
        "F10126106": (
            "F10126106",
            "F10126106",
            "-",
            None,
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_rev5, logged.append)
    assert res == []

    # 5. Name mismatch (no overlapping tokens) vs match (overlapping tokens)
    # 5a. Disjoint name tokens
    tb_name1 = {
        "F10126108": (
            "F10126108 KIT, SOMETHING ELSE",
            "F10126108",
            "-",
            "COMPLETELY DIFFERENT",
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_name1, logged.append)
    assert res == [("F10126108", "name", "KIT, SOMETHING ELSE", "COMPLETELY DIFFERENT")]

    # 5b. Overlapping name tokens
    tb_name2 = {
        "F10126108": (
            "F10126108 BRACKET ASSEMBLY",
            "F10126108",
            "-",
            "ASSEMBLY FRAME",
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_name2, logged.append)
    assert res == []

    # 5c. Filename title tail empty -> no name mismatch
    tb_name3 = {
        "F10126108": (
            "F10126108",
            "F10126108",
            "-",
            "SOME TITLE",
            False,
        )
    }
    logged.clear()
    res = _titleblock_mismatches(tb_name3, logged.append)
    assert res == []

    # 6. Sorting order with multiple stems and multiple mismatch types
    tb_multi = {
        "F10126108": (
            "F10126108 WRONG NAME",
            "F10126108",
            "-",
            "OTHER TITLE",
            False,
        ),
        "F10126106_A": (
            "F10126106_A",
            "F10126107",
            "B",
            None,
            False,
        ),
    }
    logged.clear()
    res = _titleblock_mismatches(tb_multi, logged.append)
    expected = [
        ("F10126106_A", "number", "F10126106", "F10126107"),
        ("F10126106_A", "revision", "A", "B"),
        ("F10126108", "name", "WRONG NAME", "OTHER TITLE"),
    ]
    assert res == expected
    text = "\n".join(logged)
    assert "Title block check (3 mismatch(es))" in text


def test_run_modes_sweep_leftover_supersede_staging(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    _write_parent(make_pdf, in_dir)
    _write_child(make_pdf, in_dir)
    _write_standalone(make_pdf, in_dir)
    run_full(in_dir, out, False, lambda msg: None, jobs=1)

    stale = out / "F10126106 Test Parent" / "F10126106.pdf.supersede_tmp.4242"
    stale.write_bytes(b"partial")

    # Dry-run reports the leftover but must not touch the tree.
    logged = []
    run_full(in_dir, out, True, logged.append, jobs=1)
    assert stale.is_file()
    assert any("[DRY-RUN] remove leftover supersede staging" in m for m in logged)

    # A real run clears it (incremental path wired the same way).
    logged.clear()
    run_incremental(in_dir, out, False, logged.append, jobs=1)
    assert not stale.exists()
    assert any("removed leftover supersede staging" in m for m in logged)
