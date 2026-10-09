# PDF Drawing Organizer

[![pytest](https://github.com/artgaurav16420-oss/Organizer/actions/workflows/pytest.yml/badge.svg)](https://github.com/artgaurav16420-oss/Organizer/actions/workflows/pytest.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

Sorts engineering PDF drawings into a nested folder tree derived from the BOM
(PARTS LIST) tables inside each drawing, using the title block `NAME` for folder
names. The BOM is the **only source of truth** for parent-child edges; `USED ON`
is cross-checked but never used for placement (a `USED ON` that names a parent the
parent's BOM does not list is reported as a USED ON bug). Superseded revisions,
unapproved `CHK` drawings, scanned (image-only) PDFs via OCR, and incremental
batches are all handled.

> **This repository holds program files only.** Input PDFs, the organized tree,
> reports and Excel workbooks all live in the input folder you pass.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Flags](#flags)
- [Outputs](#outputs)
- [How placement works](#how-placement-works)
- [AI agent usage](#ai-agent-usage)
- [Recovery and safe operation](#recovery-and-safe-operation)
- [Known limits](#known-limits)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Licensing and dependencies](#licensing-and-dependencies)

## Requirements

| Component | Requirement |
|---|---|
| Python | 3.10 or newer |
| PyMuPDF | `==1.28.2` (required) |
| openpyxl | `==3.1.5` (optional — only the `.xlsx` workbook) |
| Tesseract OCR | Optional — needed only for scanned (image-only) PDFs; 5.5.3+ recommended |

Tesseract is auto-detected via the `TESSERACT_EXE` environment variable, then `PATH`,
then the common install directories (`C:\Program Files\Tesseract-OCR`,
`C:\Program Files (x86)\Tesseract-OCR`, `%LOCALAPPDATA%\Programs\Tesseract-OCR`).
Without it, scanned PDFs are still logged — they simply stay orphans.

## Install

```bash
pip install -r requirements.txt
```

For a reproducible install with SHA-256 hashes:

```bash
uv pip install -r requirements.lock
```

`requirements.txt` is the human-edited dependency source; `requirements.lock` is
generated from it with `uv pip compile requirements.txt --generate-hashes -o requirements.lock`
and is what CI installs (`pip install --require-hashes -r requirements.lock`).

Add `pip install -e .` when you want the `python -m fermi_organizer.cli` entry point.

## Quick start

Always dry-run first, review the printed plan, then execute:

```bash
# 1. dry run — prints the plan, copies nothing
python organize_fermi_pdfs.py "D:\data\drawings" --output "D:\data\drawings\Output" --dry-run

# 2. execute the reviewed plan
python organize_fermi_pdfs.py "D:\data\drawings" --output "D:\data\drawings\Output"

# 3. later batches feeding the SAME output tree (input folder may differ)
python organize_fermi_pdfs.py "D:\data\more-pdfs" --incremental --output "D:\data\drawings\Output" --dry-run
python organize_fermi_pdfs.py "D:\data\more-pdfs" --incremental --output "D:\data\drawings\Output"
```

Without `--output` the tree is organized **in place**, in the input folder itself.
`<input-folder>/Output` is the usual target when you want to keep it separate.

## Flags

| Flag | Effect |
|---|---|
| `--output <dir>` | Organized tree target (default: the input folder itself — in-place). |
| `--dry-run` | Print the plan without copying files. Still rebuilds the `.xlsx` workbook and its history sidecar, and writes the timestamped `.txt` report when the output folder exists; a missing `--output` folder is created for the workbook only. Always run this first. |
| `--incremental` | Process only stems not already in the output tree, plus stored `_orphans/` (adoption). Existing organized folders stay put, except when a new revision supersedes an old one or a parent adoption moves them. |
| `--no-ocr` | Disable the OCR fallback for scanned PDFs. |
| `--jobs N` | Parallel extraction workers (`0` = auto = CPU count, `1` = serial). |
| `--rekey-titleblock` | Trust the title block over the filename: re-key a misnamed **text-layer** PDF to its title-block drawing number (revision adopted, copied file renamed). Only when no drawing of that number already exists in the run; scanned (OCR) numbers never rename a file. |

Input is scanned recursively — every `*.pdf` under the input folder counts, at any
depth, except those under a top-level `Output/` (case-insensitive) or `_`-prefixed
folder. A nested `sub/Output/` **is** scanned.

## Outputs

Everything is written under the output folder:

| Path | Contents |
|---|---|
| `<Output>/` | The organized tree: `{DRAWING_NUMBER} {PART NAME}` folders nested per the BOM hierarchy. |
| `<Output>/_superseded/` | Older revisions and `CHK` files replaced by an approved `DWG` counterpart, plus watermarked duplicates of a revision that a non-watermarked copy won. |
| `<Output>/_orphans/` | Drawings with no parent seen yet; a future incremental run adopts them automatically when their parent arrives. |
| `<Output>/organize_fermi_report.xlsx` | Live status workbook, rebuilt after every run (see sheets below). |
| `<Output>/organize_fermi_pdfs_report_*.txt` | Full text log of each run, including missing BOM references and OCR recovery details. |

Workbook sheets: Dashboard, Missing (download list), CHK, Scanned (image-only PDFs
read via OCR), Watermark, Orphans, Superseded, Roots (root drawings and their
`USED ON`), USED ON check (BOM vs `USED ON` mismatches in both directions), Title
block check (filename vs the drawing's own title block), Files, Run History.

## How placement works

- **BOM only.** A child is placed under a parent because the parent's parts list
  names it. `USED ON` is a cross-check, never a placement source.
- **Revision ranking.** `CHK` (unapproved) ranks below no-revision, which ranks below
  `A`, `B`, and so on; the highest revision wins and older ones are archived.
- **Duplicate stems.** Hyphen/underscore/numeric-prefix variants of one filename
  canonicalize to the same stem, so they are treated as the same revision. The copy
  used in the tree is chosen before extraction: non-watermarked beats watermarked,
  then a text-layer copy beats a scanned one, then the shallowest path wins.
- **Title block check.** The number, revision and `NAME` printed in each drawing's own
  title block are compared against the filename; disagreements are listed in the
  Title block check sheet. Placement follows the filename unless
  `--rekey-titleblock` opts into title-block authority.
- **Cycles.** A cycle in the BOM graph is broken and reported rather than looping.

## AI agent usage

An operator runbook for AI agents ships with the repository at
`.opencode/skills/pdf-organizer/SKILL.md`. OpenCode discovers it automatically in
any session started inside a clone, and other agent tools can be pointed at the same
file. It encodes the safe workflow (dry-run → review → execute), flag decisions, how
to summarize a run, guardrails (originals are never modified) and troubleshooting.

To organize a folder from outside a clone, install the repository's dependencies
(`pip install -r requirements.txt`, plus `pip install -e .` for the module form) and
run from the repository root:

```bash
python -m fermi_organizer.cli "D:\data\drawings" --output "D:\data\drawings\Output" --dry-run
```

The input folder may live anywhere on disk; only the *invocation* needs the repository
as the working directory (or the repository's venv interpreter, which works from any
directory once installed editable).

## Recovery and safe operation

- Original PDFs are **never modified or deleted** — keep them as your backup. Only the
  organized tree is mutated (copies, supersede swaps, moves).
- Always run `--dry-run` first and review the printed plan before executing.
- Supersede swaps archive the old revision under `_superseded/` before replacing it in
  the tree; adopted files remain available under `_orphans/` until they are placed.
- An interrupted run is safe to re-run: incremental mode leaves existing folders
  alone, and a full re-run rebuilds the tree from the input PDFs.
- One run per output folder at a time, enforced by `<output>/.fermi_organizer.lock`:
  a second concurrent run refuses with an error instead of racing the first
  (locks older than 2 h are stolen with a warning — a crashed run leaks its
  lock). A run killed mid-supersede leaves `*.supersede_tmp.*` staging files
  behind (the PDF scans never match them), and the next run's startup sweep
  removes them.
- A failing filesystem operation (copy, move, `mkdir`) logs a warning and the run
  continues rather than aborting.

## Known limits

- Rare drawing layouts can defeat the parts-list detector. OCR values are
  shape-validated and only match indexed drawing numbers, so misreads are rare — but a
  misread that collides with a different indexed number can still attach a wrong BOM
  edge. Treat OCR-derived edges as best-effort.
- The OCR parts-list reader is tuned for the common bottom-up parts-list layout (rows
  above the `ITEM`/`FERMI#` header) and was verified against scanned drawings with
  text-layer twins. Scanned title-block number, revision and `NAME` are best-effort —
  Tesseract sometimes misses the small labels entirely.
- Sheets are A0-A4. Scans stored at the scanner's dpi (1 px = 1 pt) are normalized to
  ISO page points and the OCR render is pixel-capped, but the smallest text on 153 dpi
  A0 scans sits at Tesseract's reliability edge (occasional digit misreads, e.g.
  `F10144633` read as `F10144635`).
- Missing drawings (BOM references with no matching PDF) and `CHK` stems are listed for
  download; `CHK` files are retired automatically once their approved `DWG` arrives in
  any input folder.
- Watermark detection is best-effort: watermark layers, transparent/diagonal/light
  large text, stamp annotations, and common phrases (`UNCONTROLLED`, `PRE-RELEASED`,
  `DRAFT`, `PRELIMINARY`, …). The evidence is listed in the Watermark sheet — verify
  before acting.
- Losing copies of a same-revision duplicate stay in the input folder; only watermarked
  losers are archived in `_superseded/`.
- Over-cap fan-out (10,000 planned copies) skips the largest root assemblies
  instead of refusing the run: skipped roots are reported and never placed,
  while the rest of the tree is built (the CLI exits 2 so the skip is
  noticed).
- Network-share output (SMB/NFS, lab drives) is not lock-safe: those filesystems
  do not reliably honor the exclusive-create the run lock depends on, so two
  runs against one shared output can both proceed. This includes mapped drive
  letters and Linux NFS mounts that present as local paths — only Windows UNC
  paths get a CLI warning, since the rest cannot be detected. Prefer local
  output; if the tree must live on a share, coordinate runs manually.
  (The lock file itself, `<output>/.fermi_organizer.lock`, is a dotfile: on
  Windows dotfiles are not hidden, so Explorer shows it — harmless, leave it
  alone.) A run paused past the 2 h stale window (sleep, SIGSTOP, VM pause)
  may wake to find its lock stolen; it then stops with an error (CLI exit 2)
  before its next tree change instead of writing into the other run's tree.

## Project layout

```text
organize_fermi_pdfs.py     launcher (thin shim)
fermi_report_xlsx.py       live Excel workbook writer (openpyxl, optional)
fermi_organizer/
  __init__.py              pymupdf-direct import guard (never the legacy `fitz` shim)
  cli.py                   argparse + logging + report wiring
  config.py                constants + regexes + OCR geometry
  extraction.py            BOM/USED ON/NAME extraction + OCR fallback + run-scoped OCR context
  graph.py                 pure algorithms: revision ranking, matching, cycles
  naming.py                folder naming + path-length shortening
  fsops.py                 input indexing, placement, tree scans, supersede/orphan copies
  runmodes.py              run_full + run_incremental (return a structured run context)
  report_glue.py           tree-derived NAME fallback
tests/                     pytest suite (PDFs generated at test time)
```

See [`AGENTS.md`](AGENTS.md) for the architecture notes, invariants and test gates that
contributors and coding agents need.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

Fixtures generate their PDFs at test time, so no binaries are checked in and Tesseract
is not required.

## Licensing and dependencies

- Third-party licenses and the project's own pending license decision:
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
- Security policy: [`SECURITY.md`](SECURITY.md).
- PyMuPDF: watch for `>=1.28.3` (GHSA-434w-92hw-f2m3 / CVE-2026-82035 is a path
  traversal in the PyMuPDF **CLI** font path — `extract_objects()` in
  `src/__main__.py` — which this project does not use; upstream fix is commit
  `b2c8f3a` and no fixed release exists yet).
- Tesseract OCR: 5.5.3 or newer recommended; note that eight model-file
  deserialization advisories (CVE-2026-88047..-88054, 2026-09-10) still apply to
  5.5.3 with **no fixed release yet** — they trigger only on a malicious
  tessdata `.traineddata`, never on drawings, see
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).