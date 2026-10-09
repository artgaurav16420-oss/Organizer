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

## This project's own license

Not yet chosen. No `LICENSE` file is shipped. A licensing decision is required
before this project is shared or distributed outside the usual internal use.
