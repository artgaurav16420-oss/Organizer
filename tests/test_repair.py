"""Repair mode: reconciling an existing tree with the plan its own PDFs imply.

Covers the fsops reconcile pass (move, prune, refuse) and the run_repair mode
(no-op on a clean tree, wrongly parked orphans, wrongly archived revisions,
damaged trees converging on a full-run plan).
"""
import shutil
from pathlib import Path

import pytest

from fermi_organizer.fsops import (_folder_removable, reconcile_tree,
                                   remove_empty_dirs)
from fermi_organizer.runmodes import run_full, run_incremental, run_repair
from tests.test_mode_parity import _live_paths


def _clean_tree(tmp_path, make_pdf):
    """A two-drawing tree: parent's parts list names the child."""
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    make_pdf(src / "F10197062.pdf", ["NAME", "Child part"])
    out = tmp_path / "out"
    run_full(src, out, False, lambda m: None, jobs=1)
    return src, out


# --- fsops.reconcile_tree -------------------------------------------------

def test_remove_empty_dirs_keeps_system_and_root(tmp_path):
    root = tmp_path / "out"
    (root / "A" / "B").mkdir(parents=True)
    (root / "A" / "C").mkdir()
    (root / "_orphans" / "X").mkdir(parents=True)
    (root / "A" / "keep.pdf").write_bytes(b"%PDF-1.4\n")
    assert remove_empty_dirs(root, False, lambda m: None) == 2
    assert not (root / "A" / "B").exists()
    assert not (root / "A" / "C").exists()
    assert (root / "_orphans" / "X").exists()
    assert root.is_dir()


def test_folder_removable_only_with_identical_copies(tmp_path):
    root = tmp_path / "out"
    dup, good = root / "OLD", root / "A" / "NEW"
    dup.mkdir(parents=True)
    good.mkdir(parents=True)
    (dup / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    (good / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    planned = {"F10197062": [("A", "NEW")]}
    assert _folder_removable(dup, planned, root) == (True, "")

    (good / "F10197062.pdf").write_bytes(b"%PDF-1.4\nDIFFERENT")
    ok, reason = _folder_removable(dup, planned, root)
    assert not ok
    assert "no identical copy" in reason

    (good / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    (dup / "notes.txt").write_text("keep me")
    ok, reason = _folder_removable(dup, planned, root)
    assert not ok
    assert "non-PDF" in reason


def test_folder_removable_does_not_vouch_for_its_own_copy(tmp_path):
    # A drawing already at its planned path must not count as its own twin:
    # the stale folder then holds a correctly placed child, and removing it
    # would delete that drawing's only copy.
    root = tmp_path / "out"
    stale_child = root / "STALE" / "F10197063"
    stale_child.mkdir(parents=True)
    (stale_child / "F10197063.pdf").write_bytes(b"%PDF-1.4\nonly copy")
    (root / "STALE" / "F10197062.pdf").write_bytes(b"%PDF-1.4\nstale")
    (root / "KEEP").mkdir()
    (root / "KEEP" / "F10197062.pdf").write_bytes(b"%PDF-1.4\nstale")
    organized = {"F10197062": [root / "STALE" / "F10197062.pdf",
                               root / "KEEP" / "F10197062.pdf"],
                 "F10197063": [stale_child / "F10197063.pdf"]}
    planned = {"F10197062": [("KEEP",)],
               "F10197063": [("STALE", "F10197063")]}

    counts, review = reconcile_tree(organized, planned, root, False,
                                    lambda m: None, {})

    assert counts["removed"] == 0
    assert (stale_child / "F10197063.pdf").exists()
    assert any("no identical copy" in line for line in review)


def test_folder_removable_refuses_a_symlinked_planned_target(tmp_path):
    # A planned target that is a symlink to an outside file is not an
    # independent in-tree copy, so it cannot justify deleting the duplicate.
    root = tmp_path / "out"
    dup, good = root / "OLD", root / "A" / "NEW"
    dup.mkdir(parents=True)
    good.mkdir(parents=True)
    (dup / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.4\nsame")
    try:
        (good / "F10197062.pdf").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation not permitted")

    ok, reason = _folder_removable(dup, {"F10197062": [("A", "NEW")]}, root)

    assert not ok
    assert "no identical copy" in reason


def test_folder_removable_keeps_a_symlink_entry(tmp_path):
    # A symlink is a manual entry even when it is broken; is_file() alone
    # would skip it and the folder would be removed around it.
    root = tmp_path / "out"
    dup, good = root / "OLD", root / "A" / "NEW"
    dup.mkdir(parents=True)
    good.mkdir(parents=True)
    (dup / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    (good / "F10197062.pdf").write_bytes(b"%PDF-1.4\nsame")
    try:
        (dup / "manual-link").symlink_to(dup / "does-not-exist")
    except OSError:
        pytest.skip("symlink creation not permitted")
    ok, reason = _folder_removable(dup, {"F10197062": [("A", "NEW")]}, root)
    assert not ok
    assert "non-PDF" in reason


def test_reconcile_moves_a_misplaced_folder(tmp_path):
    root = tmp_path / "out"
    (root / "A" / "F1").mkdir(parents=True)
    (root / "A" / "F1" / "F1.pdf").write_bytes(b"%PDF-1.4\nchild")
    (root / "WRONG" / "F2").mkdir(parents=True)
    (root / "WRONG" / "F2" / "F2.pdf").write_bytes(b"%PDF-1.4\nchild2")
    organized = {"F1": [root / "A" / "F1" / "F1.pdf"],
                 "F2": [root / "WRONG" / "F2" / "F2.pdf"]}
    planned = {"F1": [("A", "F1")], "F2": [("A", "F2")]}
    moved_dirs = {}
    logs = []

    counts, review = reconcile_tree(organized, planned, root, False,
                                    logs.append, moved_dirs)
    assert counts["moved"] == 1
    assert review == []
    assert (root / "A" / "F2" / "F2.pdf").exists()
    assert not (root / "WRONG" / "F2").exists()
    # The emptied parent is swept by the mode's post-reconcile pass.
    assert remove_empty_dirs(root, False, lambda m: None) == 1
    assert not (root / "WRONG").exists()
    # A second pass has nothing left to do.
    counts2, review2 = reconcile_tree(organized, planned, root, False,
                                      logs.append, moved_dirs)
    assert counts2 == {"moved": 0, "copied": 0, "removed": 0, "review": 0}
    assert review2 == []


def test_reconcile_removes_a_verified_duplicate_only(tmp_path):
    root = tmp_path / "out"
    for name in ("KEEP", "DUP"):
        d = root / name / "F10197062"
        d.mkdir(parents=True)
        (d / "F10197062.pdf").write_bytes(b"%PDF-1.4\nduplicate bytes")
    organized = {"F10197062": [root / "KEEP" / "F10197062" / "F10197062.pdf",
                               root / "DUP" / "F10197062" / "F10197062.pdf"]}
    planned = {"F10197062": [("KEEP", "F10197062")]}
    counts, review = reconcile_tree(organized, planned, root, False,
                                    lambda m: None, {})
    assert counts["removed"] == 1
    assert review == []
    assert not (root / "DUP" / "F10197062").exists()
    assert (root / "KEEP" / "F10197062" / "F10197062.pdf").exists()


def test_reconcile_keeps_a_differing_duplicate_for_review(tmp_path):
    root = tmp_path / "out"
    for name, payload in (("KEEP", b"one"), ("DUP", b"two")):
        d = root / name / "F10197062"
        d.mkdir(parents=True)
        (d / "F10197062.pdf").write_bytes(b"%PDF-1.4\n" + payload)
    organized = {"F10197062": [root / "KEEP" / "F10197062" / "F10197062.pdf",
                               root / "DUP" / "F10197062" / "F10197062.pdf"]}
    planned = {"F10197062": [("KEEP", "F10197062")]}
    counts, review = reconcile_tree(organized, planned, root, False,
                                    lambda m: None, {})
    assert counts["removed"] == 0
    assert len(review) == 1 and "no identical copy" in review[0]
    assert (root / "DUP" / "F10197062" / "F10197062.pdf").exists()


def test_reconcile_rehomes_a_root_and_refuses_a_long_path(tmp_path):
    root = tmp_path / "out"
    (root / "F10197062").mkdir(parents=True)
    (root / "F10197062" / "F10197062.pdf").write_bytes(b"%PDF-1.4\nroot")
    organized = {"F10197062": [root / "F10197062" / "F10197062.pdf"]}
    counts, review = reconcile_tree(organized, {"F10197062": [("A", "F10197062")]},
                                    root, False, lambda m: None, {})
    assert counts["moved"] == 1 and review == []
    assert (root / "A" / "F10197062" / "F10197062.pdf").exists()
    assert not (root / "F10197062").exists()

    # A target whose deepest file path would exceed MAX_PATH is refused, not
    # half-moved.
    (root / "B" / "SHORT").mkdir(parents=True)
    (root / "B" / "SHORT" / "F10197063.pdf").write_bytes(b"%PDF-1.4\nlong")
    organized = {"F10197063": [root / "B" / "SHORT" / "F10197063.pdf"]}
    planned = {"F10197063": [("B", "X" * 100, "Y" * 100, "F10197063")]}
    counts, review = reconcile_tree(organized, planned, root, False,
                                    lambda m: None, {})
    assert counts["moved"] == 0
    # A refused move must reach the review list: otherwise the CLI exits 0
    # with the tree still off plan.
    assert len(review) == 1 and "refused" in review[0]
    assert (root / "B" / "SHORT" / "F10197063.pdf").exists()


# --- run_repair -----------------------------------------------------------

def test_reconcile_refuses_a_move_when_the_source_cannot_be_listed(tmp_path,
                                                                   monkeypatch):
    # longest_dest_path returns None when the subtree cannot be listed; moving
    # anyway would skip the MAX_PATH guard, so the move is refused and
    # reported instead.
    from fermi_organizer import fsops
    root = tmp_path / "out"
    (root / "WRONG" / "F2").mkdir(parents=True)
    (root / "WRONG" / "F2" / "F2.pdf").write_bytes(b"%PDF-1.4\nchild")
    organized = {"F2": [root / "WRONG" / "F2" / "F2.pdf"]}
    monkeypatch.setattr(fsops, "longest_dest_path", lambda target, folder: None)

    counts, review = reconcile_tree(organized, {"F2": [("A", "F2")]}, root,
                                    False, lambda m: None, {})

    assert counts["moved"] == 0
    assert any("refused" in line for line in review)
    assert (root / "WRONG" / "F2" / "F2.pdf").exists()


def test_run_repair_is_a_no_op_on_a_clean_tree(tmp_path, make_pdf):
    _src, out = _clean_tree(tmp_path, make_pdf)
    lines = []
    ctx = run_repair(out, False, lines.append, jobs=1)

    assert ctx["repair"]["moved"] == 0
    assert ctx["repair"]["copied"] == 0
    assert ctx["repair"]["removed"] == 0
    assert ctx["repair"]["renamed"] == 0
    assert ctx["repair"]["adopted"] == 0
    assert ctx["repair"]["promoted"] == 0
    assert ctx["repair"]["review"] == []
    assert ctx["repair_required"] is False
    assert not any("WARNING" in line for line in lines)
    # Repair wording, not the incremental "new PDFs arrived" summary.
    assert any("Candidates examined:" in line for line in lines)
    assert not any("New PDFs scanned:" in line for line in lines)
    # The report still describes the whole tree, exactly like a full run.
    assert ctx["roots"] == [("F10126999", [])]


def test_run_repair_dry_run_changes_nothing(tmp_path, make_pdf):
    _src, out = _clean_tree(tmp_path, make_pdf)
    child = next(out.rglob("F10197062.pdf"))
    shutil.move(str(child.parent), str(out / "WRONG PLACE"))
    before = _live_paths(out)

    ctx = run_repair(out, True, lambda m: None, jobs=1)
    assert ctx["repair"]["moved"] == 1        # planned, not performed
    assert _live_paths(out) == before


def test_repair_adopts_a_wrongly_parked_orphan(tmp_path, make_pdf):
    _src, out = _clean_tree(tmp_path, make_pdf)
    child = next(out.rglob("F10197062.pdf"))
    orphans = out / "_orphans"
    orphans.mkdir(exist_ok=True)
    shutil.move(str(child.parent), str(orphans / child.parent.name))
    in_tree = [p for p in out.rglob("F10197062.pdf")
               if not p.relative_to(out).parts[0].startswith("_")]
    assert not in_tree

    lines = []
    ctx = run_repair(out, False, lines.append, jobs=1)
    assert ctx["repair"]["adopted"] == 1
    assert ctx["repair"]["review"] == []
    live = [p for p in out.rglob("F10197062.pdf")
            if not p.relative_to(out).parts[0].startswith("_")]
    assert len(live) == 1
    assert live[0].parent.parent.name.startswith("F10126999")
    # The parked copy is retired once the drawing is back in the tree. The
    # parked folder was moved in, so the check must recurse.
    assert not list(orphans.rglob("*.pdf"))


def test_repair_promotes_an_archived_newer_revision(tmp_path, make_pdf):
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    make_pdf(src / "F10197062.pdf", ["NAME", "Older revision"])
    out = tmp_path / "out"
    run_full(src, out, False, lambda m: None, jobs=1)
    assert [p.name for p in out.rglob("F10197062*.pdf")] == ["F10197062.pdf"]

    # An older run archived a newer revision under the archive's prefixed name.
    sup = out / "_superseded"
    sup.mkdir(exist_ok=True)
    make_pdf(tmp_path / "staged.pdf", ["NAME", "Newer revision"])
    shutil.copy2(tmp_path / "staged.pdf", sup / "1.1.2.0 F10197062-B.pdf")

    lines = []
    ctx = run_repair(out, False, lines.append, jobs=1)
    assert ctx["repair"]["promoted"] == 1
    assert ctx["repair"]["review"] == []
    live = {Path(p).name.lower() for p in _live_paths(out)}
    assert "f10197062_b.pdf" in live          # canonical name, no archive prefix
    assert "f10197062.pdf" not in live
    # The promoted source is gone from the archive; the demoted copy is in it,
    # so nothing was lost.
    assert not (sup / "1.1.2.0 F10197062-B.pdf").exists()
    archived = sorted(p.name for p in sup.glob("*.pdf"))
    assert archived and all("F10197062" in name for name in archived)

    # Idempotent: the swap is stable and the archive is not re-opened.
    again = run_repair(out, False, lambda m: None, jobs=1)
    assert again["repair"]["promoted"] == 0
    assert again["repair"]["review"] == []


def test_repair_keeps_an_archived_chk_out_of_the_tree(tmp_path, make_pdf):
    # An unapproved CHK never displaces an approved drawing, even when its
    # revision letter is higher.
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    make_pdf(src / "F10197062.pdf", ["NAME", "Approved, no revision"])
    out = tmp_path / "out"
    run_full(src, out, False, lambda m: None, jobs=1)

    sup = out / "_superseded"
    sup.mkdir(exist_ok=True)
    make_pdf(tmp_path / "chk.pdf", ["NAME", "Unapproved"])
    shutil.copy2(tmp_path / "chk.pdf", sup / "1.1.2.0 F10197062-B-CHK.pdf")

    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["promoted"] == 0
    assert (sup / "1.1.2.0 F10197062-B-CHK.pdf").exists()
    live = {Path(p).name.lower() for p in _live_paths(out)}
    assert "f10197062.pdf" in live


def test_repair_promotes_only_the_top_archived_revision(tmp_path, make_pdf):
    # Two archived revisions of one base: only the highest is promoted, and the
    # loser stays exactly where it is (no second archive entry under a new name).
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    make_pdf(src / "F10197062.pdf", ["NAME", "Live revision"])
    out = tmp_path / "out"
    run_full(src, out, False, lambda m: None, jobs=1)

    sup = out / "_superseded"
    sup.mkdir(exist_ok=True)
    make_pdf(tmp_path / "b.pdf", ["NAME", "Revision B"])
    make_pdf(tmp_path / "c.pdf", ["NAME", "Revision C"])
    shutil.copy2(tmp_path / "b.pdf", sup / "1.1.2.0 F10197062-B.pdf")
    shutil.copy2(tmp_path / "c.pdf", sup / "1.1.3.0 F10197062-C.pdf")
    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["promoted"] == 1
    live = {Path(p).name.lower() for p in _live_paths(out)}
    assert "f10197062_c.pdf" in live
    # The lower archived revision is untouched, the promoted source is gone, and
    # the demoted live copy is archived - with no second entry named after the
    # promoted revision (that would be archive churn).
    assert sorted(p.name for p in sup.glob("*.pdf")) == [
        "1.1.2.0 F10197062-B.pdf", "F10197062.pdf"]


def test_repair_promotes_every_sheet_at_the_top_archived_revision(tmp_path, make_pdf):
    # Two DWG sheets archived at the same revision: promoting one alone would
    # leave the other tied with the new live rank and never reconsidered.
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    make_pdf(src / "F10197062.pdf", ["NAME", "Live revision"])
    out = tmp_path / "out"
    run_full(src, out, False, lambda m: None, jobs=1)

    sup = out / "_superseded"
    sup.mkdir(exist_ok=True)
    for sheet in ("1", "2"):
        make_pdf(tmp_path / f"sheet{sheet}.pdf", ["NAME", f"Sheet {sheet}"])
        shutil.copy2(tmp_path / f"sheet{sheet}.pdf",
                     sup / f"1.1.2.0 F10197062-B_DWG{sheet}.pdf")

    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["promoted"] == 2
    live = {Path(p).name.lower() for p in _live_paths(out)}
    assert {"f10197062_b_dwg1.pdf", "f10197062_b_dwg2.pdf"} <= live


def test_repair_keeps_a_correctly_archived_watermarked_loser(tmp_path, make_pdf):
    # Same base, same stem group: an archived duplicate of a live stem is a
    # duplicate-resolution matter, not a promotion. It stays archived.
    _src, out = _clean_tree(tmp_path, make_pdf)
    sup = out / "_superseded"
    sup.mkdir(exist_ok=True)
    make_pdf(tmp_path / "dup.pdf", ["NAME", "Child part"])
    shutil.copy2(tmp_path / "dup.pdf", sup / "1.1.2.0 F10197062.pdf")

    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["promoted"] == 0
    assert (sup / "1.1.2.0 F10197062.pdf").exists()


def test_repair_converges_a_damaged_tree_on_the_full_run_plan(tmp_path, make_pdf):
    src, out = _clean_tree(tmp_path, make_pdf)
    reference = tmp_path / "reference"
    run_full(src, reference, False, lambda m: None, jobs=1)

    # Damage: the child leaves its parent's folder, and the parent's folder
    # carries a stale name.
    child = next(out.rglob("F10197062.pdf"))
    shutil.move(str(child.parent), str(out / "LOOSE CHILD"))
    parent_folder = next(p for p in out.iterdir() if p.name.startswith("F10126999"))
    parent_folder.rename(out / "F10126999 STALE NAME")
    assert _live_paths(out) != _live_paths(reference)

    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["review"] == []
    assert _live_paths(out) == _live_paths(reference)

    # Nothing left to reconcile, and the report agrees with a full run.
    again = run_repair(out, False, lambda m: None, jobs=1)
    assert again["repair"]["moved"] == 0
    assert again["repair"]["removed"] == 0
    assert again["repair"]["renamed"] == 0
    assert again["repair"]["review"] == []


def test_repair_reports_a_rename(tmp_path, make_pdf):
    _src, out = _clean_tree(tmp_path, make_pdf)
    # An earlier run named the child's folder with the wrong (bare) name.
    child = next(out.rglob("F10197062.pdf"))
    child.parent.rename(child.parent.parent / "F10197062")
    lines = []
    ctx = run_repair(out, False, lines.append, jobs=1)
    assert ctx["repair"]["renamed"] == 1
    assert any("--- Repair (tree reconciliation) ---" in line for line in lines)
    assert any("Folders renamed:        1" in line for line in lines)


def test_repair_needs_an_organized_tree(tmp_path, make_pdf):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(Exception) as exc:
        run_repair(empty, False, lambda m: None, jobs=1)
    assert "No organized PDFs" in str(exc.value)


def test_repair_is_available_from_incremental_trees(tmp_path, make_pdf):
    # A tree built by an incremental run repairs just as cleanly as a full one.
    src = tmp_path / "in"
    src.mkdir()
    make_pdf(src / "F10126999.pdf",
             ["FERMI PART LIST", "F10197062 CHILD PART", "NAME", "Parent part"])
    out = tmp_path / "out"
    run_incremental(src, out, False, lambda m: None, jobs=1)
    make_pdf(src / "F10197062.pdf", ["NAME", "Child part"])
    run_incremental(src, out, False, lambda m: None, jobs=1)

    ctx = run_repair(out, False, lambda m: None, jobs=1)
    assert ctx["repair"]["review"] == []
    assert ctx["repair"]["moved"] == 0
