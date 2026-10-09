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
from pathlib import Path

from .config import (canonical_stem, MAX_PATH, MAX_DIR, TREE_MAX_DEPTH,
                     PLANNED_COPIES_WARN, MAX_PLANNED_COPIES)
from .graph import is_chk_stem
from .naming import folder_name_for


def _native(path):
    r"""Filesystem-op view of `path`: add the Windows extended prefix when long.

    With LongPathsEnabled off, open()/copy2 on an absolute path >= 260 chars
    fails with ENOENT even though the file exists (input trees nest deep;
    the output tree is bounded by MAX_PATH, so only input-derived paths hit
    this - real run: 6 CHK archive copies failed without the prefix).
    Drive paths get `\\?\`; UNC paths need the `\\?\UNC\server\share` form.
    Forward-slash paths are left alone (`\\?\` rejects them; pathlib on
    Windows always yields backslashes).
    """
    s = os.fspath(path)
    if os.name != "nt" or len(s) < 260 or s.startswith("\\\\?\\"):
        return path
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    if os.path.isabs(s) and "/" not in s:
        return "\\\\?\\" + s
    return path


def _exists(path):
    """os.path.exists that sees beyond MAX_PATH (blind spot of Path.exists)."""
    return os.path.exists(_native(path))


def _islink(path):
    """os.path.islink that sees beyond MAX_PATH (Path.is_symlink goes blind)."""
    return os.path.islink(_native(path))


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
    """Validate revision-token convention: second underscore segment must be empty,
    a single letter, or a CHK marker (ranked, not "treated as 0")."""
    unusual = []
    for s in index:
        if is_chk_stem(s):
            continue
        parts = s.split("_")
        tok = parts[1] if len(parts) > 1 else ""
        if tok and not (len(tok) == 1 and tok.isalpha()):
            unusual.append(s)
    return unusual


def _log_scan_skips(log, skipped_output, skipped_sys):
    if not log:
        return
    if skipped_output:
        log(f"Skipped {skipped_output} PDF(s) under Output/ (organized copies are never input)")
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


def build_pdf_index(folder, log=None):
    """Map uppercase stem -> PDF path for every part-named PDF under `folder`.

    Recursive; top-level Output/ and "_"-prefixed dirs are skipped. Duplicate
    stems keep the shallowest path (input originals beat nested organized-tree
    copies), then lexicographic order. Skips/dupes/ignored files are logged via
    `log` when given.

    Returns (index, duplicates): `duplicates` maps a stem to the losing paths
    (sorted scan order) so callers can compare same-revision copies (e.g.
    prefer a non-watermarked copy over a watermarked one).
    """
    # Recursive: every *.pdf under the input folder counts, at any depth.
    # In-place output is the default, so the organized tree usually lives
    # inside the input folder; only top-level "Output/" and "_"-prefixed
    # dirs (_superseded/_orphans) are skipped. Repeated full runs rescan
    # the already-organized folders unless a separate --output is used.
    index = {}
    duplicates = defaultdict(list)
    ignored = []
    dupes = []
    skips = {"skip_output": 0, "skip_sys": 0}
    skipped_symlinks = 0
    for p in sorted(folder.rglob("*.pdf")):
        if _islink(p):
            skipped_symlinks += 1
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


def place_files(children, roots, index, folder, dry_run, log, names_of=None, folder_names=None, renames=None):
    """Copy every root's subtree into nested folders; returns the copy count.

    Folder names come from `folder_names` (already path-shortened), falling
    back to `names_of`. `renames` maps a stem to the copied file's new name
    (re-keyed misnamed files). Dry-run logs planned copies without touching
    disk; oversized paths/subtrees are skipped with a warning. Raises
    PlacementRefusedError when the planned fan-out exceeds
    MAX_PLANNED_COPIES (nothing is created).

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
                try:
                    current_folder.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as e:
                    log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                    failed_count += count_copies(stem)
                    continue
                try:
                    shutil.copy2(_native(pdf), _native(target))
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
    return name.startswith("_")


RUN_LOCK_NAME = ".fermi_organizer.lock"
# A lock older than this is presumed left by a crashed run and stolen with a
# warning. 2 h is far beyond any healthy run (thousands of PDFs take
# minutes), so a crashed run blocks the next one only briefly; the check is
# mtime-only on purpose — PID-liveness probing via os.kill has fatally
# inconsistent signal semantics across platforms, so the lock fails closed
# (refuse + tell the operator how to recover) instead of guessing.


class RunLockedError(Exception):
    """Another run holds the output-tree lock; this run is refused."""


RUN_LOCK_STALE_SECS = 2 * 3600
# Heartbeat: a live holder re-touches its lock this often, so a run longer
# than the stale window never looks stealable. Daemon thread, parent process
# only (spawn workers get a fresh interpreter without it); stopped at release.
RUN_LOCK_HEARTBEAT_SECS = 600
_heartbeats = {}


def acquire_run_lock(output, dry_run, log):
    """Exclusive per-output-tree lock; returns the lock path, None when the
    lock could not be created (warned, run continues unlocked), and raises
    RunLockedError when another run may hold the tree.

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
    portably; NFS ignores even fcntl). The file is a dotfile with JSON {pid, host, created}; scans
    only collect PDFs, so it is invisible to every tree walk.
    only collect PDFs, so it is invisible to every tree walk.
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
            log(f"  WARNING: could not create run lock ({lock.name}): {e} - "
                "continuing unlocked (do not run two processes against one output)")
            return None
        else:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"pid": os.getpid(),
                                        "host": socket.gethostname(),
                                        "created": time.time()}))
            except OSError as e:
                # The O_EXCL-created file would otherwise linger (no lock path
                # to clean up) and refuse later runs until stale: remove it
                # and run unlocked like a creation failure.
                try:
                    os.unlink(_native(lock))
                except OSError:
                    pass
                log(f"  WARNING: could not write run lock ({lock.name}): {e} - "
                    "continuing unlocked (do not run two processes against one output)")
                return None
            _heartbeat_start(lock)
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
    """Remove our lock file; best-effort (a leftover only blocks the next run
    until it goes stale, then it is stolen with a warning)."""
    if lock is None:
        return
    _heartbeat_stop(lock)
    try:
        os.unlink(_native(lock))
    except OSError:
        pass


def _heartbeat_start(lock):
    """Refresh the lock mtime periodically while we hold it."""
    stop = threading.Event()

    def beat():
        while not stop.wait(RUN_LOCK_HEARTBEAT_SECS):
            try:
                os.utime(_native(lock), None)
            except OSError:
                return

    t = threading.Thread(target=beat, daemon=True,
                         name="fermi-lock-heartbeat")
    _heartbeats[str(lock)] = (stop, t)
    t.start()


def _heartbeat_stop(lock):
    """Stop the refresh thread; join briefly so release is ordered."""
    item = _heartbeats.pop(str(lock), None)
    if item is not None:
        stop, t = item
        stop.set()
        t.join(timeout=5)


def scan_output_tree(output):
    """Live scan of the output folder, categorized by the system-dir convention.

    Returns {'tree': [pdf paths in the organized tree],
             'sup': [pdf paths under _superseded*],
             'orph': [pdf paths under _orphans*]}.
    Any other '_' prefixed top-level name is excluded from all three lists.
    """
    output = Path(output)
    tree, sup, orph = [], [], []
    if output.is_dir():
        for p in output.rglob("*.pdf"):
            top = p.relative_to(output).parts[0]
            if is_system_dir(top):
                if top.startswith("_superseded"):
                    sup.append(p)
                elif top.startswith("_orphans"):
                    orph.append(p)
            else:
                tree.append(p)
    return {"tree": tree, "sup": sup, "orph": orph}


def sweep_supersede_staging(output, dry_run, log):
    """Remove leftover `<name>.pdf.supersede_tmp.<pid>` staging files.

    _swap_revision_files stages the new revision next to its target before
    os.replace; a run killed mid-swap leaves the staging file behind. It
    never ends in .pdf, so scans ignore it - only this sweep cleans it up.
    Only the exact staging shape is swept (a PDF target name plus
    `.supersede_tmp.<digits>`), and "_"-prefixed system dirs are pruned from
    the walk: staging only ever happens next to targets in the organized
    tree, so matching lookalikes elsewhere are not ours to delete.
    Dry-run reports without touching the tree.
    """
    output = Path(output)
    if not output.is_dir():
        return
    staging_re = re.compile(r"\.pdf\.supersede_tmp\.\d+$", re.IGNORECASE)
    for dirpath, dirnames, filenames in os.walk(output):
        if Path(dirpath) == output:
            # Only top-level "_" dirs are system dirs; nested ones are not
            # pruned (is_system_dir's convention covers the top level only).
            dirnames[:] = [d for d in dirnames if not is_system_dir(d)]
        for name in sorted(filenames):
            if not staging_re.search(name):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(output)
            if is_system_dir(rel.parts[0]):
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
    return sorted(paths, key=lambda p: (len(p.parts), str(p)))[0]


def find_latest_report(folder):
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
                shutil.copy2(_native(pdf), _native(target))
            except OSError as e:
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
                shutil.copy2(_native(p), _native(target))
            except OSError as e:
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
                shutil.copy2(_native(pdf), _native(target))
            except OSError as e:
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
    A live same-stem copy only counts when its bytes match the parked copy:
    a differing copy may be the only copy of those bytes (e.g. the input was
    updated after the parked copy was made), so the parked copy is kept with
    a warning instead. `superseded` must list only stems whose archive copy
    was verified written (e.g. copy_superseded's archived set) - a failed
    archive must never delete the parked copy. In dry-run only, stems in
    planned (placed by this run but not yet on disk) also count as live.
    Returns the number retired. Accepts an optional `scan_res` (precomputed
    `scan_output_tree` result) to avoid redundant filesystem scans when the
    tree on disk has not changed.
    """
    live_pdfs = defaultdict(list)
    tree_pdfs = (scan_res["tree"] if scan_res is not None
                 else scan_output_tree(folder)["tree"])
    for p in tree_pdfs:
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
        if o not in sup and o not in planned_set:
            # Only a byte-identical regular live copy retires the parked
            # copy; a symlinked live entry never counts (same rule as the
            # archive byte-verify: a link is not a managed copy).
            retireable = any(not _islink(p) and _same_bytes(p, opath)
                             for p in live)
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
