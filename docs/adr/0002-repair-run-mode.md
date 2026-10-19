# 2. Repair mode: re-apply the current decisions to an existing tree

Date: 2026-10-19
Status: Accepted

## Context

An output tree is the product of whatever code built it and whichever source
batches it was fed, in whichever order. `--incremental` reconciles new PDFs
against the tree and renames live folders to the current name plan, but it is a
delta executor: it never revisits an existing folder whose parent edge
disappeared, and its two system folders are write-only:

- `_orphans/` is re-examined only as an adoption candidate for drawings arriving
  later (`_collect_incremental_candidates`);
- `_superseded/` is a one-way door — stems already archived are excluded from
  ever being candidates again (`already_sup`, `runmodes.py`).

So a naming/placement/ranking fix (e.g. the parts-list column gate, or the
title-block OCR psm fix) could not be applied to a tree built before it. The
only remedy was to delete the output and rebuild from every source folder in
the original order. In practice that means re-deriving the batch order from
memory, re-copying 0.45 GB, and discarding the `_superseded/` history.

## Decision

Add `--repair`, which takes an **existing tree as its only input** and
re-applies the four decisions of the current code to it:

1. **naming** — `normalize_folder_names` (already used by incremental);
2. **placement** — `fsops.reconcile_tree`: move a folder onto its planned
   parent, prune a duplicate folder (see the delete rule below);
3. **orphan state** — the tree's own `_orphans/` copies are adoption candidates,
   so a drawing a previous run parked because it could not place it is placed
   now that a parts list names it (or its own BOM reads);
4. **revision ranking** — an archived `_superseded/` copy is a promotion
   candidate only when it is not an unapproved CHK and its revision rank is
   strictly higher than the highest revision live for its base. It is staged
   under its canonical filename (archive names carry a `1.1.2.0 ` folder
   prefix) and enters the pipeline as a normal new arrival, so
   `_detect_supersede_pairs` and `_incremental_supersede` archive the demoted
   live copy through the existing `resolve_archive_target` path.

The executor is the incremental body, not a copy of it: `_run_delta_inner`
takes a `repair` flag that swaps the candidate source (`_repair_prepare`
instead of `_incremental_prepare`), keeps the "nothing to do" early exit out of
the way, and appends the reconcile pass. Everything a run *reports* — roots,
missing refs, CHK, USED ON bugs/mismatches, names — still comes from the shared
decision layer (ADR 0001), which is why
`tests/test_mode_parity.py::test_repair_reports_the_same_decisions_as_a_full_run`
holds.

### The delete rule

This is the tool's first destructive operation on the tree, so it is bounded:
`reconcile_tree` removes a folder only when

1. every file in its subtree is a PDF,
2. every one of those PDFs is byte-identical (`fsops._same_bytes`) to a copy at
   the folder the plan gives that stem, and
3. at least one planned copy exists outside the folder being removed.

Anything else keeps the folder, logs a warning, and lands in the review list —
which makes the run exit 2, like a placement refusal. A wrong BOM read or a
wrong rank can therefore *move* a drawing (visible, reversible, logged) but can
never delete the last copy of one. `_superseded/`, `_orphans/` and the output
root are never pruned, renamed or moved as folders; the only file repair ever
deletes from the archive is the promoted source, and only after the live copy
is verified byte-identical.

### What repair deliberately does not do

- It places nothing that is absent from the tree. A drawing still only in the
  source folder stays missing: that is a run, not a repair.
- It does not re-run duplicate resolution between a live copy and an archived
  copy of the *same* stem (the "the live one is the watermarked export and the
  archive holds the clean one" case).
- It does not resurrect a sheeted sibling from the archive (live
  `X_A_DWG1`, archived `X_A_DWG2`). Both of those are rank *ties*, and a tie
  cannot be resolved without re-deciding duplicate resolution — which would
  make a repair run act on a healthy tree. A first implementation of the
  candidate rule did try, and on the reference database it flagged all 62
  archived CHK copies of live drawings and started re-archiving them; the rule
  is now strictly-greater-rank and CHK-excluded, and
  `tests/test_repair.py` pins both refusals.
- It does not adopt, retire or re-rank anything outside the tree.

### Accepted consequences

- Archiving stops being a one-way door, but only for copies the current ranking
  disagrees with. Correctly archived copies (older revisions, unapproved CHK,
  watermarked losers) fail the candidate rule and stay put, which is what makes
  a repair over a freshly built tree report all zeros.
- A tree therefore becomes a function of its contents, not of its history:
  build order, batch splits and the code version that placed a folder all stop
  mattering. `tests/test_repair.py::test_repair_converges_a_damaged_tree_on_the_full_run_plan`
  pins that a damaged tree converges on a full run's layout, and that a second
  repair is a no-op.
- A hand-curated tree is not safe from repair: a manual rename or a hand-moved
  folder is a difference from the plan, so it is reverted. The dry run lists
  every rename and move first, and the runbook says to read it.

## Alternatives considered

- **Delete and rebuild.** Simple, but needs the source batches and their order,
  re-copies the whole tree, and drops `_superseded/`. Repair exists precisely
  because that is what an operator cannot always reconstruct.
- **A `--prune` flag on incremental.** Rejected: incremental's placement is
  delta-scoped, so the flag would need the same reconcile pass anyway, and the
  mode label in the report/workbook should say what happened.
- **Repairing by re-running the source scans.** Rejected: it re-introduces the
  source folder as an input, which is the dependency being removed.
