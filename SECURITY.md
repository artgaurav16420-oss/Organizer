# Security Policy

## Supported versions

The project is pre-1.0; only the latest `main` state is supported.

## Scope

This is a local command-line tool that reads untrusted PDF files and writes
copies into an output folder. The most relevant classes of issues:

- Parser/OCR crashes or excessive resource use on crafted or malformed PDFs.
- Unsafe handling of file paths (traversal, symlinks) from drawing content.
- Command execution surfaces — the tool shells out only to a locally
  installed Tesseract binary (list-form arguments, `shell=False`); the
  `TESSERACT_EXE` environment variable is operator-controlled and validated.

Dependency advisories: see `THIRD_PARTY_NOTICES.md` for known PyMuPDF/Tesseract
notes. GHSA-434w-92hw-f2m3 / CVE-2026-82035 (path traversal) is confined to the
PyMuPDF **CLI** entry module (`src/__main__.py`), which this project never
imports or invokes; the library API surface it does use never executes that
module (verified 2026-10-08 against the advisory and PyPI). The fix
(`b2c8f3a`) first shipped in pymupdf **2.0** on 2026-10-19, which requires
Python `>=3.11` — adopted: this project pins pymupdf 2.0 (see
THIRD_PARTY_NOTICES.md), so the advisory no longer applies to the shipped
artefact.

## Reporting a vulnerability

Please report privately to the repository maintainer rather than opening a
public issue. Include a minimal reproducer (input PDF if possible) and the
version/commit you tested. Do not attach sensitive drawings to public reports.
