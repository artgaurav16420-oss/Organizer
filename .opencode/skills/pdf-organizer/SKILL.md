---
name: pdf-organizer
description: Run the Fermi lab PDF drawing organizer to sort drawings into nested folders from BOM tables. Use when the user asks to run or sort Fermilab PDFs, feed more PDFs into an existing output tree, or asks about _orphans, _superseded, missing drawings or CHK files.
compatibility: opencode
metadata:
  audience: user
  repo: fermi-pdf-organizer
---

# pdf-organizer

Operate the Fermi lab PDF drawing organizer safely. Never edit or delete the original PDFs.

## Decision flow

1. Resolve paths. Use `--output "<input>/Output"` to keep the tree separate from the input. Omit `--output` (in-place run) only if the user asks for it.
2. Dry-run first, every time. A dry run copies nothing:
   ```
   python -m fermi_organizer.cli "<input>" --output "<input>/Output" --dry-run
   ```
3. Report the dry-run plan to the user. Wait for explicit go-ahead.
4. Real run after approval:
   ```
   python -m fermi_organizer.cli "<input>" --output "<input>/Output"
   ```
5. Later batches into an existing tree: dry-run, review, then run with `--incremental`:
   ```
   python -m fermi_organizer.cli "<input_2>" --incremental --output "<existing_Output>" --dry-run
   python -m fermi_organizer.cli "<input_2>" --incremental --output "<existing_Output>"
   ```
6. Several input folders into one tree: dry-run all of them and report together. After approval, run a full run for the first folder, then `--incremental` for each later one. An incremental dry-run against a tree that does not yet hold the earlier batches shows their drawings as orphans. Expect small drift between dry-run and real-run counts; quote the real run's summary.
7. Command timeouts: use at least 10 minutes for multi-thousand-file folders. A quiet phase under `--- Scanning BOM tables ---` is normal, not a hang.

Use the venv interpreter if the repo has one (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` elsewhere). Do not run `fermi_organizer/cli.py` directly; its relative imports fail.

## Performance
- A recent run passes (extracted release) now scans each PDF once per phase. Typical bottlenecks are still OCR and very large BOM fan-out.
- Use `--jobs 4` (or your CPU count) for thousands of text PDFs; expect `jobs=2` and `jobs=1` to agree on results.
- Add `--no-ocr` when drawings are known to have text layers; otherwise OCR runs per scan and dominates runtime.
- Prefer `--output "<input>/Output"` so later runs do not re-scan an organized tree as input.
- A dry run with no output folder yet still processes scans and builds the workbook even though no text report is created until the folder exists.

## Flags

| Flag | Effect |
|---|---|
| `--output <dir>` | Target folder for the organized tree. Default: the input folder (in-place). |
| `--dry-run` | Print the plan, copy nothing. Rebuilds the Excel workbook, and writes the text report only if the output folder exists. |
| `--incremental` | Process only stems not already in the tree, plus stored `_orphans/`. Existing folders stay, except supersede swaps and parent-adoption moves. |
| `--no-ocr` | Disable the OCR fallback for scanned (image-only) PDFs. |
| `--jobs N` | Extraction workers. `0` = auto (CPU count, default), `1` = serial. |
| `--rekey-titleblock` | Re-key a misnamed text-layer PDF to its title-block drawing number, only if that drawing number is not already in the run. Scanned PDFs are never renamed. |

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| `0` | Normal completion. Missing BOM references, unapproved CHK files and orphans are reported but still exit 0. | Summarize the run and the two report files. |
| `1` | Startup failure. Input is missing or not a directory, no PDFs found, output folder cannot be created, or another run holds the lock (`ERROR: could not acquire run lock ...`). No report or workbook is written. | Fix the path or permissions, or wait for the other run to finish. Do not retry blindly. |
| `2` | Argument error (argparse usage message), or the run lock was lost mid-run (`ERROR: ...`). Report and workbook are not written for a lost lock. | Argument error: fix the flags. Lost lock: check no other process touched the output, then re-run. |
| `2` | Placement refused: planned fan-out exceeds 10,000 copies. The report and workbook are written. | Do not retry. Inspect the BOM graph and ask the user first. See the refusal section below. |

Both `2` rows share a code. Tell them apart by the log: a refusal writes `Report saved to:` and the workbook; a lost lock or argument error does not.

## Placement refusal

When the plan is too large, the largest roots are skipped. The rest is placed normally.

- Partial refusal log line: `WARNING: placement partially refused - <N> oversized root(s) skipped (<names>); the rest <was placed|would be placed> ...`
- Full refusal log line: `WARNING: placement refused - folder placement skipped ...`
- Excel Dashboard, "NEEDS ATTENTION" table, "Placement refused" row: count of skipped roots when any were skipped; `1` for a full refusal with no named roots; `0` otherwise.

Skipped roots are usually "bag" drawings such as procurement-kit lists, not real assemblies. Tell the user which roots were skipped and ask before re-running.

## Troubleshooting

| Symptom | Action |
|---|---|
| `No module named fermi_organizer` | Use the venv interpreter, or reinstall editable: `uv pip install -e .` |
| `.venv` missing | `uv venv --python 3.11 .venv`, then `uv pip install -r requirements.txt pytest -e .` |
| `OCR: unavailable` | Tesseract is not installed. A scanned PDF has no readable BOM of its own. It is placed only if a readable parent BOM references it by filename; otherwise it lands in `_orphans/`. Offer installing Tesseract. |
| Exit 2 with a placement-refused log line | Fan-out cap hit, not a crash. See Placement refusal above. |
| `Report NOT saved (output folder missing)` on a dry run | The text report is skipped because the folder does not exist yet. The workbook step runs next and creates the folder, so it exists afterward. |
| `WARNING: ...\Output exists. Use --output to target it.` with `--incremental` | Re-run with `--output "<input>\Output"`. |
| Everything landed in `_orphans/` | The parent drawings are not in the input folder yet. Files are placed once their parents arrive. |

## Report to the user

From the printed run output, state: PDFs scanned, root assemblies, BOM edges, cycles broken, PDF copies written; then the CHK list, missing BOM references, orphans and USED ON bugs; then the two artifact paths (`organize_fermi_pdfs_report_*.txt` and `organize_fermi_report.xlsx`).

Flag these: USED ON bugs, a large orphan count, `WARNING: in-place run`, OCR reading many PDFs, any `copy failed` or `path too long` warning, and any placement refusal.

Summarize the printed output. Do not parse the `.txt` report to compute results. The `.xlsx` is the source of truth. Sanity check after a real run: the Dashboard `PDF FILES IN TREE (LIVE)` value should equal the recursive PDF count of the output minus every top-level directory whose name starts with `_` (`_orphans/`, `_superseded/`, and any other).

## Expected noise

Not errors. Do not report these as failures:
- `WARNING: <N> duplicate stem(s) across input subfolders`. The shallowest copy wins.
- `Skipped <N> FC-prefixed common component(s)`. Standard hardware, never placed.
- `WARNING: <N> PDF(s) without extractable NAME`. Those folders use the drawing number.
- `Image too small to scale!!` and `Line cannot be recognized!!`. Tesseract stderr during OCR.

Judge a run by its exit code and summary.

## Guardrails

- Original PDFs are never modified or deleted. Only the output tree changes: copies, supersede swaps, moves.
- The BOM table is the only placement source. USED ON bugs are reported, never used to move files.
- `_orphans/` holds drawings whose parent has not arrived. `_superseded/` holds older revisions, unapproved CHK files and watermarked duplicates. Both are normal.
- OCR is best-effort. A misread can attach a wrong BOM edge, so present OCR-derived results as needing a spot-check.
- Never hand-edit the output tree to change placement. Fix the code or the input drawings.
