"""Unit tests: system-dir convention, output tree scan, superseded copies."""
import os

import pytest
from pathlib import Path

from fermi_organizer import fsops
from fermi_organizer.config import canonical_stem
from fermi_organizer.fsops import (build_pdf_index, copy_orphans, copy_superseded,
                                   copy_watermarked_duplicates,
                                   find_organized_pdfs, is_system_dir,
                                   place_files, retire_adopted_orphans,
                                   scan_output_tree, sweep_supersede_staging,
                                   sweep_stale_claims,
                                   acquire_run_lock, release_run_lock,
                                   abandon_run_lock, run_lock_lost,
                                   RunLockedError, LockLostError, RUN_LOCK_NAME)


def test_run_lock_acquire_and_release(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    logged = []

    lock = acquire_run_lock(out, False, logged.append)

    assert lock is not None and lock.name == RUN_LOCK_NAME
    assert lock.is_file()
    assert logged == []
    release_run_lock(lock)
    assert not lock.exists()


def test_run_lock_second_holder_refused_while_live(tmp_path):
    # Our own PID is alive, so a second acquire must refuse (this is also
    # what a concurrent second process observes).
    out = tmp_path / "out"
    out.mkdir()
    lock = acquire_run_lock(out, False, lambda m: None)
    try:
        with pytest.raises(RunLockedError):
            acquire_run_lock(out, False, lambda m: None)
    finally:
        release_run_lock(lock)
    # After release the tree is acquirable again.
    lock2 = acquire_run_lock(out, False, lambda m: None)
    release_run_lock(lock2)


def test_run_lock_stale_same_host_stolen(tmp_path):
    import json
    import socket
    import time

    from fermi_organizer.fsops import RUN_LOCK_STALE_SECS

    out = tmp_path / "out"
    out.mkdir()
    lock_path = out / RUN_LOCK_NAME
    lock_path.write_text(json.dumps({"pid": 2 ** 30,
                                     "host": socket.gethostname(),
                                     "created": 0.0}), encoding="utf-8")
    old = time.time() - RUN_LOCK_STALE_SECS - 60
    os.utime(lock_path, (old, old))
    logged = []

    lock = acquire_run_lock(out, False, logged.append)

    assert lock is not None
    assert any("stale run lock" in m for m in logged)
    release_run_lock(lock)


def test_run_lock_fresh_or_garbage_refused(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / RUN_LOCK_NAME).write_text('{"pid": 1234, "host": "some-other-host"}',
                                     encoding="utf-8")
    with pytest.raises(RunLockedError):
        acquire_run_lock(out, False, lambda m: None)
    (out / RUN_LOCK_NAME).write_text("not json{{", encoding="utf-8")
    with pytest.raises(RunLockedError):
        acquire_run_lock(out, False, lambda m: None)


def test_run_lock_missing_output_dir_skips_silently(tmp_path):
    # No tree yet (first dry-run): nothing to protect, and dry-run must not
    # create the folder — skip with no log lines (golden-log contract).
    logged = []
    lock = acquire_run_lock(tmp_path / "nope", False, logged.append)
    assert lock is None
    assert logged == []
    assert not (tmp_path / "nope").exists()


def test_run_lock_heartbeat_refreshes_while_held(tmp_path, monkeypatch):
    # A live holder re-touches its lock so a long run never looks stealable;
    # release stops the heartbeat.
    import time

    monkeypatch.setattr(fsops, "RUN_LOCK_HEARTBEAT_SECS", 0.05)
    out = tmp_path / "out"
    out.mkdir()

    lock = acquire_run_lock(out, False, lambda m: None)
    first = os.path.getmtime(lock)
    time.sleep(0.25)
    assert os.path.getmtime(lock) > first
    assert str(lock) in fsops._heartbeats
    release_run_lock(lock)
    assert str(lock) not in fsops._heartbeats
    assert not lock.exists()


def test_run_lock_write_failure_cleans_up_and_runs_unlocked(tmp_path, monkeypatch):
    # Review finding: a failed lock write must not leak the O_EXCL-created
    # file (no lock path exists to clean it up) and refuse later runs.
    out = tmp_path / "out"
    out.mkdir()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fsops.json, "dumps", boom)
    logged = []

    assert acquire_run_lock(out, False, logged.append) is None
    assert not (out / RUN_LOCK_NAME).exists()
    assert any("could not write run lock" in m for m in logged)
    # A later run is not refused by the leaked file.
    monkeypatch.undo()
    lock = acquire_run_lock(out, False, logged.append)
    assert lock is not None
    release_run_lock(lock)


def test_run_lock_heartbeat_start_failure_cleans_up(tmp_path, monkeypatch):
    # Thread.start() failing after creation must not leak a fresh lock that
    # refuses later runs until stale: same degraded-unlocked path as a write
    # failure.
    import threading

    def boom(self):
        raise RuntimeError("can't start thread")

    monkeypatch.setattr(threading.Thread, "start", boom)
    out = tmp_path / "out"
    out.mkdir()
    logged = []
    assert acquire_run_lock(out, False, logged.append) is None
    assert not (out / RUN_LOCK_NAME).exists()
    assert any("heartbeat" in m for m in logged)
    assert str(out / RUN_LOCK_NAME) not in fsops._heartbeats
    # A later run is not refused by the leaked file.
    monkeypatch.undo()
    lock = acquire_run_lock(out, False, logged.append)
    assert lock is not None
    release_run_lock(lock)


def test_heartbeat_beat_refreshes_only_own_token(tmp_path):
    # Ownership-checked refresh: our token is re-touched; foreign content is
    # never touched (a steal racing the beat must not get a fresh mtime).
    import time

    from fermi_organizer.fsops import _heartbeat_beat

    out = tmp_path / "out"
    out.mkdir()
    lock = acquire_run_lock(out, False, lambda m: None)
    try:
        token = lock.read_bytes()
        old = time.time() - 100
        os.utime(lock, (old, old))
        assert _heartbeat_beat(lock, token) is True
        assert os.path.getmtime(lock) > old
        # Simulate a steal: foreign bytes with an old mtime.
        lock.write_bytes(b'{"pid": 999999, "host": "stealer"}')
        os.utime(lock, (old, old))
        assert _heartbeat_beat(lock, token) is False
        assert os.path.getmtime(lock) == old
        # Vanished file: assume stolen, never touch.
        lock.unlink()
        assert _heartbeat_beat(lock, token) is False
        assert not lock.exists()
    finally:
        abandon_run_lock(lock)
    assert not run_lock_lost(lock)


def test_heartbeat_flags_loss_on_steal(tmp_path, monkeypatch):
    # End to end: replacing the lock content trips the flag on the next beat.
    import time

    monkeypatch.setattr(fsops, "RUN_LOCK_HEARTBEAT_SECS", 0.05)
    out = tmp_path / "out"
    out.mkdir()
    lock = acquire_run_lock(out, False, lambda m: None)
    try:
        assert run_lock_lost(lock) is False
        assert run_lock_lost(None) is False
        lock.write_bytes(b'{"pid": 999999, "host": "stealer"}')
        deadline = time.time() + 5
        while not run_lock_lost(lock) and time.time() < deadline:
            time.sleep(0.05)
        assert run_lock_lost(lock) is True
    finally:
        # Abandon, not release: the file is the stealer's now.
        abandon_run_lock(lock)
    assert lock.read_bytes() == b'{"pid": 999999, "host": "stealer"}'


def test_sweep_stale_claims(tmp_path):
    import time

    from fermi_organizer.fsops import RUN_LOCK_STALE_SECS

    out = tmp_path / "out"
    out.mkdir()
    old_claim = out / f"{RUN_LOCK_NAME}.claim.1234"
    old_claim.write_text("stale", encoding="utf-8")
    old = time.time() - RUN_LOCK_STALE_SECS - 60
    os.utime(old_claim, (old, old))
    fresh_claim = out / f"{RUN_LOCK_NAME}.claim.5678"
    fresh_claim.write_text("live-steal?", encoding="utf-8")
    decoy = out / f"{RUN_LOCK_NAME}.claim.abc"
    decoy.write_text("not-a-claim", encoding="utf-8")

    logged = []
    sweep_stale_claims(out, False, logged.append)
    assert not old_claim.exists()
    assert fresh_claim.is_file()  # fresh: may be a live stealer, never touch
    assert decoy.is_file()  # wrong shape: not ours
    assert any("lock claim" in m for m in logged)

    # Dry-run reports without touching.
    old_claim.write_text("stale", encoding="utf-8")
    os.utime(old_claim, (old, old))
    logged = []
    sweep_stale_claims(out, True, logged.append)
    assert old_claim.is_file()
    assert any("DRY-RUN" in m and "lock claim" in m for m in logged)

    # Missing dir: silent no-op.
    sweep_stale_claims(tmp_path / "nope", False, logged.append)


def test_place_files_aborts_when_lock_stolen_mid_run(tmp_path, monkeypatch):
    # A steal landing mid-placement stops at the next folder: LockLostError
    # with no further writes after the steal point.
    out = tmp_path / "out"
    out.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    index = {}
    for stem in ("P", "A", "B"):
        p = src / f"{stem}.pdf"
        p.write_bytes(b"%PDF")
        index[stem] = p
    children = {"P": {"A", "B"}}
    folder_names = {"P": "P Parent", "A": "A Child", "B": "B Child"}

    lock = acquire_run_lock(out, False, lambda m: None)
    real_copy2 = fsops.shutil.copy2
    calls = []

    def stealing_copy2(s, d, *a, **k):
        out_ = real_copy2(s, d, *a, **k)
        calls.append(d)
        if len(calls) == 1:
            fsops._heartbeats[str(lock)][2].set()  # stealer strikes mid-run
        return out_

    monkeypatch.setattr(fsops.shutil, "copy2", stealing_copy2)
    logged = []
    try:
        with pytest.raises(LockLostError):
            place_files(children, ["P"], index, out, False, logged.append,
                        names_of={}, folder_names=folder_names, lock=lock)
        assert any("lock lost" in m for m in logged)
    finally:
        abandon_run_lock(lock)
    # Exactly the first folder's copy exists; the stolen run wrote nothing more.
    assert sorted(p.name for p in out.rglob("*.pdf")) == ["P.pdf"]


def test_place_files_sees_file_steal_without_heartbeat_tick(tmp_path):
    # Synchronous detection: the lock file is replaced (a completed steal)
    # with the 600 s heartbeat never firing and the flag never set — the
    # first folder check still aborts with nothing written.
    out = tmp_path / "out"
    out.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    index = {}
    for stem in ("P", "A"):
        p = src / f"{stem}.pdf"
        p.write_bytes(b"%PDF")
        index[stem] = p
    children = {"P": {"A"}}
    folder_names = {"P": "P Parent", "A": "A Child"}

    logged = []
    lock = acquire_run_lock(out, False, logged.append)
    try:
        assert run_lock_lost(lock) is False  # ours, no tick needed
        lock.write_bytes(b'{"pid": 999999, "host": "stealer"}')
        with pytest.raises(LockLostError):
            place_files(children, ["P"], index, out, False, logged.append,
                        names_of={}, folder_names=folder_names, lock=lock)
        assert any("lock lost" in m for m in logged)
    finally:
        abandon_run_lock(lock)
    assert list(out.rglob("*.pdf")) == []


def test_run_lock_lost_missing_file_reads_as_lost(tmp_path):
    # Fail closed: a lock path holding nothing readable is not ours
    # (mid-steal the rename/unlink window leaves exactly this).
    out = tmp_path / "out"
    out.mkdir()
    lock = acquire_run_lock(out, False, lambda m: None)
    try:
        assert run_lock_lost(lock) is False
        lock.unlink()
        assert run_lock_lost(lock) is True
    finally:
        abandon_run_lock(lock)


def test_is_system_dir_convention():
    # System directories start with '_'
    assert is_system_dir("_superseded")
    assert is_system_dir("_orphans")
    assert is_system_dir("_anything")
    assert is_system_dir("_")
    assert is_system_dir("__double_underscore")

    # Regular directories
    assert not is_system_dir("F10126106 Assembly")
    assert not is_system_dir("Output")
    assert not is_system_dir("output")
    assert not is_system_dir("subfolder")

    # Underscore not at start
    assert not is_system_dir("F10126106_Assembly")
    assert not is_system_dir("a_b_c")

    # Edge cases
    assert not is_system_dir("")


def test_scan_output_tree_categorizes_by_system_dir(tmp_path):
    (tmp_path / "F10126106 Assembly").mkdir()
    (tmp_path / "_superseded").mkdir()
    (tmp_path / "_orphans").mkdir()
    (tmp_path / "_scratch").mkdir()
    tree_pdf = tmp_path / "F10126106 Assembly" / "F10126106.pdf"
    root_pdf = tmp_path / "F10126107.pdf"
    sup_pdf = tmp_path / "_superseded" / "F10126106.pdf"
    orph_pdf = tmp_path / "_orphans" / "F10126108.pdf"
    hidden_pdf = tmp_path / "_scratch" / "F10126109.pdf"
    for p in (tree_pdf, root_pdf, sup_pdf, orph_pdf, hidden_pdf):
        p.write_bytes(b"%PDF")

    out = scan_output_tree(tmp_path)

    assert set(out) == {"tree", "sup", "orph"}
    assert set(out["tree"]) == {tree_pdf, root_pdf}
    assert out["sup"] == [sup_pdf]
    assert out["orph"] == [orph_pdf]
    assert hidden_pdf not in out["tree"] + out["sup"] + out["orph"]


def test_build_pdf_index_classification_warnings_and_order(tmp_path):
    for d in ("sub", "sub2", "Output", "_orphans"):
        (tmp_path / d).mkdir()
    for rel in ("F10126106.pdf", os.path.join("sub", "F10126107.pdf"),
                os.path.join("sub2", "F10126106.pdf"), "notes.pdf",
                os.path.join("Output", "F10126108.pdf"),
                os.path.join("_orphans", "F10126109.pdf"),
                "F10126110_AB.pdf", "F10126111__CHK.pdf"):
        (tmp_path / rel).write_bytes(b"%PDF")

    lines = []
    index, duplicates = build_pdf_index(tmp_path, lines.append)

    assert sorted(index) == ["F10126106", "F10126107", "F10126110_AB",
                             "F10126111__CHK"]
    assert index["F10126106"] == tmp_path / "F10126106.pdf"
    assert index["F10126107"] == tmp_path / "sub" / "F10126107.pdf"
    assert duplicates["F10126106"] == [tmp_path / "sub2" / "F10126106.pdf"]
    assert lines == [
        "Skipped 1 PDF(s) under the output tree (organized copies are never input)",
        "Skipped 1 PDF(s) under _-prefixed folders (system dirs are never input)",
        "WARNING: 1 duplicate stem(s) across input subfolders (shallowest path kept):",
        f"  {'sub2' + os.sep + 'F10126106.pdf'}  (kept F10126106.pdf)",
        "Ignored 1 PDF(s) with non-part filenames:",
        "  notes.pdf",
        "WARNING: 1 stem(s) with unrecognized revision token (rank treated as 0):",
        "  F10126110_AB",
    ]


def test_build_pdf_index_prefers_shallowest_over_nested_tree_copy(tmp_path):
    # The nested organized copy sorts first (folder name < file name), so
    # without the shallowest rule it would shadow the input original.
    tree_dir = tmp_path / "F10126106 Assembly"
    tree_dir.mkdir()
    loose = tmp_path / "F10126106.pdf"
    nested = tree_dir / "F10126106.pdf"
    loose.write_bytes(b"original")
    nested.write_bytes(b"tree copy")

    lines = []
    index, duplicates = build_pdf_index(tmp_path, lines.append)

    assert index["F10126106"] == loose
    assert duplicates["F10126106"] == [nested]
    text = "\n".join(lines)
    assert "shallowest path kept" in text
    assert f"kept {loose.name}" in text


def test_build_pdf_index_exclude_skips_output_subtree_by_path(tmp_path):
    # An output folder with a custom name inside the input tree is excluded
    # by resolved path, not just by the top-level "Output/" convention.
    tree_dir = tmp_path / "Organizer Output" / "F10126106 Assembly"
    tree_dir.mkdir(parents=True)
    loose = tmp_path / "F10126106.pdf"
    nested = tree_dir / "F10126107.pdf"
    loose.write_bytes(b"original")
    nested.write_bytes(b"tree copy")

    lines = []
    index, _duplicates = build_pdf_index(
        tmp_path, lines.append, exclude=tmp_path / "Organizer Output")

    assert sorted(index) == ["F10126106"]
    assert "under the output tree" in "\n".join(lines)


def test_build_pdf_index_exclude_ignores_outside_and_equal_paths(tmp_path):
    # exclude outside the input (the usual separate-tree layout) and
    # exclude == folder (in-place default) are both no-ops.
    (tmp_path / "F10126106.pdf").write_bytes(b"%PDF")
    outside = tmp_path / "elsewhere"

    index, _duplicates = build_pdf_index(tmp_path, None, exclude=outside)
    assert sorted(index) == ["F10126106"]

    index, _duplicates = build_pdf_index(tmp_path, None, exclude=tmp_path)
    assert sorted(index) == ["F10126106"]


def test_copy_superseded_skip_vs_overwrite(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    old_pdf = src / "F10126106.pdf"
    old_pdf.write_text("v1")
    out = tmp_path / "out"
    out.mkdir()
    target = out / "_superseded" / "F10126106.pdf"
    logged = []

    n, archived = copy_superseded({"F10126106": old_pdf}, out, False, logged.append)
    assert (n, archived) == (1, {"F10126106"})
    assert target.read_text() == "v1"

    # overwrite=False: existing archive with different bytes is skipped and
    # does NOT count as archived (it must not retire a parked orphan).
    target.write_text("v2")
    n, archived = copy_superseded({"F10126106": old_pdf}, out, False, logged.append)
    assert (n, archived) == (0, set())
    assert target.read_text() == "v2"
    assert "archive already exists with different content" in "\n".join(logged)

    # overwrite=True: differing bytes archive under a suffix, never clobber.
    n, archived = copy_superseded({"F10126106": old_pdf}, out, False, logged.append,
                                  overwrite=True)
    assert (n, archived) == (1, {"F10126106"})
    assert target.read_text() == "v2"
    assert (out / "_superseded" / "F10126106.1.pdf").read_text() == "v1"

    # overwrite=True with identical content: the suffixed copy is reused,
    # so repeat runs do not grow suffixes.
    n, archived = copy_superseded({"F10126106": old_pdf}, out, False, logged.append,
                                  overwrite=True)
    assert (n, archived) == (1, {"F10126106"})
    assert (out / "_superseded" / "F10126106.1.pdf").read_text() == "v1"
    assert not (out / "_superseded" / "F10126106.2.pdf").exists()

    # overwrite=False with an identical existing archive counts as archived.
    target.write_text("v1")
    n, archived = copy_superseded({"F10126106": old_pdf}, out, False, logged.append)
    assert (n, archived) == (0, {"F10126106"})


def test_copy_superseded_identical_archive_not_recopied_every_run(tmp_path, monkeypatch):
    # S-041: a repeat full run must not rewrite byte-identical archived PDFs
    # (multi-hundred-MB scanned sheets); the stem still counts as archived so
    # orphan retirement stays gated on verified bytes.
    src = tmp_path / "in"
    src.mkdir()
    old_pdf = src / "F10126106.pdf"
    old_pdf.write_text("v1")
    out = tmp_path / "out"
    out.mkdir()
    copy_superseded({"F10126106": old_pdf}, out, False, lambda m: None,
                    overwrite=True)
    calls = []
    real_copy2 = fsops.shutil.copy2
    monkeypatch.setattr(fsops.shutil, "copy2",
                        lambda a, b: calls.append(a) or real_copy2(a, b))
    logged = []

    n, archived = copy_superseded({"F10126106": old_pdf}, out, False,
                                  logged.append, overwrite=True)

    assert (n, archived) == (1, {"F10126106"})
    assert calls == []
    assert "already identical" in "\n".join(logged)


def test_copy_watermarked_identical_archive_not_recopied(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    w1 = src / "F10126106.pdf"
    w1.write_text("wm")
    out = tmp_path / "out"
    out.mkdir()
    copy_watermarked_duplicates([w1], out, False, lambda m: None)
    calls = []
    real_copy2 = fsops.shutil.copy2
    monkeypatch.setattr(fsops.shutil, "copy2",
                        lambda a, b: calls.append(a) or real_copy2(a, b))
    logged = []

    n = copy_watermarked_duplicates([w1], out, False, logged.append)

    assert n == 1
    assert calls == []
    assert "already identical" in "\n".join(logged)


def test_copy_superseded_symlink_target_not_archived(tmp_path):
    # A live symlink at the archive destination marks nothing as archived:
    # retiring a parked orphan on it could delete the only copy.
    src = tmp_path / "in"
    src.mkdir()
    pdf = src / "F10126106.pdf"
    pdf.write_bytes(b"%PDF incoming")
    out = tmp_path / "out"
    target = out / "_superseded" / "F10126106.pdf"
    target.parent.mkdir(parents=True)
    referent = tmp_path / "elsewhere.pdf"
    referent.write_bytes(b"%PDF elsewhere")
    try:
        target.symlink_to(referent)
    except OSError:
        pytest.skip("symlink creation not permitted")

    logged = []
    n, archived = copy_superseded({"F10126106": pdf}, out, False, logged.append)

    assert (n, archived) == (0, set())
    assert "F10126106: copy skipped (symlink refused)" in "\n".join(logged)
    assert referent.read_bytes() == b"%PDF elsewhere"


def test_resolve_archive_target_ignores_stale_filecmp_cache(tmp_path):
    # filecmp.cmp answers from a path+stat cache: a re-written archive with
    # unchanged size/mtime still reads as equal, which would discard the
    # changed revision. resolve_archive_target must not consult that cache.
    import filecmp

    sup = tmp_path / "_superseded"
    sup.mkdir(parents=True)
    src = tmp_path / "F10126106.pdf"
    src.write_bytes(b"AAAA")
    target = sup / "F10126106.pdf"
    target.write_bytes(b"AAAA")
    os.utime(src, (1_000_000_000, 1_000_000_000))
    os.utime(target, (1_000_000_000, 1_000_000_000))
    assert filecmp.cmp(fsops._native(target), fsops._native(src), shallow=False)

    target.write_bytes(b"BBBB")
    os.utime(target, (1_000_000_000, 1_000_000_000))
    # Stale cache hit: same paths, same size, same mtime, different bytes.
    assert filecmp.cmp(fsops._native(target), fsops._native(src), shallow=False)

    resolved, already = fsops.resolve_archive_target(sup, src)
    assert not already
    assert resolved == sup / "F10126106.1.pdf"


def test_place_files_copy_failure_skips_file_continues_children(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    parent_pdf = tmp_path / "F10126106.pdf"
    child_pdf = tmp_path / "F10126107.pdf"
    parent_pdf.write_bytes(b"%PDF")
    child_pdf.write_bytes(b"%PDF")
    index = {"F10126106": parent_pdf, "F10126107": child_pdf}
    children = {"F10126106": ["F10126107"]}
    real_copy2 = fsops.shutil.copy2

    def fake_copy2(src, dst):
        if Path(src).name == "F10126106.pdf":
            raise OSError("disk full")
        return real_copy2(src, dst)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []

    total = place_files(children, ["F10126106"], index, folder, False, logged.append)

    assert total == 1
    assert (folder / "F10126106" / "F10126107" / "F10126107.pdf").is_file()
    assert not (folder / "F10126106" / "F10126106.pdf").exists()
    text = "\n".join(logged)
    assert "WARNING: F10126106: copy failed" in text
    assert "FAILED 1 copy(ies) - see warnings above" in text


def test_place_files_valueerror_copy_failure_not_fatal(tmp_path, monkeypatch):
    # Embedded-NUL style hostile names raise ValueError before the syscall;
    # placement guards must degrade to a logged failure, not abort the run.
    folder = tmp_path / "out"
    parent_pdf = tmp_path / "F10126106.pdf"
    child_pdf = tmp_path / "F10126107.pdf"
    parent_pdf.write_bytes(b"%PDF")
    child_pdf.write_bytes(b"%PDF")
    index = {"F10126106": parent_pdf, "F10126107": child_pdf}
    children = {"F10126106": ["F10126107"]}
    real_copy2 = fsops.shutil.copy2

    def fake_copy2(src, dst):
        # Copies stage through a temp name next to the target: match both.
        if Path(dst).name.startswith("F10126106.pdf"):
            raise ValueError("embedded null character")
        return real_copy2(src, dst)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []

    total = place_files(children, ["F10126106"], index, folder, False, logged.append)

    assert total == 1
    assert (folder / "F10126106" / "F10126107" / "F10126107.pdf").is_file()
    assert any("copy failed" in m for m in logged)
    assert "FAILED 1 copy(ies) - see warnings above" in "\n".join(logged)


def test_place_files_sanitizes_nul_name_to_safe_folder(tmp_path):
    # A PDF-extracted NAME containing NUL must not reach mkdir/copy2 at all:
    # sanitize_folder_name strips control chars, placement succeeds.
    folder = tmp_path / "out"
    pdf = tmp_path / "F10126106.pdf"
    pdf.write_bytes(b"%PDF")
    logged = []

    total = place_files({"F10126106": []}, ["F10126106"],
                        {"F10126106": pdf}, folder, False, logged.append,
                        names_of={"F10126106": "BRACKET\x00ASSY"})

    assert total == 1
    assert (folder / "F10126106 BRACKETASSY" / "F10126106.pdf").is_file()
    assert not any("copy failed" in m for m in logged)


def test_place_files_mkdir_failure_skips_whole_subtree(tmp_path, monkeypatch):
    folder = tmp_path / "out"
    index = {}
    for stem in ("F10126106", "F10126107", "F10126108"):
        pdf = tmp_path / f"{stem}.pdf"
        pdf.write_bytes(b"%PDF")
        index[stem] = pdf
    children = {"F10126106": ["F10126107"], "F10126107": ["F10126108"]}

    def boom_mkdir(self, *args, **kwargs):
        raise OSError("access denied")

    monkeypatch.setattr(Path, "mkdir", boom_mkdir)
    logged = []

    total = place_files(children, ["F10126106"], index, folder, False, logged.append)

    assert total == 0
    assert not folder.exists()
    text = "\n".join(logged)
    assert "WARNING: F10126106: copy failed" in text
    assert "FAILED 3 copy(ies) - see warnings above" in text


def test_place_files_refuses_when_over_copy_cap(tmp_path, monkeypatch):
    # A fan-out beyond MAX_PLANNED_COPIES must refuse placement entirely
    # (input untouched) instead of flooding the disk.
    folder = tmp_path / "out"
    index = {}
    for stem in ("F10126106", "F10126107", "F10126108"):
        pdf = tmp_path / f"{stem}.pdf"
        pdf.write_bytes(b"%PDF")
        index[stem] = pdf
    children = {"F10126106": ["F10126107"], "F10126107": ["F10126108"]}
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 2)
    logged = []

    with pytest.raises(fsops.PlacementRefusedError) as exc:
        place_files(children, ["F10126106"], index, folder, False,
                    logged.append)

    assert exc.value.planned == 3
    assert not folder.exists()
    assert "placement refused" in "\n".join(logged)

    # At exactly the cap the placement still runs (the check is '>').
    logged.clear()
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 3)
    total = place_files(children, ["F10126106"], index, folder, False,
                        logged.append)
    assert total == 3
    assert (folder / "F10126106" / "F10126107" / "F10126108" /
            "F10126108.pdf").is_file()


def test_place_files_cap_counts_shared_subtree_at_true_depth(tmp_path,
                                                             monkeypatch):
    # Copy counts depend on depth (TREE_MAX_DEPTH truncation): a shared subtree
    # first counted near the depth limit must not be reused at a shallower
    # path, or the planned count slips under MAX_PLANNED_COPIES while the
    # actual placement copies more.
    folder = tmp_path / "out"
    index = {}
    for stem in ("F10126105", "F10126106", "F10126107", "F10126108",
                 "F10126109", "F10126110", "F10126111"):
        pdf = tmp_path / f"{stem}.pdf"
        pdf.write_bytes(b"%PDF")
        index[stem] = pdf
    # R1's chain reaches F10126109 at the depth limit (its tail is truncated);
    # R2 shares it at depth 1, where the whole tail is copied.
    children = {
        "F10126106": ["F10126107"],
        "F10126107": ["F10126108"],
        "F10126108": ["F10126109"],
        "F10126109": ["F10126110"],
        "F10126110": ["F10126111"],
        "F10126105": ["F10126109"],
    }
    monkeypatch.setattr(fsops, "TREE_MAX_DEPTH", 3)
    # True planned count is 8 (4 + 4); the stem-only memo answered 6.
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 6)
    logged = []

    # R1 (the deep chain) first: the undercount only happens when the shared
    # subtree is memoized from the truncated deep visit.
    with pytest.raises(fsops.PlacementRefusedError) as exc:
        place_files(children, ["F10126106", "F10126105"], index, folder,
                    False, logged.append)

    assert exc.value.planned == 8
    assert not folder.exists()
    assert "placement refused" in "\n".join(logged)


def test_fit_roots_within_cap_keeps_all_when_under_cap(monkeypatch):
    # Nothing over the cap: roots pass through untouched and nothing is
    # logged (the common path must stay silent).
    children = {"F10126106": ["F10126107"]}
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 2)
    logged = []

    kept, skipped = fsops.fit_roots_within_cap(children, ["F10126106"],
                                               logged.append)

    assert kept == ["F10126106"]
    assert skipped == []
    assert logged == []


def test_fit_roots_within_cap_drops_only_the_giant(monkeypatch):
    # A diamond root (shared grandchild copied twice = 5) plus two
    # singletons: cap 5 drops only the giant, preserving input order.
    children = {"F10126106": ["F10126107", "F10126108"],
                "F10126107": ["F10126109"],
                "F10126108": ["F10126109"]}
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 5)
    logged = []

    kept, skipped = fsops.fit_roots_within_cap(
        children, ["F10126110", "F10126106", "F10126111"], logged.append)

    assert kept == ["F10126110", "F10126111"]
    assert skipped == [("F10126106", 5)]
    text = "\n".join(logged)
    assert "placement refused for 1 oversized root(s), placing 2 of 3" in text
    assert "skipped oversized root F10126106 (5 copies planned)" in text


def test_fit_roots_within_cap_drops_largest_first_with_stem_tiebreak(
        monkeypatch):
    # Three identical 2-copy roots against a cap of 3: the two smallest
    # stems drop (tie-break), the last one fits.
    children = {"F10126106": ["F10126107"],
                "F10126108": ["F10126109"],
                "F10126110": ["F10126111"]}
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 3)
    logged = []

    kept, skipped = fsops.fit_roots_within_cap(
        children, ["F10126110", "F10126106", "F10126108"], logged.append)

    assert kept == ["F10126110"]
    assert skipped == [("F10126106", 2), ("F10126108", 2)]


def test_fit_roots_within_cap_boundary_and_empty(monkeypatch):
    # Exactly at the cap still places (the check is '>'); an empty root list
    # and a lone over-cap root are handled without raising.
    children = {"F10126106": ["F10126107", "F10126108"]}
    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 3)

    kept, skipped = fsops.fit_roots_within_cap(children, ["F10126106"],
                                               lambda msg: None)
    assert (kept, skipped) == (["F10126106"], [])

    kept, skipped = fsops.fit_roots_within_cap(children, [],
                                               lambda msg: None)
    assert (kept, skipped) == ([], [])

    monkeypatch.setattr(fsops, "MAX_PLANNED_COPIES", 2)
    logged = []
    kept, skipped = fsops.fit_roots_within_cap(children, ["F10126106"],
                                               logged.append)
    assert kept == []
    assert skipped == [("F10126106", 3)]
    assert "placing 0 of 1" in "\n".join(logged)


def test_copy_superseded_failure_warns_and_continues(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    bad_pdf = src / "F10126106.pdf"
    good_pdf = src / "F10126107.pdf"
    bad_pdf.write_text("v1")
    good_pdf.write_text("v2")
    out = tmp_path / "out"
    out.mkdir()
    real_copy2 = fsops.shutil.copy2

    def fake_copy2(s, d):
        if Path(s).name == "F10126106.pdf":
            raise OSError("locked")
        return real_copy2(s, d)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []

    n, archived = copy_superseded({"F10126106": bad_pdf, "F10126107": good_pdf},
                                  out, False, logged.append)

    assert n == 1
    # The failed stem is not in the archived set: callers must not retire an
    # _orphans/ copy that the archive never reached.
    assert archived == {"F10126107"}
    assert (out / "_superseded" / "F10126107.pdf").is_file()
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert "WARNING: F10126106: copy failed" in "\n".join(logged)


def test_copy_watermarked_duplicates_success_and_dry_run(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    pdf1 = src / "F10126106.pdf"
    pdf2 = src / "F10126107.pdf"
    pdf1.write_text("v1")
    pdf2.write_text("v2")
    out = tmp_path / "out"
    out.mkdir()
    logged = []

    n_dry = copy_watermarked_duplicates([pdf1, pdf2], out, dry_run=True, log=logged.append)
    assert n_dry == 2
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert "[DRY-RUN] mkdir+copy F10126106.pdf" in "\n".join(logged)

    logged.clear()
    n_real = copy_watermarked_duplicates([pdf1, pdf2], out, dry_run=False, log=logged.append)
    assert n_real == 2
    assert (out / "_superseded" / "F10126106.pdf").read_text() == "v1"
    assert (out / "_superseded" / "F10126107.pdf").read_text() == "v2"


def test_copy_watermarked_duplicates_dry_run_plans_distinct_suffixes(tmp_path):
    # Same-name sources with different bytes resolve against unchanged disk in
    # dry-run; planned destinations must still show the suffixes a real run
    # would write.
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = a_dir / "F10126106.pdf"
    b = b_dir / "F10126106.pdf"
    a.write_text("v1")
    b.write_text("v2")
    out = tmp_path / "out"
    logged = []

    n = copy_watermarked_duplicates([a, b], out, dry_run=True, log=logged.append)

    assert n == 2
    text = "\n".join(logged).replace("\\", "/")
    assert "_superseded/F10126106.pdf" in text
    assert "_superseded/F10126106.1.pdf" in text
    assert not (out / "_superseded").exists()


def test_copy_watermarked_duplicates_dry_run_identical_sources_share_target(tmp_path):
    # PR-FIX-007: same-name sources with identical bytes must plan the same
    # destination (execution reuses the first archive instead of suffixing).
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = a_dir / "F10126106.pdf"
    b = b_dir / "F10126106.pdf"
    a.write_text("same")
    b.write_text("same")
    out = tmp_path / "out"
    logged = []

    n = copy_watermarked_duplicates([a, b], out, dry_run=True, log=logged.append)

    assert n == 2
    text = "\n".join(logged).replace("\\", "/")
    assert text.count("_superseded/F10126106.pdf") == 2
    assert "F10126106.1.pdf" not in text

    logged.clear()
    n = copy_watermarked_duplicates([a, b], out, dry_run=False, log=logged.append)

    assert n == 2
    assert (out / "_superseded" / "F10126106.pdf").read_text() == "same"
    assert not (out / "_superseded" / "F10126106.1.pdf").exists()


def test_copy_watermarked_duplicates_never_clobbers_different_content(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    pdf = src / "F10126106.pdf"
    pdf.write_text("v1")
    out = tmp_path / "out"
    target = out / "_superseded" / "F10126106.pdf"
    target.parent.mkdir(parents=True)
    target.write_text("already archived, other copy")
    logged = []

    n = copy_watermarked_duplicates([pdf], out, dry_run=False, log=logged.append)

    assert n == 1
    # Pre-existing archive untouched; the loser archived under a suffix.
    assert target.read_text() == "already archived, other copy"
    assert (out / "_superseded" / "F10126106.1.pdf").read_text() == "v1"

    # Identical bytes reuse the suffixed copy: repeat runs do not grow names.
    logged.clear()
    n = copy_watermarked_duplicates([pdf], out, dry_run=False, log=logged.append)
    assert n == 1
    assert not (out / "_superseded" / "F10126106.2.pdf").exists()


def test_copy_watermarked_duplicates_failure_warns_and_continues(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    bad_pdf = src / "F10126106.pdf"
    good_pdf = src / "F10126107.pdf"
    bad_pdf.write_text("v1")
    good_pdf.write_text("v2")
    out = tmp_path / "out"
    out.mkdir()
    real_copy2 = fsops.shutil.copy2

    def fake_copy2(s, d):
        if Path(s).name == "F10126106.pdf":
            raise OSError("permission denied")
        return real_copy2(s, d)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []

    n = copy_watermarked_duplicates([bad_pdf, good_pdf], out, dry_run=False, log=logged.append)

    assert n == 1
    assert (out / "_superseded" / "F10126107.pdf").is_file()
    assert not (out / "_superseded" / "F10126106.pdf").exists()
    assert "WARNING: F10126106.pdf: copy failed" in "\n".join(logged)


def test_copy_watermarked_duplicates_symlink_refused(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    real_pdf = src / "F10126106.pdf"
    real_pdf.write_text("v1")
    link_pdf = src / "F10126107.pdf"
    try:
        link_pdf.symlink_to(real_pdf)
    except (OSError, NotImplementedError):
        return
    out = tmp_path / "out"
    out.mkdir()
    logged = []

    n = copy_watermarked_duplicates([link_pdf], out, dry_run=False, log=logged.append)

    assert n == 0
    assert not (out / "_superseded" / "F10126107.pdf").exists()
    assert "WARNING: F10126107.pdf: copy skipped (symlink refused)" in "\n".join(logged)


def test_copy_orphans_failure_warns_and_continues(tmp_path, monkeypatch):
    src = tmp_path / "in"
    src.mkdir()
    bad_pdf = src / "F10126106.pdf"
    good_pdf = src / "F10126107.pdf"
    bad_pdf.write_text("v1")
    good_pdf.write_text("v2")
    out = tmp_path / "out"
    out.mkdir()
    real_copy2 = fsops.shutil.copy2

    def fake_copy2(s, d):
        if Path(s).name == "F10126106.pdf":
            raise OSError("permission denied")
        return real_copy2(s, d)

    monkeypatch.setattr(fsops.shutil, "copy2", fake_copy2)
    logged = []
    index = {"F10126106": bad_pdf, "F10126107": good_pdf}
    n = copy_orphans(["F10126106", "F10126107"], index, out, False, logged.append)

    assert n == 1
    assert (out / "_orphans" / "F10126107.pdf").is_file()
    assert not (out / "_orphans" / "F10126106.pdf").exists()
    text = "\n".join(logged)
    assert "WARNING: F10126106: copy failed" in text
    assert "Stored 1 orphan(s) in _orphans/" in text


def test_canonical_stem_real_world_names():
    assert canonical_stem("F10187622_B_DWG1") == "F10187622_B_DWG1"
    assert canonical_stem("F10187622-B-CHK") == "F10187622_B_CHK"
    assert canonical_stem("F10052259---DWG1") == "F10052259___DWG1"
    assert canonical_stem("2.20.3.9 F10196150--CHK") == "F10196150__CHK"
    assert canonical_stem("2.20.3.18.F10196578-A-CHK") == "F10196578_A_CHK"
    assert canonical_stem("F10137913 KIT, MAGNETIC SHIELD COUPLER") == "F10137913"
    assert canonical_stem("F10194480-PID HB650 CM Kit Deliverables") == "F10194480_PID"
    assert canonical_stem("FC0107818___DWG1") == "FC0107818___DWG1"
    assert canonical_stem("f10126106_b_dwg1") == "F10126106_B_DWG1"
    assert canonical_stem("Annexure A-II") is None
    assert canonical_stem("19-99140_Manual") is None
    assert canonical_stem("") is None


def test_build_pdf_index_accepts_hyphen_prefixed_and_titled_names(tmp_path):
    for rel in ("F10126106---CHK.pdf", "F10126107-A-DWG1.pdf",
                "2.20.3.9 F10126108--CHK.pdf",
                "F10126109 KIT, MAGNETIC SHIELD COUPLER.pdf",
                "Annexure A-II.pdf"):
        (tmp_path / rel).write_bytes(b"%PDF")

    lines = []
    index, duplicates = build_pdf_index(tmp_path, lines.append)

    assert sorted(index) == ["F10126106___CHK", "F10126107_A_DWG1",
                             "F10126108__CHK", "F10126109"]
    assert duplicates == {}
    assert lines == [
        "Ignored 1 PDF(s) with non-part filenames:",
        "  Annexure A-II.pdf",
    ]


def test_build_pdf_index_hyphen_and_underscore_copies_share_stem(tmp_path):
    hyphen = tmp_path / "F10126106-A-DWG1.pdf"
    underscore = tmp_path / "F10126106_A_DWG1.pdf"
    hyphen.write_bytes(b"hyphen")
    underscore.write_bytes(b"underscore")

    index, duplicates = build_pdf_index(tmp_path)

    assert list(index) == ["F10126106_A_DWG1"]
    assert index["F10126106_A_DWG1"] == hyphen
    assert duplicates["F10126106_A_DWG1"] == [underscore]


def test_find_organized_pdfs_canonicalizes_hyphen_names(tmp_path):
    tree_dir = tmp_path / "F10126106 Assembly"
    tree_dir.mkdir()
    pdf = tree_dir / "F10126106-A-DWG1.pdf"
    pdf.write_bytes(b"%PDF")

    organized = find_organized_pdfs(tmp_path)

    assert list(organized) == ["F10126106_A_DWG1"]
    assert organized["F10126106_A_DWG1"] == [pdf]


def test_retire_adopted_orphans_includes_superseded_stems(tmp_path):
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    sup_orph = orphans_dir / "F10126107.pdf"
    unrelated_orph = orphans_dir / "F10126108.pdf"
    sup_orph.write_bytes(b"old rev")
    unrelated_orph.write_bytes(b"parked")

    # Empty default keeps old behavior: superseded-only orphan stays.
    logged = []
    n = retire_adopted_orphans({"F10126107": sup_orph, "F10126108": unrelated_orph},
                               out, False, logged.append)
    assert n == 0
    assert sup_orph.is_file()
    assert unrelated_orph.is_file()

    # Superseded stem with no copy of its bytes anywhere: the parked copy may
    # be the only holder, so it stays (with a warning) even when superseded.
    logged = []
    n = retire_adopted_orphans({"F10126107": sup_orph, "F10126108": unrelated_orph},
                               out, False, logged.append,
                               superseded={"F10126107"})
    assert n == 0
    assert sup_orph.is_file()
    assert unrelated_orph.is_file()

    # Superseded stem whose bytes do survive in the archive: retired.
    sup_dir = out / "_superseded"
    sup_dir.mkdir(parents=True)
    (sup_dir / "F10126107.pdf").write_bytes(b"old rev")
    n = retire_adopted_orphans({"F10126107": sup_orph}, out, False,
                               lambda msg: None, superseded={"F10126107"})
    assert n == 1
    assert not sup_orph.exists()


def test_retire_adopted_orphans_planned_covers_dry_run_placement(tmp_path):
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    orphans_dir.mkdir(parents=True)
    orph = orphans_dir / "F10126108.pdf"
    orph.write_bytes(b"parked")

    # Old behavior (default): nothing live on disk, dry-run stays silent.
    logged = []
    n = retire_adopted_orphans({"F10126108": orph}, out, True, logged.append)
    assert n == 0
    assert orph.is_file()
    assert "retire orphan copy" not in "\n".join(logged)

    # Planned (adopted by this run's placement, not yet copied in dry-run):
    # dry-run reports it without touching disk.
    logged = []
    n = retire_adopted_orphans({"F10126108": orph}, out, True, logged.append,
                               planned={"F10126108"})
    assert n == 0
    assert orph.is_file()
    text = "\n".join(logged)
    assert "[DRY-RUN] retire orphan copy:" in text
    assert "F10126108" in text


def test_retire_adopted_orphans_superseded_requires_matching_archive(tmp_path):
    # A superseded stem only retires the parked copy when the parked bytes
    # survive somewhere: an archive holding the NEW input bytes leaves the
    # parked copy as the only holder of the old ones, so it must be kept.
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    sup_dir = out / "_superseded"
    orphans_dir.mkdir(parents=True)
    sup_dir.mkdir(parents=True)
    orphan = orphans_dir / "F10126107.pdf"
    orphan.write_bytes(b"old input bytes")
    (sup_dir / "F10126107.pdf").write_bytes(b"newer input bytes")
    logged = []

    retired = retire_adopted_orphans({"F10126107": orphan}, out, False,
                                     logged.append, superseded={"F10126107"})

    assert retired == 0
    assert orphan.is_file()
    assert "not retired: no byte-identical copy in tree or _superseded/; kept" \
        in "\n".join(logged)


def test_retire_adopted_orphans_superseded_retires_on_matching_archive(tmp_path):
    # The safe supersede case: the archived copy holds exactly the parked
    # bytes, so nothing is lost by retiring the parked duplicate.
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    sup_dir = out / "_superseded"
    orphans_dir.mkdir(parents=True)
    sup_dir.mkdir(parents=True)
    orphan = orphans_dir / "F10126107.pdf"
    orphan.write_bytes(b"same bytes")
    (sup_dir / "F10126107.pdf").write_bytes(b"same bytes")
    logged = []

    retired = retire_adopted_orphans({"F10126107": orphan}, out, False,
                                      logged.append, superseded={"F10126107"})

    assert retired == 1
    assert not orphan.exists()
    assert "Retired 1 orphan copy(ies)" in "\n".join(logged)


def test_retire_adopted_orphans_keeps_differing_live_copy(tmp_path):
    # A same-stem live copy with different bytes must not retire the parked
    # copy: it may be the only copy of those bytes (input updated later).
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    live_dir = out / "F10126109 Parent" / "F10126108 Child"
    orphans_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    orphan = orphans_dir / "F10126108.pdf"
    orphan.write_bytes(b"newer input bytes")
    (live_dir / "F10126108.pdf").write_bytes(b"older placed bytes")
    logged = []

    retired = retire_adopted_orphans({"F10126108": orphan}, out, False,
                                     logged.append)

    assert retired == 0
    assert orphan.is_file()
    assert "not retired: no byte-identical regular live copy; kept" in "\n".join(logged)


def test_retire_adopted_orphans_symlinked_live_copy_kept(tmp_path):
    # A symlinked live entry is not a managed copy: even when its target has
    # identical bytes, the parked orphan stays.
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    live_dir = out / "F10126109 Parent" / "F10126108 Child"
    orphans_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    payload = b"%PDF-1.4 identical bytes"
    orphan = orphans_dir / "F10126108.pdf"
    orphan.write_bytes(payload)
    referent = tmp_path / "external.pdf"
    referent.write_bytes(payload)
    link = live_dir / "F10126108.pdf"
    try:
        link.symlink_to(referent)
    except OSError:
        pytest.skip("symlink creation not permitted")
    logged = []

    retired = retire_adopted_orphans({"F10126108": orphan}, out, False,
                                     logged.append)

    assert retired == 0
    assert orphan.is_file()
    assert "not retired: no byte-identical regular live copy" in "\n".join(logged)


def test_retire_adopted_orphans_identical_live_copy_retires(tmp_path):
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    live_dir = out / "F10126109 Parent" / "F10126108 Child"
    orphans_dir.mkdir(parents=True)
    live_dir.mkdir(parents=True)
    payload = b"%PDF-1.4 identical bytes"
    orphan = orphans_dir / "F10126108.pdf"
    orphan.write_bytes(payload)
    (live_dir / "F10126108.pdf").write_bytes(payload)
    logged = []

    retired = retire_adopted_orphans({"F10126108": orphan}, out, False,
                                     logged.append)

    assert retired == 1
    assert not orphan.exists()


def test_sweep_supersede_staging_ignores_lookalikes(tmp_path):
    # Only the exact `<name>.pdf.supersede_tmp.<pid>` shape is swept; anything
    # else that happens to contain the marker is not ours to delete.
    folder = tmp_path / "F10126106 Assembly"
    folder.mkdir()
    not_pdf = folder / "notes.supersede_tmp.123"
    bad_pid = folder / "F10126107.pdf.supersede_tmp.abc"
    upper = folder / "F10126107.PDF.supersede_tmp.7"
    for p in (not_pdf, bad_pid, upper):
        p.write_bytes(b"partial")
    logged = []

    sweep_supersede_staging(tmp_path, False, logged.append)

    assert not_pdf.is_file()
    assert bad_pid.is_file()
    assert not upper.exists()
    assert sum("removed leftover supersede staging" in m for m in logged) == 1


def test_place_files_iterative_below_default_recursion_limit(tmp_path, monkeypatch):
    # Regression for the iterative rewrite: 600-deep chain, recursion limit
    # far below the traversal depth (the recursive version raised here).
    # Path limits are lifted so the traversal actually reaches the depth cap.
    import sys

    from fermi_organizer.config import TREE_MAX_DEPTH

    monkeypatch.setattr(fsops, "MAX_PATH", 10 ** 9)
    monkeypatch.setattr(fsops, "MAX_DIR", 10 ** 9)
    chain = {f"F{10126000 + i}": [f"F{10126001 + i}"] for i in range(600)}
    chain["F10126599"] = []
    index = {stem: tmp_path / (stem + ".pdf") for stem in chain}
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        total = place_files(chain, ["F10126000"], index, tmp_path / "out",
                            True, lambda msg: None)
    finally:
        sys.setrecursionlimit(limit)
    assert total == TREE_MAX_DEPTH + 1


def test_place_files_cyclic_children_bounded_no_recursion_error(tmp_path):
    folder = tmp_path / "out"
    pdf = tmp_path / "F10126106.pdf"
    pdf.write_bytes(b"%PDF")
    logged = []

    total = place_files({"F10126106": ["F10126106"]}, ["F10126106"],
                        {"F10126106": pdf}, folder, True, logged.append)

    assert total >= 1


def test_retire_adopted_orphans_unlink_failure_logs_warning_and_continues(tmp_path, monkeypatch):
    out = tmp_path / "out"
    orphans_dir = out / "_orphans"
    sup_dir = out / "_superseded"
    orphans_dir.mkdir(parents=True)
    sup_dir.mkdir(parents=True)
    orph1 = orphans_dir / "F10126107.pdf"
    orph2 = orphans_dir / "F10126108.pdf"
    orph1.write_bytes(b"orph1")
    orph2.write_bytes(b"orph2")
    # Both stems are superseded: retirement is only due because the parked
    # bytes survive in the archive.
    (sup_dir / "F10126107.pdf").write_bytes(b"orph1")
    (sup_dir / "F10126108.pdf").write_bytes(b"orph2")

    real_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if "F10126107" in self.name:
            raise OSError("Permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    logged = []

    retired = retire_adopted_orphans(
        {"F10126107": orph1, "F10126108": orph2},
        out,
        dry_run=False,
        log=logged.append,
        superseded={"F10126107", "F10126108"},
    )

    assert retired == 1
    assert orph1.is_file()
    assert not orph2.exists()
    log_text = "\n".join(logged)
    assert "WARNING: could not retire orphan copy F10126107: Permission denied" in log_text
    assert "Retired 1 orphan copy(ies) from _orphans/ (adopted into tree)" in log_text


def test_sweep_supersede_staging_dry_run_reports_real_run_removes(tmp_path):
    (tmp_path / "F10126106 Assembly").mkdir()
    stale = tmp_path / "F10126106 Assembly" / "F10126107.pdf.supersede_tmp.12345"
    stale.write_bytes(b"partial")
    (tmp_path / "_superseded").mkdir()
    nested = tmp_path / "_superseded" / "F10126106.pdf.supersede_tmp.99999"
    nested.write_bytes(b"partial")
    logged = []

    sweep_supersede_staging(tmp_path, True, logged.append)
    assert stale.is_file() and nested.is_file()
    assert sum("[DRY-RUN]" in m for m in logged) == 1

    logged.clear()
    sweep_supersede_staging(tmp_path, False, logged.append)
    assert not stale.exists()
    # System dirs are never swept: staging only happens in the tree, so a
    # matching name under _superseded/ is not ours to delete.
    assert nested.is_file()
    assert sum("removed leftover supersede staging" in m for m in logged) == 1
    # PDFs are never touched.
    pdf = tmp_path / "F10126106 Assembly" / "F10126106.pdf"
    pdf.write_bytes(b"drawing")
    sweep_supersede_staging(tmp_path, False, logged.append)
    assert pdf.is_file()


def test_sweep_supersede_staging_missing_output_is_noop(tmp_path):
    logged = []
    sweep_supersede_staging(tmp_path / "nope", False, logged.append)
    assert logged == []


def test_sweep_removes_copy_tmp_staging(tmp_path):
    d = tmp_path / "F10126106 Assembly"
    d.mkdir()
    leftover = d / "F10126107.pdf.copy_tmp.12345"
    leftover.write_bytes(b"partial")
    lookalike = d / "F10126107.pdf.copy_tmp.notdigits"
    lookalike.write_bytes(b"x")
    logged = []

    sweep_supersede_staging(tmp_path, False, logged.append)

    assert not leftover.exists()
    assert lookalike.is_file()


def test_sweep_copy_tmp_in_system_dirs(tmp_path):
    # _copy_atomic stages orphan copies inside _orphans/, so copy_tmp
    # leftovers are swept there too; supersede staging under a system dir is
    # still left alone (it only ever happens in the tree).
    orphans = tmp_path / "_orphans"
    orphans.mkdir()
    orphan_tmp = orphans / "F10126107.pdf.copy_tmp.12345"
    orphan_tmp.write_bytes(b"partial")
    sup = tmp_path / "_superseded"
    sup.mkdir()
    sup_tmp = sup / "F10126106.pdf.supersede_tmp.99999"
    sup_tmp.write_bytes(b"partial")
    logged = []

    sweep_supersede_staging(tmp_path, False, logged.append)

    assert not orphan_tmp.exists()
    assert sup_tmp.is_file()


def test_place_files_failed_copy_leaves_no_partial_target(tmp_path, monkeypatch):
    # A failure mid-write must not leave a truncated PDF at the target:
    # later runs treat any tree PDF as placed and would never retry it.
    folder = tmp_path / "out"
    pdf = tmp_path / "F10126106.pdf"
    pdf.write_bytes(b"%PDF-real-bytes")
    calls = {"n": 0}
    real = fsops.shutil.copy2

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_bytes(b"partial")
            raise OSError("disk full")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(fsops.shutil, "copy2", flaky)
    target = folder / "F10126106" / "F10126106.pdf"

    place_files({"F10126106": ["F10126106"]}, ["F10126106"],
                {"F10126106": pdf}, folder, False, lambda m: None)
    assert not target.exists()
    assert not [p for p in folder.rglob("*") if "copy_tmp" in p.name]

    # The next run retries and completes.
    place_files({"F10126106": ["F10126106"]}, ["F10126106"],
                {"F10126106": pdf}, folder, False, lambda m: None)
    assert target.read_bytes() == b"%PDF-real-bytes"


def test_copy_orphans_failed_copy_leaves_no_partial(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    pdf = tmp_path / "F10126106.pdf"
    pdf.write_bytes(b"%PDF-real")
    calls = {"n": 0}
    real = fsops.shutil.copy2

    def flaky(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            Path(dst).write_bytes(b"partial")
            raise OSError("boom")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(fsops.shutil, "copy2", flaky)
    target = out / "_orphans" / "F10126106.pdf"

    n1 = copy_orphans(["F10126106"], {"F10126106": pdf}, out, False,
                      lambda m: None)
    assert n1 == 0
    assert not target.exists()

    n2 = copy_orphans(["F10126106"], {"F10126106": pdf}, out, False,
                      lambda m: None)
    assert n2 == 1
    assert target.read_bytes() == b"%PDF-real"


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH semantics")
def test_copy_ops_read_sources_beyond_max_path(tmp_path):
    # LongPathsEnabled=0: open() on an absolute path >= 260 chars fails with
    # ENOENT even though the file exists (real HBCM run: 6 CHK archive copies
    # failed). _native() adds the \\?\ prefix so input-derived copies work.
    seg = "s" * 40
    src_dir = tmp_path
    while len(str(src_dir / "F10126106.pdf")) < 300:
        src_dir = src_dir / seg
    Path("\\\\?\\" + str(src_dir)).mkdir(parents=True)
    src = src_dir / "F10126106.pdf"
    Path("\\\\?\\" + str(src)).write_bytes(b"deep payload")
    assert len(str(src)) >= 260

    out = tmp_path / "out"
    logged = []
    n, archived = copy_superseded({"F10126106": src}, out, False, logged.append)
    assert (n, archived) == (1, {"F10126106"})
    assert (out / "_superseded" / "F10126106.pdf").read_bytes() == b"deep payload"

    tree = tmp_path / "tree"
    assert place_files({"F10126106": []}, ["F10126106"],
                       {"F10126106": src}, tree, False, logged.append) == 1
    assert (tree / "F10126106" / "F10126106.pdf").read_bytes() == b"deep payload"
    assert not any("copy failed" in m for m in logged)
    # _exists sees beyond MAX_PATH; plain Path.exists() returns False there.
    assert fsops._exists(src)
    assert not fsops._exists(src_dir / "F10199999.pdf")
    # Remove the long chain ourselves - plain rmtree cannot reach it.
    import shutil
    shutil.rmtree("\\\\?\\" + str(tmp_path), ignore_errors=True)

def test_native_long_unc_paths_get_unc_prefix():
    if os.name != "nt":
        pytest.skip("Windows path semantics")
    long_unc = "\\\\server\\share\\folder\\" + "s" * 300 + "\\F10126106.pdf"
    out = fsops._native(long_unc)
    assert out == "\\\\?\\UNC\\" + long_unc[2:]


def test_copy_watermarked_refuses_long_destination_symlink(tmp_path):
    # A destination symlink past MAX_PATH makes Path.is_symlink() go blind
    # (lstat ENOENT -> False); the prefixed copy2 would then follow the link
    # and overwrite its referent.
    if os.name != "nt":
        pytest.skip("Windows path semantics")
    import shutil

    seg = "w" * 40
    out = tmp_path
    while len(str(out / "_superseded" / "F10126106.pdf")) < 300:
        out = out / seg
    Path("\\\\?\\" + str(out)).mkdir(parents=True)
    sf = out / "_superseded"
    Path("\\\\?\\" + str(sf)).mkdir()
    referent = tmp_path / "referent.txt"
    referent.write_bytes(b"REFERENT")
    link = sf / "F10126106.pdf"
    try:
        os.symlink(str(referent), "\\\\?\\" + str(link))
    except OSError:
        # Clean up here: pytest's tmp cleanup cannot reach >= 260-char paths.
        shutil.rmtree("\\\\?\\" + str(tmp_path), ignore_errors=True)
        pytest.skip("symlink creation not permitted at long paths")
    src = tmp_path / "F10126106.pdf"
    src.write_bytes(b"new content")
    logged = []

    n = copy_watermarked_duplicates([src], out, False, logged.append)

    assert n == 0
    assert referent.read_bytes() == b"REFERENT"
    assert any("symlink refused" in m for m in logged)
    shutil.rmtree("\\\\?\\" + str(tmp_path), ignore_errors=True)


def test_symlink_guard_checks_through_native_prefix(tmp_path, monkeypatch):
    # Path.is_symlink() goes blind past MAX_PATH (lstat ENOENT -> False), so
    # the copy guards must consult os.path.islink(_native(...)) - otherwise a
    # long destination symlink is followed by the prefixed copy2 and its
    # referent overwritten. The spy also pins the wiring on platforms where
    # creating real symlinks needs privilege we may not hold.
    recorded = []
    real_islink = os.path.islink

    def spy(p):
        recorded.append(p)
        return real_islink(p)

    monkeypatch.setattr(os.path, "islink", spy)
    src = tmp_path / "F10126106.pdf"
    src.write_bytes(b"x")
    (tmp_path / "_superseded").mkdir()
    logged = []

    n = copy_watermarked_duplicates([src], tmp_path, False, logged.append)

    assert n == 1  # no real link here; the copy proceeds
    # The guard's two calls come first; shutil.copy2/copystat may add its own
    # islink(dst) look on top (dst is already prefixed, so that one is blind-
    # proof too - but it never refuses the write, hence the guard).
    assert recorded[:2] == [
        fsops._native(src),
        fsops._native(tmp_path / "_superseded" / "F10126106.pdf"),
    ]
