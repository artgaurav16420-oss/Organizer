"""BOM / USED ON / NAME extraction from generated PDFs."""
import os
from pathlib import Path

import pytest
import pymupdf as fitz

from fermi_organizer import extraction
from fermi_organizer.config import is_fermi_value


def test_single_line_bom_detection(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART"])
    entries, _method, issues = extraction.extract_bom_entries(pdf)
    assert issues == []
    assert ("F10126107", 1, "text-fallback", "CHILD PART") in entries


def test_bom_requires_fermi_header_gate(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["F10126107 CHILD PART"])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert entries == []


def test_note_reference_to_other_drawings_is_not_bom(tmp_path, make_pdf):
    # F10118360-style fabrication note: the F-number leads the wrapped line
    # ("... PER WELDMENT DRAWING / F10126107 AND F10126108.") but only points
    # at other drawings. The bare dimension lines (80/70) satisfy ITEM_RE, so
    # a context gate would not reject this - the cross-reference description
    # must. The real USED ON box below is guarded separately. The FERMI
    # title-block line forces the page into the text parser (without any
    # FERMI text the page is skipped before parsing starts).
    lines = ["FERMI NATIONAL ACCELERATOR LABORATORY",
             "3. PEM-NUT HOLE SIZE TO BE DETERMINED",
             "BY VENDORS CHOICE OF PEM-NUT STYLE,",
             "PEM-NUT THREAD SIZE SHALL BE",
             "MAINTAINED PER WELDMENT DRAWING",
             "F10126107 AND F10126108.",
             "4. TRAILING ZERO DENOTE TOLERANCE.",
             "267.0", "80", "250.0", "70",
             "USED ON",
             "F10126107 & F10126108"]
    pdf = make_pdf(tmp_path / "F10126106.pdf", lines)
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert entries == []


def test_note_reference_rejected_even_with_parts_header(tmp_path, make_pdf):
    # Same note on a sheet that DOES carry a parts list: the header lets the
    # line parser run, but the cross-reference description still rejects the
    # note row while the genuine row is kept.
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST",
                    "F10126109 GUSSET RING",
                    "MAINTAINED PER WELDMENT DRAWING",
                    "F10126107 AND F10126108."])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert ("F10126109", 1, "text-fallback", "GUSSET RING") in entries
    assert all(v != "F10126107" for v, *_rest in entries)


def test_single_line_desc_containing_reference_is_kept(tmp_path, make_pdf):
    # Boundary: a genuine part name that merely mentions another drawing
    # ("CLAMP REF F10126108") is not a pure cross-reference - keep it.
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CLAMP REF F10126108"])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert ("F10126107", 1, "text-fallback", "CLAMP REF F10126108") in entries


def test_cross_reference_connector_forms():
    # The tokenizer must not mangle connectors: "W/" stays "W/" (it is in
    # the connector set), so every spelling of a pure pointer is rejected
    # while real names pass.
    is_ref = extraction._desc_is_cross_reference
    assert is_ref("AND F10118731.") is True
    assert is_ref("W/ F10118731") is True
    assert is_ref("F10112550 & F10118731") is True
    assert is_ref("CHILD PART") is False
    assert is_ref("CLAMP REF F10126108") is False
    assert is_ref("O-RING") is False


def test_multiline_cross_reference_desc_does_not_fall_through(tmp_path,
                                                              make_pdf):
    # Format-1 shape (ITEM above) with a cross-reference description: the
    # row must be skipped, not fall through to Format 3 (whose context the
    # ITEM line above would satisfy, emitting the rejected drawing anyway).
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "1", "F10126107", "AND F10126108."])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert entries == []


def test_split_line_column_headers_enable_fallback(tmp_path, make_pdf):
    # Column labels emitted one per line ("ITEM" / "FERMI #") still count as
    # a parts-list header; the genuine row below is kept while a note-style
    # cross-reference on the same sheet is rejected.
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["ITEM", "FERMI #", "1", "F10126107", "CHILD PART",
                    "MAINTAINED PER DRAWING", "F10126108 AND F10126109."])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    values = {v for v, *_rest in entries}
    assert "F10126107" in values
    assert "F10126108" not in values


def test_multiline_bom_skips_note_lines_until_qty(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "1", "F10126107", "CHILD PART A",
                    "SOME NOTE", "4", "2", "F10126108", "CHILD PART B", "2"])
    entries, method, issues = extraction.extract_bom_entries(pdf)
    assert issues == []
    assert method == "text-fallback"
    assert ("F10126107", 1, "text-fallback", "CHILD PART A") in entries
    assert ("F10126108", 1, "text-fallback", "CHILD PART B") in entries


class _FakeTable:
    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc

    def extract(self):
        if self._exc:
            raise self._exc
        return self._data


class _FakePage:
    def __init__(self, tables=None, exc=None):
        self._tables = tables or []
        self._exc = exc

    def find_tables(self):
        if self._exc:
            raise self._exc
        import types
        return types.SimpleNamespace(tables=self._tables)


def test_table_extraction_header_find_row_scan_and_issues():
    class _FakeDoc:
        def __init__(self, pages):
            self._pages = pages

        def __iter__(self):
            return iter(self._pages)

    issues = []
    doc = _FakeDoc([
        _FakePage([_FakeTable([["ITEM", "FERMI #", "DESC"],
                               ["1", "F10126107", "A"],
                               ["2", "FC10126107", "FC row"],
                               ["3", "ZZZ", "bad"],
                               ["4"]]),
                   _FakeTable([]),
                   _FakeTable(exc=RuntimeError("extract boom"))]),
        _FakePage(exc=RuntimeError("find boom")),
        _FakePage([_FakeTable([[None, None, None],
                               [None, None, None],
                               ["x", "FERMI NO.", "y"],
                               ["1", "F10126108", "B"]])]),
    ])
    entries = extraction.extract_bom_from_tables(doc, issues)
    assert entries == [("F10126107", 1, "table"), ("FC10126107", 1, "table"),
                       ("F10126108", 3, "table")]
    assert issues == ["page 1: table extract failed: extract boom",
                      "page 2: table detection failed: find boom"]


def test_merge_bom_entries_dedupes_by_value_first_wins():
    table = [("F1", 1, "table", "A"), ("F2", 1, "table", "B")]
    text = [("F1", 1, "text-fallback", "X"), ("F3", 1, "text-fallback", "C")]
    assert extraction.merge_bom_entries(table, text) == [
        ("F1", 1, "table", "A"),
        ("F2", 1, "table", "B"),
        ("F3", 1, "text-fallback", "C"),
    ]


def test_normalize_and_is_fermi_value():
    assert extraction.normalize(" f 101 261 07 ") == "F10126107"
    assert extraction.normalize(None) == ""
    assert is_fermi_value("F10126107")
    assert not is_fermi_value("FC10126107")
    assert not is_fermi_value("f10126107")
    assert not is_fermi_value("")
    assert not is_fermi_value(None)


def test_extract_used_on_and_drawing_name(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126107.pdf",
                   ["NAME", "CHILD PART", "USED ON", "F10126106"])
    with fitz.open(pdf) as doc:
        assert extraction.extract_used_on(doc) == ["F10126106"]
        assert extraction.extract_drawing_name(doc) == "CHILD PART"


def test_title_task_drops_own_drawing_number(tmp_path, make_pdf):
    # The USED ON box neighbours the DRAWING NUMBER box; the drawing's own
    # number is never a parent and must not survive as a USED ON value.
    pdf = make_pdf(tmp_path / "F10126107.pdf",
                   ["NAME", "CHILD PART", "USED ON", "F10126106 F10126107"])
    status, used, _name, _delta = extraction.title_task(str(pdf))
    assert status == "ok"
    assert used == ["F10126106"]


def test_strip_self_bom_drops_own_number(tmp_path):
    # OCR parts-list reads can pick up the title block's own FERMI number as a
    # row; a self-entry would make a childless leaf look like a root assembly.
    entries = [("F10126107", "CHILD PART"), ("F10126106", "SELF")]
    kept = extraction._strip_self_bom(entries, str(tmp_path / "F10126106.pdf"))
    assert [e[0] for e in kept] == ["F10126107"]


def test_extract_title_block_reads_number_and_revision(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106_A_DWG1.pdf", [
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "B",
        "NUMBER",
        "F10126106",
        "SHEET 1 OF 1",
    ])
    with fitz.open(pdf) as doc:
        assert extraction.extract_title_block(doc) == ("F10126106", "B")


def test_extract_title_block_dash_revision(tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106___DWG1.pdf", [
        "UNLESS OTHERWISE SPECIFIED",
        "REV",
        "-",
        "NUMBER",
        "F10126106",
        "SHEET 1 OF 1",
    ])
    with fitz.open(pdf) as doc:
        assert extraction.extract_title_block(doc) == ("F10126106", "-")


def test_ocr_partslist_header_prefers_bottom_up_list_over_titleblock_text():
    # Fermilab sheets draw the parts list bottom-up (rows above the header).
    # The title-block 'FERMI NATIONAL ACCELERATOR LABORATORY' text with one
    # stray aligned token must not win the header search.
    words = [
        (1210, 900, 1270, 910, "F10197140", 0, 0, 0),
        (1210, 930, 1270, 940, "F10197132", 0, 0, 0),
        (1100, 997, 1136, 1005, "ITEM", 0, 0, 0),
        (1219, 997, 1250, 1007, "FERMI", 0, 0, 0),
        (1260, 997, 1266, 1007, "#", 0, 0, 0),
        (1300, 997, 1340, 1007, "PART", 0, 0, 0),
        (1405, 1037, 1440, 1047, "FERMI", 0, 0, 0),
        (1445, 1037, 1480, 1047, "NATIONAL", 0, 0, 0),
        (1405, 1070, 1465, 1080, "F10197062", 0, 0, 0),
    ]
    hdr = extraction._partslist_header(words)
    assert hdr is not None
    assert hdr[2] == 1219  # fer_x of the real header, not the decoy
    assert hdr[6] == "above"


def test_ocr_partslist_rows_exclude_rcd_and_read_bottom_up(monkeypatch):
    words = [
        (1210, 46, 1300, 56, "F10197062-A-RCD", 0, 0, 0),  # RCD block, not a part
        (1210, 900, 1270, 910, "F10197140", 0, 0, 0),
        (1210, 930, 1270, 940, "F10197132", 0, 0, 0),
        (1100, 997, 1136, 1005, "ITEM", 0, 0, 0),
        (1219, 997, 1250, 1007, "FERMI", 0, 0, 0),
        (1300, 997, 1340, 1007, "PART", 0, 0, 0),
    ]
    monkeypatch.setattr(extraction, "_ocr_strip_tokens",
                        lambda page, rect, psm="7": [])
    assert extraction._partslist_from_words(None, words) == ["F10197140",
                                                             "F10197132"]


def test_iso_page_scale_and_ocr_render_dpi():
    import types
    from fermi_organizer.extraction import iso_page_scale, _ocr_render_dpi

    # Sheets are A0-A4, stored either at ISO size (72 dpi page) or at the
    # scanner's dpi (1 px = 1 pt), e.g. an A0 scan at 153 dpi.
    assert iso_page_scale(1191, 842) == 1.0          # A3
    assert iso_page_scale(1684, 1191) == 1.0         # A2
    assert iso_page_scale(3370, 2384) == 1.0         # A0
    assert abs(iso_page_scale(3572, 2526) - 1.060) < 0.01   # A0 @ 76 dpi
    assert abs(iso_page_scale(7152, 5051) - 2.122) < 0.01   # A0 @ 153 dpi

    # The render dpi is capped so a 627 MP render never happens (PyMuPDF
    # fails with "Overly large image" and yields zero words).
    assert _ocr_render_dpi(types.SimpleNamespace(rect=fitz.Rect(0, 0, 1191, 842))) == 300
    assert _ocr_render_dpi(types.SimpleNamespace(rect=fitz.Rect(0, 0, 7152, 5051))) == 207


def test_ocr_drawing_number_variants():
    assert extraction._ocr_drawing_number("F10197062") == "F10197062"
    assert extraction._ocr_drawing_number("Floos2259") == "F10052259"
    assert extraction._ocr_drawing_number("10197062") == "F10197062"
    assert extraction._ocr_drawing_number("1") is None
    assert extraction._ocr_drawing_number("SHEET") is None


def test_title_value_under_ocr_wide_window_and_candidate_rank():
    from fermi_organizer.config import TB_NUM_VAL_RE
    # OCR word boxes can be offset above the label; a corrected F-reading
    # outranks bare digits even when the bare digits sit closer.
    words = [
        (100, 100, 130, 110, "NUMBER", 0, 0, 0),
        (105, 70, 150, 80, "Floos2259", 0, 0, 0),   # rank 1, farther
        (105, 120, 150, 130, "10032259", 0, 0, 0),  # rank 2, closer
    ]
    v = extraction._title_value_under(
        words, "NUMBER", TB_NUM_VAL_RE, -40, 170, 40, 40, wide=True,
        convert=extraction._ocr_drawing_number, rank=extraction._ocr_number_rank)
    assert v == "F10052259"

    # With only bare digits, the closest to the label wins.
    words2 = [
        (100, 100, 130, 110, "NUMBER", 0, 0, 0),
        (105, 95, 150, 105, "10197062", 0, 0, 0),
        (105, 130, 150, 140, "10032259", 0, 0, 0),
    ]
    v2 = extraction._title_value_under(
        words2, "NUMBER", TB_NUM_VAL_RE, -40, 170, 40, 40, wide=True,
        convert=extraction._ocr_drawing_number, rank=extraction._ocr_number_rank)
    assert v2 == "F10197062"


def test_ocr_context_reset_clears_state_and_unique_doc_key_fallback():
    ocr = extraction.OCR
    ocr.reset(enabled=True)
    ocr.available = True
    ocr.reason = "stale"
    ocr.tesseract_path = "C:\\stale\\tesseract.exe"
    ocr.events["stale.pdf"] = {"pages": {0}}
    ocr.page_memo[("stale.pdf", 0)] = ([], None, [])
    ocr.words_memo[("stale.pdf", 0)] = []
    ocr.reset()
    assert ocr.enabled
    assert not ocr.available
    assert ocr.reason is None
    assert ocr.tesseract_path is None
    assert ocr.events == {}
    assert ocr.page_memo == {}
    assert ocr.words_memo == {}

    class _Page:
        def __init__(self):
            self.parent = object()  # no name/metadata -> empty-name fallback
            self.number = 0

    keys = [ocr.doc_key(_Page()), ocr.doc_key(_Page())]
    assert keys[0] != keys[1]
    assert all("id(" not in k[0] for k in keys)


def test_ocr_tok_pipe_confusion():
    # A pipe *inside* a token is Tesseract's misread 1 (FLO|44633 -> F10144633)
    # and must survive correction; a leading table-border pipe must be
    # stripped, not turned into a leading 1.
    assert extraction._ocr_tok("FLO|44633") == "F10144633"
    assert extraction._ocr_tok("|F10126106") == "F10126106"
    assert extraction._ocr_tok("1F10126106") is None
    assert extraction._ocr_tok("F10126513]SSR2") == "F10126513"


def test_worker_tasks_use_open_pdf_seam(tmp_path, make_pdf, monkeypatch):
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART", "NAME", "Parent"])
    calls = []
    real_open = fitz.open

    def spy(path, *args, **kwargs):
        calls.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(extraction, "_open_pdf", spy)

    entries, _method, _issues = extraction.extract_bom_entries(str(pdf))
    assert any(e[0] == "F10126107" for e in entries)

    status, _used, _name, _delta = extraction.title_task(str(pdf))
    assert status == "ok"

    (status2, _entries, _method2, _used2, _issues2, _wm2, _num2, _rev2,
     _name2, _scanned2, _delta2) = extraction.org_task(str(pdf))
    assert status2 == "ok"

    assert [Path(c) for c in calls] == [pdf, pdf, pdf]


def test_detect_watermark_transparent_text(tmp_path):
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((120, 400), "ACME CONFIDENTIAL", fontsize=60,
                     fill_opacity=0.25, color=(0.8, 0.8, 0.8))
    doc.save(str(p))
    doc.close()

    with fitz.open(str(p)) as doc:
        assert extraction.detect_watermark(doc) == \
            "transparent text: ACME CONFIDENTIAL"


def test_detect_watermark_keyword(tmp_path):
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PRELIMINARY", fontsize=12)
    doc.save(str(p))
    doc.close()

    with fitz.open(str(p)) as doc:
        assert extraction.detect_watermark(doc) == "watermark text: PRELIMINARY"


def test_detect_watermark_nx_style_transparent_diagonal(tmp_path):
    # Real NX/cgm2pdf watermarks: huge transparent diagonal text (the
    # PRE-RELEASED and DRAFT stamps seen in SSR2-ED Downloads).
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    page = doc.new_page()
    pivot = fitz.Point(300, 400)
    page.insert_text(pivot, "PRE-RELEASED", fontsize=80, fill_opacity=0.3,
                     morph=(pivot, fitz.Matrix(45)))
    doc.save(str(p))
    doc.close()

    with fitz.open(str(p)) as doc:
        # get_text() can truncate the stamp; the texttrace opacity check
        # still reports the full string.
        assert extraction.detect_watermark(doc) == "transparent text: PRE-RELEASED"


def test_detect_watermark_diagonal_opaque(tmp_path):
    # Opaque diagonal large text (no keyword) is still a watermark.
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    page = doc.new_page()
    pivot = fitz.Point(300, 400)
    page.insert_text(pivot, "ACME INTERNAL", fontsize=80,
                     morph=(pivot, fitz.Matrix(45)))
    doc.save(str(p))
    doc.close()

    with fitz.open(str(p)) as doc:
        assert extraction.detect_watermark(doc) == "diagonal text: ACME INTERNAL"


def test_detect_watermark_ignores_template_text(tmp_path):
    # 'Drafting' (title-block label) and a small rotated 'TEMPLATE VERSION'
    # must not be mistaken for watermarks.
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(["FERMI PART LIST", "F10126107 CHILD PART",
                              "NAME", "Drafting"]):
        page.insert_text((72, 72 + 16 * i), line, fontsize=11)
    page.insert_text((400, 300), "TEMPLATE VERSION: 2021.08.06", fontsize=7, rotate=90)
    doc.save(str(p))
    doc.close()

    with fitz.open(str(p)) as doc:
        assert extraction.detect_watermark(doc) is None


def test_scanned_pages_recorded_even_with_ocr_disabled(tmp_path):
    p = tmp_path / "F10126106.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()

    extraction.extract_bom_entries(str(p))

    assert extraction.OCR.scanned_stems() == [("F10126106", [1])]


def test_ensure_tesseract_unparseable_version_is_none(tmp_path, monkeypatch):
    fake = tmp_path / "tesseract.exe"
    fake.write_bytes(b"fake tesseract binary")
    monkeypatch.setenv("TESSERACT_EXE", str(fake))

    class _Proc:
        stdout = b"tesseract (no numeric version)"
        stderr = b""
        returncode = 0

    monkeypatch.setattr(extraction.subprocess, "run", lambda *a, **k: _Proc())
    ocr = extraction._OcrContext()
    ocr.reset(enabled=True)

    result = ocr.ensure_tesseract()

    assert result is not None
    assert os.path.normcase(result) == os.path.normcase(os.path.realpath(str(fake)))
    assert ocr.available
    assert ocr.version is None


@pytest.mark.skipif(os.name != "nt", reason="Windows PATH/drive-letter semantics")
def test_ensure_tesseract_path_prepend_idempotent(monkeypatch):
    cand = r"C:\Program Files\Tesseract-OCR"
    fake_exe = os.path.join(cand, "tesseract.exe")
    real_isfile = os.path.isfile

    def fake_isfile(p):
        if os.path.normcase(str(p)) == os.path.normcase(fake_exe):
            return True
        return real_isfile(p)

    class _Proc:
        stdout = b"tesseract v5.3.0"
        stderr = b""
        returncode = 0

    monkeypatch.delenv("TESSERACT_EXE", raising=False)
    monkeypatch.setattr(extraction.shutil, "which", lambda name: None)
    monkeypatch.setattr(extraction.os.path, "isfile", fake_isfile)
    monkeypatch.setattr(extraction.subprocess, "run", lambda *a, **k: _Proc())
    orig_path = os.environ.get("PATH", "")
    before = orig_path.split(os.pathsep).count(cand)
    monkeypatch.setenv("PATH", orig_path)
    had_tessdata = "TESSDATA_PREFIX" in os.environ
    saved_tessdata = os.environ.get("TESSDATA_PREFIX")

    ocr = extraction._OcrContext()
    try:
        for _ in range(2):
            ocr.reset(enabled=True)
            assert ocr.ensure_tesseract()
            assert ocr.version == "5.3.0"
        # Repeated reset()+ensure_tesseract() must not grow PATH with a dupe.
        assert os.environ["PATH"].split(os.pathsep).count(cand) == max(1, before)
    finally:
        if had_tessdata:
            os.environ["TESSDATA_PREFIX"] = saved_tessdata
        else:
            os.environ.pop("TESSDATA_PREFIX", None)


def test_ocr_strip_tokens_utf8_decode_and_none_stdout(monkeypatch, tmp_path, make_pdf):
    # Tesseract emits UTF-8: a curly quote's 0x9d byte is undecodable in the
    # locale codepage (cp1252), the subprocess reader thread dies, and
    # r.stdout comes back None - .upper() then crashed the extraction worker
    # and aborted the whole run (real-data HBCM dry-run).
    import subprocess as sp

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw)
        return sp.CompletedProcess(cmd, 0, stdout=None, stderr="")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    out = extraction._ocr_strip_tokens(page, fitz.Rect(72, 72, 220, 95), psm="7")

    assert out == []
    assert calls, "tesseract subprocess must be invoked"
    assert all(k.get("encoding") == "utf-8" and k.get("errors") == "replace"
               for k in calls)
    doc.close()


def test_tesseract_run_raises_on_nonzero_exit(monkeypatch):
    # A failing tesseract leaves stdout empty; callers must not read that as
    # a genuinely blank strip (silent accuracy loss - review finding S-021).
    import subprocess as sp

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, 3, stdout="", stderr="E: broken tessdata")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="tesseract exit 3: E: broken tessdata"):
        extraction._tesseract_run(["tesseract", "x.png", "stdout"], 60)


def test_ensure_tesseract_nonzero_version_exit_is_unavailable(tmp_path, monkeypatch):
    fake = tmp_path / "tesseract.exe"
    fake.write_bytes(b"fake tesseract binary")
    monkeypatch.setenv("TESSERACT_EXE", str(fake))

    class _Proc:
        stdout = b""
        stderr = b"Error opening data file"
        returncode = 1

    monkeypatch.setattr(extraction.subprocess, "run", lambda *a, **k: _Proc())
    ocr = extraction._OcrContext()
    ocr.reset(enabled=True)

    assert ocr.ensure_tesseract() is None
    assert not ocr.available
    assert "--version exit 1" in ocr.reason


def test_ocr_strip_tokens_survives_single_pass_failure(monkeypatch, tmp_path, make_pdf):
    # PR-FIX-008: one failing voting pass must not kill the strip - the
    # remaining passes still vote. (A fully broken engine still raises.)
    import subprocess as sp

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return sp.CompletedProcess(cmd, 0, stdout="F10126107", stderr="")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    out = extraction._ocr_strip_tokens(page, fitz.Rect(72, 72, 220, 95), psm="7")

    assert out == ["F10126107"]
    assert len(calls) == 3
    doc.close()


def test_ocr_strip_tokens_all_passes_failing_raises(monkeypatch, tmp_path, make_pdf):
    import subprocess as sp

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, 3, stdout="", stderr="broken")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    with pytest.raises(RuntimeError, match="tesseract exit 3"):
        extraction._ocr_strip_tokens(page, fitz.Rect(72, 72, 220, 95), psm="7")
    doc.close()


def test_extract_title_block_ocr_failure_recorded_not_silent(monkeypatch, tmp_path,
                                                             make_pdf):
    # Review finding: a failed title-block OCR read returned (None, None) with
    # no record, so the file silently skipped mismatch reporting. Failures now
    # land in the caller's issues list (same idiom as the USED ON zoom path).
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["x"])
    doc = fitz.open(pdf)
    assert len(doc[0].get_text().strip()) < extraction.OCR_MIN_CHARS
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "available", True)

    def boom(page):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(extraction.OCR, "words_cached", boom)
    issues = []
    assert extraction.extract_title_block(doc, issues) == (None, None)
    assert issues == ["title-block OCR failed: engine gone"]

    monkeypatch.setattr(extraction.OCR, "words_cached", lambda page: [])

    def boom_zoom(*a):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(extraction, "_ocr_words_zoom", boom_zoom)
    issues = []
    assert extraction.extract_title_block(doc, issues) == (None, None)
    assert issues == ["title-block zoom OCR failed: engine gone"]
    doc.close()


def test_subprocess_env_scoped_not_global(monkeypatch, tmp_path):
    # Our CLI children get an explicit env; the process-global export exists
    # only for PyMuPDF's in-process OCR (which offers no env parameter).
    import os

    ocr = extraction._OcrContext()
    ocr.reset(enabled=True)
    assert ocr.subprocess_env() is None

    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    ocr.install_dir = str(tmp_path)
    env = ocr.subprocess_env()
    assert env["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")
    assert "TESSDATA_PREFIX" not in os.environ

    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)
        import subprocess as sp
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")
    monkeypatch.setattr(extraction.OCR, "install_dir", str(tmp_path))
    extraction._tesseract_run(["tesseract", "x.png", "stdout"], 60)
    assert captured["env"]["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")


def test_ensure_tesseract_probe_uses_explicit_env(tmp_path, monkeypatch):
    # LOW: the --version probe takes the same explicit-env path as the OCR
    # calls (it must not depend on the process-global export either).
    fake = tmp_path / "tesseract.exe"
    fake.write_bytes(b"fake tesseract binary")
    monkeypatch.setenv("TESSERACT_EXE", str(fake))
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    captured = {}

    class _Proc:
        stdout = b"tesseract 5.5.3"
        stderr = b""
        returncode = 0

    def fake_run(cmd, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    ocr = extraction._OcrContext()
    ocr.reset(enabled=True)
    ocr.install_dir = str(tmp_path)

    assert ocr.ensure_tesseract() is not None
    assert captured["env"]["TESSDATA_PREFIX"] == str(tmp_path / "tessdata")
    assert "TESSDATA_PREFIX" not in os.environ


def test_export_for_pymupdf_switch_drops_stale_prefix(monkeypatch, tmp_path):
    # FIX: switching installs mid-process must not leave the old dir first on
    # PATH (later OCR would resolve the wrong tessdata through it).
    import os

    a = tmp_path / "A"
    b = tmp_path / "B"
    monkeypatch.setenv("PATH", "C:\\x")
    monkeypatch.delenv("TESSDATA_PREFIX", raising=False)
    ocr = extraction._OcrContext()

    ocr._export_for_pymupdf(str(a))
    ocr._export_for_pymupdf(str(a))
    assert os.environ["PATH"].split(os.pathsep).count(str(a)) == 1

    ocr._export_for_pymupdf(str(b))
    parts = os.environ["PATH"].split(os.pathsep)
    assert str(a) not in parts and parts[0] == str(b)
    assert os.environ["TESSDATA_PREFIX"] == str(b / "tessdata")


def _wide_window_rows(label_x):
    """USED ON label 4 rows above a data row (y=150), with spec text between."""
    rows = {110: [(label_x, "USED"), (label_x + 20, "ON")]}
    for y in (120, 130, 140):
        rows[y] = [(50, "SPEC")]
    rows[150] = [(50, "12"), (200, "X")]
    return rows


def test_word_path_row_with_keyword_words_not_title_block():
    """A BOM row's own text may contain a title-block keyword word
    ('SHEET METAL BRACKET'); only whole-row keyword captions (or a USED ON /
    drawing-size cell) mark the title block, so such a row must not disable
    the F-number rows below it."""
    rows = {100: [(50, "SHEET"), (74, "METAL"), (98, "BRACKET"), (122, "ITEM")],
            110: [(50, "1"), (60, "F10126107")],
            120: [(50, "2"), (60, "F10126108")]}
    ys = sorted(rows)
    y_index = {y: i for i, y in enumerate(ys)}
    row_words = sorted(rows[110], key=lambda t: t[0])

    assert not extraction._word_row_is_title_block(rows, ys, 110, row_words,
                                                   y_index)


def test_word_path_keyword_caption_row_is_title_block():
    # Whole-row title-block captions still disable the rows below them.
    rows = {100: [(50, "USED"), (74, "ON")],
            110: [(50, "F10126109")],
            120: [(50, "SIZE"), (74, "A0")],
            130: [(50, "1"), (60, "F10126107")]}
    ys = sorted(rows)
    y_index = {y: i for i, y in enumerate(ys)}
    assert extraction._word_row_is_title_block(rows, ys, 110,
                                               sorted(rows[110]), y_index)
    assert extraction._word_row_is_title_block(rows, ys, 130,
                                               sorted(rows[130]), y_index)


def test_used_on_wide_window_requires_horizontal_association():
    ys = sorted(_wide_window_rows(50))
    y_idx = ys.index(150)
    # Label in the row's own column: rejected as title-block content.
    aligned = _wide_window_rows(50)
    assert extraction._positional_row_is_title_block(aligned, ys, y_idx, 150)
    # Same vertical gap, label in a different column: the row is kept.
    far = _wide_window_rows(600)
    assert not extraction._positional_row_is_title_block(far, ys, y_idx, 150)
