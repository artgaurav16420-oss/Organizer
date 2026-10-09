"""Golden-log regression: fixed dry-run scenario byte-for-byte.

NOTE: intentional user-facing message changes must update EXPECTED below;
this test fails deliberately on any log-text drift so reports stay diffable.
"""
import os

from fermi_organizer.runmodes import run_full

EXPECTED = (
    "Found 2 active PDF(s)\n"
    "\n"
    "--- Scanning BOM tables ---\n"
    "  F10126106: 1 unique FERMI# (text-fallback)\n"
    "  F10126107: 0 unique FERMI# (text-fallback)\n"
    "\n"
    "--- Scanning USED ON fields ---\n"
    "  F10126107: USED ON F10126106\n"
    "\n"
    "\n"
    "--- Cycle detection ---\n"
    "  No circular references found.\n"
    "\n"
    "--- Root assemblies (1) ---\n"
    "  F10126106 [Parent]\n"
    "\n"
    "--- Creating folder structure ---\n"
    "  [DRY-RUN] mkdir+copy F10126106.pdf -> <OUT>/F10126106 Parent/F10126106.pdf\n"
    "  [DRY-RUN] mkdir+copy F10126107.pdf -> "
    "<OUT>/F10126106 Parent/F10126107 Child part/F10126107.pdf\n"
    "\n"
    "--- Summary ---\n"
    "  PDFs scanned:        2\n"
    "  Root assemblies:     1\n"
    "  BOM edges kept:      1\n"
    "  Cycles broken:       0\n"
    "  PDF copies written:  2"
)


def test_run_full_dry_run_golden_log(tmp_path, make_pdf):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out = tmp_path / "out"
    make_pdf(in_dir / "F10126106.pdf", [
        "FERMI PART LIST",
        "F10126107 CHILD PART",
        "NAME",
        "Parent",
    ])
    make_pdf(in_dir / "F10126107.pdf", [
        "NAME",
        "Child part",
        "USED ON",
        "F10126106",
    ])

    logged = []
    run_full(in_dir, out, True, logged.append, jobs=1)

    text = "\n".join(logged).replace(str(out), "<OUT>").replace(os.sep, "/")
    assert text == EXPECTED
