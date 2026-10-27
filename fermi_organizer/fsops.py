#!/usr/bin/env python3
"""Filesystem scans and side effects (indexing, placement, copies)."""
import json
import os
import re
import shutil
import socket
import threading
import time
from collections import defaultdict
from fnmatch import fnmatch
from pathlib import Path

from .config import (
    MAX_DIR,
    MAX_PATH,
    MAX_PLANNED_COPIES,
    PLANNED_COPIES_WARN,
    TREE_MAX_DEPTH,
    canonical_stem,
)
from . import treescan
from .extraction import detect_watermark
from .graph import is_chk_stem
from .naming import folder_name_for

try:
    import pymupdf
except ImportError:  # pragma: no cover
    pymupdf = None


def _native(path):
    """Filesystem-op view of path (see treescan.native)."""
    return treescan.native(path)


def _exists(path):
    """os.path.exists that sees beyond MAX_PATH (Path.exists goes blind)."""
    return os.path.exists(_native(path))


def _islink(path):
    """os.path.islink that sees beyond MAX_PATH."""
    return treescan.islink(path)


def _native_tree(path):
    r"""`native(path)`, always prefixed on Windows, for recursive operations.

    A folder can sit below MAX_PATH and still contain a child past it, so
    shutil.rmtree(folder) fails with WinError 3 on the child while
    shutil.rmtree(_native_tree(folder)) succeeds. `_native` deliberately leaves
    short paths alone (callers compare them); this variant is for walking.
    """
    text = os.fspath(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    if os.path.isabs(text) and "/" not in text:
        return "\\\\?\\" + text
    return text


def _isfile(path):
    """os.path.isfile that sees beyond MAX_PATH (Path.is_file goes blind)."""
    return os.path.isfile(_native(path))


def _isdir(path):
    """os.path.isdir that sees beyond MAX_PATH (Path.is_dir goes blind)."""
    return os.path.isdir(_native(path))


def _has_linked_ancestor(path, stop):
    """True if any component between (exclusive) stop and path is a link.

    Guards against a planned target that traverses a symlinked/junction
    directory, which would point outside the tree.
    """
    cur = Path(path)
    stop = Path(stop)
    while cur != stop and cur != cur.parent:
        if _islink(cur) or _is_dir_link(cur):
            return True
        cur = cur.parent
    return False


def _strip_ext_prefix(path):
    """Remove the extended-length prefix (see treescan.strip_ext_prefix)."""
    return treescan.strip_ext_prefix(path)


def _is_dir_link(path):
    """True for a directory that resolves elsewhere (see treescan.is_dir_link)."""
    return treescan.is_dir_link(path)


def _pdf_watermarked(path):
    """Best-effort watermark probe for one archived/on-disk PDF.

    Returns True when detect_watermark finds evidence; False (including on any
    read failure) otherwise, so undecidable copies are treated as clean rather
    than as reasons to delete parked orphan bytes.
    """
    if pymupdf is None:
        return False
    try:
        doc = pymupdf.open(str(_native(path)))
    except Exception:
        return False
    try:
        return detect_watermark(doc) is not None
    except Exception:
        return False
    finally:
        try:
            doc.close()
        except Exception:
            pass



def _classify_input_pdf(p, folder, index):
    """Classify one candidate PDF -> (kind, stem, payload), no side effects.

    kind: 'skip_output' | 'skip_sys' | 'ignored' | 'dupe' | 'index' | 'replace'.
    The stem is the canonical form (see config.canonical_stem); hyphen and
    underscore copies of one drawing therefore share a stem. On a duplicate
    stem the shallowest path wins (originals at the input root beat copies
    nested in the organized tree), so a re-export that keeps the same filename
    is not silently ignored on an in-place rerun. 'replace' payload is
    (candidate_path, (displaced_rel, candidate_rel)).
    """
    rel = p.relative_to(folder)
    top = rel.parts[0]
    if top.lower() == "output":
        return "skip_output", None, None
    if top.startswith("_"):
        return "skip_sys", None, None
    stem = canonical_stem(p.stem)
    if stem is None:
        return "ignored", None, str(rel)
    if stem in index:
        kept_rel = index[stem].relative_to(folder)
        # Shallowest wins; equal depth falls back to lexicographic order.
        if (len(rel.parts), str(rel)) < (len(kept_rel.parts), str(kept_rel)):
            return "replace", stem, (p, (str(kept_rel), str(rel)))
        return "dupe", stem, (str(rel), str(kept_rel))
    return "index", stem, p


def _unusual_revision_stems(index):
    """Validate revision-token convention: second underscore segment must be
    empty, one or two letters, or a CHK marker (all ranked, not "treated as
    0")."""
    unusual = []
    for s in index:
        if is_chk_stem(s):
            continue
        parts = s.split("_")
        tok = parts[1] if len(parts) > 1 else ""
        if tok and not (tok.isalpha() and len(tok) <= 2):
            unusual.append(s)
    return unusual


def _log_scan_skips(log, skipped_output, skipped_sys):
    if not log:
        return
    if skipped_output:
        log(f"Skipped {skipped_output} PDF(s) under the output tree (organized copies are never input)")
    if skipped_sys:
        log(f"Skipped {skipped_sys} PDF(s) under _-prefixed folders (system dirs are never input)")


def _log_capped(log, header, items, fmt):
    """Capped list log: header + first 10 items + '... and N more'."""
    if not items or not log:
        return
    log(header)
    for item in items[:10]:
        log(fmt(item))
    if len(items) > 10:
        log(f"  ... and {len(items) - 10} more")


def build_pdf_index(folder, log=None, exclude=None):
    """Map uppercase stem -> PDF path for every part-named PDF under `folder`.

    Recursive; top-level Output/ and "_"-prefixed dirs are skipped, and so is
    the `exclude` subtree (the run's output folder) when it sits inside
    `folder` under any name - a previous tree named e.g. "Organizer Output"
    must not be re-indexed as input. `folder` and `exclude` should both be
    resolved paths (the CLI resolves them). Duplicate stems keep the
    shallowest path (input originals beat nested organized-tree copies), then
    lexicographic order. Skips/dupes/ignored files are logged via `log` when
    given.

    Returns (index, duplicates): `duplicates` maps a stem to the losing paths
    (sorted scan order) so callers can compare same-revision copies (e.g.
    prefer a non-watermarked copy over a watermarked one).
    """
    # Recursive: every *.pdf under the input folder counts, at any depth.
    # In-place output is the default, so the organized tree usually lives
    # inside the input folder; only top-level "Output/" and "_"-prefixed
    # dirs (_superseded/_orphans) are skipped by name, plus the resolved
    # output path itself (any name). Repeated full runs rescan
    # the already-organized folders unless a separate --output is used.
    exclude_prefix = ()
    if exclude is not None:
        try:
            folder_key = os.path.normcase(os.path.abspath(folder))
            exclude_key = os.path.normcase(os.path.abspath(exclude))
        except (OSError, ValueError):
            folder_key = exclude_key = None
        if folder_key and exclude_key and exclude_key != folder_key and \
                exclude_key.startswith(folder_key.rstrip(os.sep) + os.sep):
            exclude_prefix = tuple(
                Path(exclude_key).relative_to(folder_key).parts)
    index = {}
    duplicates = defaultdict(list)
    ignored = []
    dupes = []
    skips = {"skip_output": 0, "skip_sys": 0}
    skipped_symlinks = 0
    skipped_linked_dirs = 0
    # os.walk with explicit pruning instead of rglob: rglob refuses directory
    # symlinks but follows NTFS junctions, so a junction in the input folder
    # would index PDFs from outside this folder under this folder's name (the
    # per-file _islink guard below only catches file links). Sorted to keep the
    # scan order deterministic, and the collected paths are sorted as a whole
    # so duplicate resolution is unchanged from the old sorted(rglob()).
    pdf_paths = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        root_path = Path(root)
        kept_dirs = []
        for name in sorted(dirs):
            if _is_dir_link(root_path / name):
                skipped_linked_dirs += 1
                continue
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if fnmatch(name, "*.pdf"):
                pdf_paths.append(root_path / name)
    for p in sorted(pdf_paths):
        if _islink(p):
            skipped_symlinks += 1
            continue
        if exclude_prefix:
            rel = p.relative_to(folder)
            if tuple(os.path.normcase(part)
                     for part in rel.parts[:len(exclude_prefix)]) == exclude_prefix:
                skips["skip_output"] += 1
                continue
        kind, stem, payload = _classify_input_pdf(p, folder, index)
        if kind in skips:
            skips[kind] += 1
        elif kind == "ignored":
            ignored.append(payload)
        elif kind == "dupe":
            dupes.append(payload)
            duplicates[stem].append(p)
        elif kind == "replace":
            new_path, dupe_info = payload
            duplicates[stem].append(index[stem])
            index[stem] = new_path
            dupes.append(dupe_info)
        else:
            index[stem] = payload
    _log_scan_skips(log, skips["skip_output"], skips["skip_sys"])
    if log and skipped_symlinks:
        log(f"Skipped {skipped_symlinks} PDF(s) that are symlinks (symlinks are never input)")
    if skipped_linked_dirs and log is not None:
        log(f"Skipped {skipped_linked_dirs} linked folder(s) "
            "(symlinked/junction folders are never input)")
    _log_capped(log, f"WARNING: {len(dupes)} duplicate stem(s) across input subfolders "
                     "(shallowest path kept):",
                dupes, lambda d: f"  {d[0]}  (kept {d[1]})")
    _log_capped(log, f"Ignored {len(ignored)} PDF(s) with non-part filenames:",
                ignored, lambda name: f"  {name}")
    unusual_rev = _unusual_revision_stems(index)
    _log_capped(log, f"WARNING: {len(unusual_rev)} stem(s) with unrecognized revision token "
                     "(rank treated as 0):",
                unusual_rev, lambda s: f"  {s}")
    return index, duplicates


class PlacementRefusedError(RuntimeError):
    """Planned copies exceeded MAX_PLANNED_COPIES; placement was refused."""

    def __init__(self, planned):
        super().__init__(
            f"{planned} planned copies exceed the {MAX_PLANNED_COPIES}-copy cap")
        self.planned = planned


def _copy_counter(children):
    """Memoized count of copies for a subtree (one per root-to-leaf path).

    Keyed by (stem, remaining depth): the count depends on the TREE_MAX_DEPTH
    truncation, so a stem-only key would undercount a subtree first visited
    near the limit when reused on a shallower path (fan-out cap bypass).
    Iterative post-order (explicit stack): depth up to TREE_MAX_DEPTH must
    not depend on the process-wide recursion limit. Cyclic input terminates
    on the depth cap, exactly like the former recursion (memo entries only
    exist for completed expansions)."""
    memo = {}

    def count(root_stem, root_depth=0):
        remaining = TREE_MAX_DEPTH - root_depth
        if remaining < 0:
            return 0
        key = (root_stem, remaining)
        if key in memo:
            return memo[key]
        result = None
        stack = [[root_stem, root_depth, 1,
                  iter(children.get(root_stem, ()))]]
        while stack:
            stem, depth, total, it = stack[-1]
            child = next(it, None)
            if child is None:
                memo[(stem, TREE_MAX_DEPTH - depth)] = total
                stack.pop()
                if stack:
                    stack[-1][2] += total
                else:
                    result = total
                continue
            c_remaining = TREE_MAX_DEPTH - (depth + 1)
            if c_remaining < 0:
                continue
            c_key = (child, c_remaining)
            if c_key in memo:
                stack[-1][2] += memo[c_key]
                continue
            stack.append([child, depth + 1, 1,
                          iter(children.get(child, ()))])
        return result

    return count


def planned_copy_count(children, roots):
    """Copies place_files would write for these roots (fan-out cap input)."""
    count = _copy_counter(children)
    return sum(count(root) for root in roots)


def ensure_placement_allowed(children, roots, log):
    """Log + raise PlacementRefusedError when the planned fan-out exceeds
    MAX_PLANNED_COPIES (or warn above the soft bound).

    Call this before building naming paths or touching the tree, so a refusal
    can never exhaust memory/time or leave partial work behind.
    """
    planned = planned_copy_count(children, roots)
    if planned > MAX_PLANNED_COPIES:
        log(f"  WARNING: {planned} copies planned exceeds the "
            f"{MAX_PLANNED_COPIES}-copy cap - placement refused (check the BOM "
            "graph; nothing was created)")
        raise PlacementRefusedError(planned)
    if planned > PLANNED_COPIES_WARN:
        log(f"  WARNING: {planned} copies planned (diamond DAG may cause exponential growth)")


def fit_roots_within_cap(children, roots, log, weights=None, extra=None):
    """Drop the largest roots until the planned fan-out fits MAX_PLANNED_COPIES.

    Returns (kept, skipped): kept preserves the input root order; skipped is
    [(root, planned_copies)] largest-first (stem order breaks ties, so repeat
    runs agree). Never raises: when nothing fits, kept is empty and the caller
    treats it like a full refusal. A single shared _copy_counter memoizes
    across roots, so the per-root breakdown costs one traversal, not one per
    root. Dropped roots are reported, never placed: their shared children are
    still placed under their other parents. `weights` multiplies a root's
    count (incremental copies a multi-claimant root under every organized
    parent, so its subtree must be charged once per claimant). `extra` adds a
    flat per-root estimate (incremental's adopted organized-subtree copies,
    which ride outside the new-children walk).
    """
    count = _copy_counter(children)
    weights = weights or {}
    extra = extra or {}
    counts = {r: count(r) * weights.get(r, 1) + extra.get(r, 0) for r in roots}
    planned = sum(counts.values())
    if planned <= MAX_PLANNED_COPIES:
        return list(roots), []
    skipped = []
    remaining = planned
    for r in sorted(roots, key=lambda s: (-counts[s], s)):
        if remaining <= MAX_PLANNED_COPIES:
            break
        skipped.append((r, counts[r]))
        remaining -= counts[r]
    dropped = {r for r, _ in skipped}
    kept = [r for r in roots if r not in dropped]
    log(f"  WARNING: {planned} copies planned exceeds the "
        f"{MAX_PLANNED_COPIES}-copy cap - placement refused for "
        f"{len(skipped)} oversized root(s), placing {len(kept)} of "
        f"{len(roots)}")
    for r, c in skipped:
        log(f"  skipped oversized root {r} ({c} copies planned): not placed - "
            "shared children are still placed under their other parents")
    return kept, skipped


def _staged_copy(src, staging, stop=None):
    """Copy `src` into a freshly created staging file, written through the
    created handle.

    shutil.copy2 opens the destination by path, so a symlink planted (or
    raced) at the staging name would redirect the write outside the tree.
    The staging name is PID-predictable, so: refuse a symlink already there,
    create the file with O_CREAT|O_EXCL (an existing file or valid link
    fails), re-check the path after the open (Windows follows a dangling
    link even with O_EXCL), and stream the bytes through the fd - the write
    can never go through a link. Metadata is copied after. A live attacker
    replacing staging names inside the tree is out of scope, as documented
    for shared writable outputs.

    `stop` is the output-tree root. When given, a symlinked/junction ancestor
    between it and the staging path is refused before the copy: a link already
    sitting at a planned folder path (or at `_superseded/`, `_orphans/`)
    would otherwise write outside the tree while the run reports success.
    """
    if stop is not None and _has_linked_ancestor(Path(staging).parent, stop):
        raise OSError(
            f"target folder traverses a symlinked/junction ancestor, refusing: {staging}")
    if _islink(staging):
        raise OSError(f"staging path is a symlink, refusing: {staging}")
    try:
        os.unlink(_native(staging))
    except OSError:
        # No stale leftover (the common case), or it could not be removed:
        # O_CREAT|O_EXCL below fails safely on anything that still exists.
        pass
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(_native(staging), flags)
    if _islink(staging):
        # The open followed a dangling link swapped in after our check (a
        # Windows O_EXCL quirk): the fd is not ours to write through.
        os.close(fd)
        try:
            os.unlink(_native(staging))
        except OSError:
            pass
        raise OSError(f"staging path became a symlink during copy, refusing: {staging}")
    try:
        with open(_native(src), "rb") as fsrc, os.fdopen(fd, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=1024 * 1024)
        # Metadata belongs inside the cleanup block: a copystat failure used to
        # escape with the staging file already written, and the caller's
        # created=False path then skipped the unlink, leaving it behind.
        shutil.copystat(_native(src), _native(staging))
    except BaseException:
        try:
            os.unlink(_native(staging))
        except OSError:
            pass
        raise


def _copy_atomic(src, dst, stop=None):
    """Copy `src` to `dst` via a staging name in the target folder.

    shutil.copy2 writes the destination in place, so a failure mid-write
    leaves a truncated PDF at `dst` that later runs treat as placed and never
    retry. Stage next to the target and os.replace, so the target is either
    absent or complete; a failed attempt removes its own staging file. See
    _staged_copy for the symlink/race handling of the staging path and for
    `stop` (the output root that bounds the linked-ancestor check).

    Cleanup only touches a staging file this call actually created (or, for
    the legacy stop=None path, the pre-existing behaviour). Unlinking the
    staging name after a refusal would follow a junction and could delete a
    same-named file outside the tree that this copy never wrote.
    """
    tmp = dst.with_name(f"{dst.name}.copy_tmp.{os.getpid()}")
    created = False
    try:
        _staged_copy(src, tmp, stop)
        created = True
        os.replace(_native(tmp), _native(dst))
    except (OSError, ValueError):
        if created or stop is None:
            try:
                os.unlink(_native(tmp))
            except OSError:
                pass
        raise


def place_files(children, roots, index, folder, dry_run, log, names_of=None, folder_names=None, renames=None,
                lock=None):
    """Copy every root's subtree into nested folders; returns the copy count.

    Folder names come from `folder_names` (already path-shortened), falling
    back to `names_of`. `renames` maps a stem to the copied file's new name
    (re-keyed misnamed files). Dry-run logs planned copies without touching
    disk; oversized paths/subtrees are skipped with a warning. Raises
    PlacementRefusedError when the planned fan-out exceeds
    MAX_PLANNED_COPIES (nothing is created).
    `lock` is this run's lock path (None = unlocked): our token is re-read
    from the file once per folder, so a run stolen mid-placement stops with
    LockLostError instead of writing into another run's tree.

    Iterative pre-order traversal (explicit stack, children pushed in reverse
    so they pop in sorted order): the log and copy order must stay identical
    to the former recursion, and depth up to TREE_MAX_DEPTH must not depend
    on the process-wide recursion limit.
    """
    total_copies = 0
    skipped_count = 0
    failed_count = 0
    names_of = names_of or {}
    folder_names = folder_names or {}
    renames = renames or {}
    count_copies = _copy_counter(children)
    ensure_placement_allowed(children, roots, log)

    def fname(stem):
        return folder_names.get(stem) or folder_name_for(stem, names_of)

    for root in sorted(roots):
        stack = [(root, folder / fname(root), 0)]
        while stack:
            stem, current_folder, depth = stack.pop()
            _ensure_lock(lock, log)
            if depth > TREE_MAX_DEPTH:
                log(f"  WARNING: placement truncated at depth {depth} in "
                    f"{current_folder.name} (deeper than any healthy tree) - manual review")
                skipped_count += count_copies(stem)
                continue
            kids = sorted(children.get(stem, ()))
            pdf = index[stem]
            target_name = renames.get(stem, pdf.name)
            target = current_folder / target_name
            if _islink(pdf) or _islink(target):
                log(f"  WARNING: {stem}: copy skipped (symlink refused): {pdf} -> {target}")
                failed_count += 1
                stack.extend((child, current_folder / fname(child), depth + 1)
                             for child in reversed(kids))
                continue
            # Dir path checked against MAX_DIR, full target against MAX_PATH.
            if len(str(current_folder)) > MAX_DIR or len(str(target)) > MAX_PATH:
                log(f"  WARNING: path too long ({len(str(target))} chars), skipping: {target}")
                skipped_count += count_copies(stem)
                continue
            if dry_run:
                log(f"  [DRY-RUN] mkdir+copy {target_name} -> {target}")
                total_copies += 1
            else:
                # mkdir failure: the subtree's paths cannot exist - skip it;
                # copy2 failure: only this file is lost, children are still tried.
                # ValueError catches embedded-NUL style hostile names that
                # raise before the syscall (guards must stay "not fatal").
                #
                # The linked-ancestor check has to run BEFORE mkdir: a junction
                # at a planned folder would otherwise have parents=True create
                # descendant directories outside the output tree, and the copy
                # guard would only refuse the file afterwards.
                if _has_linked_ancestor(current_folder, folder):
                    log(f"  WARNING: {stem}: copy skipped (folder path traverses a "
                        f"symlinked/junction ancestor, refusing): {current_folder}")
                    failed_count += count_copies(stem)
                    continue
                try:
                    current_folder.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as e:
                    log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                    failed_count += count_copies(stem)
                    continue
                try:
                    _copy_atomic(pdf, target, folder)
                except (OSError, ValueError) as e:
                    log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                    failed_count += 1
                else:
                    total_copies += 1
            stack.extend((child, current_folder / fname(child), depth + 1)
                         for child in reversed(kids))

    if skipped_count:
        log(f"  Skipped {skipped_count} path(s) due to length limits")
    if failed_count:
        log(f"  FAILED {failed_count} copy(ies) - see warnings above")
    return total_copies



def is_system_dir(name):
    """Convention: any top-level directory whose name starts with '_' is a system dir."""
    return treescan.is_system_dir(name)


RUN_LOCK_NAME = ".fermi_organizer.lock"

# Precompiled sweep regexes (F-PERF-11): avoid recompiling per call.
_STAGING_RE = re.compile(r"\.pdf\.(?:supersede|copy)_tmp\.[0-9]+$", re.IGNORECASE)
_COPY_RE = re.compile(r"\.pdf\.copy_tmp\.[0-9]+$", re.IGNORECASE)
_CLAIM_RE = re.compile(re.escape(RUN_LOCK_NAME) + r"\.claim\.[0-9]+$")
# A lock older than this is presumed left by a crashed run and stolen with a
# warning. 2 h is far beyond any healthy run (thousands of PDFs take
# minutes), so a crashed run blocks the next one only briefly; the check is
# mtime-only on purpose — PID-liveness probing via os.kill has fatally
# inconsistent signal semantics across platforms, so the lock fails closed
# (refuse + tell the operator how to recover) instead of guessing.


class RunLockedError(Exception):
    """Another run holds the output-tree lock; this run is refused."""


class LockUnavailableError(RunLockedError):
    """The output-tree lock could not be created (ACL denial, EMFILE, a share
    that rejects O_EXCL).

    Refusing is the only safe answer: the lock is what keeps two runs out of
    one tree, so a run that cannot take it must not proceed unlocked. Exits 1
    through the CLI's RunLockedError handler.
    """


class LockLostError(RunLockedError):
    """This run's lock was stolen mid-run; the run must stop mutating.

    Raised by runmodes._ensure_lock when the heartbeat observed foreign
    content in our lock file (a paused run — sleep, SIGSTOP, VM pause — that
    slept past the stale window and was stolen). Subclasses RunLockedError so
    generic handlers still refuse, but the CLI catches it separately (exit 2)
    since the tree may already hold this run's partial writes.
    """


RUN_LOCK_STALE_SECS = 2 * 3600
# Heartbeat: a live holder re-touches its lock this often, so a run longer
# than the stale window never looks stealable. Daemon thread, parent process
# only (spawn workers get a fresh interpreter without it); stopped at release.
RUN_LOCK_HEARTBEAT_SECS = 600
_heartbeats = {}


def acquire_run_lock(output, dry_run, log):
    """Exclusive per-output-tree lock; returns the lock path, or None when the
    output tree does not exist yet (nothing to protect). Raises
    RunLockedError when another run may hold the tree and
    LockUnavailableError when the lock cannot be created at all.

    A lock older than RUN_LOCK_STALE_SECS is stolen with a warning (a crashed
    run leaks its lock; the steal message is the audit trail). Anything
    younger is honored unconditionally — PIDs cannot be checked reliably
    across hosts or platforms, so the lock fails closed with a recovery
    pointer instead of guessing. Creation uses O_EXCL so two starters cannot
    both win; a steal claims the stale file aside with an atomic rename and
    only deletes the staged copy when it still matches the examined content,
    then retries the create — so the loser of a double-steal race observes
    the winner's fresh lock and refuses instead of deleting it. Residual risk
    is a 3-party microsecond interleave (file locks cannot do better
    portably; NFS ignores even fcntl). A held lock is refreshed by a
    heartbeat thread that verifies our own token before every touch: if the
    file no longer holds our bytes (stolen while paused past the stale
    window) the heartbeat stops without touching the stealer's file and flags
    the loss — runmodes checks the flag before each mutating step and aborts
    with LockLostError instead of writing into another run's tree. The file
    is a dotfile with JSON {pid, host, created}; scans only collect PDFs, so
    it is invisible to every tree walk. (On Windows dotfiles are not hidden —
    Explorer shows it; harmless, leave it alone.)
    """
    # NOTE: dry_run is intentionally not exempted — a dry-run's sweep report
    # and workbook write must not interleave with a live run's staging either.
    lock = Path(output) / RUN_LOCK_NAME
    if not Path(output).is_dir():
        # No tree yet (typical for a first dry-run): nothing to protect, and
        # dry-run must not create the folder — skip silently.
        return None
    for _ in range(3):
        try:
            fd = os.open(_native(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass  # held or stale - adjudicated below.
        except OSError as e:
            raise LockUnavailableError(
                f"could not create run lock ({lock.name}): {e} - refusing to "
                "run unlocked (two processes against one output would interleave)") from e
        else:
            try:
                token = json.dumps({"pid": os.getpid(),
                                    "host": socket.gethostname(),
                                    "created": time.time()}).encode("utf-8")
                with os.fdopen(fd, "wb") as f:
                    f.write(token)
            except OSError as e:
                # fdopen may never have taken ownership (dumps failed first):
                # close the raw fd or the cleanup unlink fails on Windows
                # (double-close after a failed write is swallowed below).
                try:
                    os.close(fd)
                except OSError:
                    pass
                # The O_EXCL-created file would otherwise linger (no lock path
                # to clean up) and refuse later runs until stale: remove it
                # and refuse the run like a creation failure.
                try:
                    os.unlink(_native(lock))
                except OSError:
                    pass
                raise LockUnavailableError(
                    f"could not write run lock ({lock.name}): {e} - refusing to "
                    "run unlocked") from e
            try:
                _heartbeat_start(lock, token)
            except RuntimeError as e:
                # Thread.start() can fail (thread limits) after the lock file
                # exists: remove it like a write failure, or the fresh lock
                # refuses later runs until stale.
                try:
                    os.unlink(_native(lock))
                except OSError:
                    pass
                raise LockUnavailableError(
                    f"could not start run lock heartbeat ({lock.name}): {e} - "
                    "refusing to run unlocked") from e
            return lock
        try:
            raw = lock.read_bytes()
            age = time.time() - os.path.getmtime(_native(lock))
        except OSError:
            continue  # vanished mid-read; retry the create.
        if age <= RUN_LOCK_STALE_SECS:
            raise RunLockedError(
                f"output tree is locked ({lock.name} present"
                f"{_holder_note(raw)}); refusing a concurrent "
                "run against one output - wait for it to finish, or delete the lock "
                "file if no run is active")
        # Stale: claim it aside with an atomic rename, then delete the staged
        # copy ONLY if it still matches what we examined. An unconditional
        # unlink here could delete a competitor's fresh lock (double-steal:
        # both verified stale, one already recreated). Mismatch means someone
        # interfered: put a displaced live lock back when the way is clear,
        # else leave the staged copy (inert dotfile, invisible to scans) and
        # refuse — never delete a file we did not verify.
        claim = lock.parent / f"{RUN_LOCK_NAME}.claim.{os.getpid()}"
        try:
            os.replace(_native(lock), _native(claim))
        except OSError:
            continue  # lost the race; re-read on next attempt.
        try:
            staged = claim.read_bytes()
        except OSError:
            continue
        if staged != raw:
            try:
                if not _exists(lock):
                    os.replace(_native(claim), _native(lock))
            except OSError:
                pass
            raise RunLockedError(
                f"output tree changed hands during steal ({lock.name}); "
                "refusing rather than risk a concurrent run")
        log(f"  WARNING: stealing stale run lock ({age / 3600:.1f} h old)")
        try:
            os.unlink(_native(claim))
        except OSError:
            pass  # lost the race (or it vanished): re-read on next attempt.
    raise RunLockedError(f"could not acquire run lock ({lock.name}) after "
                         "contended steals - wait for the other run to finish")


def _holder_note(raw):
    """Best-effort 'held by host …' suffix for the refusal message."""
    try:
        host = (json.loads(raw.decode("utf-8")) or {}).get("host") if raw else None
    except (ValueError, AttributeError):
        host = None
    return f", held by host {host}" if host else ""


def release_run_lock(lock):
    """Remove our lock file, but only while it still holds our token.

    Best-effort in the ordinary case (a leftover only blocks the next run
    until it goes stale, then it is stolen with a warning). The token check
    matters on the paths that reach here after a steal — the CLI's finally
    block, or a BaseException handler — where an unconditional unlink would
    delete the new holder's live lock and hand the tree to a third run.
    """
    if lock is None:
        return
    item = _heartbeats.get(str(lock))
    token = item[3] if item is not None else None
    _heartbeat_stop(lock)
    if token is not None:
        try:
            if lock.read_bytes() != token:
                return  # stolen: the file is the new holder's, leave it alone
        except FileNotFoundError:
            return  # already removed: nothing of ours left
        except OSError:
            # Unreadable (not gone). Return rather than unlink: ownership is
            # unproven, and deleting a live holder's lock interleaves two
            # writers. A leftover lock of our own only blocks the next run
            # until it goes stale, then it is stolen with a warning.
            return
    try:
        os.unlink(_native(lock))
    except OSError:
        pass


def abandon_run_lock(lock):
    """Stop the heartbeat without unlinking: the lock file is no longer ours.

    Used on the LockLostError path (our lock was stolen mid-run) — unlinking
    there would delete the stealer's live lock and hand the tree to a third
    run. Never raises.
    """
    if lock is None:
        return
    _heartbeat_stop(lock)


def run_lock_lost(lock):
    """True when our lock file no longer holds our token.

    Re-reads the file on every call (a few thousand small reads per run is
    negligible next to the copies), so a steal is observed at the next check
    without waiting for the heartbeat tick — the flag is only a fast path.
    A detection latches the flag. Unreadable/missing reads as lost (fail
    closed: mid-steal the path briefly holds nothing of ours). False for
    unlocked runs (None) and locks with no live heartbeat entry (no token to
    compare). Call before each mutating step (see _ensure_lock).
    """
    if lock is None:
        return False
    item = _heartbeats.get(str(lock))
    if item is None:
        return False
    _stop, _t, lost, token = item
    if lost.is_set():
        return True
    try:
        if lock.read_bytes() != token:
            lost.set()
            return True
    except OSError:
        lost.set()
        return True
    return False


def _ensure_lock(lock, log):
    """Abort when our lock file no longer holds our token (stolen mid-run).

    A run paused past the stale window (sleep, SIGSTOP, VM pause) may wake to
    find another run holding the tree; writing on would interleave two live
    writers. The check re-reads the file (see run_lock_lost), so it holds at
    every mutating step short of the read-then-write race inherent to file
    locks. Raises LockLostError (the CLI exits 2 — the tree may already hold
    this run's partial writes). No-op for unlocked runs (None).
    """
    if run_lock_lost(lock):
        log("  ERROR: run lock lost mid-run (stolen while paused?) - "
            "stopping before further tree changes")
        raise LockLostError(
            "run lock lost mid-run (stolen while paused?) - stopped before "
            "further tree changes; inspect the output tree before rerunning")


def _heartbeat_beat(lock, token):
    """One ownership-checked refresh; True when still ours and re-touched.

    Reads our token first and again after the touch: a steal racing the beat
    can cost at most one stray refresh, then the loss is flagged and the
    thread stops. Never raises (OSError means vanished/unreadable — assume
    stolen and stop without touching anything).
    """
    try:
        if lock.read_bytes() != token:
            return False
        os.utime(_native(lock), None)
        if lock.read_bytes() != token:
            return False
    except OSError:
        return False
    return True


def _heartbeat_start(lock, token):
    """Refresh the lock mtime periodically while we hold it.

    `token` is the exact bytes written at creation; the beat re-touches the
    file only while it still holds them (see _heartbeat_beat), so a lock
    stolen while this process was paused is never refreshed for the stealer.
    """
    stop = threading.Event()
    lost = threading.Event()

    def beat():
        while not stop.wait(RUN_LOCK_HEARTBEAT_SECS):
            if not _heartbeat_beat(lock, token):
                lost.set()
                return

    t = threading.Thread(target=beat, daemon=True,
                         name="fermi-lock-heartbeat")
    _heartbeats[str(lock)] = (stop, t, lost, token)
    try:
        t.start()
    except BaseException:
        _heartbeats.pop(str(lock), None)
        raise


def _heartbeat_stop(lock):
    """Stop the refresh thread; join briefly so release is ordered."""
    item = _heartbeats.pop(str(lock), None)
    if item is not None:
        stop, t, _lost, _token = item
        stop.set()
        t.join(timeout=5)


def scan_output_tree(output):
    """Live scan of the output folder, categorized by the system-dir convention.

    Thin re-export of treescan.scan_output_tree for compatibility; the
    read-only consumers import treescan directly so they do not pull in
    PyMuPDF through this module.
    """
    return treescan.scan_output_tree(output)


def sweep_supersede_staging(output, dry_run, log):
    """Remove leftover `<name>.pdf.supersede_tmp.<pid>` and
    `<name>.pdf.copy_tmp.<pid>` staging files.

    _swap_revision_files stages the new revision next to its target before
    os.replace, and _copy_atomic stages every copy the same way; a run killed
    mid-operation leaves the staging file behind. It never ends in .pdf, so
    scans ignore it - only this sweep cleans it up. Only the exact staging
    shapes are swept (a PDF target name plus `.supersede_tmp.<digits>` or
    `.copy_tmp.<digits>`). Supersede staging only ever happens next to tree
    targets, so a matching name under a top-level "_" system dir is not ours
    to delete; copy staging also happens in _orphans/, so copy_tmp leftovers
    are swept anywhere. Dry-run reports without touching the tree.
    """
    output = Path(output)
    if not output.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(output):
        # Never descend through a junction/symlinked folder: this sweep
        # deletes files, and a link inside the tree would take it outside.
        dirnames[:] = [d for d in dirnames
                       if not _is_dir_link(Path(dirpath) / d)]
        for name in sorted(filenames):
            if not _STAGING_RE.search(name):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(output)
            if is_system_dir(rel.parts[0]) and not _COPY_RE.search(name):
                continue
            if dry_run:
                log(f"  [DRY-RUN] remove leftover supersede staging: {rel}")
                continue
            try:
                p.unlink()
            except OSError as e:
                log(f"  WARNING: could not remove leftover supersede staging {rel}: {e}")
                continue
            log(f"  WARNING: removed leftover supersede staging from an interrupted run: {rel}")


def sweep_stale_claims(output, dry_run, log):
    """Remove leftover `<lock>.claim.<pid>` files older than the stale window.

    A run killed between the steal-rename and the claim-unlink leaves its
    staged claim behind (a dotfile next to the lock, invisible to scans like
    the lock itself). Only the exact claim shape at the output root is swept,
    and only when older than RUN_LOCK_STALE_SECS — a fresh claim may be a
    concurrent stealer mid-protocol and is never touched. Dry-run reports
    without touching the tree.
    """
    output = Path(output)
    if not output.is_dir():
        return
    now = time.time()
    for p in sorted(output.glob(RUN_LOCK_NAME + ".claim.*")):
        if not _CLAIM_RE.fullmatch(p.name):
            continue
        try:
            age = now - os.path.getmtime(_native(p))
        except OSError:
            continue
        if age <= RUN_LOCK_STALE_SECS:
            continue
        if dry_run:
            log(f"  [DRY-RUN] remove leftover lock claim: {p.name}")
            continue
        try:
            p.unlink()
        except OSError as e:
            log(f"  WARNING: could not remove leftover lock claim {p.name}: {e}")
            continue
        log(f"  WARNING: removed leftover lock claim from an interrupted run: {p.name}")


def find_organized_pdfs(folder, scan_res=None):
    """Map stem -> organized PDF paths nested under an output tree.

    Only nested (non-root) part-named PDFs outside "_"-prefixed system dirs
    count; callers pick the shallowest path when one stem has several copies.
    Accepts an optional `scan_res` (precomputed `scan_output_tree` result)
    to avoid redundant filesystem scans.
    """
    # Exclude system dirs (_superseded/_orphans/_*): only PDFs nested under
    # the output root count as organized (list kept; pick shallowest later).
    organized = defaultdict(list)
    tree_pdfs = (scan_res["tree"] if scan_res is not None
                 else scan_output_tree(folder)["tree"])
    for p in tree_pdfs:
        rel = p.relative_to(folder)
        stem = canonical_stem(p.stem)
        if len(rel.parts) > 1 and stem:
            organized[stem].append(p)
    return organized


def pick_shallowest(paths):
    """Return the shallowest path (fewest parts), tie-broken lexicographically."""
    return sorted(paths, key=lambda p: (len(p.parts), str(p)))[0]


def find_latest_report(folder):
    """Return the newest organize_fermi_pdfs_report_*.txt in folder, or None."""
    reports = sorted(folder.glob("organize_fermi_pdfs_report_*.txt"))
    return reports[-1] if reports else None


def folder_foreign_pdfs(folder, stem):
    """PDFs directly in `folder` that do not canonicalize to `stem`.

    A folder holding another drawing's PDF is shared (NAME/revision collision
    in placement, hand-made input trees); moving it would relocate unrelated
    files. Nested entries are always this stem's descendants by construction.
    Returns None when the folder cannot be listed (caller must fail safe).
    """
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return None
    foreign = []
    for p in entries:
        if not p.is_file() or p.suffix.lower() != ".pdf":
            continue
        if canonical_stem(p.stem) != stem:
            foreign.append(p)
    return foreign


def _same_bytes(a, b):
    """Byte-for-byte file compare that ignores any metadata cache.

    `filecmp.cmp` reuses cached deep-compare verdicts keyed by path plus
    (type, size, mtime): bytes changed with unchanged stats would look equal.
    OSError counts as different (safer)."""
    try:
        if os.path.getsize(_native(a)) != os.path.getsize(_native(b)):
            return False
        with open(_native(a), "rb") as fa, open(_native(b), "rb") as fb:
            while True:
                ba = fa.read(65536)
                bb = fb.read(65536)
                if ba != bb:
                    return False
                if not ba:
                    return True
    except OSError:
        return False


def resolve_archive_target(sup_dir, src, taken=()):
    """Archive destination for `src` under `sup_dir` that never clobbers bytes.

    Same-name archive with identical content is reused (`already_archived`
    True); differing content gets a numeric suffix (`<stem>.1.pdf`,
    incrementing until a free name; an identical suffixed copy is reused too,
    so repeat runs do not grow suffixes). Byte compares are cache-free
    (`filecmp.cmp` can reuse a stale verdict when bytes change without size or
    mtime changing); symlink candidates are always treated as occupied.
    `taken` lists destinations already claimed in this call sequence (dry-run
    planned copies), so same-name sources get distinct names there too — unless
    the planned source has identical bytes, in which case the same destination
    is reused (execution reuses the first archive via the byte compare below,
    so the plan matches it). Pass `taken` as a {destination: source} dict for
    that check; a set of destinations keeps the old distinct-name behavior.
    Callers must refuse symlinks at the base name first: `_exists` follows
    links, so a dangling link there would otherwise be written through.
    """
    target = sup_dir / src.name
    if not _exists(target) and target not in taken:
        return target, False
    if _same_bytes(target, src):
        return target, True
    if isinstance(taken, dict):
        planned_src = taken.get(target)
        if planned_src is not None and _same_bytes(planned_src, src):
            # Dry-run: the planned source has identical bytes, so execution
            # reuses the first archive instead of suffixing — plan the same
            # destination so the printed plan matches the real run.
            return target, True
    n = 1
    candidate = sup_dir / f"{target.stem}.{n}{target.suffix}"
    while _exists(candidate) or _islink(candidate) or candidate in taken:
        if not _islink(candidate) and _same_bytes(candidate, src):
            return candidate, True
        if isinstance(taken, dict):
            planned_src = taken.get(candidate)
            if planned_src is not None and _same_bytes(planned_src, src):
                return candidate, True
        n += 1
        candidate = sup_dir / f"{target.stem}.{n}{target.suffix}"
    return candidate, False


def copy_superseded(old, folder, dry_run, log, overwrite=False):
    """Copy superseded PDFs to _superseded/. Returns (count, archived stems).

    `archived` holds every stem whose archive copy exists after this call:
    copied now, already present, or (in dry-run) planned. Callers gate orphan
    retirement on it so a failed archive never deletes the last copy.
    overwrite=False (incremental): silently skip existing targets.
    overwrite=True (full): copy every superseded PDF; a name already holding
    different bytes is archived under a numeric suffix instead of clobbered.
    """
    n = 0
    archived = set()
    sf = folder / "_superseded"
    if _has_linked_ancestor(sf, folder):
        # A junction at _superseded/ makes an outside file look like the
        # archive, so an existing-copy check would retire a source whose only
        # copy is outside the managed tree. Refuse the whole directory.
        log(f"  WARNING: _superseded/ traverses a symlinked/junction ancestor, "
            f"refusing to archive: {sf}")
        return 0, archived
    for stem, pdf in sorted(old.items()):
        target = sf / pdf.name
        if _islink(pdf) or _islink(target):
            log(f"  WARNING: {stem}: copy skipped (symlink refused): {pdf} -> {target}")
            continue
        if not overwrite and _exists(target):
            # Existing archive wins in incremental mode; only an identical
            # copy proves this stem's bytes are archived (a same-name file
            # with different content must not retire a parked orphan).
            if _same_bytes(target, pdf):
                archived.add(stem)
            else:
                log(f"  {stem}: archive already exists with different content "
                    f"({target.name}) - source kept, not archived")
            continue
        target, already = resolve_archive_target(sf, pdf)
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {pdf.name} -> {target}")
        elif already:
            # Proven byte-identical: counted as archived without rewriting.
            log(f"  {stem}: archive already identical ({target.name}) - not rewritten")
        else:
            try:
                os.makedirs(_native(sf), exist_ok=True)
                _copy_atomic(pdf, target, folder)
            except (OSError, ValueError) as e:
                log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                continue
        # already=True means the destination was proven byte-identical by the
        # cache-free compare inside resolve_archive_target: re-writing the same
        # bytes on every repeat full run is pure I/O waste (archive mtime is
        # not a managed property), so the copy is skipped but the stem still
        # counts as archived and retirement stays honest.
        n += 1
        archived.add(stem)
    return n, archived


def copy_watermarked_duplicates(paths, folder, dry_run, log):
    """Copy watermarked same-revision duplicates into _superseded/.

    These lost the same-revision contest to a non-watermarked copy (or are
    redundant watermarked extras), so they are archived instead of placed.
    A name already holding different bytes is archived under a numeric
    suffix instead of clobbered; dry-run tracks planned destinations so
    same-name sources report distinct suffixes. Returns count copied.
    """
    n = 0
    sf = folder / "_superseded"
    planned = {}
    if _has_linked_ancestor(sf, folder):
        log(f"  WARNING: _superseded/ traverses a symlinked/junction ancestor, "
            f"refusing to archive: {sf}")
        return 0
    for p in sorted(paths):
        target = sf / p.name
        if _islink(p) or _islink(target):
            log(f"  WARNING: {p.name}: copy skipped (symlink refused): {p} -> {target}")
            continue
        target, already = resolve_archive_target(sf, p, planned)
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {p.name} -> {target}")
            planned[target] = p
        elif already:
            log(f"  {p.name}: archive already identical ({target.name}) - not rewritten")
        else:
            try:
                os.makedirs(_native(sf), exist_ok=True)
                _copy_atomic(p, target, folder)
            except (OSError, ValueError) as e:
                log(f"  WARNING: {p.name}: copy failed ({target}): {e}")
                continue
        # already=True: proven byte-identical archive exists - skip the
        # rewrite (same rule as copy_superseded); the count still includes it.
        n += 1
    return n


def copy_orphans(orphans, index, folder, dry_run, log, renames=None):
    """Copy orphan PDFs into _orphans/ so later runs can adopt them.
    Returns count copied."""
    n = 0
    renames = renames or {}
    orphans_dir = folder / "_orphans"
    if _has_linked_ancestor(orphans_dir, folder):
        log(f"  WARNING: _orphans/ traverses a symlinked/junction ancestor, "
            f"refusing to park orphans: {orphans_dir}")
        return 0
    for o in orphans:
        pdf = index.get(o)
        if pdf is None:
            continue
        target_name = renames.get(o, pdf.name)
        target = orphans_dir / target_name
        if _islink(pdf) or _islink(target):
            log(f"  WARNING: {o}: copy skipped (symlink refused): {pdf} -> {target}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {target_name} -> {target}")
        else:
            try:
                os.makedirs(_native(orphans_dir), exist_ok=True)
                _copy_atomic(pdf, target, folder)
            except (OSError, ValueError) as e:
                log(f"  WARNING: {o}: copy failed ({target}): {e}")
                continue
        n += 1
    if dry_run:
        log(f"Would store {len(orphans)} orphan(s) in _orphans/ "
            "(kept for later parent adoption) (dry-run)")
    else:
        log(f"Stored {n} orphan(s) in _orphans/ (kept for later parent adoption)")
    return n


def retire_adopted_orphans(stored_orphans, folder, dry_run, log, superseded=(),
                           planned=(), scan_res=None):
    """Delete parked _orphans/ copies whose stem is now live in the tree,
    or whose stem was superseded (its archived copy lives in _superseded/).
    A superseded stem is retired only when its bytes live in the tree or in
    the archive. A live same-stem copy retires the parked copy when its bytes
    match the parked copy, or when the parked copy is watermarked and the live
    copy is clean (the watermark difference must not leave order-dependent
    orphans). A differing clean parked copy is kept with a warning instead
    (it may be the only copy of those bytes). `superseded` must list only stems whose archive copy
    was verified written (e.g. copy_superseded's archived set) - a failed
    archive must never delete the parked copy. In dry-run only, stems in
    planned (placed by this run but not yet on disk) also count as live.
    Returns the number retired. Accepts an optional `scan_res` (precomputed
    `scan_output_tree` result) to avoid redundant filesystem scans when the
    tree on disk has not changed.
    """
    scan = scan_res if scan_res is not None else scan_output_tree(folder)
    live_pdfs = defaultdict(list)
    for p in scan["tree"]:
        stem = canonical_stem(p.stem)
        if stem:
            live_pdfs[stem].append(p)
    retired = 0
    sup = set(superseded)
    # Planned stems count only for dry-run reporting: in a real run the
    # placed copies are already on disk, and a failed copy must not retire
    # the orphan that adoption never actually reached.
    planned_set = set(planned) if dry_run else set()
    for o, opath in sorted(stored_orphans.items()):
        live = [p for p in live_pdfs.get(o, []) if p != opath]
        if opath.is_symlink():
            # Never compare through (or retire) a link; log only when
            # retirement was otherwise due, matching the former behavior.
            if live or o in sup or o in planned_set:
                log(f"  WARNING: could not retire orphan copy {o}: symlink refused: {opath}")
            continue
        if o in sup and o not in planned_set:
            # The superseding archive only justifies retirement when it (or a
            # live tree copy) holds the parked bytes: otherwise the parked copy
            # is the sole holder of those bytes and must survive.
            copies = live + [p for p in scan["sup"]
                            if canonical_stem(p.stem) == o]
            retireable = any(not _islink(p) and _same_bytes(p, opath)
                             for p in copies)
            if not retireable:
                log(f"  WARNING: orphan copy {o} not retired: no "
                     f"byte-identical copy in tree or _superseded/; kept: {opath}")
                continue
        elif o not in planned_set:
            # Watermark dominance (order-independent): a parked watermarked
            # copy is dominated by any clean live same-stem copy, even when the
            # bytes differ only by the watermark. A byte-identical copy also
            # retires the parked copy. Clean parked copies keep the old
            # byte-safety rule, so a genuinely newer input is not discarded.
            # Symlinked live entries are not managed copies.
            same_bytes = any(not _islink(p) and _same_bytes(p, opath)
                              for p in live)
            watermarked_dominated = (
                _pdf_watermarked(opath)
                and any(not _islink(p) and not _pdf_watermarked(p)
                        for p in live))
            retireable = same_bytes or watermarked_dominated
            if not retireable:
                if live:
                    log(f"  WARNING: orphan copy {o} not retired: no "
                         f"byte-identical regular live copy; kept: {opath}")
                continue
        if dry_run:
            log(f"  [DRY-RUN] retire orphan copy: {opath.relative_to(folder)} (now placed in tree)")
            continue
        try:
            opath.unlink()
            retired += 1
        except OSError as e:
            log(f"  WARNING: could not retire orphan copy {o}: {e}")
    if retired:
        log(f"Retired {retired} orphan copy(ies) from _orphans/ (adopted into tree)")
    return retired


def remove_empty_dirs(output, dry_run, log, lock=None):
    """Delete empty folders under an output tree, deepest first.

    Top-level system dirs (any "_" prefix) and the output root itself are never
    removed, and a folder holding anything - including a file the tool did not
    write - is left alone. `lock` is the run's lock path (None = unlocked) and
    is re-checked before each removal, so a run stolen mid-sweep stops instead
    of deleting folders another run now owns. Returns the number removed.
    """
    removed = 0
    dirs = []
    # os.walk with explicit pruning: rglob follows NTFS junctions, so a linked
    # folder in the tree could make the sweep remove empty dirs outside it.
    for dirpath, dirnames, _files in os.walk(output, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not _is_dir_link(Path(dirpath) / d)]
        for d in dirnames:
            path = Path(dirpath) / d
            if not _is_dir_link(path):
                dirs.append(path)
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    for path in dirs:
        rel = path.relative_to(output)
        if not rel.parts or rel.parts[0].startswith("_"):
            continue
        try:
            if any(path.iterdir()):
                continue
        except OSError:
            continue
        if dry_run:
            log(f"  [DRY-RUN] rmdir {rel}")
        else:
            _ensure_lock(lock, log)
            try:
                os.rmdir(_native_tree(path))
            except OSError as e:
                log(f"  WARNING: could not remove empty folder {rel}: {e}")
                continue
        removed += 1
    if removed:
        log(f"Removed {removed} empty folder(s)")
    return removed


def longest_dest_path(target_path, current):
    """Longest file path a copy of `current` under `target_path` would create.

    place_files guards every target against MAX_PATH; the folder-level MAX_DIR
    check alone does not bound descendant files, which fail mid-copy on
    Windows and force partial-tree cleanup. Returns None when `current` cannot
    be listed (callers treat that as "do not act").
    """
    try:
        entries = list(current.rglob("*"))
    except OSError:
        return None
    if not entries:
        return target_path
    return max((target_path / e.relative_to(current) for e in entries),
               key=lambda p: len(str(p)))


def _folder_removable(folder, planned_by_stem, output):
    """True when every file under `folder` also exists at a planned path.

    The rule that makes repair safe: a folder leaves the tree only after every
    PDF in its subtree is byte-identical to the copy the plan gives that stem,
    and it holds no non-PDF file. Returns (ok, reason) for the review line.
    """
    reason = _subtree_link_or_unlistable(folder, any_link=False)
    if reason is not None and folder.is_dir():
        # Not on disk yet (dry-run reads post-rename names): the rmtree below
        # fails harmlessly, so keep the pre-existing behaviour there.
        return False, reason
    try:
        entries = list(folder.rglob("*"))
    except OSError as e:
        return False, f"cannot list ({e})"
    for path in entries:
        # A symlink (live or broken) or any other non-PDF entry is a manual
        # addition: is_file() is False for a broken or directory symlink, and
        # omitting one would let the folder be removed around it.
        if not _islink(path) and _isdir(path):
            continue
        if _islink(path) or not _isfile(path):
            return False, f"holds non-PDF entry {path.name}"
        if path.suffix.lower() != ".pdf":
            return False, f"holds non-PDF file {path.name}"
        stem = canonical_stem(path.stem)
        targets = [output.joinpath(*rel) / path.name
                   for rel in planned_by_stem.get(stem, ())]
        # The twin must be a different, regular file: a drawing already at its
        # own planned path would otherwise vouch for the folder it lives in,
        # and a symlinked "twin" points outside the tree, so neither is an
        # independent planned copy. One explicit pass per target (instead of
        # any()) keeps the link and byte checks in one place; the compare itself
        # is _same_bytes, which never consults filecmp's metadata cache, so a
        # changed file with unchanged size/mtime cannot masquerade as a match -
        # correctness over performance on this destructive path.
        ok = False
        for t in targets:
            if _islink(t) or t == path or not t.exists():
                continue
            if _has_linked_ancestor(t, output):
                continue
            if _same_bytes(path, t):
                ok = True
                break
        if not ok:
            return False, f"{path.name} has no identical copy at its planned path"
    return True, ""


def _remap_folder(folder, moved_dirs):
    """Folder after earlier moves this run (exact or ancestor match)."""
    if isinstance(moved_dirs, FolderMoves):
        return moved_dirs.resolve(folder)
    for old, new in moved_dirs.items():
        if folder == old or folder.is_relative_to(old):
            return new.joinpath(*folder.relative_to(old).parts)
    return folder


class FolderMoves(dict):
    """Chronological folder relocations with a resolved-path index.

    Insertion order is chronology, exactly how the move passes record them.
    Plain-dict semantics are kept (item assignment, items(), values(), `in`,
    len) because callers already use it that way, so the only change is that
    resolve() answers the old linear-scan question without scanning every
    move per folder.

    The old form was O(folders x moves): repair on a tree that is entirely
    off-plan measured 1.18 s / 4.14 s / 15.32 s at 250 / 500 / 1,000 folders
    (4x per doubling, ~6.5 min at 5,000). resolve() looks up only the query
    folder's own ancestors, so the cost is O(depth x applied moves).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seq = 0
        self._order = {}
        self._new = {}
        for old, new in super().items():
            self._remember(Path(old), Path(new))

    def _remember(self, old, new):
        # A plain dict keeps a re-assigned key at its FIRST insertion position
        # with the LAST value; mirror that exactly, or resolution order drifts.
        if old not in self._order:
            self._seq += 1
            self._order[old] = self._seq
        self._new[old] = new

    def __setitem__(self, old, new):
        super().__setitem__(old, new)
        self._remember(Path(old), Path(new))

    def resolve(self, folder):
        """`folder` after every recorded move, applied in chronological order.

        One ordered pass is deliberate: a repeated pass would follow a
        mapping whose key was an old path later reused as another folder's
        destination, resolving that newcomer into the wrong tree. This loop
        picks the earliest not-yet-applied move among the current path's
        ancestors, which is exactly that same single ordered pass.
        """
        current = Path(folder)
        applied = 0
        while True:
            best = None
            for ancestor in (current, *current.parents):
                seq = self._order.get(ancestor)
                if seq is not None and seq > applied and (best is None or seq < best[0]):
                    best = (seq, ancestor)
            if best is None:
                return current
            seq, old = best
            current = self._new[old].joinpath(*current.relative_to(old).parts)
            applied = seq


def _drop_organized_under(organized, folder):
    """Forget every organized path inside a folder that no longer exists."""
    for stem, paths in organized.items():
        kept = [p for p in paths if not p.is_relative_to(folder)]
        if len(kept) != len(paths):
            organized[stem] = kept


def _remap_organized_under(organized, old, new):
    """Point every organized path inside `old` at `new` after a folder move."""
    for stem, paths in organized.items():
        organized[stem] = [new.joinpath(*p.relative_to(old).parts)
                           if p.is_relative_to(old) else p for p in paths]


def _subtree_link_or_unlistable(folder, any_link=True):
    """Why `folder`'s subtree cannot be trusted, or None when it is clean.

    rglob descends through NTFS junctions (followlinks only covers symlinks) and
    os.walk silently skips a level it cannot list, so either walk can miss a
    linked subtree: a move would carry the link along, and a delete would follow
    it. Callers walk prune-first and refuse on the first hit. With any_link
    False only directory links are refused here; a caller that inspects plain
    symlink entries itself keeps its own, more specific, reason.
    """
    walk_errors = []
    if _is_dir_link(folder):
        return "is a symlink/junction"
    for dirpath, dirnames, filenames in os.walk(folder,
                                                onerror=walk_errors.append):
        for name in sorted(dirnames) + sorted(filenames):
            entry = Path(dirpath) / name
            if entry.is_dir() and _is_dir_link(entry):
                return f"contains a symlink/junction: {entry.name}"
            if any_link and _islink(entry):
                return f"contains a symlink/junction: {entry.name}"
    if walk_errors:
        return f"cannot list the whole subtree ({walk_errors[0]})"
    return None


def refuse_linked_target(target, output, log, label):
    """True (and logged) when a planned destination traverses a link.

    Every mutating sink calls this before it creates or writes: mkdir, move,
    copytree and the atomic copies. A symlink/junction anywhere between the
    output root and the destination redirects the write outside the tree, and
    the run would still report success. One helper so the message and the rule
    cannot drift between call sites.
    """
    if _has_linked_ancestor(target, output):
        log(f"  WARNING: {label}: destination traverses a symlinked/junction "
            f"ancestor, refusing: {target}")
        return True
    return False


def _move_folder(stem, folder, target, output, dry_run, log, moved_dirs,
                 organized, lock):
    """Move a folder (with its subtree) to `target`; False when refused.

    Refused for an existing destination, a resulting path over MAX_PATH, or a
    failed move - each logged, never fatal. A folder shared with another
    drawing is not refused: repair moves whole subtrees on purpose, and any
    child that belongs elsewhere is reconciled by its own pass afterwards.
    """
    if _is_dir_link(folder):
        log(f"  WARNING: {stem}: source is a symlink/junction, refusing move "
            f"(manual review): {folder}")
        return False
    reason = _subtree_link_or_unlistable(folder)
    if reason is not None and folder.is_dir():
        # An absent root is not a refusal: dry-run reads the post-rename path,
        # and the checks below decide what to do with a folder not yet on disk.
        log(f"  WARNING: {stem}: source {reason}, refusing move "
            f"(manual review): {folder}")
        return False
    if _exists(target):
        log(f"  WARNING: {stem}: destination exists, not merging "
            f"(manual review): {target.relative_to(output)}")
        return False
    longest = longest_dest_path(target, folder)
    if longest is None:
        # Cannot list the subtree, so its deepest path is unknown; a move would
        # skip the MAX_PATH guard and could fail half-way. Refuse it.
        log(f"  WARNING: {stem}: cannot list source folder, skipping move "
            f"(manual review): {target.relative_to(output)}")
        return False
    if len(str(longest)) > MAX_PATH:
        log(f"  WARNING: {stem}: path too long ({len(str(longest))} chars), "
            f"skipping move: {target}")
        return False
    if dry_run:
        log(f"  [DRY-RUN] move {folder.relative_to(output)} -> "
            f"{target.relative_to(output)}")
    else:
        _ensure_lock(lock, log)
        if refuse_linked_target(target, output, log, stem):
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(target))
        except (OSError, ValueError) as e:
            log(f"  WARNING: {stem}: move failed ({target}): {e}")
            return False
    moved_dirs[folder] = target
    _remap_organized_under(organized, folder, target)
    return True


def _place_copy(stem, organized, target, output, dry_run, log, lock):
    """Copy a stem's PDF into a second planned folder (multi-claimant case)."""
    if _exists(target):
        return False
    live = [p for p in organized.get(stem, []) if p.exists()]
    src = pick_shallowest(live or organized[stem])
    dst = target / src.name
    if len(str(target)) > MAX_DIR or len(str(dst)) > MAX_PATH:
        log(f"  WARNING: {stem}: path too long ({len(str(dst))} chars), "
            f"skipping: {dst}")
        return False
    if dry_run:
        log(f"  [DRY-RUN] mkdir+copy {src.name} -> {target.relative_to(output)}")
        return True
    _ensure_lock(lock, log)
    # Check before mkdir, like place_files: mkdir(parents=True) through a
    # junction would create the planned folder outside the tree, and only the
    # copy would be refused afterwards.
    if _has_linked_ancestor(target, output):
        log(f"  WARNING: {stem}: copy skipped (folder path traverses a "
            f"symlinked/junction ancestor, refusing): {target}")
        return False
    try:
        target.mkdir(parents=True, exist_ok=True)
        _copy_atomic(src, dst, output)
    except (OSError, ValueError) as e:
        log(f"  WARNING: {stem}: copy failed ({dst}): {e}")
        return False
    organized.setdefault(stem, []).append(dst)
    return True


def reconcile_tree(organized, planned, output, dry_run, log, moved_dirs,
                   lock=None):
    """Move live folders onto the planned tree; prune verified duplicates.

    `organized` (stem -> live PDF paths) and `moved_dirs` are updated in
    place: a relocation is recorded so later passes follow it, and every
    organized path under a moved or removed folder is remapped or dropped.
    `planned` is graph.planned_paths output. A folder is deleted only when
    _folder_removable proves every file in it has a byte-identical twin at a
    planned path, so a stem's last copy is never deleted.

    Stems are visited roots-first (by planned depth), so a parent's move lands
    before its children are inspected. Returns (counts, review) with counts
    {moved, copied, removed, review} and review = ["stem: reason", ...].
    """
    counts = {"moved": 0, "copied": 0, "removed": 0, "review": 0}
    review = []

    def note(stem, msg):
        counts["review"] += 1
        review.append(f"{stem}: {msg}")
        log(f"  WARNING: {stem}: {msg} (manual review)")

    for stem in sorted(planned,
                       key=lambda s: (min(len(r) for r in planned[s]), s)):
        paths = organized.get(stem)
        if not paths:
            continue
        targets = [output.joinpath(*rel) for rel in planned[stem]]
        current = []
        for path in paths:
            folder = _remap_folder(path.parent, moved_dirs)
            if folder not in current:
                current.append(folder)
        keep = [f for f in current if f in targets]
        extra = [f for f in current if f not in targets]
        missing = [t for t in targets if t not in current]
        for folder in extra:
            if missing and not _exists(missing[0]):
                target = missing.pop(0)
                if _move_folder(stem, folder, target, output, dry_run, log,
                                moved_dirs, organized, lock):
                    counts["moved"] += 1
                    keep.append(target)
                else:
                    # A refused move leaves the stem off plan, so the run must
                    # report it: silently continuing would let the CLI exit 0
                    # on an unreconciled tree.
                    note(stem, f"move to {target.relative_to(output)} refused; "
                               f"stale copy kept at "
                               f"{folder.relative_to(output)}")
                continue
            if not keep:
                note(stem, f"stale copy kept at {folder.relative_to(output)} "
                           "(no planned copy yet)")
                continue
            ok, reason = _folder_removable(folder, planned, output)
            if not ok:
                note(stem, f"stale copy kept at {folder.relative_to(output)}: "
                           f"{reason}")
                continue
            if dry_run:
                log(f"  [DRY-RUN] remove duplicate folder "
                    f"{folder.relative_to(output)}")
            else:
                _ensure_lock(lock, log)
                try:
                    shutil.rmtree(_native_tree(folder))
                except OSError as e:
                    note(stem, f"remove failed: {e}")
                    continue
                _drop_organized_under(organized, folder)
            counts["removed"] += 1
        for target in missing:
            if _place_copy(stem, organized, target, output, dry_run, log, lock):
                counts["copied"] += 1
    return counts, review
