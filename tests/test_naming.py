"""Unit tests: folder naming + path-limit shortening."""
import os
from pathlib import Path

from fermi_organizer.config import MAX_PATH, TREE_MAX_DEPTH
from fermi_organizer.naming import (_collect_placements, _placement_len,
                                    add_bom_name, bom_name_winners,
                                    prefer_bom_names,
                                    build_folder_names, sanitize_folder_name)


def test_sanitize_folder_name_drops_control_and_bidi_chars():
    # NUL previously survived sanitize and crashed mkdir/copy2 with ValueError
    # (outside the OSError-only placement guards); bidi overrides enable
    # display spoofing of folder names.
    assert sanitize_folder_name("BRACKET\x00ASSY") == "BRACKETASSY"
    assert sanitize_folder_name("A\x1b[0m B") == "A[0m B"
    assert sanitize_folder_name("C1\x85NEXT") == "C1NEXT"
    assert sanitize_folder_name("X\u202eREV\tno.") == "XREV no"
    assert sanitize_folder_name("ok\u2066name\u2069") == "okname"


def test_sanitize_folder_name_keeps_legacy_substitutions():
    assert sanitize_folder_name('a"b/c<d>\\e|f?g*') == "ab-c-d--e-f-g-"
    assert sanitize_folder_name("  many   spaces ") == "many spaces"


def test_bom_name_winners_takes_the_majority_description():
    # Four parents list F10128174; one carries a stale description from an
    # earlier revision. First-come would take the stale one (F10119081 sorts
    # first), so the vote - not insertion order - decides.
    votes = {}
    for desc in ("NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD",) * 3 + \
                ("NUT PLATE, 1.33 DIA MINI-CONFLAT, #8-32 THD",):
        add_bom_name(votes, "F10128174", desc)
    assert bom_name_winners(votes) == {
        "F10128174": "NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD"}


def test_bom_name_winners_breaks_ties_deterministically():
    votes = {}
    for desc in ("B NAME", "A NAME"):
        add_bom_name(votes, "F10126106", desc)
    assert bom_name_winners(votes) == {"F10126106": "A NAME"}
    # Blank/whitespace rows never win (or create) a name.
    add_bom_name(votes, "F10126106", "   ")
    add_bom_name(votes, "F10126107", "")
    assert bom_name_winners(votes) == {"F10126106": "A NAME"}


def test_prefer_bom_names_replaces_ocr_and_fills_missing():
    # A scanned drawing's NAME is OCR output, so a parent's parts-list name
    # wins; a text-layer drawing with its own NAME keeps it (authoritative); a
    # drawing with no readable NAME at all is filled from the parts list rather
    # than left as a bare drawing number.
    names = {"F10128174_A_DWG1": "-CONFLAIL, NUT PLATE, MINI xX OQ.",
             "F10187623_A_DWG1": "WELDMENT, BEAM PIPE END"}  # text layer
    # bom_names is keyed by the BOM reference value (base), not by stem.
    bom = {"F10128174": "NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD",
           "F10199999": "SEAL, HEX, 154.8mm OD"}
    stems = {"F10128174_A_DWG1", "F10187623_A_DWG1", "F10199999___DWG1",
             "F10188888___DWG1"}
    out = prefer_bom_names(names, {"F10128174_A_DWG1"}, bom, stems)
    assert out["F10128174_A_DWG1"] == "NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD"
    assert out["F10187623_A_DWG1"] == "WELDMENT, BEAM PIPE END"
    assert out["F10199999___DWG1"] == "SEAL, HEX, 154.8mm OD"  # filled
    assert "F10188888___DWG1" not in out  # no description available
    assert names["F10128174_A_DWG1"] == "-CONFLAIL, NUT PLATE, MINI xX OQ."


def test_shared_stem_deep_path_shortened_for_every_root():
    # Two roots share a sub-assembly chain X -> Y. The second root's name is
    # long enough that its X/Y path only fits after shortening; if the
    # placement walk stops at the first visit of X, that path never reaches
    # _shorten_names and place_files later skips Y under the second root
    # with "path too long".
    base = Path("C:\\" + "d" * 100)
    children = {"F10100001": ["F10100003"],
                "F10100002": ["F10100003"],
                "F10100003": ["F10100004"]}
    index = {s: Path(s + ".pdf") for s in
             ("F10100001", "F10100002", "F10100003", "F10100004")}
    names_of = {"F10100001": "SHORT",
                "F10100002": "L" * 150,
                "F10100003": "X SUBASSY NAME",
                "F10100004": "Y BRACKET NAME"}
    logged = []

    names = build_folder_names(children, ["F10100001", "F10100002"], index,
                               names_of, base, logged.append)

    get_name = lambda c: names[c]
    for root in ("F10100001", "F10100002"):
        comps = (root, "F10100003", "F10100004")
        assert _placement_len(comps, base, index, get_name) <= MAX_PATH, root
    assert not any("still exceed" in m for m in logged)


def test_shorten_continues_past_irreducible_placement():
    # Regression: the old loop stopped at the FIRST over-length placement
    # when it could not shrink (bare base-number stems sit at their floor),
    # so later shrinkable paths stayed over the limit and were skipped.
    base = Path("C:\\" + "d" * 100)
    chain = [f"F1000{n:04d}" for n in range(30)]
    children = {}
    for a, b in zip(chain, chain[1:], strict=False):
        children[a] = [b]
    children["F10999999"] = ["F10999998"]
    index = {s: Path(s + ".pdf") for s in chain + ["F10999999", "F10999998"]}
    names_of = {"F10999999": "R" * 170, "F10999998": "S" * 170}
    logged = []

    names = build_folder_names(children, [chain[0], "F10999999"], index,
                               names_of, base, logged.append)

    get_name = lambda c: names[c]
    for comps in ((chain[0],), ("F10999999",), ("F10999999", "F10999998")):
        assert _placement_len(comps, base, index, get_name) <= MAX_PATH, comps


def test_collect_placements_terminates_on_cyclic_children():
    # Cycles are broken before placement in production, but the walk must
    # still terminate on its own (the depth cap replaced the stem-visit guard).
    children = {"F10100001": ["F10100002"], "F10100002": ["F10100001"]}
    placements = _collect_placements(children, ["F10100001"])
    assert 1 <= len(placements) <= TREE_MAX_DEPTH + 1
    assert all(len(comps) <= TREE_MAX_DEPTH + 1 for comps in placements)


def test_collect_placements_iterative_below_default_recursion_limit():
    # Regression for the iterative rewrite: a 600-deep chain must traverse
    # with a recursion limit far below the traversal depth (the recursive
    # version raised RecursionError here).
    import sys

    children = {f"F{10126000 + i}": [f"F{10126001 + i}"] for i in range(600)}
    children["F10126599"] = []
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        placements = _collect_placements(children, ["F10126000"])
    finally:
        sys.setrecursionlimit(limit)
    assert len(placements) == TREE_MAX_DEPTH + 1


def test_build_folder_names_shortens_long_paths_and_logs_notes():
    base = Path("C:\\" + "x" * 200)
    children = {"F10126106": ["F10126107"]}
    index = {s: Path(s + ".pdf")
             for s in ("F10126106", "F10126107")}
    names_of = {"F10126106": "PARENT ASSEMBLY LONG NAME",
                "F10126107": "CHILD LONG NAME"}
    lines = []

    names = build_folder_names(children, ["F10126106"], index, names_of,
                               base, lines.append)

    assert names == {"F10126106": "F10126106 PARENT ASSEM",
                     "F10126107": "F10126107"}
    assert lines == [
        "  NOTE: folder name shortened to fit path limit: "
        "'F10126106 PARENT ASSEMBLY LONG NAME' -> 'F10126106 PARENT ASSEM'",
        "  NOTE: folder name shortened to fit path limit: "
        "'F10126107 CHILD LONG NAME' -> 'F10126107'",
    ]


def test_build_folder_names_within_limit_keeps_full_names():
    children = {"F10126106": ["F10126107"]}
    index = {s: Path(s + ".pdf")
             for s in ("F10126106", "F10126107")}
    names_of = {"F10126106": "PARENT", "F10126107": "CHILD"}
    lines = []

    names = build_folder_names(children, ["F10126106"], index, names_of,
                               Path("C:\\short"), lines.append)

    assert names == {"F10126106": "F10126106 PARENT",
                     "F10126107": "F10126107 CHILD"}
    assert lines == []


def test_placement_len_single_component():
    base_folder = Path("/base")
    comps = ("F10126106",)
    index = {"F10126106": Path("F10126106.pdf")}
    get_name = lambda c: f"{c} NAME"

    expected_path = os.sep.join([str(base_folder), "F10126106 NAME", "F10126106.pdf"])
    assert _placement_len(comps, base_folder, index, get_name) == len(expected_path)


def test_placement_len_nested_hierarchy():
    base_folder = Path("/base/output")
    comps = ("F10126106", "F10126107", "F10126108")
    index = {
        "F10126106": Path("F10126106.pdf"),
        "F10126107": Path("F10126107.pdf"),
        "F10126108": Path("F10126108.pdf"),
    }
    names = {
        "F10126106": "ROOT ASSY",
        "F10126107": "SUB ASSY",
        "F10126108": "PART",
    }
    get_name = lambda c: f"{c} {names[c]}"

    expected_path = os.sep.join([
        str(base_folder),
        "F10126106 ROOT ASSY",
        "F10126107 SUB ASSY",
        "F10126108 PART",
        "F10126108.pdf",
    ])
    assert _placement_len(comps, base_folder, index, get_name) == len(expected_path)


def test_placement_len_custom_index_filename():
    base_folder = Path("/base")
    comps = ("F10126106", "F10126107")
    index = {
        "F10126106": Path("F10126106.pdf"),
        "F10126107": Path("F10126107_REV_A_SPECIAL.pdf"),
    }
    get_name = lambda c: c

    expected_path = os.sep.join([
        str(base_folder),
        "F10126106",
        "F10126107",
        "F10126107_REV_A_SPECIAL.pdf",
    ])
    assert _placement_len(comps, base_folder, index, get_name) == len(expected_path)


def test_placement_len_base_folder_variations():
    comps = ("F10126106",)
    index = {"F10126106": Path("F10126106.pdf")}
    get_name = lambda c: f"{c} FOLDER"

    # Test relative string path
    base_str = "output"
    expected_path_str = os.sep.join([base_str, "F10126106 FOLDER", "F10126106.pdf"])
    assert _placement_len(comps, base_str, index, get_name) == len(expected_path_str)

    # Test empty string base path
    base_empty = ""
    expected_path_empty = os.sep.join(["", "F10126106 FOLDER", "F10126106.pdf"])
    assert _placement_len(comps, base_empty, index, get_name) == len(expected_path_empty)
