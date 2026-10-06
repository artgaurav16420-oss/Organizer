#!/usr/bin/env python3
"""Filesystem scans and side effects (indexing, placement, copies)."""
import shutil
from collections import defaultdict
from pathlib import Path

from .config import (canonical_stem, MAX_PATH, MAX_DIR, TREE_MAX_DEPTH)
from .graph import is_chk_stem
from .naming import folder_name_for


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
        if p.is_symlink():
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


def place_files(children, roots, index, folder, dry_run, log, names_of=None, folder_names=None, renames=None):
    """Copy every root's subtree into nested folders; returns the copy count.

    Folder names come from `folder_names` (already path-shortened), falling
    back to `names_of`. `renames` maps a stem to the copied file's new name
    (re-keyed misnamed files). Dry-run logs planned copies without touching
    disk; oversized paths/subtrees are skipped with a warning.
    """
    total_copies = 0
    skipped_count = 0
    failed_count = 0
    names_of = names_of or {}
    folder_names = folder_names or {}
    renames = renames or {}
    # Memoize copy counts: a diamond DAG would otherwise blow up exponentially.
    copy_count_memo = {}
    def count_copies(stem):
        if stem in copy_count_memo:
            return copy_count_memo[stem]
        total = 1
        for child in children.get(stem, ()):
            total += count_copies(child)
        copy_count_memo[stem] = total
        return total
    total_planned = sum(count_copies(root) for root in roots)
    if total_planned > 1000:
        log(f"  WARNING: {total_planned} copies planned (diamond DAG may cause exponential growth)")

    def fname(stem):
        return folder_names.get(stem) or folder_name_for(stem, names_of)

    def dfs(stem, current_folder, depth=0):
        nonlocal total_copies, skipped_count, failed_count
        if depth > TREE_MAX_DEPTH:
            log(f"  WARNING: placement truncated at depth {depth} in "
                f"{current_folder.name} (deeper than any healthy tree) - manual review")
            skipped_count += count_copies(stem)
            return
        pdf = index[stem]
        target_name = renames.get(stem, pdf.name)
        target = current_folder / target_name
        if pdf.is_symlink() or target.is_symlink():
            log(f"  WARNING: {stem}: copy skipped (symlink refused): {pdf} -> {target}")
            failed_count += 1
            for child in sorted(children.get(stem, ())):
                dfs(child, current_folder / fname(child), depth + 1)
            return
        # Dir path checked against MAX_DIR, full target against MAX_PATH.
        if len(str(current_folder)) > MAX_DIR or len(str(target)) > MAX_PATH:
            log(f"  WARNING: path too long ({len(str(target))} chars), skipping: {target}")
            skipped_count += count_copies(stem)
            return
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {target_name} -> {target}")
            total_copies += 1
        else:
            # mkdir failure: the subtree's paths cannot exist - skip it;
            # copy2 failure: only this file is lost, children are still tried.
            try:
                current_folder.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                failed_count += count_copies(stem)
                return
            try:
                shutil.copy2(pdf, target)
            except OSError as e:
                log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                failed_count += 1
            else:
                total_copies += 1
        for child in sorted(children.get(stem, ())):
            dfs(child, current_folder / fname(child), depth + 1)

    for root in sorted(roots):
        dfs(root, folder / fname(root))
    if skipped_count:
        log(f"  Skipped {skipped_count} path(s) due to length limits")
    if failed_count:
        log(f"  FAILED {failed_count} copy(ies) - see warnings above")
    return total_copies



def is_system_dir(name):
    """Convention: any top-level directory whose name starts with '_' is a system dir."""
    return name.startswith("_")


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
            if top.startswith("_superseded"):
                sup.append(p)
            elif top.startswith("_orphans"):
                orph.append(p)
            elif is_system_dir(top):
                continue
            else:
                tree.append(p)
    return {"tree": tree, "sup": sup, "orph": orph}


def find_organized_pdfs(folder):
    """Map stem -> organized PDF paths nested under an output tree.

    Only nested (non-root) part-named PDFs outside "_"-prefixed system dirs
    count; callers pick the shallowest path when one stem has several copies.
    """
    # Exclude system dirs (_superseded/_orphans/_*): only PDFs nested under
    # the output root count as organized (list kept; pick shallowest later).
    organized = defaultdict(list)
    for p in scan_output_tree(folder)["tree"]:
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


def copy_superseded(old, folder, dry_run, log, overwrite=False):
    """Copy superseded PDFs to _superseded/. Returns count copied.

    overwrite=False (incremental): silently skip existing targets.
    overwrite=True (full): copy every superseded PDF, replacing existing targets.
    """
    n = 0
    sf = folder / "_superseded"
    for stem, pdf in sorted(old.items()):
        target = sf / pdf.name
        if not overwrite and target.exists():
            continue
        if pdf.is_symlink() or target.is_symlink():
            log(f"  WARNING: {stem}: copy skipped (symlink refused): {pdf} -> {target}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {pdf.name} -> {target}")
        else:
            try:
                sf.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pdf, target)
            except OSError as e:
                log(f"  WARNING: {stem}: copy failed ({target}): {e}")
                continue
        n += 1
    return n


def copy_watermarked_duplicates(paths, folder, dry_run, log):
    """Copy watermarked same-revision duplicates into _superseded/.

    These lost the same-revision contest to a non-watermarked copy (or are
    redundant watermarked extras), so they are archived instead of placed.
    Returns count copied.
    """
    n = 0
    sf = folder / "_superseded"
    for p in sorted(paths):
        target = sf / p.name
        if p.is_symlink() or target.is_symlink():
            log(f"  WARNING: {p.name}: copy skipped (symlink refused): {p} -> {target}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {p.name} -> {target}")
        else:
            try:
                sf.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
            except OSError as e:
                log(f"  WARNING: {p.name}: copy failed ({target}): {e}")
                continue
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
        if pdf.is_symlink() or target.is_symlink():
            log(f"  WARNING: {o}: copy skipped (symlink refused): {pdf} -> {target}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] mkdir+copy {target_name} -> {target}")
        else:
            try:
                orphans_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pdf, target)
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


def retire_adopted_orphans(stored_orphans, folder, dry_run, log, superseded=()):
    """Delete parked _orphans/ copies whose stem is now live in the tree,
    or whose stem was superseded (its archived copy lives in _superseded/).
    Returns the number retired."""
    live_pdfs = defaultdict(list)
    for p in scan_output_tree(folder)["tree"]:
        stem = canonical_stem(p.stem)
        if stem:
            live_pdfs[stem].append(p)
    retired = 0
    sup = set(superseded)
    for o, opath in sorted(stored_orphans.items()):
        live = [p for p in live_pdfs.get(o, []) if p != opath]
        if not live and o not in sup:
            continue
        if opath.is_symlink():
            log(f"  WARNING: could not retire orphan copy {o}: symlink refused: {opath}")
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
