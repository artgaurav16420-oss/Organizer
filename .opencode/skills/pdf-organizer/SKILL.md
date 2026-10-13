---
name: pdf-organizer
description: Run the Fermi lab PDF drawing organizer to sort drawings into nested folders from BOM tables when the user says "run the PDF organizer", "organize/sort my Fermilab PDFs", "sort these drawings", "feed more PDFs into the output tree", or asks what happened to `_orphans`, `_superseded`, missing drawings or CHK files.
compatibility: opencode
metadata:
  audience: user
  repo: fermi-pdf-organizer
---

# pdf-organizer

Operate the Fermi lab PDF drawing organizer safely and correctly. Never edit or delete the original PDF files.

## Decision flow

1. Resolve input and output paths. By default, specify `--output "<input>\Output"` to keep the organized tree separate from the input folder; omit `--output` only if an in-place run inside the input folder is explicitly requested.
2. Always perform a dry run first to generate and review the plan without copying files:
   ```
   python -m fermi_organizer.cli "<input>" --output "<input>\Output" --dry-run
   ```
3. Report the dry-run summary to the user and wait for explicit go-ahead.
4. Execute the real run without `--dry-run` after receiving approval:
   ```
   python -m fermi_organizer.cli "<input>" --output "<input>\Output"
   ```
5. Process subsequent PDF batches feeding an existing tree using incremental mode (always dry-run first, review, then execute):
   ```
   python -m fermi_organizer.cli "<input_2>" --incremental --output "<existing_Output>" --dry-run
   python -m fermi_organizer.cli "<input_2>" --incremental --output "<existing_Output>"
   ```
6. Handle multi-folder inputs by dry-running all folders first and reporting the plans together. After approval, execute sequentially with a full run for the first folder and incremental runs for subsequent folders.
7. Use command timeouts of at least 10 minutes for multi-thousand-file folders. Do not treat a quiet scan phase under `--- Scanning BOM tables ---` as a hang.

## Flags

| Flag | Effect |
|---|---|
| `--output <dir>` | Target folder for the organized tree. Default is the input folder itself (in-place run). |
| `--dry-run` | Prints the plan without copying files. Rebuilds the Excel workbook and writes the text report if the output directory exists. |
| `--incremental` | Processes only stems not already in the output tree plus stored `_orphans/`. Keeps existing folders except for supersede swaps and parent-adoption moves. |
| `--no-ocr` | Disables Tesseract OCR fallback for scanned (image-only) PDFs. |
| `--jobs N` | Sets parallel extraction worker count (`0` = auto / CPU count, default; `1` = serial). |
| `--rekey-titleblock` | Re-keys misnamed text-layer PDFs to their title-block drawing number if that drawing number is not already present in the run. |

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| `0` | Normal completion. Missing BOM references, unapproved `CHK` files, and orphans are reported but still exit 0. | Review the printed run summary and generated report artifacts (`organize_fermi_pdfs_report_*.txt` and `organize_fermi_report.xlsx`). |
| `1` | Startup failure or invalid arguments. Triggered when the input path is missing or not a directory, no PDFs are found, the output folder cannot be created on a real run, or `.fermi_organizer.lock` is held by another process (`ERROR: ... lock ...`). No report or workbook is written. | Check the input/output paths and permissions, or wait for any concurrent run holding the lock to complete. Do not retry without resolving the underlying path or lock issue. |
| `2` | Non-successful completion. Caused either by over-cap placement refusal (planned fan-out exceeds 10,000 copies; oversized root assemblies are skipped while rest is placed, or placement is skipped entirely if no roots fit; report `.txt` and workbook `.xlsx` ARE written) or by run-lock loss mid-run (lock stolen mid-run; report `.txt` and workbook `.xlsx` ARE NOT written). | Inspect the output folder for the report `.txt` and workbook `.xlsx`. If both artifacts exist, placement refusal occurred: check the log for skipped root assemblies and consult the user before re-running. If artifacts are missing, the lock was lost mid-run: ensure no concurrent process interfered and re-run. |

Placement refusal log lines and Dashboard behavior:
- Partial refusal log line: `WARNING: placement partially refused - <N> oversized root(s) skipped (<names>); the rest <was placed|would be placed> (bounded orphan/archive copies are still handled); review the BOM graph (report + workbook still written)`
- Full refusal log line: `WARNING: placement refused - folder placement skipped (bounded orphan/archive copies still ran); review the BOM graph (report + workbook still written)`
- On the Excel Dashboard sheet ("NEEDS ATTENTION" table), the "Placement refused" category count equals `len(skipped_roots)` when oversized roots are skipped, or `1` if full placement refusal occurred without skipped roots, or `0` when no refusal occurred.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No module named fermi_organizer` | Run via the repository venv interpreter (`.venv\Scripts\python.exe -m fermi_organizer.cli`), or reinstall editable (`uv pip install -e .`). |
| `.venv` missing | Recreate the virtual environment using `uv venv --python 3.11 .venv` and install dependencies with `uv pip install -r requirements.txt pytest -e .`. |
| `OCR: unavailable` | Expected when Tesseract is not installed; scanned PDFs referenced by a parent BOM are still placed, while unreferenced standalone ones stay in `_orphans/`. Pass `--no-ocr` to suppress the check deliberately. |
| Exit code 2 with placement refusal log line | The 10,000-copy fan-out cap was hit. Placement was partially refused (oversized roots skipped while fitting roots were placed) or fully refused (folder placement skipped entirely). Inspect the BOM graph in the report/workbook and ask the user before re-running. |
| `Report NOT saved (output folder missing)` on a dry run | Expected behavior when a dry run targets a non-existent `--output` directory. The text report is skipped because the directory does not exist yet, but the workbook builder then creates the output folder and saves `organize_fermi_report.xlsx`. |
| `WARNING: <path>\Output exists. Use --output to target it.` with `--incremental` | Specify `--output <path>\Output` explicitly to target the existing output tree. |
| Everything landed in `_orphans/` | Parent drawings are missing from the input folder. Files remain parked in `_orphans/` until parent PDFs arrive in a future run. |

## Guardrails

- Original PDF files are never modified or deleted. Only the output tree is mutated through file copies, supersede swaps, and moves.
- The BOM (parts list) table inside each drawing is the only source of truth for folder placement. `USED ON` fields are cross-checked and reported as USED ON bugs when they mismatch, but are never used to move files.
- `_orphans/` stores drawings whose parent assembly has not been provided yet, parked for future adoption. `_superseded/` stores older revisions, unapproved `CHK` files, and watermarked duplicates.
- OCR text extraction is best-effort. Misread numbers can attach incorrect BOM edges, so present OCR-derived placements as requiring a spot-check.
- Never hand-edit the output directory tree to alter placement. Modify the tool source code or correct the input drawing files instead.

## Expected noise

The following log lines represent standard operation and do not indicate errors:
- `WARNING: <N> duplicate stem(s) across input subfolders`
- `Skipped <N> FC-prefixed common component(s)`
- `WARNING: <N> PDF(s) without extractable NAME`
- `Image too small to scale!!` and `Line cannot be recognized!!` (Tesseract stderr output during OCR)
