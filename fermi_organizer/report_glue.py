"""Tree-derived data for the Excel workbook.

The workbook is fed the explicit structured run context returned by
run_full / run_incremental; the human-readable .txt log is user-facing only
and never parsed. `names_from_tree` remains the one structural fallback:
organized tree folders are named 'BASE NAME'.
"""
import os
import re
from collections import defaultdict
from pathlib import Path

from .fsops import is_system_dir, _is_dir_link, _islink


def names_from_tree(output):
    """Stem->NAME from the organized tree itself: root folders are 'BASE NAME'."""
    names = {}
    conflicts = defaultdict(set)
    o = Path(output)
    if not o.is_dir():
        return names
    # Walk top-down so root folders (most authoritative) are seen first;
    # sort each level for deterministic conflict resolution.
    if o.is_dir():
        for dp, dn, _ in os.walk(o):
            dn.sort()
            for name in dn:
                d = Path(dp) / name
                if is_system_dir(d.name) or " " not in d.name:
                    continue
                if _is_dir_link(d) or _islink(d):
                    continue
                rel = d.relative_to(o)
                if is_system_dir(rel.parts[0]):
                    continue
                base, _, name_part = d.name.partition(" ")
                if not re.match(r"^F(?:C)?\d+$", base):
                    continue
                # Prefer the authoritative root-level name over any stale
                # nested copy that happens to share the same base stem.
                if len(rel.parts) != 1 and base in conflicts:
                    continue
                if name_part:
                    conflicts[base].add(name_part)
    for base, cands in conflicts.items():
        if len(cands) == 1:
            names[base] = next(iter(cands))
        else:
            names[base] = sorted(cands)[0]  # deterministic pick on conflict
    return names
