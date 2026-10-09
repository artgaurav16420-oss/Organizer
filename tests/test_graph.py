"""Unit tests for fermi_organizer.graph."""
from collections import defaultdict
from pathlib import Path

from fermi_organizer.config import MAX_PATH
from fermi_organizer.graph import (_edit1, is_chk_stem, revision_letter_rank,
                                   revision_rank, split_superseded,
                                   match_pdfs, used_on_bases,
                                   find_used_on_mismatches, find_used_on_bugs,
                                   can_reach, collect_reachable, break_cycles,
                                   snap_ref)
from fermi_organizer.naming import (sanitize_folder_name,
                                    folder_name_for, build_folder_names)


def test_edit1():
    # Identical strings: 0 edits -> False
    assert not _edit1("F10126106", "F10126106")
    assert not _edit1("", "")

    # Length difference > 1 -> False
    assert not _edit1("F10126106", "F101261")
    assert not _edit1("F101261", "F10126106")

    # Equal length: exactly 1 substitution -> True
    assert _edit1("F10126106", "F10126107")
    assert _edit1("cat", "bat")
    assert _edit1("cat", "cot")
    assert _edit1("cat", "car")

    # Equal length: > 1 substitutions -> False
    assert not _edit1("cat", "dog")
    assert not _edit1("F10126106", "F10126177")

    # Length diff 1: exactly 1 insertion / deletion -> True
    # Start
    assert _edit1("cat", "scat")
    assert _edit1("scat", "cat")
    # Middle
    assert _edit1("cat", "cart")
    assert _edit1("cart", "cat")
    # End
    assert _edit1("cat", "cats")
    assert _edit1("cats", "cat")
    # Empty vs single char
    assert _edit1("", "a")
    assert _edit1("a", "")

    # Length diff 1: multiple character mismatches -> False
    assert not _edit1("abc", "ax")
    assert not _edit1("ax", "abc")

    # Symmetry check
    pairs = [
        ("abc", "abc"),
        ("abc", "a"),
        ("cat", "bat"),
        ("cat", "dog"),
        ("cat", "scat"),
        ("abc", "ax"),
    ]
    for x, y in pairs:
        assert _edit1(x, y) == _edit1(y, x)


def test_is_chk_stem():
    assert is_chk_stem("F10126106_CHK")
    assert is_chk_stem("F10126106_CHK_A")
    assert is_chk_stem("F10126106_A_CHK")
    assert not is_chk_stem("F10126106")
    assert not is_chk_stem("F10126106_A")


def test_snap_ref_unique_near_miss_and_confusions():
    stems = {"F10126106", "F10126107", "F10126108_A"}
    # Exact/base matches need no snapping.
    assert snap_ref("F10126107", stems) is None
    assert snap_ref("F10126108", stems) is None
    # A clean F+8-digit read is trusted even when one digit from a known base:
    # it is either right or a genuine missing reference (Teamcenter list).
    assert snap_ref("F10116106", stems) is None
    assert snap_ref("F10126105", stems) is None
    # Wrong length (9 digits): the extra digit is dropped when unique.
    assert snap_ref("F101261066", stems) == "F10126106"
    assert snap_ref("F1012610", stems) is None  # one edit from two bases
    # OCR confusions (O->0) applied before the membership check.
    assert snap_ref("F1O1261O6", stems) == "F10126106"
    assert snap_ref("F101261O6", stems) == "F10126106"
    # No candidate at all stays untouched.
    assert snap_ref("F99999999", stems) is None
    assert snap_ref("", stems) is None


def test_snap_ref_ambiguous_returns_none():
    stems = {"F10126105", "F10126107"}
    # One edit away from both bases: never rewrite arbitrarily.
    assert snap_ref("F10126106", stems) is None


def test_revision_letter_rank():
    assert revision_letter_rank("F10126106") == 0
    assert revision_letter_rank("F10126106___") == 0
    assert revision_letter_rank("F10126106_A") == 1
    assert revision_letter_rank("F10126106_a") == 1
    assert revision_letter_rank("F10126106_B") == 2
    assert revision_letter_rank("F10126106_CHK") == 0


def test_revision_rank_chk_below_all_dwg():
    assert revision_rank("F10126106_CHK") == -1000
    assert revision_rank("F10126106") == 0
    assert revision_rank("F10126106_A") == 1
    assert revision_rank("F10126106_B") == 2
    assert (revision_rank("F10126106_CHK")
            < revision_rank("F10126106")
            < revision_rank("F10126106_A")
            < revision_rank("F10126106_B"))


def test_split_superseded_keeps_highest_revision_per_base(tmp_path):
    dummy = tmp_path / "dummy.pdf"
    index = {
        "F10126106": dummy,
        "F10126106_A": dummy,
        "F10126106_B": dummy,
        "F10126107": dummy,
        "F10126107_CHK": dummy,
        "F10126108_A": dummy,
    }
    active, old = split_superseded(index)
    assert set(active) == {"F10126106_B", "F10126107", "F10126108_A"}
    assert set(old) == {"F10126106", "F10126106_A", "F10126107_CHK"}
    assert set(active) | set(old) == set(index)


def test_match_pdfs_exact_and_revision_pick():
    stems = {"F10126107", "F10126107_A", "F10126107_B", "F10126108"}
    assert match_pdfs("F10126107", stems) == ["F10126107"]
    assert match_pdfs("F10126107", {"F10126107_A", "F10126107_B"}) == ["F10126107_B"]
    assert match_pdfs("F10126107", {"F10126107_C"}) == ["F10126107_C"]
    assert match_pdfs("F10126107_A", stems) == ["F10126107_A"]
    assert match_pdfs("F10126109", stems) == []


def test_match_pdfs_negative_lookahead_blocks_loose_prefix():
    assert match_pdfs("F12345", {"F123456"}) == []
    assert match_pdfs("F12345", {"F123456", "F123457_A"}) == []


def test_sanitize_folder_name_windows_illegal_chars():
    assert sanitize_folder_name('A/B:C*D?E"F<G>H|I') == "A-B-C-D-EF-G-H-I"
    assert sanitize_folder_name("a\\b") == "a-b"
    assert sanitize_folder_name("Name...") == "Name"
    assert sanitize_folder_name("  A   B  ") == "A B"


def test_folder_name_for_uses_base_and_name():
    assert folder_name_for("F10126106", {"F10126106": 'Assembly "bracket"'}) == \
        "F10126106 Assembly bracket"
    assert folder_name_for("F10126106_A", {"F10126106_A": "Bracket"}) == "F10126106 Bracket"
    assert folder_name_for("F10126106", {}) == "F10126106"
    assert folder_name_for("F10126106", {"F10126106": "   "}) == "F10126106"


def test_used_on_bases():
    assert used_on_bases(["F10126106", "f10126107_a", "FC100", "junk", None]) == \
        {"F10126106", "F10126107"}


def test_find_used_on_mismatches_flags_missing_parent():
    out = find_used_on_mismatches({"F10126106": {"F10126107"}}, {"F10126107": []})
    assert out == [("F10126106", "F10126107", [])]


def test_find_used_on_mismatches_revision_insensitive():
    out = find_used_on_mismatches({"F10126106": {"F10126107"}},
                                  {"F10126107": ["F10126106_A"]})
    assert out == []


def test_find_used_on_mismatches_reports_actual_values():
    out = find_used_on_mismatches(
        {"F10126106": {"F10126107"}},
        {"F10126107": ["F10000001", "F10000002", "F10000001"]})
    assert out == [("F10126106", "F10126107", ["F10000001", "F10000002"])]


def test_find_used_on_mismatches_skips_self_edges_and_sorts():
    out = find_used_on_mismatches(
        {"F10126106": {"F10126107", "F10126106"}, "F10126107": {"F10126108"}},
        {"F10126107": [], "F10126108": ["F10126107"]})
    assert out == [("F10126106", "F10126107", [])]


def test_find_used_on_bugs_flags_parent_not_in_bom():
    bom_edges = {"F10126106": {"F10126107"}}
    used_on_of = {"F10126107": ["F10126106", "F10126108"]}
    stems = {"F10126106", "F10126107", "F10126108"}
    # F10126108 is named in C's USED ON but its BOM does not list C.
    assert find_used_on_bugs(bom_edges, used_on_of, stems) == \
        [("F10126108", "F10126107")]
    # Duplicate USED ON values still report a single bug entry.
    assert find_used_on_bugs(
        bom_edges, {"F10126107": ["F10126108", "F10126108"]}, stems) == \
        [("F10126108", "F10126107")]


def test_find_used_on_bugs_ignores_consistent_used_on():
    bom_edges = {"F10126106": {"F10126107"}}
    used_on_of = {"F10126107": ["F10126106"]}
    stems = {"F10126106", "F10126107"}
    assert find_used_on_bugs(bom_edges, used_on_of, stems) == []


def test_can_reach_bfs_and_cycles():
    children = {"A": {"B"}, "B": {"C"}, "C": set(), "X": {"Y"}, "Y": {"X"}}
    assert can_reach(children, "A", "B")
    assert can_reach(children, "A", "C")
    assert not can_reach(children, "C", "A")
    assert can_reach(children, "A", "A")          # start == target
    assert not can_reach(children, "X", "C")
    assert can_reach(children, "Y", "X")          # cycle terminates
    assert not can_reach(children, "A", "MISSING")


def test_break_cycles_breaks_a_bom_cycle():
    # BOM cycle A -> B -> A: one back-edge is removed, the cycle is gone.
    children = defaultdict(set, {"A": {"B"}, "B": {"A"}})
    parents = defaultdict(set, {"A": {"B"}, "B": {"A"}})
    removed = break_cycles(children, parents, lambda msg: None)

    assert len(removed) == 1
    a, b = removed[0]
    assert {a, b} == {"A", "B"}
    assert not (b in children.get(a, ()) and a in children.get(b, ()))


def test_break_cycles_iterative_below_default_recursion_limit():
    # Regression for the iterative rewrite: a 600-deep chain must traverse
    # with a recursion limit far below the traversal depth (the recursive
    # version raised RecursionError here).
    import sys

    children = {f"F{10126000 + i}": [f"F{10126001 + i}"] for i in range(600)}
    children["F10126599"] = []
    logged = []
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(300)
    try:
        removed = break_cycles(children, {}, logged.append)
    finally:
        sys.setrecursionlimit(limit)
    assert removed == []
    assert sum("cycle search stopped" in m for m in logged) == 1


def test_collect_reachable_includes_roots_and_terminates_on_cycles():
    children = {"A": {"B", "C"}, "B": {"A"}, "C": set()}
    assert collect_reachable(children, ["A"]) == {"A", "B", "C"}
    assert collect_reachable(children, ["D"]) == {"D"}
    assert collect_reachable(children, []) == set()


def test_build_folder_names_shortens_to_path_limit(tmp_path):
    base_folder = Path("C:/" + "d" * 60)
    long_parent = "Assembly of the cryogenic system with a deliberately long descriptive name " * 4
    long_child = "Bracket sub-component with an equally long descriptive parent name " * 4
    children = {"F10126106": {"F10126107"}}
    roots = ["F10126106"]
    index = {"F10126106": Path("F10126106.pdf"),
             "F10126107": Path("F10126107.pdf")}
    names_of = {"F10126106": long_parent, "F10126107": long_child}
    logged = []

    names = build_folder_names(children, roots, index, names_of,
                               base_folder, logged.append)

    root_target = base_folder / names["F10126106"] / index["F10126106"].name
    child_target = (base_folder / names["F10126106"] / names["F10126107"]
                    / index["F10126107"].name)
    assert len(str(root_target)) <= MAX_PATH
    assert len(str(child_target)) <= MAX_PATH
    for stem in ("F10126106", "F10126107"):
        assert names[stem].startswith(stem.split("_")[0])
        assert names[stem] != folder_name_for(stem, names_of)
    assert any("folder name shortened" in m for m in logged)
    assert not any("still exceed" in m for m in logged)
