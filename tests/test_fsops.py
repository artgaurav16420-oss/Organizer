"""Unit tests: system-dir convention, output tree scan, superseded copies."""
import os
from pathlib import Path

from fermi_organizer import fsops
from fermi_organizer.config import canonical_stem
from fermi_organizer.fsops import (build_pdf_index, copy_superseded,
                                   find_organized_pdfs, is_system_dir,
                                   place_files, retire_adopted_orphans,
                                   scan_output_tree)


def test_is_system_dir_convention():
    assert is_system_dir("_superseded")
    assert is_system_dir("_orphans")
    assert is_system_dir("_anything")
    assert not is_system_dir("F10126106 Assembly")


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
