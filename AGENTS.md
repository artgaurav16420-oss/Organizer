Karpathy Guidelines v3.7 MANDATORY: For all AI operations in this project, you MUST follow karpathy-guidelines.md as the primary behavioral ruleset.
# AGENTS.md

Fermi lab PDF drawing organizer: recursively scans a folder of PDFs, extracts BOM tables / USED ON / NAME (OCR fallback for scanned pages), builds a parent-child graph, copies into a nested tree; writes a timestamped `.txt` report + live `.xlsx` workbook per run.

Operator runbook: `.opencode/skills/pdf-organizer/SKILL.md` (auto-loaded). That file is the golden path; this file is the code reference.

## Commands

- Use the repo venv: `.venv\Scripts\python.exe` (Windows) / `.venv/bin/python` (Linux/macOS). Python >=3.10. No pip; install with `uv pip install --python .venv\Scripts\python.exe ...`.
- Run: `python organize_fermi_pdfs.py <folder> [--output DIR] [--dry-run] [--incremental] [--repair] [--no-ocr] [--jobs N] [--rekey-titleblock]` (also `python -m fermi_organizer.cli ...`, or `fermi-organize` after install). Never run `fermi_organizer\cli.py` directly (relative imports fail).
- Repair: `python organize_fermi_pdfs.py <tree> --repair [--dry-run]` re-applies the current decisions to an existing output tree (no source scan; `--output`/`--incremental`/`--rekey-titleblock` are rejected with it).
- Tests: `.venv\Scripts\python.exe -m pytest -q` (~15s; 476 passed + 8 skipped symlink tests without dev-mode privilege). Single file/case: `... -m pytest tests/test_graph.py -q` / `... "tests/test_graph.py::test_name" -q`. No Tesseract needed (autouse OCR fixture).
- Lint: `.venv\Scripts\ruff.exe check --select F,E9,B fermi_organizer tests fermi_report_xlsx.py organize_fermi_pdfs.py` (ruff 0.16.10, no config file; plain `ruff check` is not equivalent).
- Pre-push order: lint -> pytest.
- Package build: `rm -rf build dist && python -m build --wheel --no-isolation` (setuptools floor >=80). The clean-up is not optional: `--no-isolation` reuses the working-tree `build/lib`, so a stale or deleted module still ships in the wheel (verified: a ghost module copied into `build/lib` ended up in the wheel).

## Setup / env

- Add deps: edit `requirements.txt`, then `uv pip compile requirements.txt --generate-hashes -o requirements.lock`; dev tools via `requirements-dev.in` -> `requirements-dev.txt` (`--universal --python-version 3.10`). There is no `uv.lock` in the repo (a tracked one nothing consumed was removed; `uv.lock` is gitignored, so a local `uv sync` recreates it as untracked noise - delete it, never commit it).
- Always `import pymupdf`, never the `fitz` shim (deprecation notice leaks into captured stdout). Pinned `pymupdf==1.28.2`; `openpyxl==3.1.5` is optional (workbook skipped on ImportError).
- Preflight (no `uv sync`): this repo has no `uv.lock` and `uv sync` is not used; verify `.venv` exists and imports resolve before running, and recreate the venv with `uv pip install` if needed.
- Python >=3.10. CI runs Ubuntu 3.10 and 3.11, so do not use 3.11-only stdlib (`tomllib`, `datetime.UTC`, `except*` groups). Setuptools floor is >=80 because older releases reject the SPDX `license` string.
- Tesseract only for scanned PDFs: resolution order `TESSERACT_EXE` -> PATH -> common Windows dirs.
- One run per output folder is enforced by `<output>/.fermi_organizer.lock`: exit 1 if held (stale >2h is stolen with a warning), exit 2 if stolen mid-run. Network shares (SMB/NFS/mapped drives) are not lock-safe; coordinate manually.

## Architecture

`organize_fermi_pdfs.py` (launcher) -> `fermi_organizer/cli.py` (argparse, orchestration, Excel glue)

- `config.py`: regexes, constants, limits (`MAX_PATH=250`, `TREE_MAX_DEPTH=500`, `MAX_PLANNED_COPIES=10000`)
- `extraction.py`: BOM / USED ON / NAME / title block / watermark + OCR; `survey_task` opens each PDF once and returns all of them; the older `bom_task` / `title_task` / `org_task` are thin adapters over `survey_task` (preserving their legacy return shapes)
- `graph.py`: pure algorithms (revision ranking, matching, cycles, `planned_paths` = a graph's planned folder paths)
- `naming.py`: folder naming + path shortening
- `fsops.py`: indexing, placement, tree scans, supersede/orphan copies, run lock, `reconcile_tree` (repair's move/prune pass)
- `runmodes.py`: `run_full` / `run_incremental` / `run_repair` -> structured run context (workbook input, never the `.txt`); incremental and repair share `_run_delta_inner`, differing only in the candidate source (`_repair_prepare`) and the reconcile pass
- `report_glue.py`: tree-derived NAME fallback
- `fermi_report_xlsx.py`: standalone workbook builder (optional)

## Invariants (agents get these wrong)

- BOM is the only placement source. USED ON is cross-check only: child USED ON naming a parent whose BOM omits it = USED ON bug (reported, never placed); reverse mismatch also flagged.
- A parts-list row must sit in the page's FERMI# column (`extraction._partslist_band`: header `FERMI #` label, else the densest aligned F-number column; `None` = no gate). A view balloon prints a bare part number away from the table and is not a row. Names: an OCR-read NAME, or a drawing with no readable NAME, takes a parent's parts-list description (`naming.prefer_bom_names`, majority vote); a text-layer NAME is authoritative.
- Stems are canonical uppercase (`config.canonical_stem`): part token found anywhere in the name, hyphens -> underscores. Recursive scan skips only top-level `Output/` and `_`-prefixed dirs (nested `sub/Output/` is scanned), plus the run's own `--output` by resolved path. Duplicate stems: shallowest path, then lexicographic.
- Revision ranking: CHK < no-revision < A..Z < AA.. (bijective base-26). Same-revision dupes resolve before extraction: non-watermarked > text-layer > shallowest > lexicographic. Sheet-set exception: stems tied at a base's top rank with distinct sheet tokens (`___DWG1`, `___DWG2`; plain stem is its own sheet) all stay active; only duplicate exports of the same sheet collapse. Only watermarked losers are archived (`_superseded/`).
- Filename drives placement. Title block (bottom-most REV/NUMBER; RCD block sits higher) is report-only unless `--rekey-titleblock`.
- Originals are never modified or deleted; only the output tree is mutated. `_orphans/` = parked BOM-less files (adopted later); `_superseded/` = old revisions / CHK / watermarked losers. Supersede archives the old revision before replacing (never overwrites unarchived; collision -> numeric suffix; archive failure never deletes the last copy). Repair may move or delete tree folders, but only under the delete rule below.
- Repair (`--repair`) re-applies naming, placement, orphan state and revision ranking to an existing tree from the tree's own PDFs: it renames to the plan (`normalize_folder_names`), moves folders to their planned parent and prunes duplicates (`fsops.reconcile_tree`), adopts `_orphans/` copies a parts list now names, and promotes a `_superseded/` copy whose revision rank is strictly higher than the live drawing for that base (never an unapproved CHK; ties and same-stem duplicates are left alone). Folder delete rule: only when every file in the subtree is a PDF and every one is byte-identical to a copy at a planned path; anything else keeps the folder and lands in the review list -> exit 2. `_superseded/` and `_orphans/` are never pruned or renamed; the only archive file repair deletes is the promoted source, after its live copy is verified. It places nothing absent from the tree.
- Guarded, not fatal: fsops place/copy/move/mkdir failures log a warning and continue (guards catch `OSError` and `ValueError` - hostile PDFs can carry NUL/control chars in NAMEs). Over-cap fan-out drops the largest roots first (`fit_roots_within_cap`, deterministic stem tie-break); skipped roots are reported and never placed, and their exclusive children surface under Unplaced PDFs -> exit 2. A fully over-cap run places no folders (full mode still parks orphans; incremental parks nothing new). The incremental pre-check fits the cycle-broken graph, with an unbroken plan failing closed. Report + workbook still written.
- Workbook is rebuilt from the run context every run (never parsed from the `.txt`); Run History survives only through the `<xlsx>.history.json` sidecar. Sanitize PDF-derived strings against formula injection. Layout contract (header row 1, Dashboard `B5`/`B6`, sheet order) is test-pinned; no formula cells fed by drawing text.

## Run behavior / exit codes

- Default output is the input folder (in-place). Prefer `--output <input>/Output` to keep the tree separate.
- Dry-run copies nothing; skips the `.txt` when the output folder does not exist yet but still writes the workbook.
- `--incremental`: only stems missing from the tree plus parked `_orphans/`; existing folders stay except supersede swaps / parent-adoption, and (only when the batch has new PDFs) `normalize_folder_names` renames a folder that differs from this run's plan - with nothing new the run takes the zero-count early exit and touches nothing. A claimed child nested under a live parent is copied to every claimant; only a top-level organized root is re-homed (moved). Target an existing tree with `--output <input>/Output`.
- `--jobs`: 0=auto, 1=serial, >1 uses a spawn ProcessPool (do not switch to fork). `--rekey-titleblock` re-keys text-layer PDFs only, only when that number is not already in the run.
- `--repair`: the positional folder is the tree (no source). It reports a `--- Repair (tree reconciliation) ---` block (renamed/moved/copied/removed/emptied/adopted/promoted/review), after a `Candidates examined` summary (the tree's own `_orphans/`+`_superseded/` copies, not new arrivals). A hand-renamed or hand-moved folder counts as a difference from the plan and is reverted - read `--dry-run --repair` first.
- Long runs: allow >=10 min on multi-thousand-file folders; a quiet `--- Scanning BOM tables ---` phase is normal, not a hang. `--jobs 4` (or CPU count) for thousands of text PDFs.
- Exit: 0 normal (orphans/CHK/missing refs still 0); 1 refused before writing (bad path, no PDFs, uncreatable output, lock held) **or an unexpected crash, which can happen after copies were written**; 2 placement refusal OR a repair that needs manual review (report + workbook written) OR run-lock loss (no artifacts) OR an argparse usage error (no artifacts either). Distinguish the run outcomes by whether the report exists.

## Testing

- Fixtures build PDFs via `make_pdf` (`tests/conftest.py`: one page, lines from (72,72), fontsize 11, 16pt step); extraction tests depend on that layout.
- Gates: `test_golden_log.py` pins dry-run output byte-for-byte; `test_no_dead_imports.py` fails on unused imports, new public funcs without docstrings (existing 9 allowlisted - do not add), and any `extract_objects` / `pymupdf.__main__` / `"pymupdf"` string in production (CVE-2026-82035 guard); `test_parallel_jobs.py` pins `jobs=2` == `jobs=1`; `test_cve_watch.py` pins the Tesseract floor across `.github/scripts/cve_watch.py`, `README.md`, `THIRD_PARTY_NOTICES.md`. Coverage ratchet `--cov-fail-under=83` (ubuntu CI only). No formatter; informal ~100-col norm, do not reformat unrelated lines.
- CI (`.github/workflows/pytest.yml`, SHA-pinned actions): Ubuntu 3.10 + 3.11 and Windows 3.11 (windows exercises `os.name != "nt"` skip guards), hashed-lock installs, `packaging` job (wheel + `fermi-organize --help` from a foreign cwd), plus weekly `cve-watch.yml`.
- Behavior changes go in `CHANGELOG.md` `## Unreleased` (headed `## <version> — <date>` when released), not per-commit. Bot PRs touch only fix-related files (no `uv.lock`/tool artifacts); `perf:` needs before/after numbers.
- Architecture decisions in `docs/adr/` (`NNNN-title.md`). Full/incremental shared decision layer + asymmetries = ADR 0001; repair mode (delete rule, narrow archive re-opening, non-goals) = ADR 0002. Keep modes' reported decisions on one implementation, guarded by `tests/test_mode_parity.py` (now covering repair too). `tests/test_repair.py` holds the reconcile pass, the delete rule and the repair-mode behaviours.

## Noise / local gotchas

- Exclude `.opencode/node_modules/` from globs/greps. `graphify-out/`, `.code-review-cache/` are generated, not source.
- `build/`, `dist/`, `fermi_pdf_organizer.egg-info/` are gitignored packaging leftovers. `build/lib/` holds a working-tree copy the `--no-isolation` build **reuses** (do not read or edit it as source, and never build without clearing it first — see the package-build command above).
- Recreate venv: `uv venv --python 3.11 .venv; uv pip install --python .venv\Scripts\python.exe -r requirements.txt -r requirements-dev.txt -e .` (the dev lock is what provides ruff, pytest-cov and coverage, i.e. the lint -> pytest -> coverage order below).
