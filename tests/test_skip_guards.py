"""Resource-exhaustion skip guards (T-014a): behavior, not Tesseract.

Tesseract is absent in this environment, so these tests pin the documented
skip shapes for oversized / page-capped PDFs using generated PDFs plus
monkeypatched limits.
"""
import pymupdf as fitz
import pytest

from fermi_organizer import extraction


def test_pdf_size_guard_message_and_stat_failure(tmp_path, make_pdf, monkeypatch):
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Parent"])
    monkeypatch.setattr(extraction.os.path, "getsize",
                        lambda p: extraction.MAX_PDF_BYTES + 10)
    msg = extraction._pdf_size_issue(str(pdf))
    assert msg.startswith("skipped:")
    assert "500MB cap" in msg

    def _boom(p):
        raise OSError("gone")

    monkeypatch.setattr(extraction.os.path, "getsize", _boom)
    assert extraction._pdf_size_issue(str(pdf)) is None


def test_doc_pages_guard_message(tmp_path, make_pdf, monkeypatch):
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Parent"])
    with fitz.open(pdf) as doc:
        assert extraction._doc_pages_issue(doc) is None
        monkeypatch.setattr(extraction, "MAX_PDF_PAGES", 0)
        msg = extraction._doc_pages_issue(doc)
    assert msg == "skipped: 1 pages exceeds 0-page cap"


@pytest.mark.parametrize("task_name", ["bom_task", "org_task"])
def test_oversized_ok_tasks_return_skipped_shape(tmp_path, make_pdf, monkeypatch,
                                                 task_name):
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART"])
    monkeypatch.setattr(extraction, "MAX_PDF_BYTES", 1)
    res = getattr(extraction, task_name)(str(pdf))
    assert res[0] == "ok"
    assert res[2] == "skipped"
    assert "skipped:" in " ".join(str(x) for x in res)


@pytest.mark.parametrize("task_name", ["title_task", "dup_meta_task"])
def test_oversized_error_tasks_return_skip_error(tmp_path, make_pdf, monkeypatch,
                                                 task_name):
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Parent"])
    monkeypatch.setattr(extraction, "MAX_PDF_BYTES", 1)
    res = getattr(extraction, task_name)(str(pdf))
    assert res[0] == "error"
    assert "skipped:" in res[1]


@pytest.mark.parametrize("task_name", ["bom_task", "title_task",
                                       "org_task", "dup_meta_task"])
def test_page_cap_tasks_return_skip_shape(tmp_path, make_pdf, monkeypatch,
                                          task_name):
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART"])
    monkeypatch.setattr(extraction, "MAX_PDF_PAGES", 0)
    res = getattr(extraction, task_name)(str(pdf))
    assert res[0] in ("ok", "error")
    flat = " ".join(str(x) for x in res)
    assert "skipped:" in flat and "page cap" in flat


def test_multipage_bom_on_page_two(make_pdf_pages):
    # T-020 multi-page fixture variant: BOM lives on page 2 only (page_num 2
    # in the entry proves the second page was scanned).
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    pdf = make_pdf_pages(tmp / "F10126106.pdf", [
        ["NAME", "Parent"],
        ["FERMI PART LIST", "F10126107 CHILD PART"],
    ])
    entries, _method, _issues = extraction.extract_bom_entries(str(pdf))
    assert ("F10126107", 2, "text-fallback", "CHILD PART") in entries
