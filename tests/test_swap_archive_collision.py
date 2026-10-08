"""Regression for C-001: supersede archive collision with different content."""
import os
from pathlib import Path

import pytest

from fermi_organizer import fsops
from fermi_organizer.runmodes import _swap_revision_files


def _setup_collision(tmp_path, out_name, old_bytes, archived_bytes, new_bytes=b"new"):
    out = tmp_path / out_name
    sup = out / "_superseded"
    sup.mkdir(parents=True)
    (sup / "F10126107.pdf").write_bytes(archived_bytes)
    old = out / "F10126107 Base part" / "F10126107.pdf"
    old.parent.mkdir(parents=True)
    old.write_bytes(old_bytes)
    new_pdf = tmp_path / "F10126107_A.pdf"
    new_pdf.write_bytes(new_bytes)
    return out, sup, old, new_pdf


def test_swap_archive_collision_suffixes_on_different_content(tmp_path):
    out, sup, old, new_pdf = _setup_collision(
        tmp_path, "out_diff", b"old", b"already archived, different size")
    logged = []
    moved, copies = _swap_revision_files([old], new_pdf, sup, out, False,
                                         logged.append)

    assert copies == 2
    # Pre-existing archive untouched.
    assert (sup / "F10126107.pdf").read_bytes() == b"already archived, different size"
    # Old revision archived under numeric suffix with correct contents.
    assert (sup / "F10126107.1.pdf").read_bytes() == b"old"
    # Tree holds the new revision, old tree copy gone.
    assert (old.parent / "F10126107_A.pdf").read_bytes() == b"new"
    assert not old.exists()
    assert moved == [old.parent / "F10126107_A.pdf"]
    text = "\n".join(logged)
    assert "superseded:" in text
    assert "_superseded/F10126107.1.pdf" in text


def test_swap_archive_collision_skips_on_identical_content(tmp_path):
    out, sup, old, new_pdf = _setup_collision(
        tmp_path, "out_same", b"old", b"old")
    logged = []
    moved, copies = _swap_revision_files([old], new_pdf, sup, out, False,
                                         logged.append)

    assert copies == 1
    assert (sup / "F10126107.pdf").read_bytes() == b"old"
    assert not (sup / "F10126107.1.pdf").exists()
    assert (old.parent / "F10126107_A.pdf").read_bytes() == b"new"
    assert not old.exists()
    assert moved == [old.parent / "F10126107_A.pdf"]


def test_swap_archive_collision_increments_suffix_until_free(tmp_path):
    out, sup, old, new_pdf = _setup_collision(
        tmp_path, "out_incr", b"old", b"already archived, different size")
    (sup / "F10126107.1.pdf").write_bytes(b"taken, different size....")
    logged = []
    moved, copies = _swap_revision_files([old], new_pdf, sup, out, False,
                                         logged.append)

    assert copies == 2
    assert (sup / "F10126107.pdf").read_bytes() == b"already archived, different size"
    assert (sup / "F10126107.1.pdf").read_bytes() == b"taken, different size...."
    assert (sup / "F10126107.2.pdf").read_bytes() == b"old"
    assert (old.parent / "F10126107_A.pdf").read_bytes() == b"new"
    assert not old.exists()
    assert "_superseded/F10126107.2.pdf" in "\n".join(logged)


def test_swap_archive_collision_dry_run_logs_suffixed_name(tmp_path):
    out, sup, old, new_pdf = _setup_collision(
        tmp_path, "out_dry", b"old", b"already archived, different size")
    logged = []
    moved, copies = _swap_revision_files([old], new_pdf, sup, out, True,
                                         logged.append)

    assert copies == 2
    # Dry-run touches nothing on disk.
    assert old.is_file()
    assert not (sup / "F10126107.1.pdf").exists()
    assert not (old.parent / "F10126107_A.pdf").exists()
    text = "\n".join(logged)
    assert "_superseded/F10126107.1.pdf" in text
    assert "(archive skipped: exists)" not in text
    assert moved == [old.parent / "F10126107_A.pdf"]


def test_swap_archive_collision_suffixes_on_same_size_different_content(tmp_path):
    # Byte-size equality is not identity: same length, different bytes must
    # archive under a numeric suffix instead of deleting the only clean copy.
    out, sup, old, new_pdf = _setup_collision(
        tmp_path, "out_samesize", b"abcd", b"WXYZ")
    logged = []
    moved, copies = _swap_revision_files([old], new_pdf, sup, out, False,
                                         logged.append)

    assert copies == 2
    # Pre-existing archive untouched.
    assert (sup / "F10126107.pdf").read_bytes() == b"WXYZ"
    # Old revision archived under numeric suffix with correct contents.
    assert (sup / "F10126107.1.pdf").read_bytes() == b"abcd"
    # Tree holds the new revision, old tree copy gone.
    assert (old.parent / "F10126107_A.pdf").read_bytes() == b"new"
    assert not old.exists()
    assert moved == [old.parent / "F10126107_A.pdf"]
    text = "\n".join(logged)
    assert "superseded:" in text
    assert "_superseded/F10126107.1.pdf" in text


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH semantics")
def test_swap_revision_files_beyond_max_path(tmp_path):
    # Deep output root: the _exists/archive check, staging, os.replace and the
    # final unlink all sit past 260 chars - every step needs _native()/_exists,
    # or the swap silently skips and leaves a duplicate in the tree.
    seg = "o" * 40
    out = tmp_path
    while len(str(out / "_superseded" / "F10126106.pdf")) < 300:
        out = out / seg
    Path("\\\\?\\" + str(out)).mkdir(parents=True)
    sup = out / "_superseded"
    Path("\\\\?\\" + str(sup)).mkdir()
    old_dir = out / "F10126106 Assembly"
    Path("\\\\?\\" + str(old_dir)).mkdir()
    old = old_dir / "F10126106.pdf"
    Path("\\\\?\\" + str(old)).write_bytes(b"old-rev")
    new_pdf = tmp_path / "F10126106_A.pdf"  # input side stays short
    new_pdf.write_bytes(b"new-rev")
    logged = []

    moved, copies = _swap_revision_files([old], new_pdf, sup, out, False,
                                         logged.append)

    def read(p):
        return Path("\\\\?\\" + str(p)).read_bytes()

    assert copies == 2
    assert moved == [old_dir / "F10126106_A.pdf"]
    assert read(old_dir / "F10126106_A.pdf") == b"new-rev"   # replace worked
    assert not fsops._exists(old)                            # unlink worked
    assert read(sup / "F10126106.pdf") == b"old-rev"         # archived
    assert not any("supersede failed" in m or "left in tree" in m
                   for m in logged)
    import shutil
    shutil.rmtree("\\\\?\\" + str(tmp_path), ignore_errors=True)
