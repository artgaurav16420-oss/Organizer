# 1. Shared run-mode decisions (full ↔ incremental)

Date: 2026-10-14
Status: Accepted

## Context

`run_full` and `run_incremental` are two entry points over one output tree. They
have deliberately different *executors*:

- **full** derives the whole plan from the input and writes the tree;
- **incremental** reconciles against the existing tree (supersede swaps,
  parent-adoption moves, child re-homing).

Their *decisions* (which stems are roots, which refs are missing, which USED ON
entries are bugs/mismatches) were implemented twice, and drifted:

- incremental roots had to be re-derived (`_incremental_roots`) to match full;
- incremental listed an OCR misread (`F0156241`) as missing that full had
  already snapped to a real stem;
- incremental's USED ON mismatch set excluded organized→organized edges, so the
  `USED ON check` sheet differed by mode.

The operator requirement is that the two modes not differ in the
report/workbook, which is rebuilt from the run context every run.

## Decision

Both modes build **one merged view** of the tree they report on and call the
**same** decision functions:

```
merged_stems    = new_stems | org_stems
merged_boms     = {**org_boms, **new_boms}
merged_used_on  = {**org_used_on, **new_used_on}
merged_children, merged_parents = _graph_edges(merged_boms, merged_stems)
```

- roots/orphans: `_classify_roots` (both modes, whole tree);
- missing refs: `_collect_missing` (both modes, post-OCR-snap);
- CHK drawings: both modes list only the CHK that stay **live** in the tree (see
  below);
- USED ON bugs: `find_used_on_bugs` on the merged graph;
- USED ON mismatches: `find_used_on_mismatches` on the merged graph.

### CHK: report only what is live

The CHK sheet is a "download the approved DWG from Teamcenter" to-do list, so a
CHK whose base drawing has an approved/higher revision is noise: it is archived
to `_superseded/` and the approved drawing is already in the tree. Both modes
therefore compute `chk_stems` **after** `split_superseded` — full from the active
index, incremental from the live tree (`organized | new_index`, minus any
organized CHK superseded by the incoming batch) — and report only CHK stems that
remain live. Before this, full reported CHK from the raw input (including the
archived ones) and incremental reported only the incoming batch, so which
drawings were listed depended on run mode and batch order rather than on the
state of the tree.

Cycle-breaking mirrors full mode's order exactly: the merged graph is broken
**after** the report cross-checks and **before** the root/orphan split.

Incremental keeps its delta inputs (`new_*`, `org_*_of`, the placement
worklist) — those drive the executor and are unaffected.

One executor rule is shared for the same reason: "every claimant gets its own
copy". A new batch root is copied under each organized parent that claims it,
and a stem that arrives together with its own new parent is likewise copied
under each organized claimant (see `_incremental_place`). Without that second
half the tree's shape depended on which batch happened to arrive first.

### Folder names: one plan per run

Folder names are shortened to fit `MAX_PATH`, and how much is needed depends on
how deep a drawing ends up. Both modes therefore plan names **once per run over
the whole tree they are about to place** (`plan_folder_names` →
`naming.build_folder_names`), and both placement and organized-child adoption
name folders from that one map. Per-placement planning was the last way the
tree depended on history:

- a stem's folder was named by whichever placement created it first, so the
  same drawing could be spelled differently in two trees built from the same
  inputs in opposite orders;
- adoption reused the organized child's **on-disk** name, and an incremental
  run's plan stops at the organized children hanging off its own subtree, so
  the deepest copies were never measured at all — they were silently skipped
  for being over `MAX_PATH` (`Output 2` was missing 10 copies that `Output 4`
  placed, purely because the two orders shortened different folders).

Incremental additionally **normalizes** live folders to that plan
(`normalize_folder_names`): a folder created by an earlier run over a smaller
tree cannot be re-placed, so it is renamed instead. This is what makes the tree
a function of its contents rather than of the order its batches arrived in;
`tests/test_mode_parity.py::test_folder_names_do_not_depend_on_batch_order`
pins it. Full mode has no organized index to normalize against (it does not read
the output tree), so a full run into a pre-existing tree leaves its folder names
alone — accepted asymmetry 6 below.

The reported scope of incremental therefore changes from delta to whole-tree
(roots, mismatches). That is intended: a delta-only Roots section was the
inconsistency being fixed.

### NAME source for scanned drawings

An image-only drawing's NAME is OCR output, so a misread still reads as a
plausible part name (`NUT PLATE, MINI-CONFLAT` → `-CONFLAIL, NUT PLATE, MINI xX
OQ`). Both modes therefore replace a name that OCR produced with the part name
from the parents' PARTS LIST rows (`naming.prefer_bom_names`), decided by
majority description with a lexicographic tie-break
(`naming.bom_name_winners`) so a parent whose parts list still carries an old
revision's wording cannot outvote the current name. A text-layer drawing keeps
its own NAME — that one is authoritative — and an OCR name survives when no
parent describes the part. Both modes call the same two functions, and `names`
is now part of the parity comparison in `tests/test_mode_parity.py`.

## Rejected alternative: one merged algorithm

Making `full` a degenerate `incremental` (empty tree) was rejected: it routes
the common rebuild case through adoption/supersede machinery it does not need,
and the two modes' report wording (and in-place `output == input` guards) are
load-bearing. Cost of the chosen option is one extra graph pass on incremental
runs; benefit is that the decision functions cannot fork again.

## Known, accepted asymmetries

Pinned as excluded by `tests/test_mode_parity.py`:

1. **Counters / summary lines.** Incremental's are delta-oriented ("New PDFs
   scanned", "Already organized", "PDF copies written"); full's are whole-tree.
2. **`scanned`.** Run-scoped OCR events, not a tree property.
3. **`orphans`.** Full derives them from the input classification; incremental
   uses a live `_orphans/` scan, which also sees parks from earlier runs (so it
   can legitimately be a superset).
4. **Cycle section.** Full logs whole-tree breaks; incremental logs new-graph
   breaks ("Cycle detection (new PDFs)"). The merged-graph break used for the
   root split is deliberately silent.
5. **No-new-PDF early exit.** `_incremental_no_new_ctx` returns before any scan
   and reports empty decision fields. Fixing it would mean scanning the whole
   tree on a no-op run; out of scope.
6. **Folder-name normalization.** Incremental renames live folders that no
   longer match the run's name plan; full (which never reads the output tree)
   leaves pre-existing folders alone. A full rebuild into a clean output and an
   incremental run onto the same drawings therefore agree, but a full run *into*
   an existing tree does not rename what is already there.
