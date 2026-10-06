#!/usr/bin/env python3
"""Pure graph/revision algorithms (no filesystem side effects)."""
import re
from collections import defaultdict

from .config import TREE_MAX_DEPTH, is_processable_ref


def is_chk_stem(stem):
    """True if the stem beyond the base drawing number marks an unapproved CHK drawing."""
    base = stem.split("_")[0]
    return "CHK" in stem[len(base):].upper()


def revision_letter_rank(stem):
    """Letter rank of the revision token: ___ (no rev) = 0, A = 1, B = 2, etc."""
    parts = stem.split("_")
    if len(parts) < 2 or not parts[1]:
        return 0
    rev = parts[1]
    if len(rev) == 1 and rev.isalpha():
        return ord(rev.upper()) - ord("A") + 1
    return 0


def revision_rank(stem):
    """Rank revisions: CHK = -1000+letter (unapproved, loses to any DWG but still
    ordered by its own letter), ___ (no rev) = 0, A = 1, B = 2, etc."""
    if is_chk_stem(stem):
        # CHK drawings are unapproved: rank below ALL DWG ranks but still order
        # CHK revisions among themselves (A_CHK < B_CHK).  -1000 floor + letter
        # rank (max 26) keeps them ordered and below the lowest DWG rank (0).
        return -1000 + revision_letter_rank(stem)
    return revision_letter_rank(stem)


def split_superseded(index):
    """Split index into (active, superseded). Keep highest revision per base."""
    latest = {}
    for s in index:
        b = s.split("_")[0]
        if b not in latest or revision_rank(s) > revision_rank(latest[b]):
            latest[b] = s
    active = {s: p for s, p in index.items() if latest[s.split("_")[0]] == s}
    old = {s: p for s, p in index.items() if s not in active}
    return active, old


def used_on_bases(used_vals):
    """Base drawing numbers (F + digits) found in a USED ON value list."""
    bases = set()
    for v in used_vals or ():
        m = re.match(r"(F\d+)", (v or "").upper())
        if m:
            bases.add(m.group(1))
    return bases


def find_used_on_mismatches(bom_edges, used_on_of):
    """BOM edges whose child USED ON omits the parent.

    bom_edges: {parent_stem: iterable of child stems} (BOM-derived only).
    used_on_of: {stem: [USED ON values]}.
    Compares by base drawing number, so revisions never cause false flags.
    Returns [(parent, child, sorted_child_used)] sorted.
    """
    out = []
    for parent in sorted(bom_edges):
        pbase = parent.split("_")[0]
        for child in sorted(bom_edges[parent]):
            if child == parent:
                continue
            actual = sorted(set(used_on_of.get(child, []) or ()))
            if pbase not in used_on_bases(actual):
                out.append((parent, child, actual))
    return out


def find_used_on_bugs(bom_edges, used_on_of, stems):
    """Children whose USED ON names a parent the parent's BOM does not list.

    BOM is the only source of truth for parent-child edges, so such a USED ON
    is a bug in the child's USED ON field, not a placement edge.

    bom_edges: {parent_stem: iterable of child stems} (BOM-derived only).
    used_on_of: {stem: [USED ON values]}.
    stems: iterable of active stems (for matching USED ON values).
    Returns [(parent, child)] sorted.
    """
    out = []
    for child in sorted(used_on_of):
        for val in sorted(set(used_on_of[child] or ())):
            if not is_processable_ref(val):
                continue
            for m in match_pdfs(val, stems):
                if m == child:
                    continue
                children_of_m = set(bom_edges.get(m, ()))
                if child not in children_of_m:
                    out.append((m, child))
    return out


_LOOSE_RE_CACHE = {}


def match_pdfs(val, stems):
    """Match PDFs by part number. For multiple revisions, keep highest revision."""
    if val in stems:
        return [val]
    with_rev = sorted(s for s in stems if s.startswith(val + "_"))
    if with_rev:
        # startswith pins the base; return the highest revision among matches.
        return [max(with_rev, key=revision_rank)]
    # Negative lookahead keeps loose prefix matches from extending the base.
    pat = _LOOSE_RE_CACHE.get(val)
    if pat is None:
        pat = re.compile(re.escape(val) + r"(?![0-9A-Z])")
        _LOOSE_RE_CACHE[val] = pat
    loose = sorted(s for s in stems if pat.match(s))
    return loose


_OCR_SNAP_TRANS = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5",
                                 "B": "8"})


def _edit1(a, b):
    """True when a and b differ by exactly one insert/delete/substitute."""
    if a == b:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def snap_ref(ref, stems):
    """Snap a malformed OCR reference to a unique known stem, else None.

    Only reads that are not a clean F+8-digit number are snapped: a wrong
    length (7/9 digits) or OCR confusion characters (O/I/L/S/B, pipe).
    A clean read is trusted as-is - it is either right or a genuine missing
    reference that must stay visible for the Teamcenter download list, even
    when some unrelated stem sits one digit away.
    """
    if not ref or ref in stems:
        return None
    bases = {s.split("_")[0] for s in stems}
    if ref in bases:
        return None  # already matches via match_pdfs
    cands = set()
    fixed = ref.translate(_OCR_SNAP_TRANS)
    if fixed != ref and fixed in bases:
        cands.add(fixed)
    if not re.fullmatch(r"F\d{8}", ref):
        for b in bases:
            if _edit1(ref, b):
                cands.add(b)
    if len(cands) != 1:
        return None
    return cands.pop()


def can_reach(children, start, target):
    """True if target is reachable from start following children edges (BFS)."""
    visited = set()
    todo = [start]
    while todo:
        cur = todo.pop()
        if cur == target:
            return True
        if cur in visited:
            continue
        visited.add(cur)
        todo.extend(children.get(cur, ()))
    return False


def collect_reachable(children, roots):
    """All nodes reachable from any of roots following children edges
    (the roots themselves included)."""
    reachable = set()
    todo = list(roots)
    while todo:
        cur = todo.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        todo.extend(children.get(cur, ()))
    return reachable


def break_cycles(children, parents, log):
    """Break cycles by removing back-edges (BOM graph is the only edge source)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = defaultdict(int)
    removed = []
    stack = []

    def dfs(u, depth=0):
        if depth > TREE_MAX_DEPTH:
            log(f"  WARNING: cycle search stopped at depth {depth} from {u} "
                f"(deeper than any healthy tree) - review manually")
            return
        color[u] = GRAY
        stack.append(u)
        for v in sorted(children.get(u, ())):
            if color[v] == GRAY:
                # Found a cycle: remove the closing back-edge u -> v.
                children[u].discard(v)
                parents[v].discard(u)
                removed.append((u, v))
                log(f"  CYCLE BREAK: removed edge {u} -> {v}")
            elif color[v] == WHITE:
                dfs(v, depth + 1)
        stack.pop()
        color[u] = BLACK

    for node in sorted(set(children) | set(parents)):
        if color[node] == WHITE:
            dfs(node)
    return removed
