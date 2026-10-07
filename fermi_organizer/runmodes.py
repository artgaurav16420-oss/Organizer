#!/usr/bin/env python3
"""Full and incremental organization runs."""
import filecmp
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

from .config import canonical_stem, filename_title, TB_NUM_VAL_RE, is_processable_ref
from .extraction import (resolve_jobs, bom_task,
                         title_task, org_task, dup_meta_task, run_parallel, OCR)
from .graph import (is_chk_stem, revision_rank, split_superseded, match_pdfs,
                    break_cycles, find_used_on_mismatches, find_used_on_bugs,
                    collect_reachable, snap_ref)
from .naming import build_folder_names
from .fsops import (place_files, build_pdf_index, pick_shallowest,
                    scan_output_tree, find_organized_pdfs, find_latest_report,
                    copy_superseded, copy_watermarked_duplicates, copy_orphans,
                    retire_adopted_orphans)


class RunCounters(TypedDict):
    """Workbook counters for a run: scanned/roots/copies/cycles/warnings."""
    scanned: int
    roots: int
    copies: int
    cycles: int
    warnings: int


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
    in the filename), and the title (only when the filename carries a title
    and it shares no token with the title-block NAME). Returns
    [(stem, field, filename_value, titleblock_value)] sorted.
    """
    out = []
    for stem, (fname, number, rev, name, _scanned) in sorted(titleblocks.items()):
        parts = stem.split("_")
        srev = parts[1] if len(parts) > 1 and len(parts[1]) == 1 else ""
        if number and number != parts[0]:
            out.append((stem, "number", parts[0], number))
        if rev is not None:
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


def _snap_values(stem, vals, stems, log):
    """Correct OCR near-miss refs to a unique known stem (in place per value)."""
    out = []
    for v in vals:
        if is_processable_ref(v) and not match_pdfs(v, stems):
            s = snap_ref(v, stems)
            if s:
                log(f"  {stem}: OCR ref {v} corrected to {s}")
                v = s
        out.append(v)
    return out


def _snap_boms(bom_of, stems, log):
    """Snap OCR near-misses in BOM values before edge/missing computation."""
    for stem in sorted(bom_of):
        bom_of[stem] = _snap_values(stem, bom_of[stem], stems, log)


def _snap_used_on(used_on_of, stems, log):
    """Snap OCR near-misses in USED ON values (keeps cross-checks aligned)."""
    for stem in sorted(used_on_of):
        used_on_of[stem] = _snap_values(stem, used_on_of[stem], stems, log)


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
    for stem in sorted(list(titleblocks)):
        fname, number, rev, _name, scanned = titleblocks[stem]
        base = stem.split("_")[0]
        if scanned or not number or number == base:
            continue
        if not TB_NUM_VAL_RE.match(number):
            continue
        if match_pdfs(number, stems):
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
    for p, res in zip(cand_paths, results):
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
def _scan_boms(items, jobs, log, require_desc):
    """BOM extraction over sorted (stem, pdf) items.

    Logs per-PDF errors/warnings and the unique-FERMI count line. Returns
    ({stem: [values]}, {ref_value: NAME}, {stem: watermark evidence},
    {stem: (filename, title block number, revision, NAME, scanned)});
    require_desc keeps the run_full policy of storing a NAME only when the
    BOM row carries a description.
    """
    bom_of = {}
    bom_names = {}
    watermarks = {}
    titleblocks = {}
    results = run_parallel(bom_task, [str(p) for _, p in items],
                           jobs, OCR.enabled)
    for (stem, pdf), res in zip(items, results):
        if res[0] == "error":
            log(f"  {stem}: ERROR reading PDF: {res[1]}")
            continue
        _, entries, method, issues, watermark, number, rev, name, scanned = res
        for msg in issues:
            log(f"  {stem}: EXTRACTION WARNING: {msg}")
        if watermark:
            watermarks[stem] = watermark
        titleblocks[stem] = (pdf.stem, number, rev, name, scanned)
        raw_values = sorted({v for v, *_rest in entries})
        bom_of[stem] = raw_values
        for e in entries:
            if len(e) == 4 and e[0] not in bom_names and (e[3] or not require_desc):
                bom_names[e[0]] = e[3]
        log(f"  {stem}: {len(raw_values)} unique FERMI# ({method})")
    log("")
    return bom_of, bom_names, watermarks, titleblocks


def _scan_used_on(items, jobs, header, log):
    """USED ON + NAME extraction. Logs header + per-PDF lines + blank.
    Returns (used_on_of, names_of)."""
    used_on_of = {}
    names_of = {}
    log(header)
    results = run_parallel(title_task, [str(p) for _, p in items],
                           jobs, OCR.enabled)
    for (stem, _pdf), res in zip(items, results):
        if res[0] == "error":
            log(f"  {stem}: ERROR reading PDF: {res[1]}")
            continue
        _, used, name = res
        used_on_of[stem] = used
        if name:
            names_of[stem] = name
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
    for stem, vals in sorted(boms.items()):
        for v in vals:
            if not is_processable_ref(v):
                continue
            for m in match_pdfs(v, stems):
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


def _collect_used_on_bugs_incremental(new_boms, org_boms, new_used_on, org_used_on,
                                     all_stems, log):
    """Report USED ON bugs for an incremental run (BOM is the only truth)."""
    bom_children = defaultdict(set)
    for boms in (new_boms, org_boms):
        for parent, vals in boms.items():
            for v in vals:
                if not is_processable_ref(v):
                    continue
                for c in match_pdfs(v, all_stems):
                    if c != parent:
                        bom_children[parent].add(c)
    used_on_of = dict(org_used_on)
    used_on_of.update(new_used_on)
    bugs = find_used_on_bugs(bom_children, used_on_of, all_stems)
    if bugs:
        _log_used_on_bugs(bugs, log)
    return bugs


# ---------------------------------------------------------------------------
# Incremental state: candidate set, revision conflicts, supersede swaps
# ---------------------------------------------------------------------------
def _collect_incremental_candidates(scan_index, output, scan_res=None):
    """Organized stems, superseded/orphan state, and the incremental candidate
    set. Stored orphans are pulled in as adoption candidates (a parent arriving
    in this batch may adopt them). Returns
    (organized, sup_dir, stored_orphans, new_index, chk_stems).
    Accepts an optional `scan_res` (precomputed `scan_output_tree` result)
    to avoid redundant filesystem scans.
    """
    if scan_res is None:
        scan_res = scan_output_tree(output)
    organized = find_organized_pdfs(output, scan_res=scan_res)
    # Stems already archived in _superseded/ are never candidates again.
    sup_dir = output / "_superseded"
    already_sup = {s for p in scan_res["sup"]
                   if (s := canonical_stem(p.stem))}
    # Stored orphans from previous full runs: they are adoption candidates.
    # If a new/organized parent references one, it is placed into the tree and
    # its _orphans/ copy is retired; if not, it stays parked.
    orphans_dir = output / "_orphans"
    stored_orphans = {s: p for p in scan_res["orph"]
                      if (s := canonical_stem(p.stem))}
    new_index = {s: p for s, p in scan_index.items() if s not in organized and s not in already_sup}
    for o, p in stored_orphans.items():
        if o in new_index:
            # same stem arrived both at top level and in _orphans - top level wins
            continue
        new_index[o] = p
    # Unapproved CHK drawings among the new arrivals, captured BEFORE revision
    # filtering (a CHK whose DWG exists is filtered to _superseded below, but the
    # user still needs the Teamcenter lookup in the report)
    chk_stems = [s for s in sorted(new_index) if is_chk_stem(s)]
    return organized, sup_dir, stored_orphans, new_index, chk_stems


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


def _scan_organized(organized, all_stems, jobs, bom_names, log):
    """BOM + USED ON scan over organized PDFs (placement context). Logs the
    missing-reference block; returns
    (org_boms, org_used_on, org_unmatched, org_watermarks, org_titleblocks)."""
    org_boms = {}
    org_used_on = {}
    org_watermarks = {}
    org_titleblocks = {}
    log("--- Scanning BOM + USED ON (organized PDFs, for placement) ---")
    org_items = [(stem, pick_shallowest(organized[stem]))
                 for stem in sorted(organized)]
    org_results = run_parallel(org_task, [str(p) for _, p in org_items],
                               jobs, OCR.enabled)
    for (stem, pdf), res in zip(org_items, org_results):
        if res[0] == "error":
            log(f"  {stem}: ERROR reading PDF: {res[1]}")
            continue
        _, entries, method, used, issues, watermark, number, rev, name, scanned = res
        for msg in issues:
            log(f"  {stem}: EXTRACTION WARNING: {msg}")
        if watermark:
            org_watermarks[stem] = watermark
        org_titleblocks[stem] = (pdf.stem, number, rev, name, scanned)
        org_boms[stem] = sorted({v for v, *_rest in entries})
        org_used_on[stem] = used
        for e in entries:
            if len(e) == 4 and e[0] not in bom_names:
                bom_names[e[0]] = e[3]
        log(f"  {stem}: {len(entries)} BOM row(s), {len(org_boms[stem])} unique FERMI# ({method})")
    # Missing refs are reported across ALL drawings (new + organized), so the
    # Teamcenter download list in the report/workbook is complete every run.
    org_unmatched = []
    for ostem, vals in sorted(org_boms.items()):
        for v in vals:
            if not is_processable_ref(v):
                continue
            if not match_pdfs(v, all_stems):
                org_unmatched.append((ostem, v))
    if org_unmatched:
        _log_missing_refs(f"Skipped {len(org_unmatched)} missing reference(s) from organized drawing(s):",
                          org_unmatched, bom_names, log)
        log("")
    log("")
    return org_boms, org_used_on, org_unmatched, org_watermarks, org_titleblocks


def _swap_revision_files(old_paths, new_pdf, sup_dir, output, dry_run, log):
    """Archive old revision copies and place the new revision in their spot.
    Returns (new_locations, copies_written)."""
    moved = []
    copies = 0
    for p in old_paths:
        target_old = sup_dir / p.name
        target_new = p.parent / new_pdf.name
        staging = target_new.with_name(f"{target_new.name}.supersede_tmp.{os.getpid()}")
        rel_old = p.relative_to(output)
        # Archive collision: the target may already exist with different
        # content (never delete the tree copy without archiving it). Equal
        # bytes = assume duplicate, keep the old skip behavior; OSError on
        # compare means differ (safer). Differing content archives under a
        # numeric suffix (<stem>.1.pdf, incrementing until free).
        archive_target = target_old
        archived = not target_old.exists()
        if not archived:
            try:
                same = filecmp.cmp(target_old, p, shallow=False)
            except OSError:
                same = False
            if same:
                archived = False
            else:
                base = target_old.stem
                ext = target_old.suffix
                n = 1
                candidate = sup_dir / f"{base}.{n}{ext}"
                while candidate.exists():
                    n += 1
                    candidate = sup_dir / f"{base}.{n}{ext}"
                archive_target = candidate
                archived = True
        if dry_run:
            note = "" if archived else " (archive skipped: exists)"
            log(f"  [DRY-RUN] supersede: {rel_old} -> _superseded/{archive_target.name}; "
                f"copy {new_pdf.name} -> {target_new.relative_to(output)}{note}")
        else:
            # Atomic-ish swap: stage the new revision next to its target first,
            # then archive the old revision, then replace, then drop the old file.
            try:
                sup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(new_pdf, staging)
                if archived:
                    shutil.copy2(p, archive_target)
                os.replace(staging, target_new)
            except OSError as e:
                if staging.exists():
                    try:
                        staging.unlink()
                    except OSError:
                        # Best-effort staging cleanup: the supersede failure
                        # below is logged and is the actionable error.
                        pass
                log(f"  WARNING: supersede failed for {rel_old}: {e}")
                continue
            # New revision is in place; old removal is best-effort.
            try:
                p.unlink()
            except OSError as e:
                log(f"  WARNING: {rel_old}: old revision left in tree "
                    f"(could not remove): {e}")
            log(f"  superseded: {rel_old} -> _superseded/{archive_target.name}")
        copies += 2 if archived else 1
        moved.append(target_new)
    return moved, copies


def _move_children_under_superseding(swapped, organized, org_boms, org_stems,
                                     output, dry_run, moved_dirs, log):
    """Move organized parts referenced by a superseding revision under its
    folder. Mutates moved_dirs; returns the number of moves."""
    moves = 0
    for s2 in swapped:
        s_paths = organized.get(s2, [])
        if not s_paths:
            continue
        s_folder = pick_shallowest(s_paths).parent
        for v in org_boms.get(s2, []):
            if not is_processable_ref(v):
                continue
            for c in match_pdfs(v, org_stems):
                if c == s2:
                    continue
                child_paths = organized.get(c, [])
                if not child_paths:
                    continue
                child_folder = pick_shallowest(child_paths).parent
                if child_folder == s_folder or child_folder.is_relative_to(s_folder):
                    continue
                target_path = s_folder / child_folder.name
                if target_path.exists():
                    log(f"  WARNING: {c}: destination exists, skipping move (manual review): "
                        f"{target_path.relative_to(output)}")
                    continue
                if dry_run:
                    log(f"  [DRY-RUN] move {child_folder.relative_to(output)} -> {target_path.relative_to(output)}")
                else:
                    try:
                        shutil.move(str(child_folder), str(target_path))
                    except OSError as e:
                        log(f"  WARNING: move failed for {c}: {e}")
                        continue
                    log(f"  moved: {child_folder.relative_to(output)} -> {target_path.relative_to(output)}")
                moved_dirs[child_folder] = target_path
                moves += 1
    return moves


def _current_folder_of(stem, organized, output, moved_dirs):
    """Shallowest live folder for a stem, remapped through supersede moves."""
    paths = [p for p in organized.get(stem, [])
             if not p.relative_to(output).parts[0].startswith("_")]
    if not paths:
        return output
    pdf = pick_shallowest(paths)
    rel = pdf.relative_to(output)
    old_folder = output.joinpath(*rel.parts[:-1])
    # Remap folders that were moved under a superseding revision's folder.
    for old, new in moved_dirs.items():
        if old_folder == old or old_folder.is_relative_to(old):
            return new.joinpath(*old_folder.relative_to(old).parts)
    return old_folder


def _place_subassembly_of(R, P, org_par, new_children, new_index, new_names,
                          base, output, dry_run, log, renames=None):
    """R is a sub-assembly of organized part(s) P; place under P. Returns copies."""
    if len(org_par) > 1:
        log(f"  {R}: referenced by multiple organized parts {org_par}; placing under {P}")
    fn = build_folder_names(new_children, [R], new_index, new_names, base, log)
    log(f"  {R}: sub-assembly of organized part {P} -> {(base / fn.get(R, R)).relative_to(output)}")
    return place_files(new_children, [R], new_index, base, dry_run, log, new_names, fn,
                       renames=renames)


def _place_above_organized_children(R, org_child, new_children, new_index, new_names,
                                    organized, moved_dirs, output, dry_run, log,
                                    renames=None):
    """R is a higher-level assembly referencing organized part(s): create its
    folder, move the organized child folders under it, then place R's subtree.
    Mutates moved_dirs; returns (copies, moves)."""
    copies = 0
    moves = 0
    log(f"  {R}: higher-level assembly referencing organized part(s): {org_child}")
    fn = build_folder_names(new_children, [R], new_index, new_names, output, log)
    target_folder = output / fn.get(R, R)
    can_move = True
    if dry_run:
        log(f"  [DRY-RUN] mkdir {target_folder}")
    else:
        try:
            target_folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"  WARNING: {R}: mkdir failed ({target_folder}): {e} - skipping child moves")
            can_move = False
    for c in (org_child if can_move else ()):
        # Move the child's folder, not just its PDF.
        child_paths = organized.get(c, [])
        if not child_paths:
            continue
        child_path = pick_shallowest(child_paths)
        child_folder = child_path.parent
        already_moved = False
        for old, new in moved_dirs.items():
            if child_folder == old or child_folder.is_relative_to(old):
                already_moved = True
                break
        if already_moved:
            log(f"  {c}: already moved, skipping")
            continue
        # Guard against destination collisions before moving the child's folder.
        target_path = target_folder / child_folder.name
        if target_path.exists():
            same = False
            try:
                same = target_path.resolve() == child_folder.resolve()
            except OSError:
                same = False
            if same:
                log(f"  {c}: already at destination, skipping move")
                continue
            log(f"  WARNING: {c}: destination exists, skipping move (manual review): "
                f"{target_path.relative_to(output)}")
            continue
        if dry_run:
            log(f"  [DRY-RUN] move {child_folder.relative_to(output)} -> {target_path.relative_to(output)}")
            moved_dirs[child_folder] = target_path
        else:
            try:
                shutil.move(str(child_folder), str(target_path))
            except OSError as e:
                log(f"  WARNING: move failed for {c}: {e}")
                continue
            moved_dirs[child_folder] = target_path
        moves += 1
    copies += place_files(new_children, [R], new_index, output, dry_run, log, new_names, fn,
                          renames=renames)
    return copies, moves


def _place_standalone_new_root(R, new_children, new_index, new_names,
                               output, dry_run, log, renames=None):
    """Standalone new root with a BOM/children -> new root folder. Returns copies."""
    fn = build_folder_names(new_children, [R], new_index, new_names, output, log)
    log(f"  {R}: standalone assembly -> new root folder {fn.get(R, R)}/")
    return place_files(new_children, [R], new_index, output, dry_run, log, new_names, fn,
                       renames=renames)


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


def _full_prepare_index(folder, jobs, log):
    """Build index, report CHK/superseded, resolve same-revision duplicates."""
    index, duplicates = build_pdf_index(folder, log)
    if not index:
        log("No PDFs found in folder.")
        raise NoPDFsFoundError("No PDFs found in folder.")
    # Unapproved CHK drawings present at scan time (before revision filtering):
    # a CHK is reported even when an approved DWG of the same base exists, since
    # the DWG superseded it and the user still needs the Teamcenter lookup.
    chk_stems = [s for s in sorted(index) if is_chk_stem(s)]
    index, old = split_superseded(index)
    if old:
        _log_superseded_preface(old, log)
    log(f"Found {len(index)} active PDF(s)")
    log("")
    # Same-revision duplicates: pick the tree copy (clean > text-layer >
    # shallowest) BEFORE extraction, so BOM/NAME/title block come from the
    # copy that will actually be placed.
    index, watermarked_dupes = _resolve_duplicates(index, duplicates, jobs, log)
    return index, chk_stems, old, watermarked_dupes


def _full_scan_boms(index, jobs, log, rekey, chk_stems):
    """BOM scan + opt-in re-key + watermark/title-block logs + OCR snap."""
    log("--- Scanning BOM tables ---")
    bom_of, bom_names, watermarks, titleblocks = _scan_boms(
        sorted(index.items()), jobs, log, require_desc=True)
    rekeyed = {}
    if rekey:
        index, bom_of, watermarks, titleblocks, rekeyed = _rekey_misnamed(
            index, bom_of, watermarks, titleblocks, set(index), log)
        chk_stems = [rekeyed.get(s, s) for s in chk_stems]
    _log_watermarks(watermarks, log)
    # Filename vs title block: number, revision, title (report only; with
    # --rekey-titleblock a misnamed file is re-keyed first).
    tb_mismatches = _titleblock_mismatches(titleblocks, log)
    # OCR near-miss refs: correct against the known stems before edges.
    _snap_boms(bom_of, index, log)
    return index, bom_of, bom_names, watermarks, tb_mismatches, rekeyed, chk_stems


def _full_build_edges(bom_of, index, bom_names, log):
    """BOM edges + missing/FC logs. Returns (children, parents, unmatched)."""
    children = defaultdict(set)
    parents = defaultdict(set)
    unmatched = []
    skipped_fc = set()
    for stem, raw_values in sorted(bom_of.items()):
        for val in raw_values:
            if val.startswith("FC"):
                skipped_fc.add(val)
                continue
            if not is_processable_ref(val):
                continue
            matches = match_pdfs(val, index)
            if not matches:
                unmatched.append((stem, val))
                continue
            for m in matches:
                if m == stem:
                    continue
                children[stem].add(m)
                parents[m].add(stem)
    if skipped_fc:
        _log_skipped_fc(skipped_fc, log)
    if unmatched:
        _log_missing_refs(f"Skipped {len(unmatched)} BOM reference(s) with no matching PDF:",
                          unmatched, bom_names, log)
        log("")
    return children, parents, unmatched


def _full_scan_used_on(index, jobs, log):
    """USED ON + NAME scan + OCR snap + missing-NAME log."""
    used_on_of, names_of = _scan_used_on(sorted(index.items()), jobs,
                                         "--- Scanning USED ON fields ---", log)
    _snap_used_on(used_on_of, index, log)
    _log_missing_names(index, names_of, log)
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
                names_of, output, dry_run, log, rekeyed):
    """Folder creation + orphan/superseded/duplicate copies."""
    log("--- Creating folder structure ---")
    folder_names = build_folder_names(children, roots, index, names_of, output, log)
    renames = {new: f"{new}.pdf" for new in rekeyed.values()}
    total_copies = place_files(children, roots, index, output, dry_run, log, names_of,
                               folder_names, renames=renames)
    # Orphans -> _orphans/ so they are stored, not just logged: when a future
    # input batch supplies their parent, incremental can still match them
    # (they are scanned there like any other PDF and then placed properly).
    if orphans:
        total_copies += copy_orphans(orphans, index, output, dry_run, log,
                                     renames=renames)
    if old:
        total_copies += copy_superseded(old, output, dry_run, log, overwrite=True)
    if watermarked_dupes:
        total_copies += copy_watermarked_duplicates(watermarked_dupes, output,
                                                    dry_run, log)
    log("")
    return total_copies


def _full_finalize(roots, children, index, orphans, removed, total_copies,
                   warning_count, unmatched, chk_stems, used_on_of, used_mism,
                   used_on_bugs, tb_mismatches, watermarks, names_of,
                   bom_names, log) -> RunContext:
    """Unplaced log + summary + structured RunContext for the workbook."""
    _log_unplaced(roots, children, index, orphans, log)
    _log_summary([
        "--- Summary ---",
        f"  PDFs scanned:        {len(index)}",
        f"  Root assemblies:     {len(roots)}",
        f"  BOM edges kept:      {sum(len(v) for v in children.values())}",
        f"  Cycles broken:       {len(removed)}",
        f"  PDF copies written:  {total_copies}",
    ], log)
    return {
        "counters": {
            "scanned": len(index),
            "roots": len(roots),
            "copies": total_copies,
            "cycles": len(removed),
            "warnings": warning_count,
        },
        "missing": unmatched,
        "chk": chk_stems,
        "orphans": orphans,
        "roots": [(r, sorted(set(used_on_of.get(r, [])))) for r in roots],
        "used_on_mismatches": used_mism,
        "used_on_bugs": used_on_bugs,
        "titleblock_mismatches": tb_mismatches,
        "scanned": OCR.scanned_stems(),
        "watermarks": sorted(watermarks.items()),
        "names": _ctx_names(names_of, unmatched, bom_names),
    }


def run_full(folder, output, dry_run, log, jobs=0, rekey=False) -> RunContext:
    """Full run: index all of `folder` and place the whole tree under `output`.

    Returns the structured run context consumed by the Excel workbook:
    counters {scanned, roots, copies, cycles, warnings},
    missing [(referencing stem, missing ref)], chk [stem], orphans [stem],
    roots [(stem, [used_on])], used_on_mismatches [(parent, child, actual)],
    used_on_bugs [(parent, child)],
    titleblock_mismatches [(stem, field, filename value, title-block value)],
    scanned [(stem, [pages])], watermarks [(stem, evidence)],
    names {stem/ref: NAME}. Raises NoPDFsFoundError on an empty input.
    """
    # Count 'EXTRACTION WARNING' lines for the workbook context (the same
    # number the workbook previously recovered by parsing the log text).
    warning_count = 0
    base_log = log

    def log(msg):
        nonlocal warning_count
        if "EXTRACTION WARNING" in msg:
            warning_count += 1
        base_log(msg)

    jobs = resolve_jobs(jobs)
    index, chk_stems, old, watermarked_dupes = _full_prepare_index(folder, jobs, log)
    index, bom_of, bom_names, watermarks, tb_mismatches, rekeyed, chk_stems = \
        _full_scan_boms(index, jobs, log, rekey, chk_stems)

    children, parents, unmatched = _full_build_edges(bom_of, index, bom_names, log)

    # Unapproved CHK drawings: approved DWG should be downloaded from Teamcenter
    if chk_stems:
        _log_chk_block(chk_stems, log)

    used_on_of, names_of = _full_scan_used_on(index, jobs, log)

    used_on_bugs, used_mism, removed, roots, orphans = _full_analyze(
        bom_of, index, children, parents, used_on_of, names_of, log)
    total_copies = _full_place(children, roots, orphans, old,
                               watermarked_dupes, index, names_of, output,
                               dry_run, log, rekeyed)
    return _full_finalize(roots, children, index, orphans, removed,
                          total_copies, warning_count, unmatched, chk_stems,
                          used_on_of, used_mism, used_on_bugs, tb_mismatches,
                          watermarks, names_of, bom_names, log)


def _incremental_prepare(folder, output, dry_run, log):
    """Collect candidates, log counts, archive superseded among new PDFs."""
    scan_index, top_duplicates = build_pdf_index(folder, log)
    if not scan_index:
        log("No PDFs found in folder.")
        raise NoPDFsFoundError("No PDFs found in folder.")
    scan_res = scan_output_tree(output)
    organized, sup_dir, stored_orphans, new_index, chk_stems = \
        _collect_incremental_candidates(scan_index, output, scan_res=scan_res)
    new_index, old_new = split_superseded(new_index)
    supersede_pairs = _detect_supersede_pairs(new_index, old_new, organized, log)
    new_duplicates = {stem: paths for stem, paths in top_duplicates.items()
                      if stem in new_index}
    if old_new:
        log(f"Found {len(old_new)} superseded revision(s) among new PDFs")
    log(f"PDFs at top level:              {len(scan_index)}")
    log(f"Already organized (subfolders): {len(organized)}")
    log(f"New PDFs to process:            {len(new_index)}")
    _log_previous_report(output, log)
    log("")
    # Archive superseded copies even when there is nothing new to place.
    total_copies = copy_superseded(old_new, output, dry_run, log)
    return (organized, sup_dir, stored_orphans, new_index, chk_stems,
            supersede_pairs, new_duplicates, total_copies, set(old_new), scan_res)


def _incremental_no_new_ctx(organized, chk_stems, output, total_copies,
                            warning_count, log) -> RunContext:
    """Early-exit context when there is nothing new to place."""
    log("No new PDFs at top level - nothing to do.")
    log("")
    # CHK arrivals are still reported even when there is nothing to place
    if chk_stems:
        _log_chk_block(chk_stems, log)
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
    new_boms, bom_names, new_watermarks, new_titleblocks = _scan_boms(
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
    _log_watermarks(new_watermarks, log)
    # OCR near-miss refs: correct against the known stems before edges.
    _snap_boms(new_boms, all_stems, log)
    return (new_index, watermarked_dupes, new_boms, bom_names,
            new_watermarks, new_titleblocks, rekeyed, new_stems, org_stems,
            scanned_new, all_stems, chk_stems)


def _incremental_new_missing(new_boms, all_stems, bom_names, chk_stems, log):
    """Missing/FC logs for new BOMs + CHK block. Returns unmatched."""
    skipped_fc = sorted({v for vals in new_boms.values() for v in vals if v.startswith("FC")})
    if skipped_fc:
        _log_skipped_fc(skipped_fc, log)
    unmatched = []
    for nstem, vals in sorted(new_boms.items()):
        for v in vals:
            if not is_processable_ref(v):
                continue
            if not match_pdfs(v, all_stems):
                unmatched.append((nstem, v))
    if unmatched:
        _log_missing_refs(f"Skipped {len(unmatched)} BOM reference(s) with no matching PDF:",
                          unmatched, bom_names, log)
    log("")
    # Unapproved CHK drawings among the new PDFs: approved DWG should be
    # downloaded from Teamcenter (same base drawing number, no CHK suffix)
    if chk_stems:
        _log_chk_block(chk_stems, log)
    return unmatched


def _incremental_scan_organized(organized, all_stems, jobs, bom_names,
                                new_watermarks, new_titleblocks, unmatched, log):
    """Organized scan + snaps + watermark/title-block merge."""
    org_boms = {}
    org_used_on = {}
    org_watermarks = {}
    org_titleblocks = {}
    if organized:
        org_boms, org_used_on, org_unmatched, org_watermarks, org_titleblocks = \
            _scan_organized(organized, all_stems, jobs, bom_names, log)
        _snap_boms(org_boms, all_stems, log)
        _snap_used_on(org_used_on, all_stems, log)
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
    return org_boms, org_used_on, watermarks, tb_mismatches


def _incremental_scan_new_used_on(new_index, jobs, all_stems, log):
    """USED ON + NAME scan for new PDFs + OCR snap."""
    new_used_on, new_names = _scan_used_on(sorted(new_index.items()), jobs,
                                           "--- Scanning USED ON fields (new PDFs) ---", log)
    _snap_used_on(new_used_on, all_stems, log)
    return new_used_on, new_names


def _incremental_supersede(supersede_pairs, organized, org_boms, org_used_on,
                           new_boms, new_used_on, new_index, new_stems,
                           org_stems, all_stems, sup_dir, output, dry_run,
                           log, total_copies):
    """In-place revision swaps + child moves. Returns (moved_dirs, moves, copies, stems, swapped_old)."""
    # Supersede in place. When a new revision supersedes an organized one, the
    # old revision's PDF(s) move to _superseded/ and the new revision's PDF takes their
    # place in the tree. The new stem is then treated as organized for this run.
    moved_dirs = {}  # old_folder -> new_folder
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
                                                 dry_run, log)
            total_copies += copies
            org_boms[s] = new_boms.pop(s, [])
            org_used_on[s] = new_used_on.pop(s, [])
            org_boms.pop(o, None)
            org_used_on.pop(o, None)
            organized.pop(o, None)
            new_index.pop(s, None)
            new_stems.discard(s)
            if moved:
                organized[s] = moved
                swapped_old.append(o)
            org_stems.discard(o)
            org_stems.add(s)
            swapped.append(s)
        if swapped:
            log(f"  {len(swapped)} organized revision(s) superseded in place")
            all_stems = new_stems | org_stems
            # Move organized parts referenced by a superseding revision under its folder
            total_moves = _move_children_under_superseding(swapped, organized, org_boms,
                                                           org_stems, output, dry_run,
                                                           moved_dirs, log)
    return moved_dirs, total_moves, total_copies, all_stems, swapped_old


def _incremental_graph(new_boms, org_boms, new_used_on, org_used_on,
                       new_names, all_stems, new_stems, org_stems, log):
    """Graph edges + USED ON cross-checks + cycles + relationship notes."""
    _, org_parents_of = _graph_edges(org_boms, new_stems)
    org_children_of, _ = _graph_edges(new_boms, org_stems)
    new_children, new_parents = _graph_edges(new_boms, new_stems)
    # BOM is the only source of truth for parent-child edges; USED ON is a
    # cross-check only. Report children whose USED ON names a parent the
    # parent's BOM does not list as USED ON bugs (never placement edges).
    used_on_bugs = _collect_used_on_bugs_incremental(
        new_boms, org_boms, new_used_on, org_used_on, all_stems, log)
    # USED ON consistency (BOM-derived edges only): parent BOM lists the part
    # but the part USED ON omits the parent.
    incr_bom_edges, _ = _graph_edges(new_boms, all_stems)
    org_edges, _ = _graph_edges(org_boms, new_stems)
    for ostem, children_set in org_edges.items():
        incr_bom_edges[ostem] |= children_set
    used_all = dict(org_used_on)
    used_all.update(new_used_on)
    incr_mism = find_used_on_mismatches(incr_bom_edges, used_all)
    if incr_mism:
        _log_used_on_mismatches(incr_mism, new_names, log)
    removed = _log_cycle_detection("--- Cycle detection (new PDFs) ---",
                                   new_children, new_parents, log)
    _log_relationship_notes(new_stems, new_parents, org_parents_of, org_children_of, log)
    return (new_children, new_parents, org_parents_of, org_children_of,
            used_on_bugs, incr_mism, removed)


def _incremental_place(new_stems, new_parents, org_parents_of, org_children_of,
                       new_children, new_index, new_names, new_boms,
                       organized, moved_dirs, stored_orphans, output, dry_run,
                       log, rekeyed, total_copies, total_moves):
    """Placement decisions for new roots. Returns (new_roots, copies, moves)."""
    new_roots = sorted(s for s in new_stems if not new_parents.get(s))
    log("--- Placement decisions ---")
    renames = {new: f"{new}.pdf" for new in rekeyed.values()}
    standalone_orphans = []
    for R in new_roots:
        org_par = sorted(org_parents_of.get(R, ()))
        org_child = sorted(org_children_of.get(R, ()))
        if org_par:
            if org_child:
                log(f"  {R}: WARNING - both referenced by organized part(s) {org_par} and "
                    f"references organized part(s) {org_child}; treating as sub-assembly of {org_par[0]}")
            P = org_par[0]
            base = _current_folder_of(P, organized, output, moved_dirs)
            total_copies += _place_subassembly_of(R, P, org_par, new_children, new_index,
                                                  new_names, base, output, dry_run, log,
                                                  renames=renames)
        elif org_child:
            copies, moves = _place_above_organized_children(R, org_child, new_children,
                                                            new_index, new_names, organized,
                                                            moved_dirs, output, dry_run, log,
                                                            renames=renames)
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
            total_copies += _place_standalone_new_root(R, new_children, new_index,
                                                       new_names, output, dry_run, log,
                                                       renames=renames)
    if standalone_orphans:
        total_copies += copy_orphans(standalone_orphans, new_index, output, dry_run, log,
                                     renames=renames)
    log("")
    return new_roots, total_copies, total_moves


def _incremental_finalize(stored_orphans, output, dry_run, log, scanned_new,
                          organized, new_roots, new_children, removed,
                          total_copies, total_moves, warning_count, unmatched,
                          chk_stems, new_used_on, incr_mism, used_on_bugs,
                          tb_mismatches, watermarks, new_names,
                          bom_names, superseded=(), planned=(),
                          scan_res=None) -> RunContext:
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
    _log_summary([
        "--- Summary ---",
        f"  New PDFs scanned:     {scanned_new}",
        f"  Already organized:    {len(organized)}",
        f"  New graph roots:      {len(new_roots)}",
        f"  BOM edges kept:       {sum(len(v) for v in new_children.values())}",
        f"  Cycles broken:        {len(removed)}",
        f"  PDF copies written:   {total_copies}",
        f"  Root folders moved:   {total_moves}",
    ], log)
    return {
        "counters": {
            "scanned": scanned_new,
            "roots": len(new_roots),
            "copies": total_copies,
            "cycles": len(removed),
            "warnings": warning_count,
        },
        "missing": unmatched,
        "chk": chk_stems,
        "orphans": _live_orphan_stems(output),
        "roots": [(r, sorted(set(new_used_on.get(r, [])))) for r in new_roots],
        "used_on_mismatches": incr_mism,
        "used_on_bugs": used_on_bugs,
        "titleblock_mismatches": tb_mismatches,
        "scanned": OCR.scanned_stems(),
        "watermarks": sorted(watermarks.items()),
        "names": _ctx_names(new_names, unmatched, bom_names),
    }


def run_incremental(folder, output, dry_run, log, jobs=0, rekey=False) -> RunContext:
    """Incremental run: process only stems not already in the output tree plus stored orphans.

    The input scan is recursive; already-organized subfolders are never
    re-sorted (supersede swaps and parent-adoption moves still touch them).

    Returns the same structured run context as run_full: counters {scanned,
    roots, copies, cycles, warnings}, missing [(referencing stem, missing ref)],
    chk [stem], orphans [stem], roots [(stem, [used_on])],
    used_on_mismatches [(parent, child, actual)],
    used_on_bugs [(parent, child)],
    titleblock_mismatches [(stem, field, filename value, title-block value)],
    scanned [(stem, [pages])], watermarks [(stem, evidence)],
    names {stem/ref: NAME}.
    Raises NoPDFsFoundError when the input has no indexable PDFs.
    """
    # Count 'EXTRACTION WARNING' lines for the workbook context (the same
    # number the workbook previously recovered by parsing the log text).
    warning_count = 0
    base_log = log

    def log(msg):
        nonlocal warning_count
        if "EXTRACTION WARNING" in msg:
            warning_count += 1
        base_log(msg)

    jobs = resolve_jobs(jobs)
    organized, sup_dir, stored_orphans, new_index, chk_stems, \
        supersede_pairs, new_duplicates, total_copies, superseded_new, scan_res = \
        _incremental_prepare(folder, output, dry_run, log)

    if not new_index:
        return _incremental_no_new_ctx(organized, chk_stems, output,
                                       total_copies, warning_count, log)

    new_index, watermarked_dupes, new_boms, bom_names, new_watermarks, \
        new_titleblocks, rekeyed, new_stems, org_stems, scanned_new, \
        all_stems, chk_stems = _incremental_scan_new_boms(
            new_index, new_duplicates, jobs, log, rekey, organized, chk_stems)
    # Archive the watermarked duplicates that lost the pre-scan resolution.
    if watermarked_dupes:
        total_copies += copy_watermarked_duplicates(watermarked_dupes, output,
                                                    dry_run, log)
    unmatched = _incremental_new_missing(new_boms, all_stems, bom_names,
                                         chk_stems, log)

    org_boms, org_used_on, watermarks, tb_mismatches = \
        _incremental_scan_organized(organized, all_stems, jobs, bom_names,
                                    new_watermarks, new_titleblocks,
                                    unmatched, log)
    new_used_on, new_names = _incremental_scan_new_used_on(new_index, jobs,
                                                           all_stems, log)

    moved_dirs, total_moves, total_copies, all_stems, swapped_old = \
        _incremental_supersede(supersede_pairs, organized, org_boms,
                               org_used_on, new_boms, new_used_on, new_index,
                               new_stems, org_stems, all_stems, sup_dir,
                               output, dry_run, log, total_copies)
    new_children, new_parents, org_parents_of, org_children_of, used_on_bugs, \
        incr_mism, removed = _incremental_graph(
            new_boms, org_boms, new_used_on, org_used_on, new_names,
            all_stems, new_stems, org_stems, log)

    new_roots, total_copies, total_moves = _incremental_place(
        new_stems, new_parents, org_parents_of, org_children_of, new_children,
        new_index, new_names, new_boms, organized, moved_dirs, stored_orphans,
        output, dry_run, log, rekeyed, total_copies, total_moves)
    # Planned tree stems for orphan retirement: in dry-run the placements
    # above logged but copied nothing, so a stored orphan adopted by this
    # run's placement is not yet live on disk. BOM-less standalone roots stay
    # parked in _orphans/ (never placed) and are excluded, mirroring
    # _incremental_place's already-parked branch.
    planned = set()
    for _r in new_roots:
        if not (org_parents_of.get(_r) or org_children_of.get(_r)
                or new_boms.get(_r) or new_children.get(_r)):
            continue
        _stack = [_r]
        while _stack:
            _s = _stack.pop()
            if _s in planned:
                continue
            planned.add(_s)
            _stack.extend(new_children.get(_s, ()))
    reused_scan_res = scan_res if (dry_run or (total_copies == 0 and total_moves == 0)) else None
    return _incremental_finalize(stored_orphans, output, dry_run, log,
                                 scanned_new, organized, new_roots,
                                 new_children, removed, total_copies,
                                 total_moves, warning_count, unmatched,
                                 chk_stems, new_used_on, incr_mism,
                                 used_on_bugs, tb_mismatches, watermarks,
                                 new_names, bom_names,
                                 superseded=set(superseded_new) | set(swapped_old),
                                 planned=planned,
                                 scan_res=reused_scan_res)
