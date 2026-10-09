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
                                   scan_output_tree, sweep_supersede_staging)


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
        "Skipped 1 PDF(s) under Output/ (organized copies are never input)",
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

    n, archived = copy_superseded({"F10126106": old_pdf}, out, False,
                                  lambda m: None, overwrite=True)

    assert (n, archived) == (1, {"F10126106"})
    assert calls == []


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

    n = copy_watermarked_duplicates([w1], out, False, lambda m: None)

    assert n == 1
    assert calls == []


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
        if Path(dst).name == "F10126106.pdf":
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

    # Superseded stem retires even though not live in the tree.
    logged = []
    n = retire_adopted_orphans({"F10126107": sup_orph, "F10126108": unrelated_orph},
                               out, False, logged.append,
                               superseded={"F10126107"})
    assert n == 1
    assert not sup_orph.exists()
    assert unrelated_orph.is_file()


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
    orphans_dir.mkdir(parents=True)
    orph1 = orphans_dir / "F10126107.pdf"
    orph2 = orphans_dir / "F10126108.pdf"
    orph1.write_bytes(b"orph1")
    orph2.write_bytes(b"orph2")

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
