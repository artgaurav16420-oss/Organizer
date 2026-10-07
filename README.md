# PDF Organizer — Fermi Drawing Sorter

Sorts Fermilab PDF drawings into a nested folder tree derived from the
BOM (PARTS LIST) tables inside each drawing, using the title block NAME for
folder names. The BOM is the **only source of truth** for parent-child
edges; USED ON is cross-checked but never used for placement (a USED ON that
names a parent the parent's BOM does not list is reported as a USED ON bug).
Handles superseded revisions, unapproved CHK drawings, scanned (image-only)
PDFs via OCR, and incremental batches.

**This folder holds program files only.** Input PDFs, the organized tree,
reports and Excel workbooks all live in the input folder you pass. Without
`--output`, the tree is organized in place there; `<input-folder>/Output` is
the usual target when you want to keep it separate.

## Requirements

- Python 3.10+
- Tesseract OCR (optional — without it scanned PDFs stay logged as orphans);
  auto-detected via the `TESSERACT_EXE` env var, then `PATH`, then common
  install dirs (`C:\Program Files\Tesseract-OCR`,
  `C:\Program Files (x86)\Tesseract-OCR`,
  `%LOCALAPPDATA%\Programs\Tesseract-OCR`)
- Python packages: see `requirements.txt`
  ```
  pip install -r requirements.txt
  ```

## Licensing & dependency notes

- Third-party licenses and the pending own-license decision: see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
- Reproducible installs with SHA-256 hashes: `uv pip install -r requirements.lock`.
- PyMuPDF: watch for >=1.28.3 (CVE-2026-82035 affects <=1.28.2 only in the PyMuPDF CLI font path, which this project does not use).
- Tesseract OCR: 5.5.3+ recommended.

## Usage

```
python organize_fermi_pdfs.py <input-folder> [--output <folder>] [--dry-run] [--incremental] [--no-ocr] [--jobs N] [--rekey-titleblock]
```

Input is scanned recursively — every `*.pdf` under the input folder counts, at any depth,
except those under a top-level `Output/` (case-insensitive) or `_`-prefixed folder
(a nested `sub/Output/` is scanned). Use `--output` to point
the organized tree elsewhere (typically `<input-folder>/Output`).

| Flag | Effect |
|---|---|
| `--dry-run` | Print the plan without copying files. Still writes the timestamped `.txt` report when the output folder exists, and always rebuilds the `.xlsx` workbook + history sidecar; a missing `--output` folder is created for the workbook only (report-only). Always run this first. |
| `--incremental` | Process only stems not already in the output tree plus stored `_orphans/` (adoption); the input scan is recursive. Existing organized folders stay put except when a new revision supersedes an old one or a parent adoption moves them. |
| `--output <dir>` | Organized tree target (default: the input folder itself — in-place). |
| `--no-ocr` | Disable the OCR fallback for scanned PDFs. |
| `--jobs N` | Parallel extraction workers (0=auto = CPU count, 1=serial). |
| `--rekey-titleblock` | Trust the title block over the filename: re-key a misnamed **text-layer** PDF to its title-block drawing number (revision adopted, copied file renamed). Only when no drawing of that number already exists in the run; scanned (OCR) numbers never rename a file. |

Typical flow:

```
# 1. first batch - full sort, always dry-run first
python organize_fermi_pdfs.py "D:\data\ColdMass-SSR2 PDF" --output "D:\data\ColdMass-SSR2 PDF\Output" --dry-run
# 2. review the printed summary, then execute
python organize_fermi_pdfs.py "D:\data\ColdMass-SSR2 PDF" --output "D:\data\ColdMass-SSR2 PDF\Output"
# 3. later batches feeding the SAME output tree (any input folder)
python organize_fermi_pdfs.py "D:\data\more-pdfs" --incremental --output "D:\data\ColdMass-SSR2 PDF\Output" --dry-run
python organize_fermi_pdfs.py "D:\data\more-pdfs" --incremental --output "D:\data\ColdMass-SSR2 PDF\Output"
```

## Outputs

- `<Output>/` — the organized tree: `{DRAWING_NUMBER} {PART NAME}`
  folders nested per the BOM hierarchy (default output is the input folder itself).
- `<Output>/_superseded/` — older revisions and CHK files replaced by an
  approved DWG counterpart, plus watermarked duplicates of a revision that a
  non-watermarked copy won (see below).
- `<Output>/_orphans/` — drawings with no parent seen yet; a future
  incremental run adopts them automatically when their parent arrives.
- `<Output>/organize_fermi_report.xlsx` — live status workbook, rebuilt after
  every run: Dashboard, Missing (Teamcenter download list), CHK, Scanned
  (image-only PDFs read via OCR), Watermark, Orphans, Superseded, Roots (root
  drawings + their USED ON), USED ON check (BOM vs USED ON mismatches in both
  directions), Title block check (filename vs the drawing's own title block),
  Files, Run History.
- `<Output>/organize_fermi_pdfs_report_*.txt` — full text log of each run,
  including missing BOM references and OCR recovery details.

## Layout

```
organize_fermi_pdfs.py     launcher (thin shim)
fermi_report_xlsx.py       live Excel workbook writer (openpyxl, optional)
fermi_organizer/
  __init__.py            pymupdf-direct import guard (never the legacy `fitz` shim)
  cli.py                   argparse + logging + report wiring
  config.py                constants + regexes + OCR geometry
  extraction.py            BOM/USED ON/NAME extraction + OCR fallback + run-scoped OCR context
  graph.py                 pure algorithms: revision ranking, matching, cycles
  naming.py                folder naming + path-length shortening
  fsops.py                 input indexing, placement, tree scans, supersede/orphan copies
  runmodes.py              run_full + run_incremental (return a structured run context)
  report_glue.py           tree-derived NAME fallback
tests/                     pytest suite (100+ tests (see `pytest` output); PDFs generated at test time)
```

## AI agent usage

An operator runbook for AI agents ships with the repo:
`.opencode/skills/pdf-organizer/SKILL.md`. OpenCode discovers it automatically in any
session started inside this repo, and other agent tools can be pointed at the same file.
It encodes the safe workflow (dry-run -> review -> execute), flag decisions, how to
summarize a run, guardrails (originals are never modified) and troubleshooting.

To organize a folder outside the repo, clone this repo, install its dependencies
(`pip install -r requirements.txt`, plus `pip install -e .` for the module form), then
run from the repo root:

```
python -m fermi_organizer.cli "<input-folder>" --output "<input-folder>\Output" --dry-run
```

The input folder itself can be anywhere on disk; only the *invocation* must happen with
the repo as the working directory (or with the repo's venv interpreter, which works from
any directory once installed editable).

## Recovery & safe operation

- Always run `--dry-run` first and review the printed plan before executing.
- Original PDFs are never modified or deleted; keep them as your backup. Only
  the organized tree is mutated (copies, supersede swaps, moves).
- Supersede swaps archive the old revision under `_superseded/` before
  replacing it in the tree; adopted files remain available under `_orphans/`
  until they are placed.
- An interrupted run is safe to re-run: incremental mode leaves existing
  folders alone, and a full re-run rebuilds the tree from the input PDFs.

## Known limits

- Odd-shaped/rare drawing layouts can defeat the parts-list detector. OCR
  values are shape-validated and only match indexed drawing numbers, so
  misreads are rare — but a misread that collides with a different indexed
  number can still attach a wrong BOM edge. Treat OCR-derived edges as
  best-effort. The OCR parts-list reader is tuned for the Fermilab bottom-up
  layout (rows above the ITEM/FERMI# header) and was verified against scanned
  drawings with text-layer twins; scanned title-block number/rev and NAME are
  best-effort (Tesseract sometimes misses the small labels entirely). Sheets
  are A0-A4; scans stored at the scanner's dpi (1 px = 1 pt) are normalized to
  ISO page points and the OCR render is pixel-capped, but the smallest text on
  153 dpi A0 scans is at Tesseract's reliability edge (occasional digit
  misreads, e.g. F10144633 read as F10144635).
- Missing drawings (BOM refs with no matching PDF) and CHK stems are listed
  for Teamcenter download; CHK files are removed automatically once their
  approved DWG arrives in any input folder.
- USED ON is never a placement source. A child whose USED ON names a parent
  the parent's BOM does not list is reported as a USED ON bug (and stays
  wherever the BOM places it, or in `_orphans/` if it has no BOM parent);
  a BOM child whose USED ON omits the parent is flagged in the same sheet.
- Watermark detection is best-effort: watermark layers, transparent / diagonal
  / light large text, stamp annotations, and common phrases (UNCONTROLLED,
  PRE-RELEASED, DRAFT, PRELIMINARY, …). The evidence is listed in the
  Watermark sheet; verify before acting.
- Same-revision duplicates (two PDFs with the same canonical stem): the copy
  used in the tree is chosen before extraction — non-watermarked beats
  watermarked, then a text-layer copy beats an image-only (scanned) one, then
  the shallowest path wins. Watermarked duplicates are archived in
  `_superseded/` when a clean copy exists; otherwise one copy is used as-is.
  Losing clean copies are left in the input folder. Stems are canonicalized
  (hyphens -> underscores, numeric prefixes and trailing titles stripped), so
  hyphen/underscore/prefix variants of one drawing are the same revision.
- The drawing's own title block is authoritative: its drawing number, revision,
  and NAME are compared against the filename, and disagreements are listed in
  the Title block check sheet (misnamed files, stale revisions). Placement
  still follows the filename in this version, unless `--rekey-titleblock` opts
  into title-block authority for misnamed text-layer files.
