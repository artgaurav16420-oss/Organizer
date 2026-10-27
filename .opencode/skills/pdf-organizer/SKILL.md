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

0. Gather exactly two things from the user - nothing else: the input folder and the output folder. Ask them as plain chat messages, one at a time, in this order:
   - First: "Input folder (the folder containing the PDFs)?"
   - After the answer: "What output folder path should I use?"
   Use plain text messages for both - no `question` tool, no options list. If the user replies nothing to the output question, fall back to the default `<input>/Output`.
   A repair-only request (the tree itself needs correcting) has no source: ask for that one tree folder instead of the pair, and run step 8 against it.
   Wait for the answer before asking the next question; do not batch them into one prompt.
   Do NOT ask about run mode, flags, dry-run, jobs, or anything else - the agent decides all of that.
   Then echo the resolved paths back and ask for confirmation before proceeding.
   Always quote paths that contain spaces in the final commands.
   If the pasted paths are wrapped in quotes, strip the quotes before echoing them back. If the input folder does not exist, say so and ask again instead of guessing; a missing output folder is fine - the run creates it.
   Run mode (full vs `--incremental` vs `--repair`) is the agent's decision, never a user question:
   - Output folder does not exist, or exists but holds no PDFs outside `_`-prefixed dirs -> full run.
   - Output folder already holds PDFs (an existing organized tree) -> `--incremental`.
   - Later batch into a tree built from earlier inputs -> `--incremental` (step 6).
   - The tree itself needs correcting and no new PDFs are coming in (the naming
     or placement rules changed since it was built, or a drawing sits in
     `_orphans/` or `_superseded/` by mistake) -> `--repair` on the tree (step 8).
   State the chosen mode and one-line reason in the dry-run report; do not ask the user to pick it.
1. Resolve paths. Use `--output "<input>/Output"` to keep the tree separate from the input. Omit `--output` (in-place run) only if the user asks for it.
2. Dry-run first, every time. A dry run copies nothing:
   ```
   python -m fermi_organizer.cli "<input>" --output "<input>/Output" --dry-run
   ```
3. Report the dry-run plan to the user, then **wait for their go-ahead**. Never start the real run on your own judgement, however clean the dry run looked: it creates folders and copies PDFs. Ask, and use the words "Run" as the trigger only when the user has said it.
4. Real run, only after the user approves the dry run:
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
8. Repairing an existing tree (no new PDFs, no source folder):
   ```
   python -m fermi_organizer.cli "<existing_Output>" --repair --dry-run
   python -m fermi_organizer.cli "<existing_Output>" --repair
   ```
   Read the `--- Repair (tree reconciliation) ---` block before running for real:
   every rename and move is listed, and `Needs manual review` > 0 means exit 2.
   A hand-renamed or hand-moved folder is a difference from the plan and gets
   reverted, so say so to the user if the tree was curated by hand. Do not pass
   `--output`, `--incremental` or `--rekey-titleblock` with `--repair`.

   **Skip the real run when the dry run reports no action at all.** Two
   conditions, both required:

   1. all eight counts are `0` (renamed, moved, copied, removed, emptied,
      adopted, promoted, review), and
   2. the log contains no `[DRY-RUN]` action line and no
      `Retired ... orphan copy(ies)` line.

   The eight counters do not cover orphan retirement: a duplicate `_orphans/`
   copy whose stem is already live in the tree is deleted by a pass that only
   logs `[DRY-RUN] retire orphan copy: ...`, so counting alone would skip a
   real deletion. When both conditions hold the tree is already on plan and the
   real run would re-extract every drawing for the same conclusion. The dry run
   writes the report and workbook itself — but either write can fail with a
   warning, so check the tail: if an artefact is missing, run the real pass;
   otherwise quote the numbers and stop.

   Run `--repair` for real when anything above is non-empty, and re-run the dry
   run first if something touched the tree in between (another process, a
   manual rename, a copy in progress).

   **Exception — do not run when the refusal is structural and nothing else is
   pending.** If `Needs manual review` is the only non-zero count, the review
   lines say the tree's root-to-leaf paths exceed the planning bound, and the log
   has no `[DRY-RUN]` action line, then the real run refuses in exactly the same
   place and exits 2 again: it is not a fix, it is a repeated refusal. Surface
   the line to the user and stop.

   If the dry run also lists an action — typically `[DRY-RUN] retire orphan
   copy: ...` — run for real instead: retirement does not depend on the refused
   reconcile, so that pass completes the deletion and still exits 2 for the
   review line.

   Repair reads only the tree, so splitting the source input cannot clear this —
   the tree's own BOM graph has to change (fewer drawings in the tree, or a full
   run from a narrower input into a separate output).

Use the venv interpreter if the repo has one (`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` elsewhere). Do not run `fermi_organizer/cli.py` directly; its relative imports fail.

## Performance
- A recent run passes (extracted release) now scans each PDF once per phase. Typical bottlenecks are still OCR and very large BOM fan-out.
- **Check the logical-processor count first, then pass an explicit worker count.** Do not rely on `--jobs 0`: auto means *every* logical processor, which leaves no headroom for the OCR subprocesses and the parent's report/workbook work.

  ```powershell
  # Windows
  (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
  ```
  ```bash
  # Linux / macOS
  nproc 2>/dev/null || sysctl -n hw.ncpu
  ```
  ```python
  # either platform, using the project venv
  python -c "import os; print(os.cpu_count())"
  ```

  **Container caveat:** `os.cpu_count()` reports the *host's* processors, not a
  cgroup quota, and `nproc` respects CPU affinity but not a CFS quota. On a
  CPU-limited container or a shared CI runner, pass the limit explicitly
  instead of deriving it (`--jobs 6` under an 8-CPU quota). On Linux with
  affinity pinning, `len(os.sched_getaffinity(0))` is the honest count.

- **Assign `logical processors - 2` workers** (logical processors = `os.cpu_count()`, i.e. two per physical core on SMT), minimum 1, never above `MAX_JOBS` (256):

  | Logical processors | `--jobs` |
  |---|---|
  | 4 | 2 |
  | 8 | 6 |
  | 12 | 10 |
  | 16 | 14 |
  | 32 | 30 |

  SMT hardware exposes two logical processors per physical core, so `-2` frees
  one whole core for the parent process (report and workbook writing) and for an
  OCR pass. It is a floor, not a guarantee: several workers can each hold a
  Tesseract child at the same time, so a scanned-heavy batch can still
  oversubscribe. If the machine feels loaded, drop a few more; on hardware
  without SMT the same formula simply leaves two cores. A 16-thread box
  (8 cores) therefore runs `--jobs 14`.
- Expect `jobs=2` and `jobs=1` to agree on results; the worker count changes speed, never the plan.
- Add `--no-ocr` when drawings are known to have text layers; otherwise OCR runs per scan and dominates runtime.
- Prefer `--output "<input>/Output"` so later runs do not re-scan an organized tree as input.
- A dry run with no output folder yet still processes scans and builds the workbook even though no text report is created until the folder exists.

## Flags

| Flag | Effect |
|---|---|
| `--output <dir>` | Target folder for the organized tree. Default: the input folder (in-place). |
| `--dry-run` | Print the plan, copy nothing. Rebuilds the Excel workbook, and writes the text report only if the output folder exists. |
| `--incremental` | Process only stems not already in the tree, plus stored `_orphans/`. With nothing new it stops after a zero-count summary and touches nothing. When the batch has new PDFs, existing folders stay but their names are re-applied to this run's plan (a hand-titled folder is renamed back; a folder whose planned parent changed is moved), plus supersede swaps. |
| `--repair` | Re-apply the current decisions to an existing tree (the positional folder IS the tree; no source scan, no `--output`). Renames folders to the current plan, moves folders onto their planned parent, adopts wrongly parked `_orphans/`, promotes a `_superseded/` copy that out-ranks the live drawing, removes verified duplicates. Places nothing new; a folder is only deleted when every file in it has a byte-identical copy at a planned path. |
| `--no-ocr` | Disable the OCR fallback for scanned (image-only) PDFs. |
| `--jobs N` | Extraction workers. Check logical processors first and pass `logical processors - 2` (16 threads -> `14`); see **Performance**. `0` = auto = every logical processor when `os.cpu_count()` reports one (4 if it does not), `1` = serial. |
| `--rekey-titleblock` | Re-key a misnamed text-layer PDF to its title-block drawing number, only if that drawing number is not already in the run. Scanned PDFs are never renamed. |

## Exit codes

| Code | Meaning | Action |
|---|---|---|
| `0` | Normal completion. Missing BOM references, unapproved CHK files and orphans are reported but still exit 0. | Summarize the run and the two report files. |
| `1` | Startup failure OR an unexpected crash (which can happen after copies were written). Startup: input missing/not a directory, no PDFs, output folder uncreatable, the lock cannot be created, or another run holds the lock (`ERROR: output tree is locked (.fermi_organizer.lock present ...)`, or the third-attempt fallback `ERROR: could not acquire run lock ...`). No report/workbook for startup failures; a crash may leave partial writes. | Fix the path or permissions, or wait for the other run to finish. Do not retry blindly. |
| `2` | Argument error (argparse usage message), or the run lock was lost mid-run (`ERROR: ...`). Report and workbook are not written for a lost lock. | Argument error: fix the flags. Lost lock: check no other process touched the output, then re-run. |
| `2` | Placement refused: planned fan-out exceeds 10,000 copies. The report and workbook are written. | Do not retry. Inspect the BOM graph and ask the user first. See the refusal section below. |
| `2` | Repair left folders needing manual review (`Needs manual review: N` in the Repair block). The report and workbook are written. | Read the review lines in the report; do not retry. The tree is otherwise reconciled. |

These three `2` rows share a code. Tell them apart by the log: a placement refusal or a repair review writes `Report saved to:` and the workbook; a lost lock or argument error does not.

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
| `.venv` missing | `uv venv --python 3.11 .venv`, then `uv pip install -r requirements.txt -r requirements-dev.txt -e .` (the dev lock provides ruff, pytest-cov and coverage) |
| `OCR: unavailable` | Tesseract is not installed. A scanned PDF has no readable BOM of its own. It is placed only if a readable parent BOM references it by filename; otherwise it lands in `_orphans/`. Offer installing Tesseract. |
| Exit 2 with a placement-refused log line | Fan-out cap hit, not a crash. See Placement refusal above. |
| `Report NOT saved (output folder missing)` on a dry run | The text report is skipped because the folder does not exist yet. The workbook step runs next and creates the folder, so it exists afterward. |
| `WARNING: ...\Output exists. Use --output to target it.` with `--incremental` | Re-run with `--output "<input>\Output"`. |
| Everything landed in `_orphans/` | The parent drawings are not in the input folder yet. Files are placed once their parents arrive. |

## Report to the user

From the printed run output, state: PDFs scanned, root assemblies, BOM edges, cycles broken, PDF copies written; then the CHK list, missing BOM references, orphans and USED ON bugs; then the two artifact paths (`organize_fermi_pdfs_report_*.txt` and `organize_fermi_report.xlsx`). The workbook links every sheet except Missing to its drawing, so say whether the links are live: they are on a real run and on `--repair --dry-run`, and they are suppressed on a plain or incremental dry run because those rows describe input PDFs the tree does not hold yet.

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
