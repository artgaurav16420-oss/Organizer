# Third-Party Notices

Informational notices for third-party components this project depends on or uses at
runtime. Versions match `requirements.txt` / `requirements.lock`.

## PyMuPDF 2.0

- **License:** dual-licensed — AGPL-3.0-only **or** Artifex commercial license.
- **Usage note (PyMuPDF's terms, not this repo's):** under PyMuPDF's own
  licensing, purely internal use by a single entity does not trigger AGPL
  obligations. That does not describe this repository: this project itself is
  licensed AGPL-3.0-only (see `LICENSE`) and is published publicly, so the
  combined work is conveyed under AGPL-3.0 with full source obligations.
  Distribution of a combined work, or offering it as a network service,
  requires either releasing the combined work's source under AGPL-3.0 or
  holding a commercial license from Artifex.
- **Security:** this is the first release containing the fix for
  GHSA-434w-92hw-f2m3 / CVE-2026-82035 (path traversal in the font branch of
  `extract_objects()` in the CLI entry module). The library surface this
  project uses never imported that module, but the pin now names a fixed
  release, so the shipped artefact no longer carries the advisory — and
  installing into an environment that has 2.0 no longer downgrades it.
- **Still enforced:** this project never invokes the PyMuPDF CLI; pinned by
  `tests/test_no_dead_imports.py` (`test_no_pymupdf_cli_extract_objects_usage`),
  which rejects the vulnerable symbols and CLI invocation forms.
- **Layout model disabled on purpose:** 2.0 imports an ONNX layout model and
  runs it inside `find_tables()`, which measured ~4x the whole extraction cost
  (100 drawings: 55 s on 1.28.2, 245 s on 2.0, 47 s with the model off).
  `fermi_organizer/extraction.py` clears `pymupdf._get_layout` at import.

## pymupdf4llm 2.0 and pymupdf_layout 2.0

- **License:** AGPL-3.0-only (Artifex), same terms as PyMuPDF above.
- Transitive dependencies of PyMuPDF 2.0. `pymupdf4llm` provides Markdown
  extraction; `pymupdf_layout` bundles the ONNX layout model (disabled here).

## networkx 3.6.1

- **License:** BSD-3-Clause. Transitive dependency of PyMuPDF 2.0.

## numpy 2.4.6

- **License:** BSD-3-Clause, plus 0BSD / MIT / Zlib for bundled components
  (`numpy`'s own LICENSES_bundled directory). Transitive dependency of PyMuPDF
  2.0 (layout model input preparation).

## onnxruntime 1.31.1

- **License:** MIT. Transitive dependency of PyMuPDF 2.0; runs the layout model
  that this project disables at import. The wheel is large (native runtime).

## pyyaml 6.0.3

- **License:** MIT. Transitive dependency of PyMuPDF 2.0.

## psutil 7.2.2

- **License:** BSD-3-Clause. Transitive dependency of PyMuPDF 2.0.

## tabulate 0.10.0

- **License:** MIT. Transitive dependency of PyMuPDF 2.0.

## flatbuffers 25.12.19

- **License:** Apache-2.0. Transitive dependency of `onnxruntime`.

## protobuf 7.36.2

- **License:** BSD-3-Clause. Transitive dependency of `onnxruntime`.

## packaging 26.3

- **License:** Apache-2.0 OR BSD-2-Clause. Transitive dependency of PyMuPDF 2.0.

## openpyxl 3.1.5
    (`test_no_pymupdf_cli_extract_objects_usage`), which rejects the vulnerable
    symbols and CLI invocation forms (`"pymupdf"` argv, `python -m pymupdf`).
  - **Fix status:** the fix shipped in **pymupdf 2.0** and this project now pins
    it; the advisory no longer applies to the shipped artefact.
  - **History:** `docs/adr/0003-pymupdf-cve-pin.md` recorded the stay-on-1.28.2
    decision and its two re-open triggers; `docs/adr/0004-python-311-floor.md`
    records the floor change that let the pin move.

## openpyxl 3.1.5

- **License:** MIT.
- Optional dependency: used only to build the `.xlsx` workbook; the tool runs
  without it (workbook skipped with a warning).

## et-xmlfile 2.0.0

- **License:** MIT.
- Transitive dependency of openpyxl.

## Tesseract OCR

- **License:** Apache-2.0 (external binary, not bundled or vendored).
- Optional at runtime: only needed for scanned (image-only) PDFs. Without it a
  scanned drawing is still placed when a readable parent's parts list names it;
  it is parked in `_orphans/` only when nothing readable defines its parent.
  5.5.3+ recommended.
- **Security:** eight advisories published 2026-09-10 for Tesseract **≤ 5.5.3**
  (CVE-2026-88047 through CVE-2026-88054; up to CVSS 8.6 HIGH): memory-safety
  flaws in legacy-model and LSTM `.traineddata` deserialization. Verified
  2026-10-09 against the NVD API: **no fixed release is available** (5.5.3 is
  still the latest). Scope here: all eight trigger on a **malicious tessdata
  model file**, not on untrusted PDFs or rendered images (the OCR child process
  only ever reads a locally rendered PNG), and the CLI OCR invocations are
  subprocess-bounded (120/180 s timeouts), so the threat model of this tool is
  not reached by feeding it hostile drawings. (The separate in-process
  `get_textpage_ocr` full-page pass is also bounded: it runs in a thread that
  is joined with `OCR_INPROCESS_TIMEOUT_SEC` = 120 s, and a timeout is reported
  as an OCR issue rather than hanging the run.)
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
