# Changelog

All notable changes to the Fermi PDF organizer are recorded here.

## Unreleased

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
