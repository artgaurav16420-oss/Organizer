# Changelog

All notable changes to the Fermi PDF organizer are recorded here.

## Unreleased

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
