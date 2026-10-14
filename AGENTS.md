# AGENTS.md

## What this is

Fermi lab PDF drawing organizer: scans a folder of PDFs, extracts BOM tables / USED ON / NAME (OCR fallback for scanned pages), builds a parent-child graph, copies files into a nested tree. Writes a timestamped `.txt` report + live `.xlsx` workbook per run.

Operator runbook for agents: `.opencode/skills/pdf-organizer/SKILL.md` (auto-loaded in-repo). This file is the code reference; the skill is the golden path (dry-run → review → execute).

## Run

```bash
python organize_fermi_pdfs.py <folder> [--output DIR] [--dry-run] [--incremental] [--no-ocr] [--jobs N] [--rekey-titleblock]
```

- Repo venv `.venv\Scripts\python.exe` (Python 3.11, uv-managed, **no pip** — install via `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`). Editable install present, so `python -m fermi_organizer.cli` and `fermi-organize` work from any cwd; never run `fermi_organizer\cli.py` directly (relative imports fail).
- Default output is the **input folder itself** (in-place). Pass `--output <input>/Output` to keep the tree separate. Always `--dry-run` first. Dry-run copies nothing; it skips the `.txt` when the output folder doesn't exist yet (folder isn't pre-created) but still writes the workbook.
- `--incremental` processes only stems missing from the output tree plus parked `_orphans/`; existing folders stay put except supersede swaps / parent-adoption moves. A claimed child nested under a live parent is copied to every claimant; only a top-level organized root is re-homed (moved). Target an existing tree with `--output <input>/Output`.
- `--jobs N`: 0=auto (default), 1=serial; N>1 uses spawn ProcessPool (do not switch to fork). `--rekey-titleblock` re-keys misnamed **text-layer** PDFs only (never OCR reads), and only when that number isn't already in the run.
- One run per output folder at a time, enforced by `<output>/.fermi_organizer.lock` (CLI exit 1 if held; steals stale >2 h with warning; CLI exit 2 if the lock is stolen mid-run). Network-share output (SMB/NFS, mapped drives) is **not lock-safe** — coordinate manually.
- Exit codes: `0` normal (orphans/CHK/missing refs are still 0), `1` fatal (bad path, no PDFs, output folder uncreatable, crash), `2` = over-cap placement refusal (report + workbook still written) **or** run-lock loss (no artifacts written) — `fermi_organizer/cli.py` `sys.exit` call sites; both `2`s are distinguished by whether the report exists.

## Dependencies

- `import pymupdf`, never the legacy `fitz` shim (leaks a deprecation notice into captured stdout). Pinned `pymupdf==1.28.2`, `openpyxl==3.1.5` (openpyxl optional at runtime — only the workbook; ImportError is caught). Tesseract needed only for scanned PDFs (`TESSERACT_EXE` → PATH → common Windows dirs).
- `requirements.txt` is the human-edited source; regen lock with `uv pip compile requirements.txt --generate-hashes -o requirements.lock`. Dev tools: edit `requirements-dev.in`, regen with `uv pip compile requirements-dev.in --generate-hashes --universal --python-version 3.10 -o requirements-dev.txt`. Pin/CVE notes live in README *Licensing & dependency notes* — check before bumping. The root `uv.lock` is consumed by nothing (CI installs `requirements.lock` + `requirements-dev.txt`) — don't regenerate or "fix" it.
- `pyproject.toml` build floor: `setuptools>=80` (77 rejects the SPDX `license` string; measured, don't lower without re-probing). CI's packaging job builds the wheel `--no-isolation` so setuptools comes from the hashed dev lock.
- `requires-python >=3.10` and CI's ubuntu matrix runs **3.10** — no 3.11-only stdlib (`tomllib`, `datetime.UTC`, `except*` groups).

## Architecture

```
organize_fermi_pdfs.py   thin launcher (sys.path shim -> fermi_organizer.cli:main)
fermi_organizer/cli.py       argparse, orchestration, Excel glue
  config.py      regexes, constants, limits (MAX_PATH=250, TREE_MAX_DEPTH=500, 10k-copy cap)
  extraction.py  BOM / USED ON / NAME / title block / watermark + OCR
  graph.py       pure algorithms (revision ranking, matching, cycles)
  naming.py      folder naming + path shortening
  fsops.py       indexing, placement, tree scans, supersede/orphan copies, run lock
  runmodes.py    run_full / run_incremental (return structured run context — the workbook's input, never the .txt)
  report_glue.py tree-derived NAME fallback
fermi_report_xlsx.py     standalone workbook builder (openpyxl, optional)
tests/               PDFs generated at test time, no binaries
```

## Invariants (agents get these wrong)

- **BOM is the only placement source.** USED ON is cross-check only: child USED ON naming a parent whose BOM omits it = USED ON bug (reported, never placed); the reverse mismatch is also flagged.
- **Stems are canonical uppercase** (`config.canonical_stem`): part token found anywhere in the name, hyphens→underscores, so variants share a stem. Input scan is recursive but skips only **top-level** `Output/` and `_`-prefixed dirs (nested `sub/Output/` is scanned) — plus the run's own `--output` folder by resolved path, whatever it is named. Duplicate stems: shallowest path wins, then lexicographic.
- **Revision ranking**: CHK < no-revision < A < B … Z < AA < AB (bijective base-26, so two-letter revs beat any single letter). Same-revision duplicates resolve **before** extraction: non-watermarked > text-layer > shallowest > lexicographic. Exception — **sheet-set ties**: stems tied at a base's top rank with distinct sheet tokens (`___DWG1`, `___DWG2`; a plain stem counts as its own sheet) all stay active, since a dropped sheet would orphan its children; only duplicate exports of the *same* sheet collapse (first-seen wins). Only watermarked losers are archived (`_superseded/`); other losers stay ignored in place.
- **Filename drives placement.** Title block (bottom-most REV/NUMBER labels; RCD block sits higher) is report-only unless `--rekey-titleblock`.
- **Originals are never modified/deleted** — only the output tree is mutated. `_orphans/` = parked BOM-less files (adopted by later runs); `_superseded/` = old revisions / CHK / watermarked losers. Supersede archives the old revision before replacing (never overwrites unarchived; collision → numeric suffix); an archive failure never deletes the last copy.
- **Guarded, not fatal**: fsops place/copy/move/mkdir failures log a warning and continue (guards catch `OSError` and `ValueError` — hostile PDFs can carry NUL/control chars in NAMEs). Over-cap fan-out drops the largest roots first (`fit_roots_within_cap`, deterministic stem tie-break) and places the rest — skipped roots land in `skipped_roots` (reported, never placed; their exclusive children surface under Unplaced PDFs), CLI exit 2; a fully over-cap run places no folders (full mode still parks orphans; incremental parks nothing new). The incremental pre-check fits the cycle-broken graph (an unbroken plan fails closed on phantom fan-out); report + workbook still written.
- **Workbook**: rebuilt from the run context every run (never parsed from the `.txt`); Run History survives only via the `<xlsx>.history.json` sidecar. Sanitize all PDF-derived strings against formula injection; layout contract (header row 1, Dashboard `B5`/`B6`, sheet order) is test-pinned — don't add formula cells fed by drawing text.

## Testing

- Full: `.venv\Scripts\python.exe -m pytest -q`. Single: `... -m pytest tests/test_graph.py -q` / `... "tests/test_graph.py::test_name" -q`. Tests work without the editable install (conftest shims repo root). No Tesseract needed (autouse fixture disables OCR).
- Fixtures build PDFs via `make_pdf` (`tests/conftest.py`: one page, lines from (72,72), fontsize 11, 16 pt step) — extraction tests depend on that layout.
- Gates: `test_golden_log.py` pins dry-run output byte-for-byte (update EXPECTED on intentional message changes); `test_no_dead_imports.py` fails on unused imports, new public functions without docstrings (existing 9 are allowlisted — don't add to `NODOC_ALLOWLIST`), and any `extract_objects` / `pymupdf.__main__` / quoted `"pymupdf"` string in production modules (CVE-2026-82035 guard); `test_parallel_jobs.py` pins `jobs=2` == `jobs=1`; `test_cve_watch.py` pins the Tesseract floor to match across `.github/scripts/cve_watch.py`, `README.md`, `THIRD_PARTY_NOTICES.md`. Coverage ratchet `--cov-fail-under=83` (ubuntu CI only). Lint: `ruff check --select F,E9,B fermi_organizer tests fermi_report_xlsx.py organize_fermi_pdfs.py` (pinned `ruff==0.16.10`, no config file — the select list lives only in the CI job; plain `ruff check` is not equivalent). No formatter; informal ~100-col norm, don't reformat unrelated lines.
- CI (`.github/workflows/pytest.yml`, SHA-pinned actions): ubuntu 3.10+3.11 + windows 3.11 (windows exercises the `os.name != "nt"` skip guards), hashed-lock installs, `packaging` job (wheel + `fermi-organize --help` from foreign cwd), plus weekly `cve-watch.yml` (PyMuPDF pin vs PyPI latest).
- Recreate venv: `uv venv --python 3.11 .venv; uv pip install --python .venv\Scripts\python.exe -r requirements.txt pytest -e .`
- Pre-push check (matches CI, in order): lint command above → `.venv\Scripts\python.exe -m pytest -q` (~13 s, 403 tests; the 6 skips are symlink tests and are normal without dev-mode symlink privilege).
- Behavior changes go in `CHANGELOG.md`'s `## Unreleased` section (headed `## <version> — <date>` when released), not per-commit entries. Bot PRs: touch only fix-related files (no `uv.lock`/tool artifacts), `perf:` needs before/after numbers in the body.

## Noise

- Exclude `.opencode/node_modules/` (large gitignored plugin tree) from globs/greps. `graphify-out/`, `.code-review-cache/` are generated, not source.
- `build/`, `dist/`, `fermi_pdf_organizer.egg-info/` are gitignored packaging leftovers — `build/lib/fermi_organizer/*.py` is a **stale copy** (older than `fermi_organizer/`); never read or edit it as source.
