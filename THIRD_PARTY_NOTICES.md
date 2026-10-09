# Third-Party Notices

Informational notices for third-party components this project depends on or uses at
runtime. Versions match `requirements.txt` / `requirements.lock`.

## PyMuPDF 1.28.2

- **License:** dual-licensed — AGPL-3.0-only **or** Artifex commercial license.
- **Usage note:** internal use by a single entity does not trigger AGPL obligations.
  Distribution of a combined work, or offering it as a network service, requires
  either releasing the combined work's source under AGPL-3.0 or holding a
  commercial license from Artifex.
- **Security:** advisory GHSA-434w-92hw-f2m3 / CVE-2026-82035 (high, path
  traversal in the font branch of `extract_objects()`). Verified directly on
  2026-10-08 against the GitHub advisory API and the PyPI JSON API:
  - **Scope:** the flawed code lives in the **CLI entry module**
    (`src/__main__.py`): it joins a document-controlled BaseFont name onto the
    user-supplied output directory without stripping separators. The library
    API used by this project (`pymupdf.open`, text/image extraction, page
    render, OCR) never imports or executes `src/__main__.py`, so library-only
    use does not reach the vulnerable path. The advisory covers every CLI
    invocation, not the library surface.
  - **Enforcement:** this project must never invoke the PyMuPDF CLI; pinned by
    `tests/test_no_dead_imports.py:89`
    (`test_no_pymupdf_cli_extract_objects_usage`), which rejects the vulnerable
    symbols and CLI invocation forms (`"pymupdf"` argv, `python -m pymupdf`).
  - **Fix status:** upstream fix is commit `b2c8f3a`; as of 2026-10-08 **no
    fixed release exists** (PyPI latest is 1.28.2, the pinned version). No pin
    bump is possible today.
  - **Standing action:** re-check PyPI on each PyMuPDF release; when the first
    release containing `b2c8f3a` appears, bump `pyproject.toml`,
    `requirements.txt`, and `requirements.lock` (re-lock) together.

## openpyxl 3.1.5

- **License:** MIT.
- Optional dependency: used only to build the `.xlsx` workbook; the tool runs
  without it (workbook skipped with a warning).

## et-xmlfile 2.0.0

- **License:** MIT.
- Transitive dependency of openpyxl.

## Tesseract OCR

- **License:** Apache-2.0 (external binary, not bundled or vendored).
- Optional at runtime: only needed for scanned (image-only) PDFs; without it
  those drawings are logged as orphans. 5.5.3+ recommended.
- **Security:** eight advisories published 2026-09-10 for Tesseract **≤ 5.5.3**
  (CVE-2026-88047 through CVE-2026-88054; up to CVSS 8.6 HIGH): memory-safety
  flaws in legacy-model and LSTM `.traineddata` deserialization. Verified
  2026-10-09 against the NVD API: **no fixed release is available** (5.5.3 is
  still the latest). Scope here: all eight trigger on a **malicious tessdata
  model file**, not on untrusted PDFs or rendered images (the OCR child process
  only ever reads a locally rendered PNG), and the CLI OCR invocations are
  subprocess-bounded (120/180 s timeouts), so the threat model of this tool is
  not reached by feeding it hostile drawings. (The separate in-process
  `get_textpage_ocr` full-page pass has no such subprocess timeout; it renders
  and reads the page in-process rather than shelling out.)
  - **Mitigation note:** the legacy-engine vectors (88047/88051/88053) are
    additionally avoided by an LSTM-only engine (`--oem 1`); this tool also
    runs `--oem 3` passes for accuracy, so forcing oem 1 is an operator
    trade-off, not applied. 88054 affects the LSTM path and is not avoided.
  - **Standing action:** re-check upstream releases; bump the recommendation
    floor in README and here when a release post-5.5.3 ships the patches
    (merge commits only so far).

## This project's own license

GNU Affero General Public License v3.0 only (`LICENSE`), matching the
AGPL-3.0-only terms of the PyMuPDF dependency: any distribution of the
combined tool (or network use of a modified version) carries the full
AGPL source and offer obligations.
