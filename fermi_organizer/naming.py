#!/usr/bin/env python3
"""Folder naming and path-length policy."""
import os
import re
from collections import Counter

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


def add_bom_name(votes, ref, desc):
    """Record one parts-list description for `ref` (input to the vote)."""
    desc = (desc or "").strip()
    if desc:
        votes.setdefault(ref, Counter())[desc] += 1


def bom_name_winners(votes):
    """ref -> best description: most frequent, ties broken lexicographically.

    Parents can disagree about a child's description (a stale parts list keeps
    an old revision's wording). The majority is the current one; the
    lexicographic tie-break keeps the pick deterministic across runs and
    parallel job counts.
    """
    return {ref: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            for ref, c in (votes or {}).items()}


def prefer_bom_names(names_of, ocr_named_stems, bom_names, stems=()):
    """Take folder NAMEs from a parent's parts list where the drawing has none to trust.

    An image-only drawing's own NAME is OCR output, so a misread still reads
    as a plausible part name ('NUT PLATE, MINI-CONFLAT' -> '-CONFLAIL, NUT
    PLATE, MINI xX OQ'); a parent's PARTS LIST row carries the same name as a
    text layer, so it wins. A drawing whose NAME could not be read at all (no
    OCR name, no title-block line) is filled the same way rather than left as a
    bare drawing number. Text-layer drawings with their own NAME keep it - that
    one is authoritative.

    `bom_names` is keyed by the BOM reference value (the base number), while
    `names_of` is keyed by stem, so a stem is matched through its base.

    Returns a new dict (callers keep their own name map).
    """
    names = dict(names_of or {})
    bom_names = bom_names or {}
    ocr_named = ocr_named_stems or set()
    for stem in set(stems or ()) | set(ocr_named):
        if stem in names and stem not in ocr_named:
            continue
        bom_name = bom_names.get(stem) or bom_names.get(stem.split("_")[0])
        if bom_name:
            names[stem] = bom_name
    return names


class PlacementPlanTooLarge(RuntimeError):
    """The name plan would enumerate more root-to-leaf paths than allowed.

    Raised from the placement walk as soon as the limit is passed, so a diamond
    DAG in the tree cannot exhaust time and memory before the caller's fan-out
    cap is consulted (measured: 3.1M placements / 805 MB on a 41-node ladder).
    """


def _collect_placements(children, roots, limit=None):
    """Root-to-leaf component tuples, pre-order; roots and children sorted.

    Every root-to-leaf path is collected: a stem shared by two parents must
    be shortened for both chains (place_files copies it under both), so the
    walk must not stop at a stem's first visit. Cycles are broken before
    placement; the depth cap bounds any cyclic input that slips through.
    With `limit`, the walk stops by raising PlacementPlanTooLarge as soon as
    more than `limit` distinct paths have been collected.
    """
    placements = []
    seen = set()

    # Iterative pre-order walk with a single mutable path list (F-PERF-07):
    # avoid O(depth) tuple concat on every child edge; convert to a tuple only
    # when recording a placement or testing membership. Children are consumed
    # through a per-frame iterator in sorted order.
    for root in sorted(roots):
        root_key = (root,)
        if root_key in seen:
            continue
        seen.add(root_key)
        placements.append(root_key)
        if limit is not None and len(placements) > limit:
            raise PlacementPlanTooLarge(len(placements))
        path = [root]
        stack = [(root, iter(sorted(children.get(root, ()))), 0)]
        while stack:
            stem, it, depth = stack[-1]
            child = next(it, None)
            if child is None:
                stack.pop()
                path.pop()
                continue
            child_depth = depth + 1
            if child_depth > TREE_MAX_DEPTH:
                continue
            path.append(child)
            key = tuple(path)
            if key in seen:
                path.pop()
                continue
            seen.add(key)
            placements.append(key)
            if limit is not None and len(placements) > limit:
                raise PlacementPlanTooLarge(len(placements))
            stack.append((child, iter(sorted(children.get(child, ()))), child_depth))
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


def build_folder_names(children, roots, index, names_of, base_folder, log,
                       limit=None):
    """Compute folder names for a subtree, shortened so all target paths fit the Windows limit.

    `limit` bounds the placement walk (see _collect_placements): the plan is
    refused, not truncated, because a partial plan would name folders that the
    full walk would have shortened differently.
    """
    names = {}

    def get_name(stem):
        if stem not in names:
            names[stem] = folder_name_for(stem, names_of)
        return names[stem]

    placements = _collect_placements(children, roots, limit=limit)

    def total_len(comps):
        return _placement_len(comps, base_folder, index, get_name)

    _shorten_names(placements, names, get_name, total_len)
    _log_name_shortening(placements, names, get_name, index, names_of, total_len, log)
    return names
