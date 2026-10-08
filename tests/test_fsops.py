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

    assert copy_superseded({"F10126106": old_pdf}, out, False, logged.append) == 1
    assert target.read_text() == "v1"

    target.write_text("v2")
    assert copy_superseded({"F10126106": old_pdf}, out, False, logged.append) == 0
    assert target.read_text() == "v2"

    assert copy_superseded({"F10126106": old_pdf}, out, False, logged.append,
                           overwrite=True) == 1
    assert target.read_text() == "v1"


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

    n = copy_superseded({"F10126106": bad_pdf, "F10126107": good_pdf},
                        out, False, logged.append)

    assert n == 1
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
    assert sum("[DRY-RUN]" in m for m in logged) == 2

    logged.clear()
    sweep_supersede_staging(tmp_path, False, logged.append)
    assert not stale.exists() and not nested.exists()
    assert sum("removed leftover supersede staging" in m for m in logged) == 2
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
    import shutil

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
    assert copy_superseded({"F10126106": src}, out, False, logged.append) == 1
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
