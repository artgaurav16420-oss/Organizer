"""Tree-derived data for the Excel workbook.

The workbook is fed the explicit structured run context returned by
run_full / run_incremental; the human-readable .txt log is user-facing only
and never parsed. `names_from_tree` remains the one structural fallback:
organized tree folders are named 'BASE NAME'.
"""
import re
from collections import defaultdict
from pathlib import Path

from .fsops import is_system_dir


def names_from_tree(output):
    """Stem->NAME from the organized tree itself: root folders are 'BASE NAME'."""
    names = {}
    conflicts = defaultdict(set)
    o = Path(output)
    if not o.is_dir():
        return names
    # root folders first (most authoritative), then nested folders
    for d in sorted(o.rglob("*/")):
        if is_system_dir(d.name) or " " not in d.name:
            continue
        rel = d.relative_to(o)
        if is_system_dir(rel.parts[0]):
            continue
        base, _, name = d.name.partition(" ")
        if not re.match(r"^F(?:C)?\d+$", base):
            continue
        if name:
            conflicts[base].add(name)
    for base, cands in conflicts.items():
        if len(cands) == 1:
            names[base] = next(iter(cands))
        else:
            names[base] = sorted(cands)[0]  # deterministic pick on conflict
    return names
