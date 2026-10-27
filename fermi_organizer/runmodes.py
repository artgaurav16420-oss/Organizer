#!/usr/bin/env python3
"""Full, incremental and repair organization runs."""
import os
import re
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TypedDict

from .config import (
    MAX_DIR,
    MAX_PATH,
    MAX_PLANNED_COPIES,
    PLAN_PLACEMENT_FACTOR,
    TB_NUM_VAL_RE,
    TREE_MAX_DEPTH,
    canonical_stem,
    filename_title,
    is_processable_ref,
)
from .extraction import (
    OCR,
    dup_meta_task,
    org_task,
    resolve_jobs,
    run_parallel,
    survey_task,
)
from .fsops import (
    FolderMoves,
    LockLostError,
    _copy_atomic,
    _ensure_lock,
    _exists,
    _has_linked_ancestor,
    _islink,
    refuse_linked_target,
    _native,
    _native_tree,
    _same_bytes,
    _staged_copy,
    abandon_run_lock,
    acquire_run_lock,
    build_pdf_index,
    copy_orphans,
    copy_superseded,
    copy_watermarked_duplicates,
    find_latest_report,
    find_organized_pdfs,
    fit_roots_within_cap,
    folder_foreign_pdfs,
    longest_dest_path,
    pick_shallowest,
    place_files,
    planned_copy_count,
    reconcile_tree,
    release_run_lock,
    remove_empty_dirs,
    resolve_archive_target,
    retire_adopted_orphans,
    scan_output_tree,
    sweep_stale_claims,
    sweep_supersede_staging,
)
from .graph import (
    StemIndex,
    _edit1,
    break_cycles,
    collect_reachable,
    find_used_on_bugs,
    find_used_on_mismatches,
    is_chk_stem,
    match_pdfs,
    planned_paths,
    revision_rank,
    revision_token,
    snap_ref,
    split_superseded,
)
from .naming import (
    PlacementPlanTooLarge,
    add_bom_name,
    bom_name_winners,
    build_folder_names,
    folder_name_for,
    prefer_bom_names,
)


class RunCounters(TypedDict):
    """Workbook counters for a run: scanned/roots/copies/cycles/warnings."""
    scanned: int
    roots: int
    copies: int
    cycles: int
    warnings: int


# Repair staging dirs still on disk, keyed by output tree, so a run that raises
# before its cleanup removes only its own (see sweep_repair_staging).
_REPAIR_STAGING: dict = {}
# Prefix of every staging dir this tool creates in the temp dir; the stateless
# sweep (sweep_stale_repair_staging) recognises abandoned dirs by it.
_REPAIR_STAGING_PREFIX = "fermi_repair_"


def _register_staging(output, staging):
    """Track one staging dir under the tree the repair is working on."""
    _REPAIR_STAGING.setdefault(str(output), set()).add(Path(staging))


def _unregister_staging(staging):
    """Forget a staging dir once it is gone from disk."""
    for dirs in _REPAIR_STAGING.values():
        dirs.discard(Path(staging))


class RunContext(TypedDict):
    """Structured run context consumed by the Excel workbook."""
    counters: RunCounters
    missing: list[tuple[str, str]]
    chk: list[str]
    orphans: list[str]
    roots: list[tuple[str, list[str]]]
    used_on_mismatches: list[tuple[str, str, list[str]]]
    used_on_bugs: list[tuple[str, str]]
    titleblock_mismatches: list[tuple[str, str, str, str]]
    scanned: list[tuple[str, list[int]]]
    watermarks: list[tuple[str, str]]
    names: dict[str, str]
    placement_refused: bool
    skipped_roots: list[tuple[str, int]]


class NoPDFsFoundError(RuntimeError):
    """Raised when the input folder contains no indexable PDF drawings."""


# ---------------------------------------------------------------------------
# Log blocks shared by both run modes. The log text is user-facing only: the
# workbook consumes the structured context returned by run_full/run_incremental.
# Every message string stays byte-for-byte stable (reports get diffed).
# ---------------------------------------------------------------------------
def _log_superseded_preface(old, log):
    log(f"Found {len(old)} superseded revision(s) - will be placed in _superseded/")
    for s in sorted(old)[:10]:
        log(f"  {s}")
    if len(old) > 10:
        log(f"  ... and {len(old) - 10} more")


def _log_skipped_fc(skipped_fc, log):
    names = sorted(skipped_fc)
    log(f"Skipped {len(names)} FC-prefixed common component(s): {', '.join(names[:10])}"
        f"{'...' if len(names) > 10 else ''}")


def _log_missing_refs(header, refs, bom_names, log):
    """Log a block of BOM references with no matching PDF, with NAME per ref."""
    log(header)
    for stem, val in refs:
        log(f"  {stem} -> {val}")
        if val in bom_names:
            log(f"  NAME {val}: {bom_names[val]}")


def _collect_missing(boms, stems, bom_names, header, log):
    """Refs in `boms` that match no stem, logged as one block.

    The single implementation of the missing-reference check for full and
    incremental runs (callers pass their own header suffix and keep their own
    blank-line/FC/CHK bookkeeping). `boms` must already be OCR-snapped, or a
    misread that snaps to a real stem is reported missing too. Returns
    [(referencing stem, ref)] in sorted order; logs nothing when empty.
    """
    missing = []
    idx = StemIndex(stems)
    for stem, vals in sorted(boms.items()):
        for v in vals:
            if not is_processable_ref(v):
                continue
            if not match_pdfs(v, idx):
                missing.append((stem, v))
    if missing:
        _log_missing_refs(f"Skipped {len(missing)} {header}", missing, bom_names, log)
    return missing


def _log_chk_block(chk_stems, log):
    log(f"--- UNAPPROVED DRAWINGS (CHK) ({len(chk_stems)}) - "
        f"download approved DWG from Teamcenter: ---")
    for s in chk_stems:
        log(f"  {s}  ->  {s.split('_')[0]}")
    log("")


def _log_missing_names(index, names_of, log):
    no_name = [s for s in sorted(index) if s not in names_of]
    if no_name:
        log(f"  WARNING: {len(no_name)} PDF(s) without extractable NAME (folder will use stem):")
        for s in no_name[:10]:
            log(f"    {s}")
        if len(no_name) > 10:
            log(f"    ... and {len(no_name) - 10} more")
    log("")


def _log_used_on_mismatches(used_mism, names_of, log):
    log(f"--- USED ON mismatches ({len(used_mism)}) - parent BOM lists part but part USED ON omits parent ---")
    logged_mm_names = set()
    for p, c, actual in used_mism:
        actual_txt = ", ".join(actual) if actual else "none"
        log(f"  {p} -> {c}  [child USED ON: {actual_txt}]")
        for s in (p, c):
            if s in names_of and s not in logged_mm_names:
                logged_mm_names.add(s)
                log(f"  NAME {s}: {names_of[s]}")
    log("")


def _log_watermarks(watermarks, log):
    """Log the watermarked PDFs found during this run (stem: evidence)."""
    if not watermarks:
        return
    log(f"--- Watermarked PDFs ({len(watermarks)}) ---")
    for stem, reason in sorted(watermarks.items()):
        log(f"  {stem}: {reason}")
    log("")


def _log_titleblock_mismatches(mismatches, log):
    """Log filename-vs-title-block disagreements (number, revision, title)."""
    if not mismatches:
        return
    log(f"--- Title block check ({len(mismatches)} mismatch(es)) ---")
    for stem, field, fval, tval in mismatches:
        log(f"  {stem}: {field}: filename {fval!r}, title block {tval!r}")
    log("")


def _titleblock_mismatches(titleblocks, log):
    """Compare each file's title-block fields against its filename stem.

    titleblocks: {stem: (filename stem, drawing number, revision, NAME, scanned)}.
    Reports the drawing number (misnamed file), the revision (stale or absent
    in the filename - skipped when the number already mismatches), and the
    title (only when the filename carries a title
    and it shares no token with the title-block NAME). Returns
    [(stem, field, filename_value, titleblock_value)] sorted.

    A scanned (OCR) number that differs from the filename by a single character
    is treated as an OCR misread, not a misnamed file: the filename is the CAD
    export name and is authoritative, and one glyph off is not distinguishable
    from noise on these sheets.
    """
    out = []
    for stem, (fname, number, rev, name, scanned) in sorted(titleblocks.items()):
        parts = stem.split("_")
        srev = revision_token(stem)
        number_mismatch = bool(number) and number != parts[0]
        if number_mismatch and scanned and _edit1(number, parts[0]):
            number_mismatch = False
        if number_mismatch:
            out.append((stem, "number", parts[0], number))
        # A misnamed file (filename number != title-block number) is already
        # flagged; its filename revision is meaningless, so don't also report
        # a revision row for the same drawing.
        if rev is not None and not number_mismatch:
            crev = "" if rev == "-" else rev
            if crev != srev:
                out.append((stem, "revision", srev or "-", crev or "-"))
        tail = filename_title(fname)
        if tail and name:
            ft = {t for t in re.split(r"[^A-Z0-9]+", tail.upper()) if t}
            nt = {t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t}
            if ft and nt and not (ft & nt):
                out.append((stem, "name", tail, name))
    if out:
        _log_titleblock_mismatches(out, log)
    return out


def _snap_values(stem, vals, stems, log, bases=None):
    """Correct OCR near-miss refs to a unique known stem (in place per value).

    `stems` is a stem collection or a StemIndex; pass an index from a loop over
    stems so the set is not re-scanned per value.
    """
    out = []
    idx = stems if isinstance(stems, StemIndex) else StemIndex(stems)
    if bases is None:
        bases = idx.bases
    for v in vals:
        if is_processable_ref(v) and not match_pdfs(v, idx):
            s = snap_ref(v, idx, bases=bases)
            if s:
                log(f"  {stem}: OCR ref {v} corrected to {s}")
                v = s
        out.append(v)
    return out


def _snap_boms(bom_of, stems, log):
    """Snap OCR near-misses in BOM values before edge/missing computation."""
    idx = StemIndex(stems)
    for stem in sorted(bom_of):
        bom_of[stem] = _snap_values(stem, bom_of[stem], idx, log, bases=idx.bases)


def _snap_bom_votes(bom_votes, stems, log):
    """Re-key parts-list votes filed under an OCR near-miss reference.

    `_snap_boms` corrects the BOM value, but the description vote is filed
    under the raw reference, so a description read from a scanned parent would
    never reach the child that corrected reference names.
    """
    if not bom_votes:
        return
    idx = StemIndex(stems)
    for raw in sorted(bom_votes):
        if not is_processable_ref(raw) or match_pdfs(raw, idx):
            continue
        snapped = snap_ref(raw, idx, bases=idx.bases)
        if not snapped or snapped == raw:
            continue
        log(f"  OCR ref {raw} corrected to {snapped}")
        bom_votes.setdefault(snapped, Counter()).update(bom_votes.pop(raw))


def _snap_used_on(used_on_of, stems, log):
    """Snap OCR near-misses in USED ON values (keeps cross-checks aligned)."""
    idx = StemIndex(stems)
    for stem in sorted(used_on_of):
        used_on_of[stem] = _snap_values(stem, used_on_of[stem], idx, log,
                                        bases=idx.bases)


def _rekey_misnamed(index, bom_of, watermarks, titleblocks, stems, log):
    """Opt-in: re-key a misnamed file to its title-block drawing number.

    Only text-layer reads are trusted (OCR number misreads must never rename
    a file). Re-keys only when the title-block number is a valid F-number,
    disagrees with the filename base, and no drawing of that number already
    exists in `stems` (new + organized). The filename's tail is kept and the
    title-block revision is adopted (exact text-layer read). Returns the
    updated maps plus {old_stem: new_stem} for the re-keyed files.
    """
    rekeyed = {}
    stems_idx = StemIndex(stems)
    for stem in sorted(list(titleblocks)):
        fname, number, rev, _name, scanned = titleblocks[stem]
        base = stem.split("_")[0]
        if scanned or not number or number == base:
            continue
        if not TB_NUM_VAL_RE.match(number):
            continue
        if match_pdfs(number, stems_idx):
            log(f"  {stem}: title block says {number} but that drawing exists "
                f"- keeping filename (report only)")
            continue
        parts = stem.split("_")
        if rev and rev != "-" and len(rev) == 1:
            # Adopt the title-block revision (exact text-layer read).
            if len(parts) > 1:
                parts[1] = rev
            # Triple-underscore export names (F10038961___DWG1) keep an empty
            # slot after the revision; drop it so the stem matches the usual
            # {number}_{rev}_{tail} shape.
            if len(parts) > 2 and parts[2] == "":
                del parts[2]
        new_stem = "_".join([number] + parts[1:])
        if new_stem in index or new_stem in stems:
            log(f"  {stem}: title block says {number} but {new_stem} collides "
                f"- keeping filename (report only)")
            continue
        log(f"  {stem}: misnamed - re-keyed to {new_stem} (title block {number})")
        index[new_stem] = index.pop(stem)
        if stem in bom_of:
            bom_of[new_stem] = bom_of.pop(stem)
        if stem in watermarks:
            watermarks[new_stem] = watermarks.pop(stem)
        titleblocks[new_stem] = titleblocks.pop(stem)
        rekeyed[stem] = new_stem
    return index, bom_of, watermarks, titleblocks, rekeyed


def _resolve_duplicates(index, duplicates, jobs, log):
    """Pick the live-tree copy among same-revision duplicates.

    Same revision == same stem. Ranking: non-watermarked beats watermarked,
    then text-layer beats scanned (image-only), then shallowest path, then
    lexicographic. Runs BEFORE _scan_boms so BOM/NAME/title block are
    extracted from the chosen copy (the scanned loser must not source the
    tree's BOM - under --no-ocr it has none and its children would be
    wrongly orphaned). Returns (index, extras) where extras are watermarked
    candidates for _superseded/ - but only when a clean copy exists; with
    all-watermarked copies the best-ranked one is used as-is and nothing is
    archived. Losing clean copies are left ignored in the input folder.
    """
    if not duplicates:
        return index, []
    # Meta for every candidate, winner included: this runs before _scan_boms,
    # so the indexed copy's watermark/scanned status is not known yet.
    cand_paths = sorted({p for stem, paths in duplicates.items() if stem in index
                         for p in (index[stem], *paths)})
    results = run_parallel(dup_meta_task, [str(p) for p in cand_paths], jobs, False)
    meta = {}
    for p, res in zip(cand_paths, results, strict=True):
        # Unreadable candidate: clean but unverified (assume scanned) - it may
        # lose to a verified text-layer copy, never to a watermarked one.
        meta[p] = (res[1], bool(res[2])) if res[0] == "ok" else (None, True)
    extras = []
    for stem, losers in sorted(duplicates.items()):
        if stem not in index:
            continue
        winner = index[stem]
        cands = [winner, *losers]
        clean = [p for p in cands if meta[p][0] is None]
        chosen = min(cands, key=lambda p: (meta[p][0] is not None, meta[p][1],
                                           len(p.parts), str(p)))
        if chosen != winner:
            if meta[winner][0] is not None and clean:
                log(f"  {stem}: watermarked {winner.name} replaced by "
                    f"non-watermarked {chosen.name}")
            else:
                log(f"  {stem}: scanned {winner.name} replaced by "
                    f"text-layer {chosen.name}")
            index[stem] = chosen
        if clean:
            extras.extend(p for p in cands if meta[p][0] is not None)
    if extras:
        log(f"  {len(extras)} watermarked duplicate(s) -> _superseded/")
        log("")
    return index, extras


def _log_used_on_bugs(bugs, log):
    """Report children whose USED ON names a parent the parent's BOM omits."""
    log(f"--- USED ON bugs ({len(bugs)}) - child USED ON lists a parent whose BOM does not list it ---")
    for p, c in bugs:
        log(f"  {c}: USED ON {p} but {p} BOM does not list it")
    log("")


def _log_cycle_detection(header, children, parents, log):
    log(header)
    removed = break_cycles(children, parents, log)
    if not removed:
        log("  No circular references found.")
    log("")
    return removed


def _log_roots_and_orphans(roots, orphans, used_on_of, names_of, log):
    log(f"--- Root assemblies ({len(roots)}) ---")
    for r in roots:
        used = sorted(set(used_on_of.get(r, [])))
        used_txt = f"  USED ON {', '.join(used)}" if used else ""
        name_txt = f" [{names_of.get(r, '')}]" if names_of.get(r) else ""
        log(f"  {r}{name_txt}{used_txt}")
    log("")
    if orphans:
        log(f"--- Orphans ({len(orphans)}) - no BOM, no parent in folder ---")
        for o in orphans:
            log(f"  {o} [{names_of.get(o, '')}]".replace(" []", ""))
        log("")


def _log_unplaced(roots, children, index, orphans, log):
    reachable = collect_reachable(children, roots)
    unplaced = set(index) - reachable - set(orphans)
    if unplaced:
        log(f"--- Unplaced PDFs ({len(unplaced)}) ---")
        for s in sorted(unplaced):
            log(f"  {s}")
        log("")


def _log_relationship_notes(new_stems, new_parents, org_parents_of, org_children_of, log):
    for nstem in sorted(new_stems):
        if not new_parents.get(nstem):
            continue
        extra = []
        if org_parents_of.get(nstem):
            extra.append(f"referenced by organized part(s) {sorted(org_parents_of[nstem])}")
        if org_children_of.get(nstem):
            extra.append(f"references organized part(s) {sorted(org_children_of[nstem])}")
        if extra:
            log(f"  NOTE: {nstem} {' and '.join(extra)}; placed under new assembly {sorted(new_parents[nstem])}")
    log("")


def _log_previous_report(output, log):
    latest = find_latest_report(output)
    if not latest:
        return
    log(f"Previous report:                {latest.name}")
    try:
        for line in latest.read_text(encoding="utf-8", errors="replace").splitlines():
            if "active PDF(s)" in line:
                log(f"  (last run: {line.strip()})")
                break
    except OSError as e:
        log(f"WARNING: could not read previous report {latest.name}: {e}")


def _log_summary(rows, log):
    for row in rows:
        log(row)


# ---------------------------------------------------------------------------
# Scanning / graph phases
# ---------------------------------------------------------------------------
def _ocr_named_stems():
    """Stems whose NAME was actually read by OCR this run.

    A text-layer drawing keeps its own NAME: that one is authoritative. Only a
    name OCR produced is a guess, and only then may a parent's parts list
    replace it. Reading the OCR events (rather than the page's text length)
    keeps --no-ocr runs and text-layer sheets untouched.
    """
    named = set()
    for path, ev in OCR.events.items():
        if path and ev.get("name"):
            stem = canonical_stem(Path(path).stem)
            if stem:
                named.add(stem)
    return named


def _scan_boms(items, jobs, log, require_desc):
    """BOM extraction over sorted (stem, pdf) items.

    Logs per-PDF errors/warnings and the unique-FERMI count line. Returns
    ({stem: [values]}, {ref_value: NAME}, {stem: watermark evidence},
    {stem: (filename, title block number, revision, NAME, scanned)},
    extraction warning count); require_desc keeps the run_full policy of
    storing a NAME only when the BOM row carries a description.

    `ref_value: NAME` comes from the parts-list descriptions: parents can
    disagree about a child (a stale parts list keeps an old revision's
    wording), so the majority description wins (see naming.bom_name_winners).
    """
    bom_of = {}
    bom_votes = {}
    watermarks = {}
    titleblocks = {}
    warnings = 0
    used_on_of = {}
    used_names = {}
    # survey_task opens each PDF once; BOM, USED ON, name, etc. all come from
    # that single pass. The USED ON figures are stashed for the (non-reopening)
    # second section below.
    results = run_parallel(survey_task, [str(p) for _, p in items],
                           jobs, OCR.enabled)
    for (stem, pdf), res in zip(items, results, strict=True):
        if res[0] == "error":
            log(f"  {stem}: ERROR reading PDF: {res[1]}")
            continue
        _, entries, method, used, issues, watermark, number, rev, name, scanned = res
        for msg in issues:
            log(f"  {stem}: EXTRACTION WARNING: {msg}")
        warnings += len(issues)
        if watermark:
            watermarks[stem] = watermark
        titleblocks[stem] = (pdf.stem, number, rev, name, scanned)
        raw_values = sorted({v for v, *_rest in entries})
        bom_of[stem] = raw_values
        for e in entries:
            if len(e) == 4 and (e[3] or not require_desc):
                add_bom_name(bom_votes, e[0], e[3])
        used_on_of[stem] = used
        if name:
            used_names[stem] = name
        log(f"  {stem}: {len(raw_values)} unique FERMI# ({method})")
    log("")
    return bom_of, bom_name_winners(bom_votes), watermarks, titleblocks, warnings, \
        used_on_of, used_names, bom_votes


def _scan_used_on(items, jobs, header, log, used_on_cache=None, names_cache=None):
    """Report the USED ON + NAME figures already collected by survey_task.

    Logs the header, one line per PDF, then a blank line. Returns
    (used_on_of, names_of).

    `used_on_cache` is optional because callers may legitimately have no survey
    results yet (`_full_scan_used_on` defaults to None); None means "nothing
    cached", not "run a second pool". This used to carry a whole second
    extraction pool for that case; every caller now passes the survey results,
    so the branch was dead code (16 statements) that no test and no run could
    reach - and a second pool would re-open every PDF the single-open survey
    had just read.
    """
    used_on_cache = used_on_cache or {}
    used_on_of = {}
    names_of = {}
    log(header)
    for (stem, _pdf) in items:
        if stem not in used_on_cache:
            continue
        used = used_on_cache.get(stem) or []
        used_on_of[stem] = used
        if names_cache and names_cache.get(stem):
            names_of[stem] = names_cache[stem]
        if used:
            log(f"  {stem}: USED ON {', '.join(sorted(set(used)))}")
    log("")
    return used_on_of, names_of


def _collect_used_on_bugs_full(used_on_of, index, children, log):
    """Report children whose USED ON names a parent the parent's BOM omits.

    BOM is the only source of truth for parent-child edges, so these are
    USED ON bugs, not placement edges. Returns [(parent, child)] sorted.
    """
    bugs = find_used_on_bugs(children, used_on_of, index)
    if bugs:
        _log_used_on_bugs(bugs, log)
    return bugs


def _graph_edges(boms, stems):
    """(children, parents) edge maps implied by BOM values matched against stems."""
    children = defaultdict(set)
    parents = defaultdict(set)
    idx = StemIndex(stems)
    for stem, vals in sorted(boms.items()):
        for v in vals:
            if not is_processable_ref(v):
                continue
            for m in match_pdfs(v, idx):
                if m == stem:
                    continue
                children[stem].add(m)
                parents[m].add(stem)
    return children, parents


def _classify_roots(index, parents, bom_of, children):
    """Split active stems into root assemblies and parentless leaf orphans."""
    roots = []
    orphans = []
    for s in sorted(index):
        if parents.get(s):
            continue
        if bom_of.get(s) or children.get(s):
            roots.append(s)
        else:
            orphans.append(s)
    return roots, orphans


def _in_place_tree_copy(p, stem, output):
    """(In-place runs) True when a scanned PDF looks like an organized copy:
    it sits inside the exact stem/base folder or a '{base} NAME' folder, the
    shapes place_files writes. A PDF at the output root or in an unrelated
    input subfolder is an input original, not a placed copy. A user folder
    deliberately named '{base} something' is indistinguishable from the
    convention and still counts as organized."""
    base = stem.split("_")[0]
    if p.parent == output:
        return False
    name = p.parent.name.upper()
    return name == stem or name == base or name.startswith(base + " ")


def _collect_incremental_candidates(scan_index, output, scan_res=None,
                                    in_place=False, allow_archived=False):
    """Organized stems, superseded/orphan state, and the incremental candidate
    set. Stored orphans are pulled in as adoption candidates (a parent arriving
    in this batch may adopt them). Returns
    (organized, sup_dir, stored_orphans, new_index).
    Accepts an optional `scan_res` (precomputed `scan_output_tree` result)
    to avoid redundant filesystem scans. `in_place` (output IS the input
    folder) keeps only tree-shaped copies as organized, so new arrivals at
    the root or in input subfolders are not silently skipped. `allow_archived`
    (repair mode) lets a caller's own candidate index name stems that already
    sit in _superseded/ - normally archiving is a one-way door; the repair
    caller decides which archived copies deserve a fresh decision.
    """
    if scan_res is None:
        scan_res = scan_output_tree(output)
    organized = find_organized_pdfs(output, scan_res=scan_res)
    if in_place:
        organized = {s: kept for s, paths in organized.items()
                     if (kept := [p for p in paths
                                  if _in_place_tree_copy(p, s, output)])}
    # Stems already archived in _superseded/ are never candidates again.
    sup_dir = output / "_superseded"
    already_sup = {s for p in scan_res["sup"]
                   if (s := canonical_stem(p.stem))}
    # Stored orphans from previous full runs: they are adoption candidates.
    # If a new/organized parent references one, it is placed into the tree and
    # its _orphans/ copy is retired; if not, it stays parked.
    stored_orphans = {s: p for p in scan_res["orph"]
                      if (s := canonical_stem(p.stem))}
    new_index = {s: p for s, p in scan_index.items()
                 if s not in organized
                 and (allow_archived or s not in already_sup)}
    for o, p in stored_orphans.items():
        if o in organized:
            # A same-stem copy already lives in the tree: the parked one is a
            # (possibly watermarked) duplicate handled by orphan retirement,
            # not re-placed as a new candidate.
            continue
        if o in new_index:
            # same stem arrived both at top level and in _orphans - top level wins
            continue
        new_index[o] = p
    return organized, sup_dir, stored_orphans, new_index


def _detect_supersede_pairs(new_index, old_new, organized, log):
    """Compare new PDFs against organized stems for revision conflicts.
    Pops older revisions into old_new; returns the (new, old) supersede pairs."""
    org_latest = {}
    for s in organized:
        b = s.split("_")[0]
        if b not in org_latest or revision_rank(s) > revision_rank(org_latest[b]):
            org_latest[b] = s
    supersede_pairs = []
    for s in list(new_index):
        b = s.split("_")[0]
        if b not in org_latest:
            continue
        if revision_rank(s) > revision_rank(org_latest[b]):
            supersede_pairs.append((s, org_latest[b]))
            log(f"  {s}: supersedes organized {org_latest[b]} - will replace it in the tree")
        elif revision_rank(s) < revision_rank(org_latest[b]):
            why = "UNAPPROVED CHK" if is_chk_stem(s) else "older revision"
            log(f"  {s}: older than organized {org_latest[b]} -> _superseded/ ({why})")
            old_new[s] = new_index.pop(s)
        else:
            log(f"  WARNING: {s} ties with organized {org_latest[b]} - manual review needed")
    return supersede_pairs


def _scan_organized(organized, all_stems, jobs, bom_names, log, bom_votes):
    """BOM + USED ON scan over organized PDFs (placement context). Logs the
    missing-reference block; returns (org_boms, org_used_on, org_unmatched,
    org_watermarks, org_titleblocks, org_names, extraction warning count).

    `bom_votes` accumulates this scan's parts-list descriptions so the caller
    can re-decide a NAME once every parent has been read; `bom_names` keeps
    the first-seen description (used for the missing-reference log).
    """
    org_boms = {}
    org_used_on = {}
    org_watermarks = {}
    org_titleblocks = {}
    org_names = {}
    warnings = 0
    log("--- Scanning BOM + USED ON (organized PDFs, for placement) ---")
    org_items = [(stem, pick_shallowest(organized[stem]))
                 for stem in sorted(organized)]
    org_results = run_parallel(org_task, [str(p) for _, p in org_items],
                               jobs, OCR.enabled)
    for (stem, pdf), res in zip(org_items, org_results, strict=True):
        if res[0] == "error":
            log(f"  {stem}: ERROR reading PDF: {res[1]}")
            continue
        _, entries, method, used, issues, watermark, number, rev, name, scanned = res
        for msg in issues:
            log(f"  {stem}: EXTRACTION WARNING: {msg}")
        warnings += len(issues)
        if watermark:
            org_watermarks[stem] = watermark
        org_titleblocks[stem] = (pdf.stem, number, rev, name, scanned)
        if name:
            org_names[stem] = name
        org_boms[stem] = sorted({v for v, *_rest in entries})
        org_used_on[stem] = used
        for e in entries:
            if len(e) == 4:
                add_bom_name(bom_votes, e[0], e[3])
                if e[3] and e[0] not in bom_names:
                    bom_names[e[0]] = e[3]
        log(f"  {stem}: {len(entries)} BOM row(s), {len(org_boms[stem])} unique FERMI# ({method})")
    # OCR near-miss refs: correct against the known stems before the missing
    # check, or a misread that snaps to a real stem (F0156241 -> F10156241) is
    # also listed as missing. Full mode snaps before its missing check.
    _snap_boms(org_boms, all_stems, log)
    _snap_used_on(org_used_on, all_stems, log)
    # Missing refs are reported across ALL drawings (new + organized), so the
    # Teamcenter download list in the report/workbook is complete every run.
    org_unmatched = _collect_missing(
        org_boms, all_stems, bom_names,
        "missing reference(s) from organized drawing(s):", log)
    if org_unmatched:
        log("")
    log("")
    return (org_boms, org_used_on, org_unmatched, org_watermarks,
            org_titleblocks, org_names, warnings)


def _swap_revision_files(old_paths, new_pdf, sup_dir, output, dry_run, log,
                         lock=None):
    """Archive old revision copies and place the new revision in their spot.
    Returns (new_locations, copies_written). `lock` is this run's lock path
    (None = unlocked): re-checked before each swap, so a run stolen
    mid-supersede stops with LockLostError instead of replacing into another
    run's tree."""
    moved = []
    copies = 0
    if _has_linked_ancestor(sup_dir, output):
        # A junction at _superseded/ can make an outside file look like the
        # existing archive; resolve_archive_target would then report
        # already=True, the guarded copy would be skipped, and the tree's old
        # revision would be unlinked with its only copy outside the managed
        # tree. Refuse the archive before touching anything.
        log(f"  WARNING: _superseded/ traverses a symlinked/junction ancestor, "
            f"refusing to supersede: {sup_dir}")
        return moved, copies
    for p in old_paths:
        _ensure_lock(lock, log)
        target_old = sup_dir / p.name
        target_new = p.parent / new_pdf.name
        staging = target_new.with_name(f"{target_new.name}.supersede_tmp.{os.getpid()}")
        rel_old = p.relative_to(output)
        # Refuse symlinks like every copy helper does: _exists() follows
        # links, so a dangling link at target_old would read as "no archive
        # yet" and the archive copy would write through it (outside
        # _superseded) before the old tree copy is unlinked.
        if _islink(p) or _islink(target_old):
            log(f"  WARNING: {rel_old}: supersede skipped (symlink refused): "
                f"{p} -> {target_old}")
            continue
        # Archive collision: the target may already exist with different
        # content (never delete the tree copy without archiving it). Equal
        # bytes = assume duplicate, keep the old skip behavior; OSError on
        # compare means differ (safer). Differing content archives under a
        # numeric suffix (<stem>.1.pdf, incrementing until free).
        archive_target, already = resolve_archive_target(sup_dir, p)
        archived = not already
        if dry_run:
            note = "" if archived else " (archive skipped: exists)"
            log(f"  [DRY-RUN] supersede: {rel_old} -> _superseded/{archive_target.name}; "
                f"copy {new_pdf.name} -> {target_new.relative_to(output)}{note}")
        else:
            # Atomic-ish swap: stage the new revision next to its target first,
            # then archive the old revision, then replace, then drop the old file.
            created = False
            try:
                os.makedirs(_native(sup_dir), exist_ok=True)
                _staged_copy(new_pdf, staging, output)
                created = True
                if archived:
                    # Atomic archive copy: a raw copy2 leaves a truncated PDF
                    # under a real _superseded/ name if the write dies halfway,
                    # and the next workbook lists that stub as a valid archived
                    # revision (_copy_atomic stages + os.replace, or removes its
                    # own staging file on failure).
                    _copy_atomic(p, archive_target, output)
                os.replace(_native(staging), _native(target_new))
            except OSError as e:
                # Only clean up a staging file this swap created: when the
                # guard refuses the copy before _staged_copy writes anything,
                # unlinking the staging name would follow the junction and
                # delete a same-named file outside the tree.
                if created and _exists(staging):
                    try:
                        os.unlink(_native(staging))
                    except OSError:
                        # Best-effort staging cleanup: the supersede failure
                        # below is logged and is the actionable error.
                        pass
                log(f"  WARNING: supersede failed for {rel_old}: {e}")
                continue
            # New revision is in place; old removal is best-effort.
            try:
                os.unlink(_native(p))
            except OSError as e:
                log(f"  WARNING: {rel_old}: old revision left in tree "
                    f"(could not remove): {e}")
            log(f"  superseded: {rel_old} -> _superseded/{archive_target.name}")
        copies += 2 if archived else 1
        moved.append(target_new)
    return moved, copies


def _shared_folder_blocks_move(c, child_folder, output, log):
    """True when `child_folder` holds another drawing's PDF or is unlistable.

    Moving a shared folder would relocate files unrelated to the move, so the
    move is refused with a manual-review warning (fail safe)."""
    foreign = folder_foreign_pdfs(child_folder, c)
    if foreign is None:
        log(f"  WARNING: {c}: could not list folder, skipping move "
            f"(manual review): {child_folder.relative_to(output)}")
        return True
    if foreign:
        names = ", ".join(p.name for p in foreign)
        log(f"  WARNING: {c}: folder shared with {names} - skipping move "
            f"(manual review): {child_folder.relative_to(output)}")
        return True
    return False


def _move_children_under_superseding(swapped, organized, org_boms, org_stems,
                                   output, dry_run, moved_dirs, log):
    """Move organized parts referenced by a superseding revision under its
    folder. Mutates moved_dirs; returns (moves, copies).

    A child that also lives under another parent is shared: it is COPIED
    (the other parent keeps its copy, matching place_files), and the copy
    is not recorded in moved_dirs - the original stays the reference for
    later claimants. Only a child whose sole copy is this folder moves."""
    moves = 0
    copies = 0
    org_idx = StemIndex(org_stems)
    for s2 in swapped:
        s_paths = organized.get(s2, [])
        if not s_paths:
            continue
        s_folder = pick_shallowest(s_paths).parent
        for v in org_boms.get(s2, []):
            if not is_processable_ref(v):
                continue
            for c in match_pdfs(v, org_idx):
                if c == s2:
                    continue
                child_paths = organized.get(c, [])
                if not child_paths:
                    continue
                child_folder = pick_shallowest(child_paths).parent
                if child_folder == s_folder or child_folder.is_relative_to(s_folder):
                    continue
                if _shared_folder_blocks_move(c, child_folder, output, log):
                    continue
                target_path = s_folder / child_folder.name
                if _exists(target_path):
                    log(f"  WARNING: {c}: destination exists, skipping move (manual review): "
                        f"{target_path.relative_to(output)}")
                    continue
                if any(p.parent != child_folder for p in child_paths):
                    # Shared child: copy so the other parent keeps its copy.
                    plan_copies = sum(1 for _ in child_folder.rglob("*.pdf"))
                    if dry_run:
                        log(f"  [DRY-RUN] copy {child_folder.relative_to(output)} -> "
                            f"{target_path.relative_to(output)}")
                    elif refuse_linked_target(target_path, output, log, c):
                        continue
                    else:
                        try:
                            shutil.copytree(str(child_folder), str(target_path),
                                            symlinks=True)
                        except OSError as e:
                            log(f"  WARNING: copy failed for {c}: {e}")
                            shutil.rmtree(_native_tree(target_path), ignore_errors=True)
                            continue
                        log(f"  copied: {child_folder.relative_to(output)} -> "
                            f"{target_path.relative_to(output)}")
                    copies += plan_copies
                    continue
                if dry_run:
                    log(f"  [DRY-RUN] move {child_folder.relative_to(output)} -> {target_path.relative_to(output)}")
                elif refuse_linked_target(target_path, output, log, c):
                    continue
                else:
                    try:
                        shutil.move(str(child_folder), str(target_path))
                    except OSError as e:
                        log(f"  WARNING: move failed for {c}: {e}")
                        continue
                    log(f"  moved: {child_folder.relative_to(output)} -> {target_path.relative_to(output)}")
                moved_dirs[child_folder] = target_path
                moves += 1
    return moves, copies


def _resolve_moved_folder(folder, moved_dirs):
    """Follow folder relocations in the order they happened.

    One ordered pass is deliberate. A repeated pass would follow a mapping
    whose key was an old path later reused as another folder's destination,
    resolving that newcomer into the wrong tree. Moves are recorded
    chronologically, so one pass follows each folder's own chain.
    """
    if isinstance(moved_dirs, FolderMoves):
        return moved_dirs.resolve(folder)
    for old, new in moved_dirs.items():
        if folder == old or folder.is_relative_to(old):
            folder = new.joinpath(*folder.relative_to(old).parts)
    return folder


def _current_folder_of(stem, organized, output, moved_dirs):
    """Shallowest live folder for a stem, remapped through supersede moves."""
    paths = [p for p in organized.get(stem, [])
             if not p.relative_to(output).parts[0].startswith("_")]
    if not paths:
        return output
    pdf = pick_shallowest(paths)
    rel = pdf.relative_to(output)
    old_folder = output.joinpath(*rel.parts[:-1])
    return _resolve_moved_folder(old_folder, moved_dirs)


def _longest_dest_path(target_path, current):
    """Longest file path the copy of `current` under `target_path` would create."""
    return longest_dest_path(target_path, current)


def _copy_org_child_tree(c, target_folder, organized, moved_dirs, output,
                         dry_run, log, lock=None, folder_names=None):
    """Copy organized child c's folder under target_folder; returns PDF copies.

    Nested under a live parent means COPY, never move: moving would hollow
    out the existing parent, and later claimants would find nothing (every
    claimant gets its own copy, matching place_files). A failed copy is
    guarded, with partial-tree cleanup so later runs don't trip over it.

    The copy is named from this run's folder-name plan, not from the name it
    carries on disk: an on-disk name is whatever the run that created it first
    chose, so reusing it would make the tree depend on the order the batches
    arrived in, and could leave the copy past the path limit and silently
    skipped.
    """
    child_paths = organized.get(c, [])
    if not child_paths:
        return 0
    child_path = pick_shallowest(child_paths)
    child_folder = child_path.parent
    # Follow earlier relocations: the folder may have moved along with an
    # adopted ancestor claimed earlier this run. Chain parent+child renames.
    current = _resolve_moved_folder(child_folder, moved_dirs)
    # A dry run never moves anything, so a folder it "moved" is simulated
    # at its destination: accept that as present for later claimants.
    # A child below a moved ancestor counts too (exact equality would
    # skip a copy in dry-run that the real run makes).
    simulated = dry_run and (current in moved_dirs.values() or any(
        current.is_relative_to(v) for v in moved_dirs.values()))
    if not simulated and not _exists(current):
        log(f"  {c}: already moved, skipping")
        return 0
    # Guard against destination collisions before adopting the folder.
    target_path = target_folder / ((folder_names or {}).get(c) or child_folder.name)
    if _exists(target_path):
        same = False
        try:
            same = target_path.resolve() == current.resolve()
        except OSError:
            same = False
        if same:
            log(f"  {c}: already at destination, skipping move")
            return 0
        log(f"  WARNING: {c}: destination exists, skipping move (manual review): "
            f"{target_path.relative_to(output)}")
        return 0
    # Copying cannot strand unrelated files the way a move of a shared folder
    # would, so the shared-folder guard is move-only and never blocks a copy:
    # a source folder holding foreign PDFs still lands whole under the target.
    longest = _longest_dest_path(target_path, current)
    if longest is not None and len(str(longest)) > MAX_PATH:
        log(f"  WARNING: path too long ({len(str(longest))} chars), skipping: "
            f"{longest}")
        return 0
    # Nested under a live parent: copy the whole subtree so the existing
    # parent keeps its copy. A failed copy is guarded below.
    if dry_run:
        log(f"  [DRY-RUN] copy {current.relative_to(output)} -> {target_path.relative_to(output)}")
    else:
        # Recheck ownership before the long copy: a steal racing place_files
        # must stop this run the way it stops place_files (LockLostError).
        _ensure_lock(lock, log)
        if refuse_linked_target(target_path, output, log, c):
            return 0
        # symlinks=True copies links as links; the default would copy the
        # link targets' contents, which may live outside the tree.
        copied = False
        try:
            shutil.copytree(str(current), str(target_path), symlinks=True)
            copied = True
        except OSError as e:
            log(f"  WARNING: copy failed for {c}: {e}")
            return 0
        finally:
            # The target did not exist before this call, so any failure past
            # OSError (KeyboardInterrupt, ...) must not leave a partial tree
            # that later runs mistake for a placed child.
            if not copied:
                shutil.rmtree(_native_tree(target_path), ignore_errors=True)
    return sum(1 for paths in organized.values() for p in paths
               if p.is_relative_to(current) or p.is_relative_to(child_folder))


def normalize_folder_names(organized, folder_names, output, dry_run, log, moved_dirs,
                           lock=None):
    """Rename live folders whose name differs from this run's plan.

    The plan is computed from the merged tree, so it is the name a full rebuild
    of the same drawings would give every folder. A folder created by an earlier
    run over a smaller tree can still carry the name that fitted then - usually
    the longer one - and that is the only reason two trees built from the same
    drawings in different batch orders were not identical. Renaming to the plan
    makes the tree a function of its contents instead of of its history.

    Guarded like every other tree mutation: a colliding destination or an
    over-long target is logged and skipped, never fatal. Returns the number of
    folders renamed."""
    renamed = 0
    candidates = []
    for stem, paths in organized.items():
        wanted = folder_names.get(stem)
        if wanted is None:
            continue
        for path in paths:
            candidates.append((stem, path, wanted))
    # Parents first: a child renamed before its ancestor leaves `moved_dirs`
    # pointing inside the ancestor's old path (resolved by the chain helper).
    candidates.sort(key=lambda item: (len(item[1].parts), item[0], str(item[1])))
    for stem, path, wanted in candidates:
        folder = _resolve_moved_folder(path.parent, moved_dirs)
        if folder.name == wanted:
            continue
        target = folder.parent / wanted
        # Check the deepest file, not just the folder: renaming a folder to its
        # full NAME can push a descendant past MAX_PATH ("WELDM" ->
        # "WELDMENT, FIELD" put a drawing at 260 chars), which then reads as
        # unreadable and makes every later repair refuse to touch it.
        longest = longest_dest_path(target, folder)
        too_long = len(str(target)) > MAX_PATH or (
            longest is not None and len(str(longest)) > MAX_PATH)
        if too_long:
            shown = longest if longest is not None else target
            log(f"  WARNING: {stem}: folder name not normalized "
                f"(path too long, {len(str(shown))} chars): {target.relative_to(output)}")
            continue
        if _exists(target):
            log(f"  WARNING: {stem}: folder name not normalized "
                f"(destination exists, manual review): {target.relative_to(output)}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] rename {folder.name} -> {wanted}")
        else:
            _ensure_lock(lock, log)
            try:
                folder.rename(target)
            except OSError as e:
                log(f"  WARNING: {stem}: rename failed ({target}): {e}")
                continue
        moved_dirs[folder] = target
        renamed += 1
    if renamed:
        log(f"Normalized {renamed} folder name(s) to this run's plan")
    return renamed


def _adopt_one_org_child(c, target_folder, organized, moved_dirs, output,
                         dry_run, log, lock=None, folder_names=None):
    """Adopt one organized child under target_folder; returns (copies, moves).

    Top-level folders are re-homed (moved, so no stale root is left behind;
    the shared-folder guard applies since a move relocates foreign PDFs).
    Nested folders are copied via _copy_org_child_tree (shared sources land
    whole). Later claimants resolve the new home through moved_dirs. The
    destination folder is named from this run's plan (see _copy_org_child_tree).
    """
    child_paths = organized.get(c, [])
    if not child_paths:
        return 0, 0
    child_path = pick_shallowest(child_paths)
    child_folder = child_path.parent
    # Follow earlier relocations: the folder may have moved along with an
    # adopted ancestor claimed earlier this run. Chain parent+child renames.
    current = _resolve_moved_folder(child_folder, moved_dirs)
    # A dry run never moves anything, so a folder it "moved" is simulated
    # at its destination: accept that as present for later claimants.
    # A child below a moved ancestor counts too (exact equality would
    # skip a copy in dry-run that the real run makes).
    simulated = dry_run and (current in moved_dirs.values() or any(
        current.is_relative_to(v) for v in moved_dirs.values()))
    if not simulated and not _exists(current):
        log(f"  {c}: already moved, skipping")
        return 0, 0
    # Guard against destination collisions before adopting the folder.
    target_path = target_folder / ((folder_names or {}).get(c) or child_folder.name)
    if _exists(target_path):
        same = False
        try:
            same = target_path.resolve() == current.resolve()
        except OSError:
            same = False
        if same:
            log(f"  {c}: already at destination, skipping move")
            return 0, 0
        log(f"  WARNING: {c}: destination exists, skipping move (manual review): "
            f"{target_path.relative_to(output)}")
        return 0, 0
    if current.parent == output:
        # Top-level organized root: move (re-home) so no stale root
        # folder is left behind. Later claimants copy from the new home
        # via the relocation tracking above.
        if _shared_folder_blocks_move(c, current, output, log):
            return 0, 0
        longest = _longest_dest_path(target_path, current)
        if longest is not None and len(str(longest)) > MAX_PATH:
            log(f"  WARNING: path too long ({len(str(longest))} chars), skipping: "
                f"{longest}")
            return 0, 0
        if dry_run:
            log(f"  [DRY-RUN] move {current.relative_to(output)} -> {target_path.relative_to(output)}")
            moved_dirs[current] = target_path
        else:
            _ensure_lock(lock, log)
            if refuse_linked_target(target_path, output, log, c):
                return 0, 0
            try:
                shutil.move(str(current), str(target_path))
            except OSError as e:
                log(f"  WARNING: move failed for {c}: {e}")
                return 0, 0
            moved_dirs[current] = target_path
        return 0, 1
    copies = _copy_org_child_tree(c, target_folder, organized,
                                  moved_dirs, output, dry_run, log,
                                  lock=lock, folder_names=folder_names)
    return copies, 0


def _adopt_org_children_below(starts, new_children, org_children_of,
                              folder_names, names_of, organized, moved_dirs,
                              output, dry_run, log, skip=(), lock=None):
    """Adopt already-organized children under new non-root parents;
    returns (copies, moves).

    place_files only walks new children, so a new parent placed under other
    new parents keeps just its new subtree: its organized children never land
    in its folder (new roots with organized children are covered by
    _place_above_organized_children; this covers everything below them).
    `starts` maps each placed subtree root to its own folder; child folders
    derive exactly like place_files (folder_names, else folder_name_for).
    Nested under a live parent means COPY, never move.
    """
    skip = set(skip)

    def fname(stem):
        return folder_names.get(stem) or folder_name_for(stem, names_of)

    total = 0
    total_moves = 0
    for stem, own_folder in starts.items():
        stack = [(stem, own_folder, 0)]
        seen = set()
        while stack:
            s, folder, depth = stack.pop()
            if (s, str(folder)) in seen:
                continue
            seen.add((s, str(folder)))
            if depth > TREE_MAX_DEPTH:
                log(f"  WARNING: organized-child adoption truncated at depth {depth} "
                    f"in {folder.name} - manual review")
                continue
            if len(str(folder)) > MAX_DIR:
                log(f"  WARNING: path too long ({len(str(folder))} chars), skipping "
                    f"organized-child adoption: {folder}")
                continue
            if not dry_run and not _exists(folder):
                continue
            if s not in skip:
                for c in sorted(org_children_of.get(s, ())):
                    _c, _m = _adopt_one_org_child(c, folder, organized,
                                                  moved_dirs, output,
                                                  dry_run, log, lock=lock,
                                                  folder_names=folder_names)
                    total += _c
                    total_moves += _m
            for ch in sorted(new_children.get(s, ())):
                stack.append((ch, folder / fname(ch), depth + 1))
    return total, total_moves


def _adopted_copy_estimate(roots, children, org_children_of, organized,
                           output, weights=None, moved_dirs=None):
    """Per-root estimates of adopted organized-subtree copies (fan-out cap).

    The adoption passes copy organized children claimed anywhere in a root's
    new subtree, outside the new-children walk the cap counts - so a small
    accepted graph could copy large organized subtrees past
    MAX_PLANNED_COPIES. Counts files on disk per claimed folder (strays ride
    along in copytree), with the indexed tally as fallback for unlistable
    folders. Instances are counted per placed path, not per stem: diamonds
    multiply, like _copy_counter (layered BFS, depth-capped like the pass).
    A top-level folder is moved by its first claimant and copied by the
    rest, so it contributes Copies-1 there: first-sorted-root order matches
    placement, and repeated placements of one root (weights) multiply its
    instances first. Folders relocated by supersede swaps/child moves are
    resolved through moved_dirs (the pass copies the relocated tree); a
    vanished folder falls back to the indexed tally so the cap cannot
    undercount. Estimate only: folder resolution and MAX_DIR skips are
    resolved at copy time.
    """
    weights = weights or {}
    moved_dirs = moved_dirs or {}
    pdf_memo = {}

    def folder_pdfs(folder):
        key = str(folder)
        if key not in pdf_memo:
            try:
                pdf_memo[key] = sum(1 for e in folder.rglob("*")
                                    if e.is_file())
            except OSError:
                pdf_memo[key] = None
        n = pdf_memo[key]
        if n is None or (n == 0 and not folder.is_dir()):
            n = sum(1 for ps in organized.values() for p in ps
                    if p.is_relative_to(folder))
        return n

    def path_counts(R):
        counts = {}
        level = {R: 1}
        depth = 0
        while level and depth <= TREE_MAX_DEPTH:
            nxt = {}
            for stem, n in level.items():
                counts[stem] = counts.get(stem, 0) + n
                if depth < TREE_MAX_DEPTH:
                    for ch in children.get(stem, ()):
                        nxt[ch] = nxt.get(ch, 0) + n
            level, depth = nxt, depth + 1
        return counts

    def child_folder(c):
        paths = organized.get(c, [])
        folder = pick_shallowest(paths).parent if paths else None
        if folder is not None:
            folder = _resolve_moved_folder(folder, moved_dirs)
        return folder

    nested = {}
    top_inst = {}
    folders = {}
    for R in roots:
        K = weights.get(R, 1)
        nested[R] = 0
        for S, n in path_counts(R).items():
            for c in org_children_of.get(S, ()):
                if c not in folders:
                    folders[c] = child_folder(c)
                folder = folders[c]
                if folder is None:
                    continue
                if folder.parent == output:
                    top_inst.setdefault(R, []).append((c, n * K))
                else:
                    nested[R] += folder_pdfs(folder) * n * K
    est = dict(nested)
    for R in sorted(roots):
        est.setdefault(R, 0)
    moved = set()
    for R in sorted(roots):
        for c, n in top_inst.get(R, []):
            pdfs = folder_pdfs(folders[c])
            if c in moved:
                est[R] += pdfs * n
            else:
                moved.add(c)
                est[R] += pdfs * max(0, n - 1)
    return est


def _subtree_stems(root, children):
    """`root` plus every stem reachable through `children` (cycle-safe)."""
    seen = set()
    stack = [root]
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        stack.extend(children.get(s, ()))
    return seen


def _names_for_placement(folder_names, children, roots, index, names_of, base, log):
    """Folder names for one placement: the run's whole-tree plan if given.

    Both modes plan folder names for the WHOLE tree about to be placed (one
    `plan_folder_names` call over the merged/new graph), and every placement
    and adoption then uses that single map. The per-subtree call below is the
    fallback for callers that drive a helper directly (tests, repair passes).
    Planning per placement instead is what made a stem's folder name depend on
    which call happened to create it first.
    """
    if folder_names is not None:
        return folder_names
    return build_folder_names(children, roots, index, names_of, base, log)


def plan_folder_names(children, roots, index, names_of, output, log):
    """The run's folder-name plan: one map over the whole tree (see naming.py).

    `naming.build_folder_names` measures every root-to-leaf path, so a stem's
    folder is shortened as far as its deepest copy needs - and the same stem
    gets the same name wherever it lands. Full mode calls this over its whole
    graph; incremental calls it over the merged graph (new + already
    organized), which is why the two modes agree on the tree's shape.

    Returns None (after logging) when the tree's root-to-leaf walk exceeds the
    placement bound (PLAN_PLACEMENT_FACTOR x the copy cap): the walk stops
    instead of enumerating, and the caller must refuse placement/reconcile
    rather than plan from a partial map.
    """
    try:
        return build_folder_names(children, roots, index, names_of, output, log,
                                  limit=MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR)
    except PlacementPlanTooLarge:
        log(f"  WARNING: the tree's root-to-leaf paths exceed the "
            f"{MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR}-path planning bound "
            f"(copy cap {MAX_PLANNED_COPIES}) - no name plan, placement and "
            "reconcile skipped (check the BOM graph)")
        return None


def _place_subassembly_of(R, P, org_par, new_children, new_index, new_names,
                           base, output, dry_run, log, org_children_of,
                           organized, moved_dirs, renames=None, lock=None,
                           folder_names=None):
    """R is a sub-assembly of organized part(s) P; place under P.

    Returns (copies, moves)."""
    if len(org_par) > 1:
        log(f"  {R}: referenced by multiple organized parts {org_par}; placing under {P}")
    fn = _names_for_placement(folder_names, new_children, [R], new_index,
                              new_names, base, log)
    log(f"  {R}: sub-assembly of organized part {P} -> {(base / fn.get(R, R)).relative_to(output)}")
    placed = place_files(new_children, [R], new_index, base, dry_run, log, new_names, fn,
                         renames=renames, lock=lock)
    own = base / (fn.get(R) or folder_name_for(R, new_names))
    _c, _m = _adopt_org_children_below(
        {R: own}, new_children, org_children_of, fn, new_names,
        organized, moved_dirs, output, dry_run, log, lock=lock)
    return placed + _c, _m


def _place_above_organized_children(R, org_child, new_children, new_index, new_names,
                                    organized, moved_dirs, output, dry_run, log,
                                    org_children_of,
                                    renames=None, lock=None, folder_names=None):
    """R is a higher-level assembly referencing organized part(s): create its
    folder, adopt the organized child folders under it, then place R's subtree.
    Mutates moved_dirs; returns (copies, moves).

    A top-level organized root is re-homed (moved) so no stale root folder is
    left behind. A child nested under another live parent is COPIED - moving
    it would hollow out its existing parent, and later claimants would find
    nothing (every claimant gets its own copy, matching place_files)."""
    copies = 0
    moves = 0
    log(f"  {R}: higher-level assembly referencing organized part(s): {org_child}")
    fn = _names_for_placement(folder_names, new_children, [R], new_index,
                              new_names, output, log)
    target_folder = output / fn.get(R, R)
    can_move = True
    if dry_run:
        log(f"  [DRY-RUN] mkdir {target_folder}")
    elif refuse_linked_target(target_folder, output, log, R):
        can_move = False
    else:
        try:
            target_folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"  WARNING: {R}: mkdir failed ({target_folder}): {e} - skipping child moves")
            can_move = False
    for c in (org_child if can_move else (())):
        _c, _m = _adopt_one_org_child(c, target_folder, organized,
                                      moved_dirs, output, dry_run, log,
                                      lock=lock, folder_names=fn)
        copies += _c
        moves += _m
    copies += place_files(new_children, [R], new_index, output, dry_run, log, new_names, fn,
                          renames=renames, lock=lock)
    _c, _m = _adopt_org_children_below(
        {R: target_folder}, new_children, org_children_of, fn, new_names,
        organized, moved_dirs, output, dry_run, log, skip={R}, lock=lock)
    copies += _c
    moves += _m
    return copies, moves


def _place_standalone_new_root(R, new_children, new_index, new_names,
                               output, dry_run, log, org_children_of,
                               organized, moved_dirs,
                               renames=None, lock=None, folder_names=None):
    """Standalone new root with a BOM/children -> new root folder.

    Returns (copies, moves)."""
    fn = _names_for_placement(folder_names, new_children, [R], new_index,
                              new_names, output, log)
    log(f"  {R}: standalone assembly -> new root folder {fn.get(R, R)}/")
    placed = place_files(new_children, [R], new_index, output, dry_run, log, new_names, fn,
                         renames=renames, lock=lock)
    own = output / (fn.get(R) or folder_name_for(R, new_names))
    _c, _m = _adopt_org_children_below(
        {R: own}, new_children, org_children_of, fn, new_names,
        organized, moved_dirs, output, dry_run, log, lock=lock)
    return placed + _c, _m


def _merge_names(org_names, new_names):
    """Prior-batch names plus this batch: newer extraction wins."""
    names = dict(org_names)
    names.update(new_names)
    return names


def _ctx_names(names_of, missing, bom_names):
    """Stem/value -> NAME for the workbook: names extracted from the PDFs this
    run, plus BOM names for missing references (the old NAME-line parse)."""
    names = dict(names_of)
    for _stem, val in missing:
        if bom_names.get(val):
            names.setdefault(val, bom_names[val])
    return names


def _live_orphan_stems(output):
    """Stems currently parked in <output>/_orphans/ (live truth)."""
    orphans_dir = Path(output) / "_orphans"
    if not orphans_dir.is_dir():
        return []
    return sorted({s for p in orphans_dir.glob("*.pdf")
                   if (s := canonical_stem(p.stem))})


def _full_prepare_index(folder, output, jobs, log):
    """Build index, report CHK/superseded, resolve same-revision duplicates."""
    index, duplicates = build_pdf_index(folder, log, exclude=output)
    if not index:
        log("No PDFs found in folder.")
        raise NoPDFsFoundError("No PDFs found in folder.")
    index, old = split_superseded(index)
    if old:
        _log_superseded_preface(old, log)
    log(f"Found {len(index)} active PDF(s)")
    log("")
    # Same-revision duplicates: pick the tree copy (clean > text-layer >
    # shallowest) BEFORE extraction, so BOM/NAME/title block come from the
    # copy that will actually be placed.
    index, watermarked_dupes = _resolve_duplicates(index, duplicates, jobs, log)
    # Report only CHK drawings that stay live in the tree: a CHK whose base has
    # an approved/higher revision is superseded (archived to _superseded/) and the
    # approved drawing is already in the tree, so listing the CHK would ask the
    # user to fetch a drawing they already have. Computed after the split so the
    # list reflects the tree, not the raw input.
    chk_stems = [s for s in sorted(index) if is_chk_stem(s)]
    return index, chk_stems, old, watermarked_dupes


def _full_scan_boms(index, jobs, log, rekey, chk_stems):
    """BOM scan + opt-in re-key + watermark/title-block logs + OCR snap.
    Returns the scan results plus the extraction warning count and the
    single-pass USED ON figures (so the USED ON section reuses them)."""
    log("--- Scanning BOM tables ---")
    bom_of, bom_names, watermarks, titleblocks, warnings, used_on_of, \
        used_names, bom_votes = _scan_boms(
            sorted(index.items()), jobs, log, require_desc=True)
    rekeyed = {}
    if rekey:
        index, bom_of, watermarks, titleblocks, rekeyed = _rekey_misnamed(
            index, bom_of, watermarks, titleblocks, set(index), log)
        chk_stems = [rekeyed.get(s, s) for s in chk_stems]
        # Keep the single-pass USED ON figures aligned with re-keyed stems.
        for old, new in rekeyed.items():
            if old in used_on_of:
                used_on_of[new] = used_on_of.pop(old)
            if old in used_names:
                used_names[new] = used_names.pop(old)
    _log_watermarks(watermarks, log)
    # Filename vs title block: number, revision, title (report only; with
    # --rekey-titleblock a misnamed file is re-keyed first).
    tb_mismatches = _titleblock_mismatches(titleblocks, log)
    # OCR near-miss refs: correct against the known stems before edges. The
    # description votes are re-keyed with them, or the corrected child finds
    # no parts-list name for its folder.
    _snap_boms(bom_of, index, log)
    _snap_bom_votes(bom_votes, index, log)
    bom_names = bom_name_winners(bom_votes)
    return (index, bom_of, bom_names, watermarks, tb_mismatches, rekeyed,
            chk_stems, warnings, used_on_of, used_names)


def _full_build_edges(bom_of, index, bom_names, log):
    """BOM edges + missing/FC logs. Returns (children, parents, unmatched)."""
    children = defaultdict(set)
    parents = defaultdict(set)
    skipped_fc = set()
    idx = StemIndex(index)
    for stem, raw_values in sorted(bom_of.items()):
        for val in raw_values:
            if val.startswith("FC"):
                skipped_fc.add(val)
                continue
            if not is_processable_ref(val):
                continue
            for m in match_pdfs(val, idx):
                if m == stem:
                    continue
                children[stem].add(m)
                parents[m].add(stem)
    if skipped_fc:
        _log_skipped_fc(skipped_fc, log)
    unmatched = _collect_missing(bom_of, index, bom_names,
                                 "BOM reference(s) with no matching PDF:", log)
    if unmatched:
        log("")
    return children, parents, unmatched


def _full_scan_used_on(index, jobs, log, used_on_of=None, names_of=None):
    """USED ON + NAME scan + OCR snap (missing-NAME log follows the BOM fallback)."""
    used_on_of, names_of = _scan_used_on(sorted(index.items()), jobs,
                                         "--- Scanning USED ON fields ---", log,
                                         used_on_cache=used_on_of,
                                         names_cache=names_of)
    _snap_used_on(used_on_of, index, log)
    return used_on_of, names_of


def _full_analyze(bom_of, index, children, parents, used_on_of, names_of, log):
    """USED ON cross-checks, cycles, roots/orphans."""
    # BOM is the only source of truth for parent-child edges. USED ON is a
    # cross-check only: a child whose USED ON names a parent the parent's BOM
    # does not list has a buggy USED ON field (not a placement edge).
    used_on_bugs = _collect_used_on_bugs_full(used_on_of, index, children, log)
    # USED ON consistency: parent BOM lists the part but the part USED ON
    # omits the parent -> almost certainly a wrong USED ON number.
    full_bom_edges, _ = _graph_edges(bom_of, index)
    used_mism = find_used_on_mismatches(full_bom_edges, used_on_of)
    if used_mism:
        _log_used_on_mismatches(used_mism, names_of, log)
    removed = _log_cycle_detection("--- Cycle detection ---",
                                   children, parents, log)
    roots, orphans = _classify_roots(index, parents, bom_of, children)
    _log_roots_and_orphans(roots, orphans, used_on_of, names_of, log)
    return used_on_bugs, used_mism, removed, roots, orphans


def _full_place(children, roots, orphans, old, watermarked_dupes, index,
                names_of, output, dry_run, log, rekeyed, lock=None):
    """Folder creation + orphan/superseded/duplicate copies.

    Returns (copies, placement_refused, skipped_roots): an over-cap fan-out
    drops the largest roots (logged with their planned counts) and still
    places the rest; only when nothing fits is folder placement skipped
    entirely. The bounded orphan/archive copies always run so the run still
    reports."""
    log("--- Creating folder structure ---")
    renames = {new: f"{new}.pdf" for new in rekeyed.values()}
    # Cap check first: a pathological DAG must be trimmed before
    # build_folder_names enumerates its root-to-leaf paths.
    kept, skipped = fit_roots_within_cap(children, roots, log)
    refused = bool(skipped)
    total_copies = 0
    if kept:
        folder_names = plan_folder_names(children, kept, index, names_of, output, log)
        if folder_names is None:
            # Cap consulted before the name plan as well: an over-cap path count
            # refuses placement instead of planning from a partial map.
            refused = True
        else:
            total_copies = place_files(children, kept, index, output, dry_run,
                                       log, names_of, folder_names, renames=renames,
                                       lock=lock)
    # Orphans -> _orphans/ so they are stored, not just logged: when a future
    # input batch supplies their parent, incremental can still match them
    # (they are scanned there like any other PDF and then placed properly).
    if orphans:
        total_copies += copy_orphans(orphans, index, output, dry_run, log,
                                     renames=renames)
    if old:
        n, _archived = copy_superseded(old, output, dry_run, log, overwrite=True)
        total_copies += n
    if watermarked_dupes:
        total_copies += copy_watermarked_duplicates(watermarked_dupes, output,
                                                    dry_run, log)
    log("")
    return total_copies, refused, skipped


def _build_run_context(*, counters, missing, chk, orphans, roots,
                       used_on_mismatches, used_on_bugs, titleblock_mismatches,
                       watermarks, names, refused, skipped):
    """The structured run context every mode returns (one shape, one builder).

    Both modes used to assemble this dict by hand from their own finalizer, one
    taking 29 parameters and one 19, with four adjacent same-typed list
    parameters at the call site: a transposed pair type-checks and silently
    writes one sheet's rows into another. Keyword-only here, so that cannot
    happen again.
    """
    return {
        "counters": counters,
        "missing": missing,
        "chk": chk,
        "orphans": orphans,
        "roots": roots,
        "used_on_mismatches": used_on_mismatches,
        "used_on_bugs": used_on_bugs,
        "titleblock_mismatches": titleblock_mismatches,
        "scanned": OCR.scanned_stems(),
        "watermarks": watermarks,
        "names": names,
        "placement_refused": refused,
        "skipped_roots": list(skipped),
    }


def _full_finalize(roots, children, index, orphans, removed, total_copies,
                    warning_count, unmatched, chk_stems, used_on_of, used_mism,
                    used_on_bugs, tb_mismatches, watermarks, names_of,
                    bom_names, log, refused=False, skipped=()) -> RunContext:
    """Unplaced log + summary + structured RunContext for the workbook."""
    skipped_set = {r for r, _ in skipped}
    _log_unplaced([r for r in roots if r not in skipped_set], children,
                  index, orphans, log)
    summary = [
        "--- Summary ---",
        f"  PDFs scanned:        {len(index)}",
        f"  Root assemblies:     {len(roots)}",
        f"  BOM edges kept:      {sum(len(v) for v in children.values())}",
        f"  Cycles broken:       {len(removed)}",
        f"  PDF copies written:  {total_copies}",
    ]
    if skipped:
        summary.append(f"  Roots skipped (oversized): {len(skipped)}")
    _log_summary(summary, log)
    return _build_run_context(
        counters={
            "scanned": len(index),
            "roots": len(roots),
            "copies": total_copies,
            "cycles": len(removed),
            "warnings": warning_count,
        },
        missing=unmatched,
        chk=chk_stems,
        orphans=orphans,
        roots=[(r, sorted(set(used_on_of.get(r, [])))) for r in roots],
        used_on_mismatches=used_mism,
        used_on_bugs=used_on_bugs,
        titleblock_mismatches=tb_mismatches,
        watermarks=sorted(watermarks.items()),
        names=_ctx_names(names_of, unmatched, bom_names),
        refused=refused,
        skipped=skipped,
    )


def run_full(folder, output, dry_run, log, jobs=0, rekey=False,
             *, hold_lock=False):
    """Full run: index all of `folder` and place the whole tree under `output`.

    Holds the exclusive output-tree lock for the whole run (refuses with
    RunLockedError when another live run holds it); see
    fsops.acquire_run_lock. With hold_lock=True the lock is NOT released on
    return: the caller receives (ctx, lock_path) and must release_run_lock it
    once its own post-work (report + workbook writes) finishes, so a second
    run cannot interleave those shared-file updates. Returns the structured run context consumed by
    the Excel workbook: counters {scanned, roots, copies, cycles, warnings},
    missing [(referencing stem, missing ref)], chk [stem], orphans [stem],
    roots [(stem, [used_on])], used_on_mismatches [(parent, child, actual)],
    used_on_bugs [(parent, child)],
    titleblock_mismatches [(stem, field, filename value, title-block value)],
    scanned [(stem, [pages])], watermarks [(stem, evidence)],
    names {stem/ref: NAME}. Raises NoPDFsFoundError on an empty input.
    Raises LockLostError (without unlinking the stealer's lock) when the
    heartbeat observed our lock stolen mid-run.
    """
    lock = acquire_run_lock(output, dry_run, log)
    try:
        ctx = _run_full_inner(folder, output, dry_run, log, jobs, rekey,
                              lock=lock)
    except LockLostError:
        # Our lock now belongs to the stealer: stop the heartbeat but never
        # unlink (release would delete the live holder's lock).
        abandon_run_lock(lock)
        raise
    except BaseException:
        release_run_lock(lock)
        raise
    if hold_lock:
        return ctx, lock
    release_run_lock(lock)
    return ctx


def _run_full_inner(folder, output, dry_run, log, jobs=0, rekey=False,
                    lock=None) -> RunContext:
    """run_full body (see run_full); split out so the lock spans all returns."""
    jobs = resolve_jobs(jobs)
    _ensure_lock(lock, log)
    sweep_supersede_staging(output, dry_run, log)
    sweep_stale_claims(output, dry_run, log)
    index, chk_stems, old, watermarked_dupes = _full_prepare_index(folder, output, jobs, log)
    index, bom_of, bom_names, watermarks, tb_mismatches, rekeyed, chk_stems, \
        warning_count, _used_on_of, _used_names = _full_scan_boms(
            index, jobs, log, rekey, chk_stems)

    children, parents, unmatched = _full_build_edges(bom_of, index, bom_names, log)

    # Unapproved CHK drawings: approved DWG should be downloaded from Teamcenter
    if chk_stems:
        _log_chk_block(chk_stems, log)

    used_on_of, names_of = _full_scan_used_on(index, jobs, log,
                                            used_on_of=_used_on_of,
                                            names_of=_used_names)
    # A drawing's own NAME is authoritative only when a text layer carried it:
    # an OCR-read name is a guess and a missing one leaves nothing to place, so
    # in both cases the parents' parts-list description wins.
    names_of = prefer_bom_names(names_of, _ocr_named_stems(), bom_names, index)
    _log_missing_names(index, names_of, log)

    used_on_bugs, used_mism, removed, roots, orphans = _full_analyze(
        bom_of, index, children, parents, used_on_of, names_of, log)
    _ensure_lock(lock, log)
    total_copies, refused, skipped = _full_place(children, roots, orphans, old,
                                                 watermarked_dupes, index, names_of,
                                                 output, dry_run, log, rekeyed,
                                                 lock=lock)
    return _full_finalize(roots, children, index, orphans, removed,
                          total_copies, warning_count, unmatched, chk_stems,
                          used_on_of, used_mism, used_on_bugs, tb_mismatches,
                          watermarks, names_of, bom_names, log,
                          refused=refused, skipped=skipped)


def _incremental_prepare(folder, output, dry_run, log):
    """Collect candidates from the input folder (see _incremental_prepare_from_index)."""
    scan_index, top_duplicates = build_pdf_index(folder, log, exclude=output)
    if not scan_index:
        log("No PDFs found in folder.")
        raise NoPDFsFoundError("No PDFs found in folder.")
    scan_res = scan_output_tree(output)
    in_place = Path(folder).resolve() == Path(output).resolve()
    return _incremental_prepare_from_index(scan_index, top_duplicates, output,
                                           dry_run, log, scan_res, in_place)


def _incremental_prepare_from_index(scan_index, top_duplicates, output, dry_run,
                                    log, scan_res=None, in_place=False,
                                    allow_archived=False,
                                    candidate_line="PDFs at top level:              "):
    """Collect candidates, log counts, archive superseded among new PDFs.

    Split out of _incremental_prepare so repair mode can feed the tree's own
    system folders in as the candidate set (`allow_archived` lifts the
    "an archived stem is never a candidate again" rule for that caller).
    An empty candidate set is legal here: repair still has to reconcile the
    tree, so the caller decides whether emptiness is fatal.

    Returns (organized, sup_dir, stored_orphans, new_index, chk_stems,
    supersede_pairs, new_duplicates, copies, archived_new, scan_res) where
    archived_new holds only stems whose archive copy was verified written.
    """
    if scan_res is None:
        scan_res = scan_output_tree(output)
    organized, sup_dir, stored_orphans, new_index = \
        _collect_incremental_candidates(scan_index, output, scan_res=scan_res,
                                        in_place=in_place,
                                        allow_archived=allow_archived)
    new_index, old_new = split_superseded(new_index)
    supersede_pairs = _detect_supersede_pairs(new_index, old_new, organized, log)
    # Report only CHK drawings that stay live in the tree (whole-tree scope, like
    # the other reported decisions). A CHK whose base has an approved/higher
    # revision is archived to _superseded/ below and the approved drawing is
    # already in the tree, so listing the CHK would ask the user to fetch a
    # drawing they already have. Organized CHK superseded by this batch drop out
    # as well.
    superseded_by_batch = {o for _s, o in supersede_pairs}
    chk_stems = [s for s in sorted((set(organized) | set(new_index))
                                   - superseded_by_batch) if is_chk_stem(s)]
    new_duplicates = {stem: paths for stem, paths in top_duplicates.items()
                      if stem in new_index}
    if old_new:
        log(f"Found {len(old_new)} superseded revision(s) among new PDFs")
    log(f"{candidate_line}{len(scan_index)}")
    log(f"Already organized (subfolders): {len(organized)}")
    log(f"New PDFs to process:            {len(new_index)}")
    _log_previous_report(output, log)
    log("")
    # Archive superseded copies even when there is nothing new to place.
    total_copies, archived_new = copy_superseded(old_new, output, dry_run, log)
    return (organized, sup_dir, stored_orphans, new_index, chk_stems,
            supersede_pairs, new_duplicates, total_copies, archived_new, scan_res)


def _repair_candidates(scan_res, organized):
    """Archived _superseded/ copies that deserve a fresh decision.

    Archiving is normally a one-way door. A repair run re-opens it narrowly: an
    archived copy is a candidate only when its stem is not live, it is not an
    unapproved CHK, and its revision rank is strictly higher than the highest
    revision live for that base. A correctly archived copy - an older revision,
    a CHK, a watermarked loser - fails that test and stays put, so a repair over
    a tree the current code built changes nothing.

    Deliberately not a candidate: the same-stem case (duplicate resolution
    between a live and an archived export) and the sheet-restoration case (a
    `DWG2` whose sibling is live) - both are ties, and a tie cannot be resolved
    without re-deciding duplicate resolution, which would let a repair run churn
    a healthy tree. A base with no live copy at all is left alone too: repair
    never re-adds a drawing the tree does not have.
    """
    live_rank: dict = {}
    for stem in organized:
        base = stem.split("_")[0]
        live_rank[base] = max(live_rank.get(base, -1), revision_rank(stem))
    best: dict = {}
    for path in sorted(scan_res["sup"]):
        stem = canonical_stem(path.stem)
        if not stem or stem in organized:
            continue
        if is_chk_stem(stem):
            continue
        base = stem.split("_")[0]
        if base not in live_rank or revision_rank(stem) <= live_rank[base]:
            continue
        # One revision per base - the highest. Handing the batch several
        # revisions of one base would make split_superseded demote the losers
        # into old_new, and prepare would then archive copies that are already
        # in the archive. Distinct sheets at that one revision travel together:
        # the sheet exception keeps them all active, and promoting a single
        # sheet would leave its siblings tied with the new live rank and never
        # reconsidered.
        rank = revision_rank(stem)
        current = best.get(base)
        if current is None or rank > current[0]:
            best[base] = (rank, [stem])
        elif rank == current[0]:
            current[1].append(stem)
    keep = {s for _rank, sheets in best.values() for s in sheets}
    return {stem: path for stem, path in
            ((canonical_stem(p.stem), p) for p in sorted(scan_res["sup"]))
            if stem in keep}


def _stage_repair_candidates(candidates, log):
    """Copy archived candidates to a temp dir under their canonical names.

    An archive filename carries a folder prefix ("1.1.2.0 F10197062-B-CHK.pdf"),
    so a promoted copy must be placed as "{stem}.pdf" or the tree gets that
    prefix. Staging in the system temp dir keeps the output folder untouched
    even in a dry run. Returns (staged_index, staging_dir_or_None); the caller
    removes the directory (see repair_cleanup, and sweep_repair_staging for the
    exception path).
    """
    if not candidates:
        return {}, None
    staged_dir = Path(tempfile.mkdtemp(prefix=_REPAIR_STAGING_PREFIX))
    staged = {}
    for stem, path in sorted(candidates.items()):
        dst = staged_dir / f"{stem}.pdf"
        try:
            shutil.copy2(_native(path), _native(dst))
        except OSError as e:
            log(f"  WARNING: {stem}: could not stage archived copy ({path}): {e}")
            continue
        staged[stem] = dst
    return staged, staged_dir


def _repair_prepare(output, dry_run, log):
    """Prepare a repair run from the tree's own system folders.

    Returns ((organized, sup_dir, stored_orphans, new_index, chk_stems,
    supersede_pairs, new_duplicates, copies, archived_new, scan_res), meta)
    with the tuple shaped exactly like _incremental_prepare's, so
    _run_delta_inner can drive either mode. Parked _orphans/ copies arrive
    through the shared stored_orphans path; the _superseded/ copies worth a
    fresh decision are staged (see _stage_repair_candidates) and recorded in
    meta for cleanup. An empty candidate set is legal - repair still has a tree
    to reconcile.
    """
    scan_res = scan_output_tree(output)
    organized = find_organized_pdfs(output, scan_res=scan_res)
    if not organized:
        log("No organized PDFs found - not an organized tree, nothing to repair.")
        raise NoPDFsFoundError("No organized PDFs found in the tree.")
    archived = _repair_candidates(scan_res, organized)
    staged, staged_dir = _stage_repair_candidates(archived, log)
    if staged_dir is not None:
        _register_staging(output, staged_dir)
    prepared = _incremental_prepare_from_index(
        staged, {}, output, dry_run, log, scan_res, False,
        allow_archived=True,
        candidate_line="Repair candidates:            ")
    meta = {"archived": archived, "staged": staged, "staging_dir": staged_dir}
    return prepared, meta


def repair_cleanup(meta, log):
    """Remove the repair staging directory (never the tree's own files)."""
    staging = (meta or {}).get("staging_dir")
    if not staging:
        return
    try:
        shutil.rmtree(staging)
    except OSError as e:
        log(f"  WARNING: could not remove repair staging dir "
            f"{staging}: {e}")
        return
    _unregister_staging(staging)


def sweep_repair_staging(output, log):
    """Remove this tree's staging dirs if the repair raised before cleanup.

    `_stage_repair_candidates` copies every candidate before extraction,
    placement and the lock checks run, so a failure there would otherwise leak
    the copies for the life of the temp dir. Keyed by tree: a concurrent repair
    on another output keeps its own candidates while it places them.
    """
    for staging in sorted(_REPAIR_STAGING.get(str(output), ())):
        repair_cleanup({"staging_dir": staging}, log)
    _REPAIR_STAGING.pop(str(output), None)


def sweep_stale_repair_staging(log, max_age_secs=24 * 3600):
    """Remove staging dirs a killed repair left behind in the temp dir.

    The in-process sweep above only knows the dirs its own process created
    (_REPAIR_STAGING is memory), so a SIGKILL, a crash outside Python or a
    power loss left full copies of the drawings in %TEMP% forever - outside
    the tree the operator controls and outside every later run's sweep. This
    pass is stateless: anything carrying our prefix and older than
    `max_age_secs` is abandoned (a live concurrent repair's directory is
    minutes old, so the age gate leaves it alone). Never raises.
    """
    for path in sorted(Path(tempfile.gettempdir()).glob(f"{_REPAIR_STAGING_PREFIX}*")):
        if not path.is_dir():
            continue
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            continue
        if age < max_age_secs:
            continue
        try:
            shutil.rmtree(path)
        except OSError as e:
            log(f"  WARNING: could not remove abandoned repair staging dir "
                f"{path}: {e}")
            continue
        _unregister_staging(path)
        log(f"  Removed abandoned repair staging dir (left by a killed run): {path}")


def _finish_repair_promotions(organized, archived, staged_stems, output,
                              dry_run, log, lock=None):
    """Delete promoted archive sources whose live copy is verified.

    A promotion copies the archived drawing into the tree and leaves the
    archive file in place; once the live copy is byte-identical the archive
    original is a duplicate of a live revision, so it goes. A missing or
    differing live copy keeps it - nothing is ever lost. `live_index` must be
    a post-reconcile scan: an index taken before a rename points at a path that
    no longer exists. This is a delete, so the lock is re-checked first.
    Returns the number removed.
    """
    removed = 0
    for stem in sorted(staged_stems):
        src = archived.get(stem)
        if src is None or dry_run:
            continue
        live = [p for p in organized.get(stem, []) if p.exists()]
        if not any(_same_bytes(p, src) for p in live):
            log(f"  WARNING: {stem}: promoted archive copy kept "
                "(no verified live copy)")
            continue
        _ensure_lock(lock, log)
        try:
            src.unlink()
            removed += 1
        except OSError as e:
            log(f"  WARNING: {stem}: could not remove promoted archive copy: {e}")
    return removed


def _repair_reconcile(output, children, roots, folder_names, dry_run, log,
                      moved_dirs, lock=None):
    """Put the live tree on the plan: fresh scan, reconcile, sweep empties.

    Runs after placement, on a fresh index - supersede swaps and orphan
    adoption have already moved folders, so the pre-run index is stale. Returns
    (counts, review, index) where index is the post-reconcile stem -> paths map
    (paths as they are on disk now, so callers can verify bytes).
    """
    scan_res = scan_output_tree(output)
    organized = find_organized_pdfs(output, scan_res=scan_res)
    try:
        planned = planned_paths(children, roots, folder_names)
    except PlacementPlanTooLarge:
        log(f"  WARNING: the tree's root-to-leaf paths exceed the "
            f"{MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR}-path planning bound "
            f"(copy cap {MAX_PLANNED_COPIES}) - reconcile skipped "
            "(check the BOM graph)")
        review = ["planning bound: the tree's root-to-leaf paths exceed "
                  f"{MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR} - reconcile skipped"]
        counts = {"moved": 0, "copied": 0, "removed": 0, "review": len(review),
                  "emptied": 0}
        return counts, review, organized
    counts, review = reconcile_tree(organized, planned, output, dry_run, log,
                                    moved_dirs, lock=lock)
    counts["emptied"] = remove_empty_dirs(output, dry_run, log, lock=lock)
    return counts, review, organized


def _log_repair_block(counts, review, log):
    """Report what the repair pass moved, removed, and left for a human."""
    log("")
    log("--- Repair (tree reconciliation) ---")
    log(f"  Folders renamed:        {counts.get('renamed', 0)}")
    log(f"  Folders moved:          {counts.get('moved', 0)}")
    log(f"  Folders copied:         {counts.get('copied', 0)}")
    log(f"  Folders removed:        {counts.get('removed', 0)}")
    log(f"  Empty folders removed:  {counts.get('emptied', 0)}")
    log(f"  Orphans adopted:        {counts.get('adopted', 0)}")
    log(f"  Revisions promoted:     {counts.get('promoted', 0)}")
    log(f"  Needs manual review:    {len(review)}")
    for line in review:
        log(f"    {line}")
    log("")


def _incremental_no_new_ctx(organized, chk_stems, output, total_copies,
                            warning_count, log, stored_orphans=(),
                            archived=(), dry_run=False) -> RunContext:
    """Early-exit context when there is nothing new to place.

    Stored orphans whose stem was just archived (the only "new" candidate was
    an older parked revision) are retired here too - the full finalize path
    never runs on this branch.
    """
    log("No new PDFs at top level - nothing to do.")
    log("")
    # CHK arrivals are still reported even when there is nothing to place
    if chk_stems:
        _log_chk_block(chk_stems, log)
    if stored_orphans:
        retire_adopted_orphans(stored_orphans, output, dry_run, log,
                               superseded=archived)
    _log_summary([
        "--- Summary ---",
        "  New PDFs scanned:     0",
        f"  Already organized:    {len(organized)}",
        "  New graph roots:      0",
        "  BOM edges kept:       0",
        "  Cycles broken:        0",
        f"  PDF copies written:   {total_copies}",
        "  Root folders moved:   0",
    ], log)
    return {
        "counters": {
            "scanned": 0,
            "roots": 0,
            "copies": total_copies,
            "cycles": 0,
            "warnings": warning_count,
        },
        "missing": [],
        "chk": chk_stems,
        "orphans": _live_orphan_stems(output),
        "roots": [],
        "used_on_mismatches": [],
        "used_on_bugs": [],
        "titleblock_mismatches": [],
        "scanned": OCR.scanned_stems(),
        "watermarks": [],
        "names": {},
        "placement_refused": False,
        "skipped_roots": [],
    }


def _incremental_scan_new_boms(new_index, new_duplicates, jobs, log, rekey,
                               organized, chk_stems):
    """Dedup + BOM scan + re-key + watermark log + OCR snap for new PDFs."""
    new_stems = set(new_index)
    org_stems = set(organized)
    scanned_new = len(new_index)
    # Same-revision duplicates: pick the tree copy (clean > text-layer >
    # shallowest) BEFORE extraction, so BOM/NAME/title block come from the
    # copy that will actually be placed.
    new_index, watermarked_dupes = _resolve_duplicates(
        new_index, new_duplicates, jobs, log)
    log("--- Scanning BOM tables (new PDFs) ---")
    new_boms, bom_names, new_watermarks, new_titleblocks, warnings, \
        new_used_on, new_names, new_bom_votes = _scan_boms(
            sorted(new_index.items()), jobs, log, require_desc=False)
    all_stems = new_stems | org_stems
    rekeyed = {}
    if rekey:
        new_index, new_boms, new_watermarks, new_titleblocks, rekeyed = \
            _rekey_misnamed(new_index, new_boms, new_watermarks,
                            new_titleblocks, all_stems, log)
        if rekeyed:
            new_stems = set(new_index)
            scanned_new = len(new_index)
            chk_stems = [rekeyed.get(s, s) for s in chk_stems]
            all_stems = new_stems | org_stems
        for old, new in rekeyed.items():
            if old in new_used_on:
                new_used_on[new] = new_used_on.pop(old)
            if old in new_names:
                new_names[new] = new_names.pop(old)
    _log_watermarks(new_watermarks, log)
    # OCR near-miss refs: correct against the known stems before edges; the
    # description votes are re-keyed with them (see _snap_bom_votes).
    _snap_boms(new_boms, all_stems, log)
    _snap_bom_votes(new_bom_votes, all_stems, log)
    bom_names = bom_name_winners(new_bom_votes)
    return (new_index, watermarked_dupes, new_boms, bom_names,
            new_watermarks, new_titleblocks, rekeyed, new_stems, org_stems,
            scanned_new, all_stems, chk_stems, warnings, new_used_on,
            new_names, new_bom_votes)


def _incremental_new_missing(new_boms, all_stems, bom_names, chk_stems, log):
    """Missing/FC logs for new BOMs + CHK block. Returns unmatched."""
    skipped_fc = sorted({v for vals in new_boms.values() for v in vals if v.startswith("FC")})
    if skipped_fc:
        _log_skipped_fc(skipped_fc, log)
    unmatched = _collect_missing(new_boms, all_stems, bom_names,
                                 "BOM reference(s) with no matching PDF:", log)
    log("")
    # Unapproved CHK drawings among the new PDFs: approved DWG should be
    # downloaded from Teamcenter (same base drawing number, no CHK suffix)
    if chk_stems:
        _log_chk_block(chk_stems, log)
    return unmatched


def _incremental_scan_organized(organized, all_stems, jobs, bom_names,
                                new_watermarks, new_titleblocks, unmatched, log,
                                bom_votes):
    """Organized scan + snaps + watermark/title-block merge.
    Returns (org_boms, org_used_on, watermarks, tb_mismatches, org_names,
    warnings)."""
    org_boms = {}
    org_used_on = {}
    org_watermarks = {}
    org_titleblocks = {}
    org_names = {}
    warnings = 0
    if organized:
        org_boms, org_used_on, org_unmatched, org_watermarks, \
            org_titleblocks, org_names, warnings = \
            _scan_organized(organized, all_stems, jobs, bom_names, log,
                            bom_votes)
        # (_scan_organized snaps org_boms/org_used_on before its missing check.)
        unmatched.extend(org_unmatched)
        if org_watermarks:
            _log_watermarks(org_watermarks, log)
    watermarks = dict(org_watermarks)
    watermarks.update(new_watermarks)
    # Filename vs title block: number, revision, title (report only; with
    # --rekey-titleblock a misnamed new file is re-keyed before scanning).
    all_titleblocks = dict(org_titleblocks)
    all_titleblocks.update(new_titleblocks)
    tb_mismatches = _titleblock_mismatches(all_titleblocks, log)
    return org_boms, org_used_on, watermarks, tb_mismatches, org_names, \
        warnings


def _incremental_scan_new_used_on(new_index, jobs, all_stems, log,
                                  cached_used_on=None, cached_names=None):
    """USED ON + NAME scan for new PDFs + OCR snap."""
    new_used_on, new_names = _scan_used_on(
        sorted(new_index.items()), jobs,
        "--- Scanning USED ON fields (new PDFs) ---", log,
        used_on_cache=cached_used_on, names_cache=cached_names)
    _snap_used_on(new_used_on, all_stems, log)
    return new_used_on, new_names


def _incremental_supersede(supersede_pairs, organized, org_boms, org_used_on,
                           new_boms, new_used_on, new_index, new_stems,
                           org_stems, all_stems, sup_dir, output, dry_run,
                           log, total_copies, lock=None):
    """In-place revision swaps + child moves. Returns (moved_dirs, moves, copies, stems, swapped_old)."""
    # Supersede in place. When a new revision supersedes an organized one, the
    # old revision's PDF(s) move to _superseded/ and the new revision's PDF takes their
    # place in the tree. The new stem is then treated as organized for this run.
    moved_dirs = FolderMoves()  # old_folder -> new_folder (chronological)
    total_moves = 0
    swapped_old: list = []
    if supersede_pairs:
        swapped = []
        for s, o in supersede_pairs:
            old_paths = sorted(organized.get(o, []))
            new_pdf = new_index.get(s)
            if not old_paths or new_pdf is None:
                log(f"  WARNING: {s}: no organized copy of {o} found - placing as a new PDF")
                continue
            moved, copies = _swap_revision_files(old_paths, new_pdf, sup_dir, output,
                                                 dry_run, log, lock=lock)
            total_copies += copies
            if not moved:
                # Swap failed (logged inside _swap_revision_files, e.g. a
                # symlink refusal or OSError): the new revision was neither
                # archived nor placed. Leave it as a new stem so it is placed
                # (and retried next run); marking it organized here would
                # strand its children at the output root. The stale organized
                # copy stays until the retry succeeds. Stop here: the pre-swap
                # bound assumed this source would be archived, so later swaps
                # would run against a graph it undercounted.
                log(f"  WARNING: {s}: supersede swap failed - placing as a new PDF "
                    "(retry next run); later swaps deferred")
                break
            org_boms[s] = new_boms.pop(s, [])
            org_used_on[s] = new_used_on.pop(s, [])
            org_boms.pop(o, None)
            org_used_on.pop(o, None)
            organized.pop(o, None)
            new_index.pop(s, None)
            new_stems.discard(s)
            organized[s] = moved
            swapped_old.append(o)
            org_stems.discard(o)
            org_stems.add(s)
            swapped.append(s)
        if swapped:
            log(f"  {len(swapped)} organized revision(s) superseded in place")
            all_stems = new_stems | org_stems
            # Move organized parts referenced by a superseding revision under its folder
            total_moves, move_copies = _move_children_under_superseding(
                swapped, organized, org_boms, org_stems, output, dry_run,
                moved_dirs, log)
            total_copies += move_copies
    return moved_dirs, total_moves, total_copies, all_stems, swapped_old


def _incremental_graph(new_boms, org_boms, new_used_on, org_used_on,
                       all_stems, new_stems, org_stems, log,
                       *, merged_children, merged_used_on, merged_names):
    """Graph edges + USED ON cross-checks + cycles + relationship notes.

    Placement maps stay delta-scoped (``new_*`` / ``org_*_of``); the two
    *reported* cross-checks run on the merged tree with the same functions full
    mode uses, so the two modes report identical USED ON sections.
    """
    _, org_parents_of = _graph_edges(org_boms, new_stems)
    org_children_of, _ = _graph_edges(new_boms, org_stems)
    new_children, new_parents = _graph_edges(new_boms, new_stems)
    # BOM is the only source of truth for parent-child edges; USED ON is a
    # cross-check only. Report children whose USED ON names a parent the
    # parent's BOM does not list as USED ON bugs (never placement edges).
    used_on_bugs = find_used_on_bugs(merged_children, merged_used_on, all_stems)
    if used_on_bugs:
        _log_used_on_bugs(used_on_bugs, log)
    # USED ON consistency (BOM-derived edges only): parent BOM lists the part
    # but the part USED ON omits the parent. Whole merged tree, plus the merged
    # name map, so the sheet and the report's NAME lines match a full run.
    incr_mism = find_used_on_mismatches(merged_children, merged_used_on)
    if incr_mism:
        _log_used_on_mismatches(incr_mism, merged_names, log)
    removed = _log_cycle_detection("--- Cycle detection (new PDFs) ---",
                                   new_children, new_parents, log)
    _log_relationship_notes(new_stems, new_parents, org_parents_of, org_children_of, log)
    return (new_children, new_parents, org_parents_of, org_children_of,
            used_on_bugs, incr_mism, removed)


def _incremental_placeable(R, new_boms, new_children, org_parents_of,
                           org_children_of):
    """Shared placeable-root predicate: a new root counts against the fan-out
    cap (and reaches placement) only with a BOM, new children, or an
    organized-graph relation on either side; BOM-less standalone roots stay
    parked in _orphans/ and are never counted. One definition so the pre-cap
    check and the placement backstop cannot diverge (a root only matched as an
    organized child still counts against the cap)."""
    return bool(new_boms.get(R) or new_children.get(R)
                or org_parents_of.get(R) or org_children_of.get(R))


def _incremental_place(new_stems, new_parents, org_parents_of, org_children_of,
                       new_children, new_index, new_names, new_boms,
                       organized, moved_dirs, stored_orphans, output, dry_run,
                       log, rekeyed, total_copies, total_moves, lock=None,
                       folder_names=None):
    """Placement decisions for the parentless new stems (the placement worklist).

    Returns (worklist, copies, moves, placement_refused, skipped_roots), where
    worklist is every parentless new stem - including BOM-less standalones that
    are only parked and stems placed under an organized parent. Reported roots
    are computed separately from the merged tree with full mode's rule (see
    ``_classify_roots`` in ``_run_incremental_inner``). The per-run fan-out check
    trims oversized roots before any placement, so a partial refusal still
    places the rest; skipped roots are reported, never placed. BOM-less
    standalone roots are still parked (bounded by the root count, not by the
    graph fan-out), matching full mode - except on a full refusal, which places
    and parks nothing, as before.

    `folder_names` is this run's whole-tree plan (``_run_incremental_inner``);
    placement and organized-child adoption both name folders from it, so the
    tree does not depend on which run created a folder first. Callers that pass
    nothing (tests, repair passes) fall back to a per-placement plan."""
    new_roots = sorted(s for s in new_stems if not new_parents.get(s))
    log("--- Placement decisions ---")
    renames = {new: f"{new}.pdf" for new in rekeyed.values()}
    # Parked roots (no BOM, no children, no organized relation) never reach
    # place_files, so they must not be charged against the cap.
    placeable = [R for R in new_roots
                 if _incremental_placeable(R, new_boms, new_children,
                                           org_parents_of, org_children_of)]
    # A root referenced by several organized parents is copied under each
    # claimant: charge its subtree once per claimant against the cap.
    weights = {R: max(1, len(org_parents_of.get(R, ()))) for R in placeable}
    # Adopted organized-subtree copies ride outside the new-children walk:
    # charge them per claimant too, or a small accepted graph could copy
    # large organized subtrees past the cap.
    extra = _adopted_copy_estimate(placeable, new_children, org_children_of,
                                       organized, output, weights,
                                       moved_dirs)
    kept, skipped = fit_roots_within_cap(new_children, placeable, log,
                                         weights=weights, extra=extra)
    refused = bool(skipped)
    skipped_set = {r for r, _ in skipped}
    if refused and not kept:
        # Full refusal, as before: nothing is placed and nothing is parked
        # (bounded archive copies already ran in prepare).
        log("")
        return [], total_copies, total_moves, refused, skipped
    standalone_orphans = []
    # (stem, organized claimant) pairs already copied by the root case below, so
    # the cross-batch claimant pass does not copy any of them twice.
    covered: set = set()
    for R in new_roots:
        if R in skipped_set:
            # Oversized root: reported by fit_roots_within_cap above, never
            # placed (its shared children still land under their other
            # parents).
            continue
        org_par = sorted(org_parents_of.get(R, ()))
        org_child = sorted(org_children_of.get(R, ()))
        if org_par:
            if org_child:
                log(f"  {R}: WARNING - both referenced by organized part(s) {org_par} and "
                    f"references organized part(s) {org_child}; placing under every claimant")
            # Every claimant gets its own copy (as in full mode): placing
            # under only the first would leave the other parents' BOMs
            # pointing at a child missing from their subtrees.
            subtree = _subtree_stems(R, new_children)
            for P in org_par:
                base = _current_folder_of(P, organized, output, moved_dirs)
                _c, _m = _place_subassembly_of(R, P, org_par, new_children, new_index,
                                               new_names, base, output, dry_run, log,
                                               org_children_of, organized, moved_dirs,
                                               renames=renames, lock=lock,
                                               folder_names=folder_names)
                total_copies += _c
                total_moves += _m
                covered.update((X, P) for X in subtree)
        elif org_child:
            copies, moves = _place_above_organized_children(R, org_child, new_children,
                                                            new_index, new_names, organized,
                                                            moved_dirs, output, dry_run, log,
                                                            org_children_of,
                                                            renames=renames, lock=lock,
                                                            folder_names=folder_names)
            total_copies += copies
            total_moves += moves
        elif not (new_boms.get(R) or new_children.get(R)):
            # BOM-less standalone part: park it like full mode so a later
            # parent can adopt it (already-parked orphans are left as-is).
            if R in stored_orphans:
                log(f"  {R}: standalone part (no BOM) - already parked in _orphans/")
            else:
                log(f"  {R}: standalone part (no BOM) - storing in _orphans/")
                standalone_orphans.append(R)
        else:
            _c, _m = _place_standalone_new_root(R, new_children, new_index,
                                                   new_names, output, dry_run, log,
                                                   org_children_of, organized, moved_dirs,
                                                   renames=renames, lock=lock,
                                                   folder_names=folder_names)
            total_copies += _c
            total_moves += _m
    # Cross-batch claimant symmetry. A stem that arrives together with its own
    # new parent is placed only under that parent, so a pre-existing (organized)
    # claimant whose BOM lists it never gets a copy: its folder is missing a
    # child its own BOM points at, and the resulting tree depends on the order
    # the batches happened to arrive in. Give every organized claimant the same
    # copy the batch-root case above already makes.
    placed = set()
    for R in new_roots:
        if R in skipped_set or R in standalone_orphans:
            continue
        placed |= _subtree_stems(R, new_children)
    for S in sorted(placed):
        org_par = sorted(org_parents_of.get(S, ()))
        for P in org_par:
            if (S, P) in covered:
                continue
            base = _current_folder_of(P, organized, output, moved_dirs)
            _c, _m = _place_subassembly_of(S, P, org_par, new_children, new_index,
                                           new_names, base, output, dry_run, log,
                                           org_children_of, organized, moved_dirs,
                                           renames=renames, lock=lock,
                                           folder_names=folder_names)
            total_copies += _c
            total_moves += _m
    if standalone_orphans:
        total_copies += copy_orphans(standalone_orphans, new_index, output, dry_run, log,
                                     renames=renames)
    log("")
    return new_roots, total_copies, total_moves, refused, skipped


def _incremental_finalize(stored_orphans, output, dry_run, log, scanned_new,
                          organized, new_roots, new_children, removed,
                          total_copies, total_moves, warning_count, unmatched,
                          chk_stems, roots_used_on, incr_mism, used_on_bugs,
                          tb_mismatches, watermarks, new_names,
                          bom_names, org_names=None, merged_names=None,
                          superseded=(), planned=(),
                          scan_res=None, refused=False,
                          skipped=(), repair=False) -> RunContext:
    """Orphan retirement + summary + structured RunContext."""
    # Orphan retirement: a parked _orphans/ copy is retired when the same stem
    # exists live in the tree (adopted into an earlier or this run's placement)
    # or was superseded (its archived copy lives in _superseded/). In dry-run
    # this run's placements copied nothing, so planned tree stems (passed via
    # planned=) also count as live for the retirement report.
    if stored_orphans:
        retire_adopted_orphans(stored_orphans, output, dry_run, log,
                               superseded=superseded, planned=planned,
                               scan_res=scan_res)
    log("")
    if repair:
        # A repair run imports nothing: its "new" set is the tree's own
        # _orphans/ and _superseded/ candidates, and it places no delta edges,
        # so the incremental wording ("New PDFs scanned", "BOM edges kept")
        # would read as if drawings had arrived.
        summary = [
            "--- Summary ---",
            f"  Candidates examined:  {scanned_new}",
            f"  Tree folders:         {len(organized)}",
            f"  Root assemblies:     {len(new_roots)}",
            f"  PDF copies written:   {total_copies}",
            f"  Root folders moved:   {total_moves}",
        ]
    else:
        summary = [
            "--- Summary ---",
            f"  New PDFs scanned:     {scanned_new}",
            f"  Already organized:    {len(organized)}",
            f"  Root assemblies:     {len(new_roots)}",
            f"  BOM edges kept:       {sum(len(v) for v in new_children.values())}",
            f"  Cycles broken:        {len(removed)}",
            f"  PDF copies written:   {total_copies}",
            f"  Root folders moved:   {total_moves}",
        ]
    if skipped:
        summary.append(f"  Roots skipped (oversized): {len(skipped)}")
    _log_summary(summary, log)
    return _build_run_context(
        counters={
            "scanned": scanned_new,
            "roots": len(new_roots),
            "copies": total_copies,
            "cycles": len(removed),
            "warnings": warning_count,
        },
        missing=unmatched,
        chk=chk_stems,
        orphans=_live_orphan_stems(output),
        roots=[(r, sorted(set(roots_used_on.get(r, [])))) for r in new_roots],
        used_on_mismatches=incr_mism,
        used_on_bugs=used_on_bugs,
        titleblock_mismatches=tb_mismatches,
        watermarks=sorted(watermarks.items()),
        names=_ctx_names(merged_names if merged_names is not None
                         else _merge_names(org_names or {}, new_names),
                         unmatched, bom_names),
        refused=refused,
        skipped=skipped,
    )


def run_incremental(folder, output, dry_run, log, jobs=0, rekey=False,
                    *, hold_lock=False):
    """Incremental run: process only stems not already in the output tree plus stored orphans.

    Holds the exclusive output-tree lock like run_full (see
    fsops.acquire_run_lock); with hold_lock=True returns (ctx, lock_path) and
    the caller must release_run_lock it after its post-work. The input scan is recursive; already-organized
    subfolders are never re-sorted (supersede swaps and parent-adoption moves
    still touch them).

    Returns the same structured run context as run_full: counters {scanned,
    roots, copies, cycles, warnings}, missing [(referencing stem, missing ref)],
    chk [stem], orphans [stem], roots [(stem, [used_on])],
    used_on_mismatches [(parent, child, actual)],
    used_on_bugs [(parent, child)],
    titleblock_mismatches [(stem, field, filename value, title-block value)],
    scanned [(stem, [pages])], watermarks [(stem, evidence)],
    names {stem/ref: NAME}.
    Raises NoPDFsFoundError when the input has no indexable PDFs.
    Raises LockLostError (without unlinking the stealer's lock) when the
    heartbeat observed our lock stolen mid-run.
    """
    lock = acquire_run_lock(output, dry_run, log)
    try:
        ctx = _run_incremental_inner(folder, output, dry_run, log, jobs,
                                     rekey, lock=lock)
    except LockLostError:
        # Our lock now belongs to the stealer: stop the heartbeat but never
        # unlink (release would delete the live holder's lock).
        abandon_run_lock(lock)
        raise
    except BaseException:
        release_run_lock(lock)
        raise
    if hold_lock:
        return ctx, lock
    release_run_lock(lock)
    return ctx


def run_repair(folder, *, dry_run, log, jobs=0, hold_lock=False):
    """Repair mode: re-apply the current decisions to an existing output tree.

    Reads nothing outside `folder`. The tree's own `_orphans/` copies are
    adoption candidates and its `_superseded/` copies are promotion candidates
    when they out-rank the live drawing or restore a missing sheet; naming,
    placement and revision ranking then run through the same functions a
    normal run uses, followed by a reconcile pass that moves folders onto the
    plan and removes verified duplicates. It never places a drawing that is
    absent from the tree, and never deletes anything without a byte-identical
    twin. Holds the output-tree lock like the other modes; with hold_lock=True
    returns (ctx, lock_path) and the caller releases it after its post-work.
    Raises NoPDFsFoundError when the folder holds no organized tree.

    Every parameter after `folder` is keyword-only: repair has no separate
    output folder (the tree is `folder`), and the natural cross-mode call
    `run_repair(tree, output, log)` used to bind a truthy Path to `dry_run` and
    return a clean exit 0 having changed nothing.
    """
    output = Path(folder).resolve()
    if not dry_run:
        # A dry run must not delete anything; the stale sweep is for real runs.
        sweep_stale_repair_staging(log)
    lock = acquire_run_lock(output, dry_run, log)
    try:
        ctx = _run_repair_inner(output, dry_run, log, jobs, lock=lock)
    except LockLostError:
        abandon_run_lock(lock)
        raise
    except BaseException:
        release_run_lock(lock)
        raise
    finally:
        sweep_repair_staging(output, log)
    if hold_lock:
        return ctx, lock
    release_run_lock(lock)
    return ctx


def _run_incremental_inner(folder, output, dry_run, log, jobs=0, rekey=False,
                           lock=None) -> RunContext:
    """run_incremental body (see run_incremental); split out so the lock spans all returns."""
    return _run_delta_inner(folder, output, dry_run, log, jobs, rekey, lock=lock)


def _run_repair_inner(output, dry_run, log, jobs=0, lock=None) -> RunContext:
    """run_repair body (see run_repair); split out so the lock spans all returns."""
    return _run_delta_inner(output, output, dry_run, log, jobs, False, lock=lock,
                            repair=True)


def _run_delta_inner(folder, output, dry_run, log, jobs=0, rekey=False,
                     lock=None, repair=False) -> RunContext:
    """Shared body of the incremental and repair runs.

    Both modes decide on a delta - incremental from the input folder's new
    PDFs, repair from the tree's own system folders - and both must then report
    the same whole-tree decisions; sharing this body is what keeps them from
    drifting (ADR 0001). Repair adds a reconcile pass after placement and skips
    the "nothing to do" shortcut, because a repair with no candidates still has
    a tree to put in order.
    """
    jobs = resolve_jobs(jobs)
    _ensure_lock(lock, log)
    sweep_supersede_staging(output, dry_run, log)
    sweep_stale_claims(output, dry_run, log)
    repair_meta = None
    if repair:
        prepared, repair_meta = _repair_prepare(output, dry_run, log)
    else:
        prepared = _incremental_prepare(folder, output, dry_run, log)
    organized, sup_dir, stored_orphans, new_index, chk_stems, \
        supersede_pairs, new_duplicates, total_copies, archived_new, scan_res = \
        prepared

    if not new_index and not repair:
        # No scan ran on this path, so there are no extraction warnings.
        _ensure_lock(lock, log)
        return _incremental_no_new_ctx(organized, chk_stems, output,
                                       total_copies, 0, log,
                                       stored_orphans=stored_orphans,
                                       archived=archived_new,
                                       dry_run=dry_run)

    new_index, watermarked_dupes, new_boms, bom_names, new_watermarks, \
        new_titleblocks, rekeyed, new_stems, org_stems, scanned_new, \
        all_stems, chk_stems, warning_count, _new_used_on, _new_names, \
        bom_votes = _incremental_scan_new_boms(
            new_index, new_duplicates, jobs, log, rekey, organized, chk_stems)
    # Archive the watermarked duplicates that lost the pre-scan resolution.
    _ensure_lock(lock, log)
    if watermarked_dupes:
        total_copies += copy_watermarked_duplicates(watermarked_dupes, output,
                                                    dry_run, log)
    unmatched = _incremental_new_missing(new_boms, all_stems, bom_names,
                                         chk_stems, log)

    org_boms, org_used_on, watermarks, tb_mismatches, org_names, \
        org_warnings = _incremental_scan_organized(
            organized, all_stems, jobs, bom_names, new_watermarks,
            new_titleblocks, unmatched, log, bom_votes)
    warning_count += org_warnings
    new_used_on, new_names = _incremental_scan_new_used_on(new_index, jobs,
                                                           all_stems, log,
                                                           cached_used_on=_new_used_on,
                                                           cached_names=_new_names)
    # A drawing's own NAME is a guess when OCR read it and absent when nothing
    # read it; either way a parent's parts list names it. Both scans above fed
    # bom_votes, so the vote now covers new + organized parents; re-key any
    # vote filed under a reference the BOM snap corrected.
    _snap_bom_votes(bom_votes, all_stems, log)
    bom_names = bom_name_winners(bom_votes)
    merged_names = prefer_bom_names(_merge_names(org_names or {}, new_names),
                                    _ocr_named_stems(), bom_names, all_stems)

    # Cap check BEFORE any supersede swaps/moves: a refused run must not
    # mutate the tree. Uses the pre-merge new graph (cycle-broken, like the
    # placement graphs below) plus organized-parent relations (invariant for
    # roots) and excludes roots that would be parked;
    # `_incremental_place` re-checks its exact placement set as a backstop.
    # The lock is re-checked first: swaps/moves/placements are the run's
    # heaviest mutations and must not start under a stolen lock.
    _ensure_lock(lock, log)
    pre_children, pre_parents = _graph_edges(new_boms, new_stems)
    _, pre_org_par = _graph_edges(org_boms, new_stems)
    pre_org_ch, _ = _graph_edges(new_boms, org_stems)
    # Break new-graph cycles before the cap fit: placement below runs on
    # cycle-broken graphs too, so an unbroken plan would fail closed on
    # phantom fan-out (a few breaks can collapse tens of thousands of cyclic
    # paths back under the cap). Silent here; _incremental_graph logs the
    # same breaks on the merged graph later.
    break_cycles(pre_children, pre_parents, lambda msg: None)
    pre_placeable = [r for r in sorted(s for s in new_stems
                                        if not pre_parents.get(s))
                      if _incremental_placeable(r, new_boms, pre_children,
                                                pre_org_par, pre_org_ch)]
    # Silent fit: the pre-check only gates the supersede swaps below. The
    # backstop in _incremental_place logs authoritatively, on the final
    # graph. Weights match the backstop (multi-claimant roots are copied
    # once per organized parent).
    _pre_weights = {r: max(1, len(pre_org_par.get(r, ())))
                    for r in pre_placeable}
    _pre_extra = _adopted_copy_estimate(pre_placeable, pre_children, pre_org_ch,
                                            organized, output, _pre_weights)
    _pre_kept, _pre_dropped = fit_roots_within_cap(pre_children,
                                                   pre_placeable,
                                                   lambda msg: None,
                                                   weights=_pre_weights,
                                                   extra=_pre_extra)
    refused = bool(_pre_dropped)

    # Placement-walk bound, on the same pre-supersede view. `fit_roots_within_cap`
    # above bounds the delta's copies; the name plan later walks every
    # root-to-node path of the MERGED tree, which a diamond DAG in the EXISTING
    # tree can multiply exponentially (measured: 3.1M paths / 805 MB on a
    # 20-level ladder). Checking it only at plan time meant a refused run had
    # already applied supersede swaps. _copy_counter memoizes by node, so this
    # costs O(nodes) instead of enumerating, and it runs before any swap/move.
    walk_refused = False
    if not refused:
        # Match the post-swap graph: the old revision is archived before the
        # plan is built, so a base-number reference that exact-matched it must
        # resolve to the incoming revision here too.
        _superseded_org = {o for _s, o in supersede_pairs}
        _walk_stems = (new_stems | org_stems) - _superseded_org
        _walk_boms = {k: v for k, v in {**org_boms, **new_boms}.items()
                      if k not in _superseded_org}
        _walk_children, _walk_parents = _graph_edges(_walk_boms, _walk_stems)
        # Break cycles first, exactly as the plan does: a cyclic graph has no
        # roots, and _copy_counter has no cycle guard, so an unbroken branchy
        # cycle is counted to TREE_MAX_DEPTH with every step multiplied - which
        # refused batches full mode places happily (and under-counted a closed
        # cycle component, letting the backstop fire after a supersede).
        break_cycles(_walk_children, _walk_parents, lambda msg: None)
        _walk_roots, _ = _classify_roots(_walk_stems, _walk_parents,
                                         _walk_boms, _walk_children)
        # The count is exact for the post-swap graph: pending supersede sources
        # are excluded above, so a reference that exact-matched the old plain
        # stem resolves to the incoming revision here as it will after the swap.
        _walk_planned = planned_copy_count(_walk_children, _walk_roots)
        _walk_limit = MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR
        if _walk_planned > _walk_limit:
            log(f"  WARNING: the merged tree plans {_walk_planned} paths, over "
                f"the {_walk_limit}-path planning bound - placement refused "
                "before any swap or move (check the BOM graph)")
            refused = True
            walk_refused = True

    swapped_old: list = []
    if refused:
        moved_dirs, total_moves = FolderMoves(), 0
        log("  supersede skipped: placement refused (no swaps/moves while over cap)")
    else:
        moved_dirs, total_moves, total_copies, all_stems, swapped_old = \
            _incremental_supersede(supersede_pairs, organized, org_boms,
                                   org_used_on, new_boms, new_used_on, new_index,
                                   new_stems, org_stems, all_stems, sup_dir,
                                   output, dry_run, log, total_copies,
                                   lock=lock)
    # One merged view of the tree this run reports on (new + already organized).
    # Every reported decision below - roots, USED ON bugs, USED ON mismatches -
    # is computed from it with the same functions full mode uses, so the two
    # modes cannot drift. Placement keeps its own delta inputs (new_* /
    # org_*_of): those drive the executor, not the report. Safe here because
    # nothing after this point mutates the stem/bom maps.
    merged_stems = new_stems | org_stems
    merged_boms = {**org_boms, **new_boms}
    merged_used_on = {**org_used_on, **new_used_on}
    merged_children, merged_parents = _graph_edges(merged_boms, merged_stems)

    new_children, new_parents, org_parents_of, org_children_of, used_on_bugs, \
        incr_mism, removed = _incremental_graph(
            new_boms, org_boms, new_used_on, org_used_on,
            all_stems, new_stems, org_stems, log,
            merged_children=merged_children, merged_used_on=merged_used_on,
            merged_names=merged_names)
    # Break merged-graph cycles only now, exactly like full mode: the report
    # cross-checks above run on the unbroken merged graph, and the break exists
    # for the root/orphan split. Silent, so the "Cycle detection (new PDFs)"
    # section keeps reporting new-graph breaks only (the pre-cap break above is
    # silent for the same reason).
    break_cycles(merged_children, merged_parents, lambda msg: None)
    # Reported roots use full mode's rule and scope: every stem of the merged
    # tree with no parent and at least one child edge. A refused run still
    # reports the tree's roots exactly as full mode does (it reports the plan,
    # not what it managed to place).
    reported_roots, _merged_orphans = _classify_roots(
        merged_stems, merged_parents, merged_boms, merged_children)

    # Folder names for the WHOLE merged tree, exactly as full mode plans them
    # (naming.py): one map, shared by placement and by organized-child
    # adoption. Planning per placement instead would name one stem differently
    # depending on which call created its folder first, and an incremental
    # subtree plan stops at the organized children hanging off it, so the
    # deepest copies were never measured - that is how a copy of an
    # already-organized child could land under a name that no longer fits the
    # path limit and be skipped instead of placed.
    merged_index = {s: pick_shallowest(paths) for s, paths in organized.items() if paths}
    merged_index.update(new_index)
    folder_names = None if walk_refused else plan_folder_names(
        merged_children, reported_roots, merged_index, merged_names, output, log)
    # Make the live tree's folder names match that plan before placing anything
    # else, so a folder an earlier run named for a smaller tree cannot survive
    # as a stale spelling (see normalize_folder_names). An over-cap plan is None
    # (see plan_folder_names): no renames, no placement, no reconcile - an empty
    # plan would fall back to bare stem names and move the whole tree.
    if folder_names is not None:
        renamed_folders = normalize_folder_names(organized, folder_names, output,
                                                 dry_run, log, moved_dirs,
                                                 lock=lock)
    else:
        renamed_folders = 0

    # Placement always runs through the backstop fit below (authoritative,
    # on the post-supersede graph): a full refusal drops everything there and
    # places nothing, exactly as before.
    if folder_names is not None:
        (worklist, total_copies, total_moves, place_refused,
         skipped) = _incremental_place(
            new_stems, new_parents, org_parents_of, org_children_of, new_children,
            new_index, new_names, new_boms, organized, moved_dirs, stored_orphans,
            output, dry_run, log, rekeyed, total_copies, total_moves,
            lock=lock, folder_names=folder_names)
    else:
        worklist, place_refused, skipped = [], True, []
    # Reported roots use the SAME rule and scope as full mode: every stem of
    # the merged tree (new + already organized) with no parent and at least one
    # child edge. Organically parentless BOM-less standalones are orphans, not
    # roots, and a refused run still reports the tree's roots exactly as full
    # mode does (it reports the plan, not what it managed to place).
    # A pre-check drop stays refused even when the backstop drops nothing
    # further (cycle breaking can only shrink the plan between the two).
    refused = refused or place_refused
    # Planned tree stems for orphan retirement: in dry-run the placements
    # above logged but copied nothing, so a stored orphan adopted by this
    # run's placement is not yet live on disk. BOM-less standalone roots stay
    # parked in _orphans/ (never placed) and are excluded, as are oversized
    # roots skipped above. A fully refused run must not advertise retirements
    # for placements that never happened.
    skipped_stems = {r for r, _ in skipped}
    planned = set()
    for _r in worklist:
        if _r in skipped_stems:
            continue
        if not _incremental_placeable(_r, new_boms, new_children,
                                      org_parents_of, org_children_of):
            continue
        _stack = [_r]
        while _stack:
            _s = _stack.pop()
            if _s in planned:
                continue
            planned.add(_s)
            _stack.extend(new_children.get(_s, ()))
    reused_scan_res = scan_res if (dry_run or (total_copies == 0 and total_moves == 0)) else None
    # Finalize retires adopted orphans (deletes), so the lock is re-checked.
    _ensure_lock(lock, log)
    if repair:
        if folder_names is not None:
            repair_counts, repair_review, live_index = _repair_reconcile(
                output, merged_children, reported_roots, folder_names, dry_run, log,
                moved_dirs, lock=lock)
        else:
            # Over the planning bound: no plan exists, so reconcile would have
            # to guess. Leave the reconcile pass alone (supersede swaps from
            # this run, if any, already applied) and hand it to a human (exit 2).
            repair_counts = {"moved": 0, "copied": 0, "removed": 0,
                             "emptied": 0}
            repair_review = ["planning bound: the tree's root-to-node paths "
                             f"exceed {MAX_PLANNED_COPIES * PLAN_PLACEMENT_FACTOR} "
                             "- reconcile skipped"]
            live_index = organized
        repair_counts["adopted"] = len(planned)
        repair_counts["renamed"] = renamed_folders
        promoted_stems = {s for s, o in supersede_pairs if o in set(swapped_old)}
        repair_counts["promoted"] = len(promoted_stems)
        if repair_meta is not None:
            repair_counts["promoted_archive_cleaned"] = _finish_repair_promotions(
                live_index, repair_meta["archived"], promoted_stems,
                output, dry_run, log, lock=lock)
        # The tree changed after placement: finalize must see it as it is now,
        # so do not hand it the pre-reconcile scan.
        reused_scan_res = None
    ctx = _incremental_finalize(
        stored_orphans=stored_orphans,
        output=output,
        dry_run=dry_run,
        log=log,
        scanned_new=scanned_new,
        organized=organized,
        new_roots=reported_roots,
        new_children=new_children,
        removed=removed,
        total_copies=total_copies,
        total_moves=total_moves,
        warning_count=warning_count,
        unmatched=unmatched,
        chk_stems=chk_stems,
        roots_used_on=merged_used_on,
        incr_mism=incr_mism,
        used_on_bugs=used_on_bugs,
        tb_mismatches=tb_mismatches,
        watermarks=watermarks,
        new_names=new_names,
        bom_names=bom_names,
        org_names=org_names,
        merged_names=merged_names,
        superseded=archived_new | set(swapped_old),
        planned=planned,
        scan_res=reused_scan_res,
        refused=refused, skipped=skipped,
        repair=repair)
    if repair:
        repair_cleanup(repair_meta, log)
        ctx["repair"] = repair_counts
        ctx["repair"]["review"] = repair_review
        ctx["repair_required"] = bool(repair_review)
        _log_repair_block(repair_counts, repair_review, log)
    return ctx
