"""Unit tests: folder naming + path-limit shortening."""
import os
from pathlib import Path

from fermi_organizer.config import MAX_PATH, TREE_MAX_DEPTH
from fermi_organizer.naming import (_collect_placements, _placement_len,
                                    build_folder_names)


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


def test_collect_placements_terminates_on_cyclic_children():
    # Cycles are broken before placement in production, but the walk must
    # still terminate on its own (the depth cap replaced the stem-visit guard).
    children = {"F10100001": ["F10100002"], "F10100002": ["F10100001"]}
    placements = _collect_placements(children, ["F10100001"])
    assert 1 <= len(placements) <= TREE_MAX_DEPTH + 1
    assert all(len(comps) <= TREE_MAX_DEPTH + 1 for comps in placements)


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
