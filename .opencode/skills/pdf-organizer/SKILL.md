---
name: pdf-organizer
description: Run the Fermi lab PDF drawing organizer on an input folder — sorts Fermilab drawings into nested `{DRAWING_NUMBER} {NAME}` folders from their BOM tables, handles superseded revisions, unapproved CHK drawings and scanned (image-only) PDFs via OCR. Use when the user says "run the PDF organizer", "organize/sort my Fermilab PDFs", "sort these drawings", "feed more PDFs into the output tree", or asks what happened to `_orphans`, `_superseded`, missing drawings or CHK files.
compatibility: opencode
metadata:
  audience: user
  repo: fermi-pdf-organizer
---

# pdf-organizer — run the Fermi PDF organizer for the user

Given an input folder (and possibly an existing output tree), produce a correct,
reviewable organizer run: **dry-run first, real run only after the user says go.**
Never edit or delete the user's original PDFs.

Code for this repo lives in the current repository (see `AGENTS.md` here for the code
reference); this skill is the operator runbook. If the cwd is not inside the repo,
clone/locate the repo first and run the commands from it.

## Tool facts (verified)

- Requirements: Python >=3.10, PyMuPDF `==1.28.2` (required), openpyxl `==3.1.5`
  (optional — only the `.xlsx` workbook), Tesseract (optional, scanned PDFs only).
- Prefer the repo venv interpreter (Windows PowerShell, works from any cwd because the
  package is installed editable):

  ```
  & "<repo>\.venv\Scripts\python.exe" -m fermi_organizer.cli <folder> [flags]
  ```

  Other options: `fermi-organize` (console script, if the venv is on PATH), or
  `<repo>\organize_fermi_pdfs.py` (thin launcher, also works from any cwd). Do **not**
  run `fermi_organizer\cli.py` directly — its relative imports fail.
- The venv has **no pip**. Install with `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`
  (add `-e .` to restore the editable install). Plain `pip install -r requirements.txt`
  also works outside the venv.
- Tesseract absent → the run still succeeds; scanned PDFs stay `Orphans` and the log
  prints `OCR: unavailable (...)`. Use `--no-ocr` to skip OCR deliberately.

## Golden path (follow this order every time)

1. **Resolve paths.** With only an input folder given:
   - keep the tree separate (usual): `--output "<input>\Output"`
   - in-place (only if the user asks): omit `--output` — the tool then re-scans its own
     output on later full runs and prints a warning; mention it to the user.
2. **Always dry-run first** and show the user the printed plan:

   ```
   & "<repo>\.venv\Scripts\python.exe" -m fermi_organizer.cli "<input>" --output "<input>\Output" --dry-run
   ```

   A dry run copies nothing. Do not skip this even for a tiny folder.
3. **Report back** (see below) and wait for an explicit go-ahead. Only then re-run
   **without** `--dry-run`.
4. **Later batches** feeding an existing tree: `--incremental --output "<existing Output>"`.
   The input folder may differ from the one holding the tree.

## Flag decisions

| Situation | Flags |
|---|---|
| First sort of a folder | `--output "<input>\Output"` (dry-run first) |
| More PDFs for a tree that already exists | `--incremental --output "<existing Output>"` |
| User wants files organized beside the originals | omit `--output` |
| Tesseract missing, or speed over OCR | `--no-ocr` |
| Very large folder / slow extraction | `--jobs N` (0 = auto = CPU count, default; 1 = serial) |
| User trusts title blocks over filenames | `--rekey-titleblock` — opt-in only; re-keys misnamed **text-layer** PDFs, never scanned ones |

Exit code `1` (`ERROR: not a directory`, or no PDFs found) means bad input: fix the path
instead of retrying.

## Report to the user

From the printed run output, state: PDFs scanned, root assemblies, BOM edges, cycles
broken, PDF copies written; then the CHK list, missing BOM references, orphans and USED ON
bugs; then the two artifact paths in the output folder
(`organize_fermi_pdfs_report_*.txt` and `organize_fermi_report.xlsx`). Flag anything
surprising: USED ON bugs, `Cycles broken`, a large `Orphans` count,
`WARNING: in-place run`, or OCR reading many PDFs.

Do **not** parse the `.txt` report to compute results — the `.xlsx` workbook is the
source of truth; the printed summary is what to summarize.

## Guardrails

- Original PDFs are never modified or deleted; only the output tree is mutated (copies,
  supersede swaps, moves). Say so if the user is anxious.
- `_orphans/` = drawings whose parent is not in this folder, parked for later adoption.
  `_superseded/` = older revisions, unapproved CHK files, watermarked duplicates. Both
  are normal outcomes, not failures.
- The BOM is the only placement source. `USED ON bugs` are reported, never used to move
  files.
- OCR is best-effort: a misread can attach a wrong BOM edge. Present OCR-derived results
  as needing a spot-check.
- Scans stored at scanner dpi (1 px = 1 pt) are supported; rare digit misreads happen.
- For behavior changes, edit the code (`AGENTS.md` is the map) — never hand-edit the
  output tree to "fix" placement.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No module named fermi_organizer` | use the venv interpreter, or reinstall editable (`-e .`) |
| `.venv` missing | `uv venv --python 3.11 .venv` then install `requirements.txt pytest -e .` |
| `OCR: unavailable` | expected without Tesseract; scanned PDFs stay orphans — offer `--no-ocr` |
| `Report NOT saved (output folder missing)` on a dry run | normal: dry runs don't create the folder; the workbook is still written |
| `WARNING: ...\Output exists. Use --output to target it.` with `--incremental` | add `--output <input>\Output` and re-run |
| Everything landed in `_orphans/` | the parents are missing from the folder; they get placed once those PDFs arrive |

## Repo references

- `AGENTS.md` — code architecture, invariants, test gates.
- `README.md` — full flag table, outputs, known limits.
- `tests/test_golden_log.py` pins the user-facing log byte-for-byte; update it when
  messages intentionally change.