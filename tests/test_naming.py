"""Unit tests: folder naming + path-limit shortening."""
from pathlib import Path

from fermi_organizer.naming import build_folder_names


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
