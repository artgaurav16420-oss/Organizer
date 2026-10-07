"""Unit tests: folder naming + path-limit shortening."""
import os
from pathlib import Path

from fermi_organizer.naming import _placement_len, build_folder_names


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
