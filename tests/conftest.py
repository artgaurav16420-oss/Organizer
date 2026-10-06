"""Shared test wiring: repo-root sys.path, generated PDFs, OCR disabled.

Packaging lives in pyproject.toml; the sys.path shim mirrors
organize_fermi_pdfs.py so tests import the same modules without installation.
"""
import sys
from pathlib import Path

import pymupdf as fitz
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fermi_organizer import extraction  # noqa: E402


def make_pdf(path, lines):
    """One-page PDF, one insert_text per line: (72,72), fontsize 11, step 16."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 16
    doc.save(str(path))
    doc.close()
    return Path(path)


def make_pdf_pages(path, pages_lines):
    """Multi-page variant of make_pdf: one page per entry, same geometry."""
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11)
            y += 16
    doc.save(str(path))
    doc.close()
    return Path(path)


@pytest.fixture(name="make_pdf")
def _make_pdf():
    return make_pdf


@pytest.fixture(name="make_pdf_pages")
def _make_pdf_pages():
    return make_pdf_pages


def ensure_ocr_off():
    extraction.OCR.reset(enabled=False)


@pytest.fixture(autouse=True)
def _ocr_off():
    ensure_ocr_off()
