# Changelog

All notable changes to the Fermi PDF organizer are recorded here.

## Unreleased

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
