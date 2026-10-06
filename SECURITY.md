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
notes (e.g. CVE-2026-82035 affects only the PyMuPDF CLI path, which this project
does not use).

## Reporting a vulnerability

Please report privately to the repository maintainer rather than opening a
public issue. Include a minimal reproducer (input PDF if possible) and the
version/commit you tested. Do not attach sensitive drawings to public reports.
