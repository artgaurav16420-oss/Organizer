#!/usr/bin/env python3
"""Pure graph/revision algorithms (no filesystem side effects)."""
import re
from collections import defaultdict

from .config import TREE_MAX_DEPTH, is_processable_ref


def is_chk_stem(stem):
    """True if the stem beyond the base drawing number marks an unapproved CHK drawing."""
    _, _, suffix = stem.partition("_")
    return "CHK" in suffix.upper()


def revision_letter_rank(stem):
    """Letter rank of the revision token: ___ (no rev) = 0, A = 1 .. Z = 26,
    AA = 27, AB = 28 (bijective base-26). A token that is not one or two
    letters (CHK, DWG2, ...) counts as no revision."""
    parts = stem.split("_")
    if len(parts) < 2 or not parts[1]:
        return 0
    rev = parts[1]
    if rev.isalpha() and len(rev) <= 2:
        rank = 0
        for ch in rev.upper():
            rank = rank * 26 + (ord(ch) - ord("A") + 1)
        return rank
    return 0


def revision_rank(stem):
    """Rank revisions: CHK = -1000+letter (unapproved, loses to any DWG but
    still ordered by its own letter), ___ (no rev) = 0, A = 1, B = 2, ...,
    Z = 26, AA = 27 (bijective base-26, so a two-letter rev beats any single
    letter)."""
    if is_chk_stem(stem):
        # CHK drawings are unapproved: rank below ALL DWG ranks but still order
        # CHK revisions among themselves (A_CHK < B_CHK).  -1000 floor + letter
        # rank (max 702 for ZZ) keeps them ordered and below the lowest DWG
        # rank (0).
        return -1000 + revision_letter_rank(stem)
    return revision_letter_rank(stem)


_SHEET_TOKEN_RE = re.compile(r"^DWG\d+$")


def _sheet_token(stem):
    """Sheet suffix of a stem ('DWG2' in 'F10038961_A___DWG2'), or None."""
    for part in stem.split("_")[1:]:
        if _SHEET_TOKEN_RE.match(part):
            return part
    return None


def _sheet_winners(tied):
    """One stem per distinct sheet token when every tied stem is a sheet
    variant: sibling sheets of one drawing are one part, while duplicate
    exports of the SAME sheet (same token, different trailing text, e.g.
    '___DWG1_XML2347' vs '___DWG1_XML2348') are one sheet and keep the
    first-seen stem. A tie without sheet tokens keeps the first-seen stem."""
    tokens = [_sheet_token(s) for s in tied]
    if len(tied) > 1 and all(tokens):
        winners = {}
        for s, token in zip(tied, tokens, strict=True):
            winners.setdefault(token, s)
        return list(winners.values())
    return [tied[0]]


def split_superseded(index):
    """Split index into (active, superseded). Keep highest revision per base.

    Stems tied at a base's top rank resolve to one stem per distinct sheet
    token when every tied stem carries one: sibling sheets are all kept (a
    dropped sheet would drop its BOM, surfacing its children as orphans),
    while duplicate exports of one sheet collapse. Any other tie keeps the
    first-seen stem. Stems are collected in sorted order, so tie resolution
    is deterministic and independent of dict insertion order.
    """
    by_base = defaultdict(list)
    for s in sorted(index):
        by_base[s.split("_")[0]].append(s)
    active_stems = set()
    for stems in by_base.values():
        top = max(revision_rank(s) for s in stems)
        tied = [s for s in stems if revision_rank(s) == top]
        active_stems.update(_sheet_winners(tied))
    active = {}
    old = {}
    for s, p in index.items():
        if s in active_stems:
            active[s] = p
        else:
            old[s] = p
    return active, old


_USED_ON_BASE_RE = re.compile(r"(F\d+)")


def used_on_bases(used_vals):
    """Base drawing numbers (F + digits) found in a USED ON value list."""
    bases = set()
    for v in used_vals or ():
        m = _USED_ON_BASE_RE.match((v or "").upper())
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
    child_info = {}
    for children in bom_edges.values():
        for child in children:
            if child not in child_info:
                actual = sorted(set(used_on_of.get(child, []) or ()))
                child_info[child] = (actual, used_on_bases(actual))

    out = []
    for parent in sorted(bom_edges):
        pbase = parent.split("_")[0]
        for child in sorted(bom_edges[parent]):
            if child == parent:
                continue
            actual, cbases = child_info[child]
            if pbase not in cbases:
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
    bom_edges_sets = {
        p: cs if isinstance(cs, set) else set(cs)
        for p, cs in bom_edges.items()
    }
    for child in sorted(used_on_of):
        for val in sorted(set(used_on_of[child] or ())):
            if not is_processable_ref(val):
                continue
            for m in match_pdfs(val, stems):
                if m == child:
                    continue
                if child not in bom_edges_sets.get(m, ()):
                    out.append((m, child))
    return out


_LOOSE_RE_CACHE = {}


def match_pdfs(val, stems):
    """Match PDFs by part number. For multiple revisions, keep highest revision."""
    if val in stems:
        return [val]
    with_rev = sorted(s for s in stems if s.startswith(val + "_"))
    if with_rev:
        # startswith pins the base; return the highest revision among matches
        # (one stem per sibling sheet, duplicate sheet exports collapsed).
        top = max(revision_rank(s) for s in with_rev)
        tied = [s for s in with_rev if revision_rank(s) == top]
        return _sheet_winners(tied)
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
    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > 1:
        return False
    if len_a == len_b:
        i = 0
        while a[i] == b[i]:
            i += 1
        return a[i + 1:] == b[i + 1:]
    if len_a > len_b:
        a, b = b, a
        len_a = len_b
    i = 0
    while i < len_a and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def snap_ref(ref, stems, bases=None):
    """Snap a malformed OCR reference to a unique known stem, else None.

    Only reads that are not a clean F+8-digit number are snapped: a wrong
    length (7/9 digits) or OCR confusion characters (O/I/L/S/B, pipe).
    A clean read is trusted as-is - it is either right or a genuine missing
    reference that must stay visible for the Teamcenter download list, even
    when some unrelated stem sits one digit away.
    """
    if not ref or ref in stems:
        return None
    if bases is None:
        bases = {s.split("_")[0] for s in stems}
    if ref in bases:
        return None  # already matches via match_pdfs
    cands = set()
    fixed = ref.translate(_OCR_SNAP_TRANS)
    if fixed != ref and fixed in bases:
        cands.add(fixed)
    if not re.fullmatch(r"F\d{8}", ref):
        ref_len = len(ref)
        for b in bases:
            if abs(len(b) - ref_len) <= 1 and _edit1(ref, b):
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
    """Break cycles by removing back-edges (BOM graph is the only edge source).

    Iterative white/gray/black DFS (explicit stack of per-node child
    iterators): recursion is avoided because inputs can be up to
    TREE_MAX_DEPTH deep and the traversal must not depend on the process-wide
    recursion limit.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    removed = []
    sorted_children = {}

    def ordered_children(u):
        ch_list = sorted_children.get(u)
        if ch_list is None:
            ch_list = sorted(children.get(u, ()))
            sorted_children[u] = ch_list
        return ch_list

    for node in sorted(set(children) | set(parents)):
        if color.get(node, WHITE) != WHITE:
            continue
        stack = [(node, 0, iter(ordered_children(node)))]
        color[node] = GRAY
        while stack:
            u, depth, it = stack[-1]
            v = next(it, None)
            if v is None:
                color[u] = BLACK
                stack.pop()
                continue
            v_color = color.get(v, WHITE)
            if v_color == GRAY:
                # Found a cycle: remove the closing back-edge u -> v.
                children[u].discard(v)
                parents[v].discard(u)
                removed.append((u, v))
                log(f"  CYCLE BREAK: removed edge {u} -> {v}")
            elif v_color == WHITE:
                if depth + 1 > TREE_MAX_DEPTH:
                    log(f"  WARNING: cycle search stopped at depth {depth + 1} from {v} "
                        f"(deeper than any healthy tree) - review manually")
                else:
                    color[v] = GRAY
                    stack.append((v, depth + 1, iter(ordered_children(v))))
    return removed
