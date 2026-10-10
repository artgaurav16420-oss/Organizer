#!/usr/bin/env python3
"""Folder naming and path-length policy."""
import os
import re

from .config import MAX_PATH, NAME_SHORTEN_MAX_PASSES, TREE_MAX_DEPTH


def sanitize_folder_name(name):
    """Make a drawing name safe for a Windows folder name."""
    # Drop C0/C1 control chars (NUL would crash mkdir/copy2 with ValueError,
    # past the OSError-only placement guards) and bidi overrides (display
    # spoofing in Explorer); whitespace is collapsed below, not dropped.
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]",
               "", name)
    s = s.replace('"', "").replace("/", "-")
    s = re.sub(r'[<>:\\|?*]', "-", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".")
    return s


def folder_name_for(stem, names_of):
    """Folder name: '{base number} {NAME}', fallback to stem."""
    names_of = names_of or {}
    base = stem.split("_")[0]
    name = names_of.get(stem)
    if name:
        clean = sanitize_folder_name(name)
        if clean:
            return f"{base} {clean}"
    return stem


def _collect_placements(children, roots):
    """Root-to-leaf component tuples, pre-order; roots and children sorted.

    Every root-to-leaf path is collected: a stem shared by two parents must
    be shortened for both chains (place_files copies it under both), so the
    walk must not stop at a stem's first visit. Cycles are broken before
    placement; the depth cap bounds any cyclic input that slips through.
    """
    placements = []
    seen = set()

    # Iterative pre-order walk (explicit stack, children pushed in reverse so
    # they pop in sorted order): placements must keep the exact recursion
    # order (_shorten_names shortens in that order), and depth up to
    # TREE_MAX_DEPTH must not depend on the recursion limit.
    for root in sorted(roots):
        stack = [(root, (root,), 0)]
        while stack:
            stem, comps, depth = stack.pop()
            key = tuple(comps)
            if key in seen or depth > TREE_MAX_DEPTH:
                continue
            seen.add(key)
            placements.append(key)
            for child in reversed(sorted(children.get(stem, ()))):
                stack.append((child, comps + (child,), depth + 1))
    return placements


def _placement_len(comps, base_folder, index, get_name):
    parts = [str(base_folder)]
    for c in comps:
        parts.append(get_name(c))
    parts.append(index[comps[-1]].name)
    return len(os.sep.join(parts))


def _shorten_names(placements, names, get_name, total_len):
    """Trim folder names in place until every path fits MAX_PATH or no progress.

    Every over-length placement is attempted each pass: the first one may be
    irreducible (bare base-number stems sit at their floor), and stopping
    there would leave later, shrinkable paths over the limit and skipped at
    placement time."""
    cache = {}
    for _ in range(NAME_SHORTEN_MAX_PASSES):
        changed = False
        for comps in placements:
            tl = cache.get(comps)
            if tl is None:
                tl = total_len(comps)
                cache[comps] = tl
            if tl <= MAX_PATH:
                continue
            excess = tl - MAX_PATH
            renamed = set()
            for c in reversed(comps):
                cur = get_name(c)
                floor = len(c.split("_")[0])  # base part number, e.g. F10126106
                if len(cur) > floor:
                    new_len = max(floor, len(cur) - excess)
                    new_name = cur[:new_len].rstrip(" ,-._")
                    if len(new_name) < floor:
                        new_name = cur[:floor]
                    if new_name != cur:
                        names[c] = new_name
                        renamed.add(c)
                        excess -= len(cur) - len(new_name)
                        if excess <= 0:
                            break
            if renamed:
                changed = True
                for key in list(cache):
                    if any(c in renamed for c in key):
                        cache.pop(key, None)
        if not changed:
            break


def _log_name_shortening(placements, names, get_name, index, names_of, total_len, log):
    # Placements that still exceed the limit after shortening will be skipped.
    still_over = [comps for comps in placements if total_len(comps) > MAX_PATH]
    if still_over:
        log(f"  WARNING: {len(still_over)} placement(s) still exceed {MAX_PATH} chars "
            f"after shortening (will be skipped):")
        for comps in still_over[:5]:
            path_disp = os.sep.join(get_name(c) for c in comps) + os.sep + index[comps[-1]].name
            log(f"    {path_disp}")
        if len(still_over) > 5:
            log(f"    ... and {len(still_over) - 5} more")

    for stem in sorted(names):
        full = folder_name_for(stem, names_of)
        if names[stem] != full:
            log(f"  NOTE: folder name shortened to fit path limit: {full!r} -> {names[stem]!r}")


def build_folder_names(children, roots, index, names_of, base_folder, log):
    """Compute folder names for a subtree, shortened so all target paths fit the Windows limit."""
    names = {}

    def get_name(stem):
        if stem not in names:
            names[stem] = folder_name_for(stem, names_of)
        return names[stem]

    placements = _collect_placements(children, roots)

    def total_len(comps):
        return _placement_len(comps, base_folder, index, get_name)

    _shorten_names(placements, names, get_name, total_len)
    _log_name_shortening(placements, names, get_name, index, names_of, total_len, log)
    return names
