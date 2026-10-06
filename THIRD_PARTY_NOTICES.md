# Third-Party Notices

Informational notices for third-party components this project depends on or uses at
runtime. Versions match `requirements.txt` / `requirements.lock`.

## PyMuPDF 1.28.2

- **License:** dual-licensed — AGPL-3.0-only **or** Artifex commercial license.
- **Usage note:** internal use by a single entity does not trigger AGPL obligations.
  Distribution of a combined work, or offering it as a network service, requires
  either releasing the combined work's source under AGPL-3.0 or holding a
  commercial license from Artifex.
- **Security:** advisory CVE-2026-82035 affects PyMuPDF <= 1.28.2, but only the
  PyMuPDF CLI font path (`extract_objects()` in `src/__main__.py`). This project
  imports PyMuPDF as a library and never invokes that CLI path. Monitor the
  advisory and bump to >= 1.28.3 when released.

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
