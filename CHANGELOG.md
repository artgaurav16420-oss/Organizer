# Changelog

All notable changes to the Fermi PDF organizer are recorded here.

## Unreleased

### Fix: codebase-review Phase 1 (correctness, data safety, operator contract)
- **A short text layer no longer loses its parts list when OCR reads nothing.**
  `extract_bom_from_text` skipped the native parser whenever the page was in the
  OCR word memo, and `analyze_page` memoizes every page it sees - including one
  where OCR returned no words at all. A page under `OCR_MIN_CHARS` that did carry
  a text-layer `FERMI #` list therefore dropped its rows the moment Tesseract was
  installed, losing the child edge and parking the child in `_orphans/` with exit
  0. The skip now keys on actual OCR content. The suite's autouse OCR-off fixture
  had hidden this; the new test runs with OCR enabled.
- **Lock release is ownership-checked.** `release_run_lock` deleted the lock path
  without comparing the token it wrote, so a run that resumed after its lock was
  stolen (paused past the stale window) removed the *new* holder's lock on the way
  out and let a third run mutate the same tree. It now compares the bytes held in
  `_heartbeats` and leaves a stolen file alone.
- **Directory junctions in the input and output trees are not followed.** The
  input scan used `rglob`, which follows NTFS junctions (pathlib already refuses
  directory symlinks), so PDFs from outside the input folder were indexed as its
  drawings and republished (the log said "Skipped 0 PDF(s) that are symlinks").
  The scan now prunes them and reports `Skipped N linked folder(s)`; the output
  tree scans and the staging sweep prune them too, so repair never lists or
  deletes through a link. File-level symlink refusal is unchanged.
- **Supersede archives atomically.** The archive leg used raw `shutil.copy2`;
  a failed write left a truncated PDF under a real `_superseded/` name that the
  next workbook listed as a valid archived revision. It now goes through
  `_copy_atomic` (stage + replace).
- **A killed repair no longer strands drawing copies in `%TEMP%`.** The
  in-process sweep only knows the directories its own process created. Every
  real (non-dry-run) repair now starts with a stateless pass that removes
  `fermi_repair_*` dirs older than 24 h.
- **`run_repair` is keyword-only after `folder`.** `run_repair(tree, output, log)`
  bound a truthy `Path` to `dry_run` and returned a clean exit 0 having changed
  nothing; it now raises `TypeError`.
- **Packaging:** the wheel ships `THIRD_PARTY_NOTICES.md`
  (`license-files`), and the packaging CI job installs it into a clean venv so
  its `Requires-Dist` is actually resolved instead of riding on the hashed lock.
- **Docs:** corrected the operator contract - `--incremental` re-applies folder
  names to the current plan once its batch has new PDFs (with nothing new it
  stops after a zero-count summary and touches nothing); `--no-ocr` does not
  orphan a drawing a readable parent names;
  exit 1 can follow writes (a crash is not "nothing ran"); the fourth exit-2 case
  (argparse usage error, no artifacts); `MAX_PATH` placement skips exit 0; the
  licence is AGPL-3.0-only; the runbook no longer tells an agent to start the real
  run without the operator's go-ahead; the venv-recreate recipe uses the dev
  lock; the package build clears `build dist` first. CVE-2026-82035's fix status
  is corrected in README, SECURITY.md and THIRD_PARTY_NOTICES.md (pymupdf 2.0
  shipped it on 2026-10-19; adopting it means dropping the 3.10 floor).
- **Removed:** the tracked `uv.lock` nothing consumes (one `uv sync` in this repo
  uninstalled pytest, ruff, pytest-cov, openpyxl and setuptools); the dead
  `graph.can_reach` helper (its BFS semantics are now pinned on
  `collect_reachable`, the helper the organizer actually calls).

### Added: `--repair` re-applies the current decisions to an existing tree
- New mode: `python organize_fermi_pdfs.py <tree> --repair [--dry-run]` takes an
  existing output folder as its **only** input (no source scan, no `--output`)
  and re-applies the current code's decisions to it: it renames live folders to
  the current name plan, moves a folder onto its planned parent, adopts a
  drawing a previous run wrongly parked in `_orphans/`, promotes a `_superseded/`
  copy whose revision out-ranks the live drawing (every sheet at that revision
  travels together; never an unapproved CHK), and removes duplicate folders.
  Nothing is placed that is absent from the tree.
- A tree built by older code (or by an unknown batch order) can therefore be
  corrected in place instead of deleting the output and rebuilding from every
  source folder in order; `_superseded/` history is preserved.
- Delete rule: a folder is removed only when every file in its subtree is a PDF
  and every one is byte-identical to a copy at a planned path. Anything else is
  kept, reported, and makes the run exit 2 (`Needs manual review`). Nothing is
  ever removed from `_superseded/` except the promoted source, once its live
  copy is verified.
- `_superseded/` stops being a one-way door, but only for copies the current
  ranking disagrees with: correctly archived copies (older revisions, CHK,
  watermarked losers) are untouched, so a repair over a freshly built tree
  reports all zeros.
- Repair shares the incremental executor (`_run_delta_inner`) and the whole
  decision layer, so its report matches a full run's
  (`tests/test_mode_parity.py`).

### Names come from a parent's parts list when the drawing is scanned
- A scanned drawing's own NAME is OCR output, so a misread still looks like a
  real name (`F10128174` was named `-CONFLAIL, NUT PLATE, MINI xX OQ.`). The
  parents' PARTS LIST rows carry the same name as a text layer, so a name read
  by OCR is replaced by the BOM description, and a drawing whose NAME could not
  be read at all is filled from it instead of staying a bare drawing number
  (both run modes, folders and the workbook). A text-layer drawing keeps its
  own title-block NAME.
- Parents can disagree (a stale parts list keeps an old revision's wording), so
  the description is chosen by majority vote with a deterministic
  lexicographic tie-break instead of first-seen.
- `SINGLE_LINE_RE` now allows commas, so a parts-list row keeps its full
  description (`NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD`) instead of the stub up
  to the first comma.

### Fix: view-balloon part numbers are not BOM rows
- A sheet can print a part number next to a circled item balloon (`11
  F10218071`) in a view, away from the parts list. Both the line parser and the
  Format 4 positional parser read those as children, so `F10202541` (12 rows in
  its PARTS LIST) got 14 folders. Every BOM row is now required to sit in the
  page's parts-list FERMI# column: the column is anchored on the `FERMI #`
  header label, falling back to the densest aligned column of F-numbers, and is
  used for the line, Format 4 and positional paths. Pages where no column can
  be found keep the old behaviour.
- A Format 1 description that is an item digit or a stray letter (`F`) is no
  longer kept as a part NAME.

### Fix: title-block OCR ran at the wrong page-segmentation mode
- The high-zoom title-block pass invoked Tesseract as `... stdout tsv --psm 6`.
  Tesseract parses options *before* the config file, so `--psm` was silently
  dropped and the pass always ran at the default psm 3, which garbles a title
  block. Options now precede `tsv`, so the intended psm 6 applies: across the
  55 scanned drawings in the test database this gained 13 NAMEs and 1 USED ON
  value, lost 1 garbled NAME (its parent's parts list supplies the correct
  one), and changed no BOM row.

### Fix: OCR NAME words kept in reading order
- The NAME words were ordered by `round(y)` before being joined, so two boxes
  on the same visual line whose top edges straddled a half-point boundary were
  split into separate rows and the later one was emitted first (`F10128174`
  read `-CONFLAIL, NUT PLATE, MINI xX OQ.`). Rows are now grouped by the same
  y tolerance the next-row drop uses, so the name reads left to right.

### Fix: long-path input handling during BOM extraction
- Extraction now routes PDF open calls through `fsops._native`, adding
  the Windows `\\?\` extended prefix for input paths >=260 chars so
  PDFs stored deeply in renamed/moved input trees no longer fail with a
  misleading 'no such file' error while reading BOM etc.

### Performance: single PDF open per phase
- `survey_task` extracts BOM, USED ON, title block, name, watermark and
  scanned state from one opened PDF document instead of separate BOM and
  USED ON passes. The run reports USED ON from the single-pass cache without
  reopening PDFs, cutting extraction-side filesystem/PDF-open work while
  keeping the golden dry-run log byte-for-byte.
- `canonical_stem`/`filename_title` are now memoized; `scan_output_tree` uses
  `os.walk` instead of recursive glob.
- `report_glue.names_from_tree` uses `os.walk` and `_staged_copy` uses a
  1 MiB copy buffer.

### Order-independent orphan adoption
- A parked watermarked `_orphans/` copy is now retired when a clean live
  same-stem copy exists in the tree, even when the PDF bytes differ only
  by the watermark. Split-run order no longer changes the final tree;
  generic differing (non-watermarked) parked copies keep their
  byte-safety behaviour so a newer input is not discarded.

### A title-block number is not a parts-list row
- On a scanned sheet, the OCR parts-list matcher counted the F-numbers aligned
  in the `FERMI#` header's column and took whatever it found as rows. On a sheet
  whose parts list is empty (or whose list the OCR never read) the only such
  number is a title-block value, which then became a phantom BOM edge and a
  phantom entry on the Missing sheet: `F10133759` (a flange, no parts list)
  reported its own `USED ON F10133744` missing, and `F10148138` reported
  `F10196096` from a title-block cross-reference row 89% down the page.
- Two guards, both word-path counterparts of the text path's
  `_fermi_line_is_title_block`: below the header a bare FERMI token now counts
  only when its visual row carries an ITEM number (these sheets draw parts lists
  bottom-up, so a top-down list has to look like one - a title-block row never
  does), and no row may be a number the page's own `USED ON` claims as a parent.
  Parts-list rows are read after the USED ON recovery, so values recovered by
  the title-block zoom/strip passes count too. (A blanket "no rows in the bottom
  15%" bound was measured first and rejected: `F10209823`'s own bottom-up list
  legitimately extends into that band, 9 of its 32 rows were in it.)

### Folder names no longer depend on the batch order
- Both modes now plan folder names **once per run over the whole tree** and name
  every placement *and* every organized-child adoption from that one map. Two
  trees built from the same two inputs in opposite orders previously disagreed
  on folder spelling (a stem's folder was named by whichever placement created
  it first) and differed by 10 copies: an adopted copy reused the name it
  carried on disk, and an incremental run's plan stops at the organized children
  hanging off its own subtree, so the deepest copies were never measured and
  were skipped for being over `MAX_PATH`.
- `--incremental` also renames live folders that no longer match this run's plan
  (`Normalized N folder name(s) to this run's plan`), so a folder named by an
  earlier run over a smaller tree converges to the name a full rebuild of the
  same drawings would use. Guarded: a colliding destination or an over-long
  target is logged and skipped, never fatal.

### Incremental copies a new child under its pre-existing claimants
- A drawing that arrives together with its own new parent is now also copied
  under every already-organized part whose BOM lists it. The cross-batch
  "every claimant gets its own copy" rule previously applied only to a run's
  batch roots, so a pre-existing parent's folder could be missing a child its
  own BOM points at, and the final tree depended on the order the batches
  happened to arrive in (two trees built from the same two inputs in opposite
  order differed by 76 copies). The pass covers stems *placed by the run*, so an
  existing tree built before this fix is not repaired by re-running
  `--incremental` (nothing is new) - it needs a rebuild.

### CHK list reports only unapproved drawings still live in the tree
- A CHK drawing is now listed only while it is live in the tree. A CHK whose base
  has an approved/higher revision is archived to `_superseded/` and the approved
  drawing is already in the tree, so listing it asked the operator to download a
  drawing they already had. The list is computed after revision filtering
  (`split_superseded`), and incremental now covers the whole live tree rather
  than only the incoming batch. Previously the reported set depended on the run
  mode and batch order: an incremental run could list the archived CHK copies
  while hiding the CHK drawings that were still live and unresolved.
- **Behaviour change:** a full run over inputs whose CHK drawings all have
  approved revisions now reports zero CHK entries (previously every CHK in the
  input), and incremental reports the live CHK of the whole tree.

### Full and incremental share one decision layer
- Both modes now compute their reported decisions (roots/orphans, missing refs,
  USED ON bugs, USED ON mismatches) from one merged view of the tree using the
  same functions, so the two modes cannot report different things. Cyclic
  merged graphs are broken before the root/orphan split, mirroring full mode's
  order. Removed the forked `_incremental_roots` and
  `_collect_used_on_bugs_incremental`; added `_collect_missing` and
  `tests/test_mode_parity.py`, which fails if a decision is forked again.
  Known, deliberate asymmetries (counters, `scanned`, `chk`, `orphans`, cycle
  section, the no-new-PDF early exit) are recorded in
  `docs/adr/0001-shared-run-mode-decisions.md`.
- **Behaviour change (incremental):** the Roots sheet/count and the USED ON
  check's mismatch table now cover the whole tree rather than only the stems
  arriving in that run, matching full mode. The incremental summary line
  "New graph roots:" is now "Root assemblies:" (same value and wording as full).

### Incremental missing list matches full mode after OCR snap
- Incremental computed the "missing reference(s) from organized drawing(s)"
  list from un-snapped BOMs, so a misread that snaps to a real stem
  (`F0156241` -> `F10156241`) was reported missing only in incremental runs
  (full mode snaps before its missing check). The snap now runs before the
  organized-drawing missing check, so both modes report the same refs.

### Workbook file links are relative
- File cells on the `Files`, `Orphans` and `Superseded` sheets now use a
  relative hyperlink target instead of a `file:///...` URI. Excel resolves it
  against the workbook's folder, so links survive moving or copying the tree,
  and deep space/comma-heavy paths stay under Excel's 255-character hyperlink
  limit (a `file:///` URI percent-encodes spaces and commas, which left 51 of
  516 Files rows unlinked).

### OCR title-block NAME: retry when the read is a stray fragment
- A 1-2 letter NAME (OCR noise from a mangled cell) is now treated as missing,
  so the title-block zoom re-OCR fires and reads the real value (`F10134685`
  read `er` for `SSR/650 CM LIFTING INTERFACE ASSY`). A three-letter real name
  (`LUG`) is left untouched.

### Title block check: OCR number notes and scanned near-misses
- The drawing-number label must sit in the right half of the sheet, so note
  text on the left ("MANUFACTURER'S PART NUMBER F10154728") no longer hijacks
  the field (`F10007338`, `F10007339`, `F10007343`, `F10007348`).
- A scanned (OCR) number that differs from the filename by a single character
  is treated as an OCR misread rather than a misnamed file (`F10134728`,
  `F10157448`). Text-layer reads are unaffected and stay exact.

### OCR title-block number: reject split fragments
- The OCR title-block reader now accepts only a full-length FERMI number
  (`F` + 8 digits, or `FC` + 7). Tesseract splitting a cramped number
  (`F10119580` -> tokens `F`, `1Q1`, `19580`) no longer fabricates `F19580`,
  so the Title block check stops reporting bogus number mismatches
  (`F10128056`, `F10119580`, `F10142926`, `F10144043`, `F10144047`,
  `F10042432`). Such a field now reads as unreadable instead of wrong.

### Title block check: no revision row for a misnamed file
- When a file's drawing number disagrees with its title block, the Title
  block check sheet (and log) no longer also reports a revision row for that
  drawing - the filename revision is meaningless once the number is wrong.
  Revision rows are unchanged for correctly numbered files.

### Workbook Files sheet is tree-only
- The `Files` sheet now lists only the organized tree (`Active` / `CHK`);
  parked `_orphans/` files are no longer repeated there and live solely on the
  `Orphans` sheet. The `PDF FILES IN TREE (LIVE)` count was already tree-only.

### OCR title-block NAME: note-row guard and zoom retry
- `_find_name_anchor` now rejects a `NAME` label only when a `PART` word sits
  **immediately to its left** (the parts-list `PART NAME` header pair) instead
  of any `PART` word on the same visual row. A left-column note ("PART TO BE
  FREE OF ALL SHARP EDGES") no longer hides the title-block label and drops a
  legible NAME (`F10144038` "FLAT RAIL SUPPORT BLOCK", `F10144048`
  "CYLINDRICAL RAIL SUPPORT BLOCK - 1").
- The title-block zoom re-OCR now also runs when a NAME is missing (previously
  only for a missing USED ON), recovering small NAME labels the full-page pass
  misses (`F10112629`, `F10007335`, `F10128174`, ...).

### OCR title-block NAME on tall cells
- The OCR NAME reader now retries with a vertical-overlap window when the
  strict top-edge window finds nothing, so a value centred in a tall
  title-block cell (its OCR box starting above the `NAME` label's bottom
  edge) is read. Drawings that already read a NAME keep their exact result -
  the strict pass wins, so a tall box cannot hijack the row. The scanned
  `F10209823` (`HB650 PROD CM - PIPING PROCUREMENT`) had fallen back to a bare
  drawing-number folder despite a legible NAME.

### Incremental root classification matches full mode
- The incremental Roots sheet/count no longer includes parentless BOM-less
  standalones (which are parked in `_orphans/`) or new stems placed under an
  already organized parent. Reported roots now use the same rule as full mode
  (`_classify_roots`: no parent + at least one child edge), via the new
  `_incremental_roots`. Previously the internal placement worklist (every
  parentless new stem) was reported as roots, so a drawing could appear in
  both Roots and Orphans (e.g. `F10038961`). Placement itself is unchanged.

### Workbook NAME coverage fixes
- Incremental runs now carry organized names forward (`_scan_organized`
  already extracted them and dropped them): prior-batch NAMEs survive into
  later workbooks even when path shortening stripped them from folder names.
- Same-drawing fallback in `_resolve_names`: reported-but-unscanned stems
  (superseded CHK) reuse their DWG twin's NAME instead of showing `-`.
  Blocked across different sheet tokens, so sibling sheets never inherit
  each other's names; extracted names always win. A sheeted CHK is still
  that sheet's content (same-sheet donor only); only a sheetless CHK may
  reuse a cross-sheet donor. The bare-base key serves only unrevisioned
  sheetless stems (or a sheetless CHK). Non-exact-rev donors resolve
  newest-revision-first, so a revisionless CHK never shows a stale rev A
  NAME when rev B exists. A malformed prior workbook (including bad inner
  XML) yields no priors instead of aborting the rebuild.

### Incremental placement completeness fix
- A new non-root parent referencing already-organized children now gets copies
  of them in its folder (`_adopt_org_children_below`, mirroring
  `_place_above_organized_children`'s nested-copy rule). Previously only new
  roots routed organized children into place; non-roots silently skipped them,
  leaving BOM-listed children out of their folder (9 parents, ~15 copies on
  the SSR2+HBCM corpora). Nested under a live parent means copy, never move.
- A copy of a relocated organized folder counts its indexed PDFs: after an
  ancestor move the copy reads from the new home while `organized` still
  indexes the old path, which used to report zero copies for a successful
  copy.

### Excel report performance, security, and layout improvements
- Optimized column auto-fitting (`_autofit`) using row-major `ws.iter_rows()` to eliminate random-access cell dictionary lookups.
- Streamlined stem name resolution (`_resolve_names`) by populating a set directly, avoiding intermediate list concatenations.
- Expanded formula injection neutralization (`_inert_text`) to neutralize leading formula-trigger characters (`=`, `@`, `+`, `-`, `\t`, `\r`).
- Protected against Excel workbook corruption prompts by suppressing hyperlink setting (`_link`) when target URI exceeds 255 characters.
- Dynamically calculated table base row in `_mismatch_sheet` to maintain uniform 2-row vertical spacing between tables regardless of empty-state rendering.
- Added `WorkbookContext` (`TypedDict`) for typed context data transfer in `build_workbook` and `refresh_from_run`.

### USED ON no longer leaks into the BOM (cycle fix)
- The positional extractors read a drawing's title-block `USED ON` value as a
  parts-list entry, adding an inverted edge (child -> parent) that closed a
  2-node cycle against the parent that genuinely lists the child. On real
  corpora this was every cycle seen: 68 of 68 in SSR2 Parts and 55 of 55 in
  HBCM Parts traced to the leak, with the graph reaching 0 cycles once fixed.
  The `USED ON` field stays cross-check-only, as the BOM-only invariant
  requires.
- Root cause was row clustering being y-only: a left-column note line lands in
  the same row as the right-column caption ('BREAK ALL SHARP EDGES, MAX: 0.5 |
  USED ON | NAME'), so the joined text no longer starts with 'USED ON' and the
  caption read as prose. The per-token `_used_on_label_x` helper finds the
  label in either the span or word path regardless of what merged into the row.
- The wide window's horizontal association widened from +/-60 to +/-180: the
  label's x is its left edge while the value starts past the label text, so
  real label-to-value offsets (88-116 pt, F10205800 / F10153201 / F10197064 /
  F10144381) all fell outside the old window and went undetected. The label
  must also sit at or left of the value, which keeps a parts-list description
  containing the phrase ('BRACKET USED ON ASSY' - text that trails the
  F-number) working as BOM content.
- The word-level path gained the same tall-cell window the span path already
  had, so its two remaining leaks close as well. Genuine edges are untouched:
  one real parts-list row that also appears in its own `USED ON` field
  (F10204743 -> F10204745) is correctly still placed.

### Sheet-set tie fixes
- Mixed ties of a plain stem and its sheeted exports (e.g. `F10038961` /
  `F10038961___DWG1`) now keep every sheet active instead of archiving the
  sheeted stems: the dropped sheet's BOM used to be lost, surfacing its
  children as orphans. Exact duplicate exports of one sheet still collapse
  to the sorted-first stem.
- `match_pdfs` applies the same rule to an exact reference hit (same base
  and revision rank only - never across revisions), so a plain reference
  now also places the sibling sheets instead of dropping them.

### Logic-audit fixes (round 2)
- `BAD_DESC_RE` no longer rejects part names starting with `ENERGY`
  (`ENERGY ABSORBER`); the boilerplate title-block line is still caught via
  `UNITED`/`DEPARTMENT`.
- Table cells with a same-line split value (`F101 26145`) re-join the split
  digits before tokenizing, while a newline-stacked quantity
  (`F10112345\n2`) still splits.
- In-place organized detection requires place_files' own shapes (exact
  stem/base folder or `{base} NAME`): a user folder that merely starts with
  the base (`F10126107_backup`) no longer counts as organized. A folder
  deliberately named `{base} something` is indistinguishable from the
  convention and still counts.
- The incremental fan-out cap charges a root referenced by several
  organized parents once per claimant (it is copied once per claimant).
- `BAD_DESC_RE` now matches whole words: a single-line BOM row whose part
  name starts like a label (`REVERSE SHAFT`, `PARTITION PLATE`, `TABLETOP`)
  is no longer rejected as `REV`/`PART`/`TABLE` text. Plural and
  `FIGURE`/`ASSEMBLY` forms still match.
- A stacked USED ON block (`USED ON` followed by several F-number lines) is
  now entirely title block: the line parser walks up the whole run of
  F-number lines to the label, so the third+ values no longer become phantom
  BOM children.
- In-place `--incremental` (output == input) keeps only tree-shaped copies
  as organized: a PDF whose parent folder does not start with its own base
  number (a file dropped at the root or in an input subfolder) counts as a
  new arrival instead of being silently skipped - or worse, having its
  input subfolder adopted/moved by parent adoption.
- `_shorten_names` no longer stops at the first over-length placement it
  cannot shrink (bare base-number stems sit at their floor): every
  over-length path is attempted, so later shrinkable paths fit `MAX_PATH`
  instead of being skipped at placement time.
- Incremental supersede re-homing copies a child that also lives under
  another parent (the other parent keeps its copy, like place_files) and
  moves only a sole copy; previously the move hollowed out the other
  parent's subtree.
- A new child referenced by several organized parents is placed under every
  claimant, not just the alphabetically first one (full-mode parity).
- A failed supersede swap leaves the new revision as a new arrival (placed
  and retried next run) instead of marking it organized with no tree copy -
  which used to strand its children at the output root.
- `place_files` and `copy_orphans` stage each copy next to its target and
  `os.replace` into place: a failure mid-write leaves no truncated PDF in
  the tree, so the next run retries instead of treating it as placed. The
  staging sweep also removes `.copy_tmp.<pid>` leftovers, including inside
  `_orphans/` where orphan copies are staged (supersede staging under a
  system dir is still left alone).
- The supersede re-home copy is counted in the run's copy total (the
  summary used to underreport it).
- Staging paths are PID-predictable and `shutil.copy2` follows a symlink on
  write: a symlink planted at a `.copy_tmp.<pid>` or
  `.supersede_tmp.<pid>` staging path is refused, and the bytes are written
  through an `O_CREAT|O_EXCL`-created handle that is re-checked after the
  open (Windows follows a dangling link even with `O_EXCL`) instead of
  re-opening the path, so no data is written through a swapped-in link. An
  attacker with live write access to the output tree remains out of scope,
  as for every path check (see Known limits).
- BOM table cells are read token-wise: a quantity stacked under the value
  (`F10112345\n2`, which used to normalize to the wrong number
  `F101123452`) or an item number before it (`1 F10126108`, dropped) no
  longer corrupts the extracted value.
- The text-path NAME extraction skips a parts-list header pair (`PART`
  directly above `NAME`): the cell below it is no longer taken as the
  drawing name.

### Placement correctness fixes
- Sibling sheets of one drawing (e.g. `F10038961___DWG1` / `___DWG2`) are no
  longer treated as rival revisions: the lower-ranked sheet was dropped with
  its BOM, so its children surfaced as orphans and were never placed under
  the drawing. Ties at a base's top rank now resolve to one stem per distinct
  sheet token (`_sheet_winners`, shared by `split_superseded` and
  `match_pdfs`): sibling sheets all stay, while duplicate exports of the SAME
  sheet (`___DWG1_XML2347` vs `___DWG1_XML2348`) collapse to the first-seen
  stem. Any tie without sheet tokens keeps the previous first-seen winner.
  A drawing reference returns every sibling sheet as a child, so a parent
  that lists the drawing gets all sheets (a full run places each sheet with
  its own subtree, and nests every sheet under such a parent).
- `retire_adopted_orphans` no longer deletes a parked `_orphans/` copy on the
  superseded stem alone: the parked copy is retired only when its exact bytes
  survive somewhere else - in the tree or in the archive (`_superseded/`).
  Otherwise the parked copy is the only holder of those bytes and is kept
  with a warning. Affects the deletion path only; dry-run reporting is
  unchanged.
- The word-path title-block check no longer treats a title-block keyword word
  inside a BOM row (`SHEET METAL BRACKET`) as a title-block caption: that
  silently dropped the F-number rows below it. A nearby row is a caption only
  when it starts with a configured keyword (phrases included) followed solely
  by value tokens - `SCALE 1:1`, `SHEET 1 OF 2`, `USED ON F10126107`, bare
  `REV` - so value-bearing captions stay effective while BOM text containing a
  keyword word (`BRACKET USED ON ASSY`) no longer disables the rows below it.
  An F-number counts as a value only for the fields that hold one (USED ON,
  DRAWING NUMBER, NUMBER, NEXT ASSY): `ITEM 1 F10126107` is a BOM row, not a
  caption, and its child is kept.
  The same rule now gates the row's own skip test and the positional parser's
  USED ON checks (own row via the full caption test, nearby rows via a
  USED-ON-only caption test, as is the line parser's two-lines-above check):
  a row, line, or BOM description merely containing the phrase no longer
  disables the rows below it.

### Placement fan-out guard
- Over-cap runs now degrade gracefully instead of refusing the whole batch:
  `fit_roots_within_cap` drops the largest roots (deterministic stem-order
  tie-break) until the plan fits `MAX_PLANNED_COPIES`, places the rest, and
  reports every skipped root with its planned count (`skipped_roots` in the
  run context, `Roots skipped (oversized)` summary line, CLI exit still 2).
  Skipped roots are never placed; their shared children still land under
  their other parents, and a child exclusive to a skipped root is listed
  under Unplaced PDFs instead of vanishing from the report. A fully
  over-cap run places no folders (full mode still parks BOM-less orphans;
  bounded archive copies still run in both modes). Both run modes share the helper; the incremental pre-check and the
  placement backstop both fit, so the backstop (on the final graph) is
  authoritative.
- The incremental pre-check now fits the cycle-broken new graph (placement
  always ran broken): previously an unbroken plan failed closed on phantom
  fan-out — e.g. 16,386 planned collapsing to ~1,100 after 7 cycle breaks —
  refusing swaps/moves for a batch that placed fine.
- The CLI now forwards `placement_refused` to the workbook, so the
  Dashboard `Placement refused` action row actually lights up after real
  runs (it was only reachable via direct `build_workbook` calls before).

### Input scan
- The input scan excludes the run's output folder by resolved path, not just
  by the top-level `Output/` name: a previous tree kept inside the input
  folder under any custom name (e.g. `Organizer Output`) is no longer
  re-indexed as input on later full runs. Name-based skips (`Output/`,
  `_`-prefixed dirs) are unchanged.

### Incremental adoption
- Adopting an organized child under a newly arrived parent no longer steals
  shared folders: a child nested under another live parent is now COPIED to
  every claimant (matching place_files), while a top-level organized root is
  still re-homed (moved). Previously the move hollowed out the former parent
  (e.g. `F10187622` lost its `F10128174` copy to `F10119081`) and left later
  claimants empty-handed. Affected trees should be rebuilt with a fresh run.
- Audited sibling: `_move_children_under_superseding` still moves (its old
  folder is archived away, so re-homing is correct there), with a documented
  residual risk for children shared under another live parent; zero such
  moves observed on real corpora, so it stays untouched pending a failing
  case. All other tree writes are copies or byte-verified retirements.

### Extraction precision
- Fabrication-note references no longer become BOM edges: a single-line
  "description" that only points at other drawings (`F10112550 AND
  F10118731.`, including `W/`-style connectors) is rejected as a
  cross-reference - in the line parser, in the Format-1 fallthrough (which
  otherwise re-emitted the row via Format 3), and in the positional parser
  (which checks the row below for bare-FERMI stacks).
- The USED ON title-block guard of both positional parsers now looks
  further above the row: tall cells park the value several rows below the
  label, and a USED ON value is an F-number by design. Full-width on
  purpose - the real case sits down-and-right of its label, so any
  horizontal-overlap requirement would miss it.
- The line-based fallback additionally requires a parts-list header
  (`PARTS LIST`, `BOM`, an `ITEM`/`FERMI` column combo, or the column labels
  on adjacent lines) somewhere in the document: on a headerless sheet every
  F-numbered text line is a note, a reference, or title-block content.
  Table and positional parsers keep their own structural gates. All three
  had attached phantom parent-child edges that surfaced as BOM cycles;
  verified on HBCM data (7 phantom cycles gone, copy totals unchanged).

### Concurrency & process safety
- Exclusive per-output run lock (`<output>/.fermi_organizer.lock`): a second
  concurrent run is refused with `RunLockedError` (CLI exits 1) instead of
  racing the sweep/supersede machinery. Locks older than 2 h are stolen with
  a warning; anything younger fails closed with a recovery pointer. A steal
  claims the file aside with an atomic rename and deletes the staged copy
  only if it still matches, so a double-starter race leaves a single holder
  short of a documented 3-party microsecond interleave; a heartbeat refreshes
  a held lock so long runs never look
  stealable. The CLI holds the lock through report + workbook writes
  (`hold_lock`), closing the post-run shared-file window; a failed lock
  write cleans up instead of leaking a refusing file.
- Tesseract environment is scoped: our CLI children get an explicit env
  (`TESSDATA_PREFIX` only); the process-global export remains solely for
  PyMuPDF's in-process OCR, which offers no env parameter (documented in
  `OCR._export_for_pymupdf`).

### CI & coverage
- Weekly `cve-watch` workflow compares the PyMuPDF pin and the Tesseract
  floor against upstream releases and opens a tracking issue on change
  (the CVE standing actions, automated; label dedupe, abort on `gh`
  failure, serialized runs).
- Coverage ratchet raised 80 -> 83 (measured ~84%).

### Concurrency hardening (post-merge review)
- Lock steal verifies content before unlinking (re-read must match), closing
  the double-starter race; the loser observes the winner's fresh lock and
  refuses. Stale window shortened 24 h -> 2 h; refusal names the holder host.
  No PID probing by design (unreliable signal semantics across platforms).
- The heartbeat verifies its own token before every refresh: a run paused
  past the stale window (sleep, SIGSTOP, VM pause) that wakes stolen stops
  refreshing (never touches the stealer's file), flags the loss, and aborts
  with `LockLostError` at the next lock check before any further tree
  mutation (CLI exits 2); the wrappers stop the heartbeat without unlinking
  the stealer's live lock, and the CLI re-checks before report/workbook
  writes (discards + exits 2 on loss).
- Leftover steal-staging files (`<lock>.claim.<pid>` from a kill between
  rename and unlink) are swept only when older than the stale window — a
  fresh claim may be a live stealer mid-protocol.
- The CLI holds the lock through report + workbook writes (`hold_lock`), so a
  second run cannot interleave those shared-file updates.
- Network-share output documented as not lock-safe (README Known limits),
  now explicit that mapped drive letters / NFS mounts presenting as local
  paths are included (only Windows UNC paths get a CLI warning, since the
  rest cannot be detected); the lock dotfile is visible in Windows Explorer
  (harmless).

### Robustness (hostile/corrupt inputs can no longer abort a run)
- Drawing-derived folder names are stripped of C0/C1 control characters and
  bidi override codepoints; a PDF whose NAME (or BOM description) contains a
  NUL byte no longer crashes placement with an uncaught `ValueError` mid-run.
  The mkdir/copy guards now also catch `ValueError` ("tree mutations are
  guarded, not fatal" restored).
- A failing or hung Tesseract is now loud, not silent: a non-zero
  `tesseract --version` marks OCR unavailable, non-zero OCR runs raise
  (surfaced as that PDF's extraction error) instead of reading as a blank
  strip, and `subprocess` timeouts at the strip-OCR call sites no longer
  escape the extraction task boundaries.
- Incremental pre-cap check, placement backstop, and retirement planning share
  one placeable-root predicate; a root placeable only as an organized-child is
  now counted against the fan-out cap on the pre-check too (cap-bypass
  asymmetry closed).

### Performance
- Repeat full runs no longer re-copy already byte-identical archived PDFs
  (`_superseded/`): the verified-identical verdict from the archive resolver is
  honored, saving a full read+write per archived loser every run. The archive
  still counts toward orphan-retirement safety.

### Reporting & docs
- The operator runbook documents exit code `2` (placement refused: nothing
  mutated, do not retry like a crash) alongside the existing `1` guidance.
- Third-party notices disclose the eight Tesseract ≤ 5.5.3 model-file
  advisories (CVE-2026-88047..-88054, no fixed release yet) with the
  scope argument and a standing re-check action.
- Retired dead review-ticket markers (`T-0xx`) from code comments.

### Testing & CI
- CI installs pinned, hash-locked dev tooling (`requirements-dev.in` ->
  `requirements-dev.txt`) instead of a floating `pip install pytest`;
  `actions/checkout`/`setup-python` are pinned to commit SHAs.
- New `packaging` CI job builds the wheel (`--no-isolation`, hashed
  setuptools), installs it, and smoke-tests `fermi-organize --help` plus
  `import fermi_report_xlsx` from a foreign cwd - the packaging contract is no
  longer validated only by the conftest sys.path shim.
- Coverage measurement added to the Linux test job (`pytest-cov`, ratchet floor
  80%; measured baseline 83%).
- New pinned behaviors: incremental `--rekey-titleblock` end-to-end,
  `_log_ocr_startup` message branches, empty-folder exit 1, NUL/control-char
  name sanitization, ValueError-tolerant placement, tesseract non-zero-exit
  handling, identical-archive copy skip.

### Licensing
- The project is now licensed **AGPL-3.0-only** (`LICENSE` added,
  `pyproject.toml` license field set), matching the AGPL terms of PyMuPDF;
  `THIRD_PARTY_NOTICES.md` records the decision and its obligations.

### Review follow-ups (PR #42 bot comments, verified before applying)
- Build floor is now `setuptools>=80`: the SPDX-string `project.license` form
  is rejected by setuptools 77 and below (measured by building with 68/77/80).
- Corrected the refusal guarantee everywhere (runbook, AGENTS.md, 0.2.0
  notes): a refused run skips folder placement and supersede swaps/moves, but
  the bounded orphan-parking and supersede-archive copies still run.
- Archive reuse is now logged (`already identical ... - not rewritten`) so
  repeat-run copy counts stay honest.
- Dry-run archive planning matches execution for identical same-name sources
  (same destination, no phantom `.1` suffix).
- Strip-OCR voting survives a single failing pass (remaining passes vote; a
  fully broken engine still raises).
- Timeout threat-model note now covers CLI OCR subprocesses only (the
  in-process `get_textpage_ocr` pass has no such timeout).
- Title-block OCR/zoom failures are recorded in the run issues instead of
  returning `(None, None)` silently (mismatch reporting no longer skips
  failed reads without a trace).

### Previously undocumented (folded in from 0.2.0-era commits)
- Excel formula-injection neutralization for PDF-derived cell values
  (`_inert_text`, 141aab5/d776322) with a cross-sheet fuzz test (b82ec61).
- PR #34 fixes: UTF-8 OCR subprocess decode (88d01aa) and `\\?\` long-path
  copies; earlier symlink-refusal warnings (c60c2a8, ab20f4c) and
  shared-stem folder shortening across all root-to-leaf paths (8af2948).
- Orphan retirement now requires a byte-identical **regular-file** live copy;
  symlinked live entries keep the parked orphan with a warning (6823f37).

## 0.2.0 — 2026-10-08

### Placement safety
- Placement refusal is surfaced: the run writes report + workbook, shows a
  "Placement refused" action row on the Dashboard, and exits with code 2.
  Folder placement and supersede swaps/moves are skipped, but the bounded
  orphan-parking and supersede-archive copies still run.
- The copy cap (`MAX_PLANNED_COPIES`) is enforced **per run** and checked
  before naming or any tree write; parked roots are excluded from the count,
  and a refused batch performs no supersede swaps or folder moves.
- Supersede archive-name collisions with different content are logged
  (`archive already exists with different content`) instead of staying silent.
- Orphan retirement byte-verifies the live tree copy (cache-free compare)
  before deleting the parked copy; a differing live copy keeps the orphan.

### Structure & stats
- Extraction results flow through a structured run context (counters, missing,
  chk, orphans, roots, used-on/title-block mismatches, scanned, watermarks,
  placement_refused); the workbook consumes it directly instead of parsing the
  text log. Parallel extraction merges worker OCR events back into the run.

### Reliability
- Iterative depth-capped traversals replace the recursive DFS; the
  process-wide `sys.setrecursionlimit` bump is gone (library callers no longer
  depend on the CLI setting it).
- The extraction process pool uses an explicit `spawn` start method.
- The staging sweep only removes the exact
  `<name>.pdf.supersede_tmp.<pid>` shape and prunes system dirs.

### Security & CI
- PyMuPDF CVE-2026-82035 (CLI-only `extract_objects()` traversal) documented
  with direct verification, scope reasoning, and a re-check-and-bump policy; a
  regression test pins that the PyMuPDF CLI is never invoked.
- CI adds a ruff job and installs the hash-pinned lock on a Python 3.10/3.11
  matrix (Windows + Linux).

### Excel report redesign (`fermi_report_xlsx.py`)
- Dashboard: 8 severity-tinted KPI cards with click-through links, a "Needs
  attention" banner + table (count, status pill, link per sheet), and a native
  bar chart; color guide and provenance footer.
- List sheets: Arial throughout, zebra rows, status pills, tab colors by
  severity, per-sheet "About this sheet" panel with a back-to-Dashboard link,
  friendly empty states, auto-fit widths, filters, landscape/fit-to-width print
  setup with repeating header row.
- Files / Orphans / Superseded: drawing names are hyperlinks that open the PDF;
  Files gains a folder column.
- Run History: DRY-RUN / EXECUTE mode pills, copies data bars, open-items trend
  line chart.
- Layout contract unchanged (header row 1, data from row 2, Dashboard B5/B6,
  sheet names/order), so existing readers and the full test suite are unaffected.

## 0.1.1 — 2026-10-06

### Duplicate resolution
- Same-revision duplicates now prefer the **text-layer copy** over an
  image-only (scanned) copy in the live tree: ranking is non-watermarked >
  text-layer > shallowest path > lexicographic.
- Duplicate resolution moved **before** BOM/USED ON/title-block extraction in
  both run modes (`_resolve_duplicates`, replacing the post-scan
  `_prefer_clean_duplicates`), so extraction always reads the copy that will be
  placed. Previously a scanned shallowest copy could source the tree's BOM —
  under `--no-ocr` it has none and its children were wrongly orphaned.
- Archival rule unchanged: watermarked losers go to `_superseded/` only when a
  clean copy exists; all other losing copies stay ignored in the input folder.

### Cleanup
- Import `pymupdf` directly instead of the legacy `fitz` shim: the shim's
  deprecation notice printed through the stdout handle pymupdf captured at ITS
  import time, so it leaked whenever pymupdf was imported before
  `fermi_organizer.extraction` (the old `redirect_stdout` buffer only covered
  the lucky import order). Removed the buffer and the now-dead warning filter
  in `fermi_organizer/__init__.py`; pinned by a subprocess regression test.

## 0.1.0 — 2026-09-30

Codebase review response (re-review #1 fix plan, 26 tasks). No feature changes;
behavior is preserved and pinned by a new test suite.

### Reliability
- Guard tree-mutation filesystem operations (place/copy/move/mkdir) so an
  `OSError` logs a warning instead of aborting the run; report writing is
  best-effort and warns without stopping the run.
- Run-history sidecar: atomic writes (`os.replace`), corrupt files preserved as
  `.corrupt`, and load failures surfaced instead of silently resetting history.
- Surface Tesseract detection/OCR failures with reasons; capture the detected
  Tesseract version and warn on versions older than v5.
- `TESSERACT_EXE` path is canonicalized (`realpath`) and documented as a
  `[SECURITY]` trust boundary; idempotent `PATH`/`TESSDATA_PREFIX` setup.

### Architecture
- Split the former god module: `graph.py` (pure algorithms), `naming.py`
  (folder naming/path policy), `fsops.py` (indexing, placement, tree scans,
  supersede/orphan copies).
- Decomposed `run_full` (cc 80→17) and `run_incremental` (cc 184→28) into
  phased helpers with byte-identical log output.
- Workbook now consumes a structured run context returned by
  `run_full`/`run_incremental`; the `.txt` log is no longer parsed and the
  prior-report fallback re-parse was removed.
- OCR state encapsulated in a run-scoped context (reset per run, no `id()`
  cache keys, bounded memos).
- Consolidated duplicated BFS/tree-scan/copy logic; one system-dir convention
  (top-level `_`-prefixed dirs are system).

### Quality
- Decomposed the highest-complexity functions (`extract_bom_from_text` cc 85→11,
  `build_workbook` cc 42→3, and 6 more), pinned by characterization diffs.
- Removed ~42 dead imports + a dead variable; added an AST-based dead-import
  test (`tests/test_no_dead_imports.py`).
- Removed 51 review-marker comments; fixed stale/contradictory comments;
  documented public functions and the workbook context contract.
- Externalized OCR geometry literals into named constants; fixed the dry-run
  `total_moves` counter; single-source depth/history caps.

### Dependencies / packaging
- `pyproject.toml` with `requires-python >=3.10` and a `fermi-organize` entry
  point; README Python floor corrected to 3.10+ (PyMuPDF 1.28.2 requires it).
- `requirements.lock` with SHA-256 hashes; `THIRD_PARTY_NOTICES.md` documenting
  PyMuPDF AGPL-3.0-or-commercial (own-license decision still pending) and the
  CVE-2026-82035 monitor note.

### Tests / repo
- 69 pytest tests (unit + end-to-end CLI smoke) covering graph, extraction,
  report glue, workbook, run modes, and packaging seams.
- `git init`, `.gitignore`, `SECURITY.md`; original `codebase-health-report.md`
  marked superseded.
