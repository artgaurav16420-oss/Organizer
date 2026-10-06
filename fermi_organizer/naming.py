#!/usr/bin/env python3
"""Folder naming and path-length policy."""
import os
import re

from .config import MAX_PATH, NAME_SHORTEN_MAX_PASSES


def sanitize_folder_name(name):
    """Make a drawing name safe for a Windows folder name."""
    s = name.replace('"', "").replace("/", "-")
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
    """Root-to-leaf component tuples, pre-order; roots and children sorted."""
    placements = []
    seen = set()
    visited = set()

    def walk(stem, comps):
        key = tuple(comps)
        if key in seen:
            return
        seen.add(key)
        placements.append(key)
        if stem in visited:
            return
        visited.add(stem)
        for child in sorted(children.get(stem, ())):
            walk(child, comps + (child,))

    for root in sorted(roots):
        walk(root, (root,))
    return placements


def _placement_len(comps, base_folder, index, get_name):
    p = str(base_folder)
    for c in comps:
        p += os.sep + get_name(c)
    p += os.sep + index[comps[-1]].name
    return len(p)


def _shorten_names(placements, names, get_name, total_len):
    """Trim folder names in place until every path fits MAX_PATH or no progress."""
    cache = {}
    for _ in range(NAME_SHORTEN_MAX_PASSES):
        over = None
        for comps in placements:
            tl = cache.get(comps)
            if tl is None:
                tl = total_len(comps)
                cache[comps] = tl
            if tl > MAX_PATH:
                over = (comps, tl)
                break
        if over is None:
            break
        comps, tl = over
        excess = tl - MAX_PATH
        changed = False
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
                    changed = True
                    if excess <= 0:
                        break
        if renamed:
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
