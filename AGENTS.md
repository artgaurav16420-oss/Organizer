# AGENTS.md

## What this is

Fermi lab PDF drawing organizer. Scans a folder of PDF drawings, extracts BOM tables / USED ON / NAME fields (with OCR fallback for scanned pages), detects watermarks, builds a parent-child graph, and copies files into a nested folder tree. Produces a timestamped `.txt` report and a live `.xlsx` workbook after every run.

## Run

```bash
pip install -r requirements.txt          # plain Python >=3.10; or: uv pip install -r requirements.lock (SHA-256 pinned)
python organize_fermi_pdfs.py <folder> [--output DIR] [--dry-run] [--incremental] [--no-ocr] [--jobs N] [--rekey-titleblock]
```

- Use the repo venv: `.venv\Scripts\python.exe ...` (Python 3.11 + deps + pytest). It is uv-managed and has **no pip** (`python -m pip` fails) — install packages with `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`.
- The project is installed **editable** in `.venv`, so `python -m fermi_organizer.cli` and the `fermi-organize` console script work from any cwd. Running `fermi_organizer\cli.py` directly fails on relative imports; the launcher `organize_fermi_pdfs.py` (sys.path shim) also works from any cwd.
- `--dry-run` prints actions without copying files; still writes the timestamped `.txt` report when the output folder exists and always rebuilds the `.xlsx` workbook (including the history sidecar). A missing `--output` folder is created for the workbook only (report-only). Always run this first (README workflow).
- `--incremental` re-processes only stems not already in the output tree (the input scan is recursive) plus stored `_orphans/` (adoption); already-organized subfolders are never re-sorted — supersede swaps and parent-adoption moves still touch them. New BOM-less standalone PDFs are parked in `_orphans/` like a full run. With the default output it targets the input folder itself — to feed an existing tree at `<input>/Output`, pass `--output <input>/Output`.
- `--no-ocr` disables Tesseract fallback for image-only PDFs.
- `--jobs N` = extraction worker processes (0=auto=CPU count, 1=serial; default 0). N>1 uses a ProcessPoolExecutor and merges worker OCR events back into the run report.
- `--rekey-titleblock` (opt-in) trusts the title block over the filename: a **text-layer** PDF whose title-block number disagrees with its filename is re-keyed to the title-block number (revision adopted, copied file renamed) — only when no drawing of that number already exists in the run; otherwise it stays report-only. Scanned (OCR) numbers are never used to rename a file.
- Default output = **the input folder itself** (in-place). Pass `--output` to keep the tree separate (e.g. `<input>/Output`).

## Dependencies

- **PyMuPDF** — required; pinned `==1.28.2` in `pyproject.toml` / `requirements.txt`. Import as `pymupdf`, **never the legacy `fitz` shim** (the shim prints a deprecation notice through the stdout handle pymupdf captured at its import time, so it leaks into output whenever pymupdf was imported first).
- **openpyxl** — optional at runtime; only for the `.xlsx` workbook (ImportError is caught and logged, run continues).
- **Tesseract** — needed only for scanned (image-only) PDFs. Auto-detected via `TESSERACT_EXE`, then `PATH`, then common Windows dirs (Program Files, Program Files (x86), `%LOCALAPPDATA%\Programs`); only the common-dir fallback prepends the install dir to `PATH` and sets `TESSDATA_PREFIX` for PyMuPDF's OCR.
- `requirements.lock` is regenerated with `uv pip compile requirements.txt --generate-hashes -o requirements.lock` (command recorded in the lock header) whenever `requirements.txt` changes.

## Architecture

```
organize_fermi_pdfs.py   — thin launcher (sys.path shim -> fermi_organizer.cli:main)
fermi_organizer/
  cli.py                 — argparse, orchestration, Excel glue, recursion-limit bump
  config.py              — shared regexes, constants, path/depth limits, OCR geometry
  extraction.py          — BOM/table/text/OCR extraction, USED ON, NAME, title block (number/REV/NAME), watermark detection; OCR context
  graph.py               — pure algorithms: revision ranking, matching, cycles
  naming.py              — folder naming + path-length shortening
  fsops.py               — indexing, placement, tree scans, supersede/orphan copies
  runmodes.py            — run_full / run_incremental (return the structured run context)
  report_glue.py         — tree-derived NAME fallback for the workbook
fermi_report_xlsx.py     — standalone workbook builder (openpyxl, optional)
tests/                   — pytest suite (PDFs generated at test time)
```

## Key conventions

- **Stems are canonical uppercase** (`config.canonical_stem`): the part token is located anywhere in the name (numeric prefixes like `2.20.3.9 F10196150--CHK`, trailing titles like `F10137913 KIT, ...`), hyphens become underscores (`F10187622-B-CHK` -> `F10187622_B_CHK`), and `PART_STEM_RE` (`FC?` + 5-8 digits + `[A-Z0-9_]*`) validates the result; non-part PDFs are ignored with a warning. Hyphen/underscore/prefix variants of one drawing therefore share a stem. Input scan is recursive, but only **top-level** `Output/` (case-insensitive) and `_`-prefixed dirs are skipped — a nested `sub/Output/` is scanned. Duplicate stems: shallowest path wins (input originals beat nested organized-tree copies), then lexicographic order.
- **Revision ranking**: CHK (unapproved) < no-revision < A < B < … CHK stems rank below all DWG stems but still order among themselves.
- **BOM is the only source of truth** for parent-child edges. USED ON is a cross-check only, never a placement source: a child whose USED ON names a parent the parent's BOM does not list is a **USED ON bug** (reported, not placed). The reverse (BOM lists a part but its USED ON omits the parent) is also flagged.
- **Same-revision duplicates** (two PDFs with the same canonical stem): resolution runs **before** extraction (both run modes) so BOM/NAME/title block come from the copy that will be placed. Ranking: non-watermarked > text-layer > shallowest > lexicographic. Watermarked losers are archived in `_superseded/` only when a clean copy exists (all-watermarked → best-ranked used as-is, nothing archived); every other loser stays ignored in the input folder. "Same revision" means same canonical stem (hyphen/underscore/prefix variants of one name share it).
- **Title block check**: the drawing number, revision, and NAME printed in each scanned PDF's own title block are compared against the filename — the bottom-most `REV`/`NUMBER` labels are the title block (the revision-history RCD block sits higher). Mismatches go to the log and the workbook `Title block check` sheet; the filename drives placement unless `--rekey-titleblock` re-keys a misnamed **text-layer** file (see Run).
- **OCR geometry / reliability quirks** (extraction.py): OCR values use a wide symmetric window and ranked candidates (exact F-number > OCR-corrected > bare digits) because Tesseract word boxes can be offset by a row. Parts-list OCR reads the bottom-up Fermilab layout (rows above the `ITEM/FERMI#` header; the title-block `FERMI NATIONAL ACCELERATOR LABORATORY` text is never the header), retries psm 6 when a row strip glues the item digit into the FERMI token, and skips RCD numbers. OCR strips are read with a few zoom/psm/oem passes and voted (a value read by two passes wins); a pipe read as the digit 1 (`FLO|44633`) is corrected (`_ocr_tok`). Scanned sheets are A0-A4 stored either at ISO page size or at the scanner's dpi (1 px = 1 pt, e.g. a 153 dpi A0 scan is a 7152x5051 pt page): OCR coordinates are normalized to ISO page points and the full-page render dpi is capped at ~300 MP (`OCR_MAX_RENDER_MP`; a fixed 300 dpi render of that page is 627 MP and PyMuPDF's OCR fails silently), and strip/zoom magnification is divided by the page scale.
- **USED ON hygiene**: the drawing's own number is dropped from its USED ON values (the box neighbours the DRAWING NUMBER box) and from its BOM rows (an OCR parts-list read can pick up the title block's own FERMI number). OCR refs that are not clean `F`+8-digit reads (wrong length, or O/I/L/S/B/pipe confusions) are snapped to a unique known stem before edges/missing are computed; a clean read is trusted as-is (a genuine missing reference must stay visible for the Teamcenter download list even when some unrelated stem sits one digit away), and ambiguous reads are left as-is.
- **Folder names**: `{base} {NAME}` (e.g. `F10126106 Assembly bracket`), sanitized for Windows. Auto-shortened to stay under `MAX_PATH=250` / `MAX_DIR=240`.
- **Output tree**: organized folders + `_orphans/` (parked, adoptable by later incremental runs) + `_superseded/` (older revisions / unapproved CHK).
- **Workbook consumes a structured run context** — `run_full`/`run_incremental` return the counters/lists/names (`counters, missing, chk, orphans, roots, used_on_mismatches, used_on_bugs, titleblock_mismatches, scanned, watermarks, names`) that `cli.py` feeds to the Excel workbook directly; the `.txt` log is user-facing only and is no longer parsed. `used_on_bugs` lists children whose USED ON names a parent the parent's BOM does not list; `titleblock_mismatches` lists `(stem, field, filename value, title-block value)` rows; `scanned` lists image-only PDFs (recorded even with `--no-ocr`); `watermarks` lists detected watermark evidence. Folder names are still read for the NAME fallback (`report_glue.names_from_tree`).
- **Workbook state**: `<output>/organize_fermi_report.xlsx` plus a sidecar `<xlsx>.history.json`; Run History survives rebuilds only via the sidecar.
- Notable behavior changes are recorded in `CHANGELOG.md` (`## <version> - <date>` sections with `###` groupings).

## Testing

- Full suite: `.venv\Scripts\python.exe -m pytest -q` (~3 s). Single file: `... -m pytest tests/test_graph.py -q`; single test: `... -m pytest "tests/test_graph.py::test_name" -q`.
- Fixtures generate PDFs at test time via PyMuPDF (`make_pdf` in `tests/conftest.py`: one page, text lines from (72,72), fontsize 11, 16 pt line step — extraction tests depend on this layout). No binary fixtures are checked in; OCR is disabled for every test (autouse fixture), so Tesseract is not needed.
- The only lint is `tests/test_no_dead_imports.py`: an unused import in `fermi_organizer/*.py`, `organize_fermi_pdfs.py`, or `fermi_report_xlsx.py` fails the suite. There is no CI and no ruff/mypy config.
- Recreate the venv if missing: `uv venv --python 3.11 .venv; uv pip install --python .venv\Scripts\python.exe -r requirements.txt pytest -e .` (`-e .` restores the editable install; tests alone work without it because `conftest.py` shims the repo root onto `sys.path`).
- For end-to-end checks, run `--dry-run` against a real drawing folder and inspect the log output + `.xlsx`.

## Stale docs

- `codebase-health-report.md` is a superseded one-time review snapshot (banner at top). Its findings were largely fixed by the 2026-09-30 review response; verify against code before acting on it.
- README's test count ("99 tests") is stale (the suite is larger); trust the run output.
