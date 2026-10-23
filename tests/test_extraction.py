"""BOM / USED ON / NAME extraction from generated PDFs."""
import os
import types
from pathlib import Path

import pymupdf as fitz
import pytest

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


def test_single_line_desc_keeps_commas(tmp_path, make_pdf):
    # F10187622-style parts list: the description is a comma-separated part
    # name. The char class used to stop at the first comma, truncating
    # 'NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD' to 'NUT PLATE' and feeding that
    # stub to a scanned child's folder name.
    pdf = make_pdf(tmp_path / "F10187622.pdf",
                   ["FERMI PART LIST", "F10128174 NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD"])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert ("F10128174", 1, "text-fallback",
            "NUT PLATE, MINI-CONFLAT, M4 X 0.7 THD") in entries


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


def test_ocr_words_zoom_passes_psm_before_the_tsv_config(tmp_path, make_pdf, monkeypatch):
    # Tesseract parses options before the config file: with 'tsv' first, every
    # --psm was silently dropped and the pass ran at the default psm 3
    # (F10051459's title-block NAME came back empty).
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "X"])
    # Hold the document: the page belongs to it, and opening inline would leave
    # only the page alive as a temporary.
    src_doc = fitz.open(pdf)
    page = src_doc[0]
    seen = []

    def fake_run(args, timeout):
        seen.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(extraction, "_tesseract_run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract")
    extraction._ocr_words_zoom(page, fitz.Rect(0, 0, 100, 50), 2, "6")
    src_doc.close()

    assert seen, "tesseract was not invoked"
    argv = seen[0]
    assert argv[-1] == "tsv"
    assert argv.index("--psm") < argv.index("tsv")
    assert argv[argv.index("--psm") + 1] == "6"
    # F10202541-style sheet: the parts list is a FERMI# column at x=450 and a
    # view balloon prints a bare part number at x=100. The balloon used to
    # become a phantom child (and an extra folder for a real PDF in the tree).
    doc = fitz.open()
    page = doc.new_page(width=1400, height=900)
    page.insert_text((400, 450), "FERMI PART LIST", fontsize=11)
    for text, x in [("ITEM", 400), ("FERMI", 450), ("#", 490),
                    ("PART", 530), ("NAME", 575), ("QTY", 630)]:
        page.insert_text((x, 470), text, fontsize=11)
    for text, x, y in [("F10126109", 100, 300),
                       ("F10126107 CHILD PART", 450, 500),
                       ("F10126108 CHILD TWO", 450, 520)]:
        page.insert_text((x, y), text, fontsize=11)
    pdf = tmp_path / "F10202541.pdf"
    doc.save(str(pdf))
    doc.close()

    page = fitz.open(pdf)[0]
    words = extraction._page_words(page)
    band = extraction._partslist_band(words)
    assert band is not None
    in_band = extraction._fermi_values_in_band(words, band)
    assert "F10126107" in in_band and "F10126108" in in_band
    # ...and it sits outside the parts-list column, so it is not a row.
    assert "F10126109" not in in_band
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert [v for v, *_rest in entries] == ["F10126107", "F10126108"]


def test_desc_plausible_rejects_item_digits_and_stray_letters():
    assert not extraction._desc_plausible("")
    assert not extraction._desc_plausible("F")
    assert not extraction._desc_plausible("11")
    assert extraction._desc_plausible("SEAL")
    assert extraction._desc_plausible("O-RING")
    # A numeric catalogue name is a real description (F10149343's FC0078646).
    assert extraction._desc_plausible("8020-30-3030")


def test_multiline_bom_keeps_row_but_drops_junk_description(tmp_path, make_pdf):
    # Format 1 takes the line below the FERMI# verbatim; a bare letter there is
    # not a part name (F10202541's balloon row read a description of 'F').
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "1", "F10126107", "F", "2"])
    entries, _method, _issues = extraction.extract_bom_entries(pdf)
    assert [v for v, *_rest in entries] == ["F10126107"]
    assert entries[0][3] == ""


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


def test_ocr_partslist_ignores_titleblock_used_on_of_empty_parts_list(
        tmp_path, make_pdf, monkeypatch):
    # F10133759 is a flange with an EMPTY parts list. Its ITEM / FERMI# / PART
    # NAME header is still drawn, and the only F-number aligned in that column
    # is the title block's own USED ON value (a PARENT). It used to be read as a
    # parts-list row, inventing both a BOM edge and a missing reference out of a
    # parent number. Geometry is the real sheet's (1190.6 x 841.9 pts).
    words = [
        (550, 658, 578, 667, "ITEM", 0, 0, 0),
        (621, 658, 659, 667, "FERMI", 0, 0, 0),
        (669, 658, 675, 667, "#", 0, 0, 0),
        (877, 658, 900, 667, "PART", 0, 0, 0),
        (924, 658, 947, 667, "NAME", 0, 0, 0),
        (600, 735, 640, 745, "USED", 0, 0, 0),
        (646, 735, 668, 745, "ON", 0, 0, 0),
        (700, 738, 780, 748, "F10133744", 0, 0, 0),
    ]
    hdr = extraction._partslist_header(words)
    assert hdr is not None and hdr[6] == "below"  # the trap is set

    used, _name, _used_anchor = extraction._title_fields_from_words(words)
    assert used == ["F10133744"]
    # A lone F-number below the header is not a row at all (it has no ITEM
    # number): the sheet keeps its empty parts list.
    assert extraction._partslist_from_words(None, words, used) == []

    # Give the row the ITEM number a real list row has, so the row itself is
    # legitimate and only the title-block claim can reject the value.
    with_item = words + [(570, 738, 578, 748, "1", 0, 0, 0)]
    monkeypatch.setattr(extraction, "_ocr_strip_tokens",
                        lambda page, rect, psm="7": ["F10133744"])
    assert extraction._partslist_from_words(None, with_item, used) == []
    assert extraction._partslist_from_words(None, with_item, []) == ["F10133744"]

    extraction.OCR.reset(enabled=True)
    pdf = make_pdf(tmp_path / "F10133759.pdf", ["NAME", "Part"])
    page = fitz.open(str(pdf))[0]
    monkeypatch.setattr(extraction.OCR, "available", True)
    monkeypatch.setattr(extraction.OCR, "words_cached", lambda *a, **k: words)
    monkeypatch.setattr(extraction, "_ocr_words_zoom", lambda *a, **k: [])

    page_used, _page_name, page_bom = extraction.OCR.analyze_page(
        page, [], "F10133759")

    assert page_used == ["F10133744"]
    assert page_bom == []


def test_ocr_partslist_below_header_row_needs_its_item_number(monkeypatch):
    # A bottom-up parts list puts every row ABOVE the header, so a lone F-number
    # below it is the title block's - and reading it as a row invented BOM edges
    # and Missing entries (F10148138 took the cross-reference F10196096 89% down
    # the page). A top-down list is the exception, and it shows its ITEM numbers.
    header = [
        (588, 653, 620, 662, "ITEM", 0, 0, 0),
        (622, 653, 660, 662, "FERMI", 0, 0, 0),
        (668, 653, 674, 662, "#", 0, 0, 0),
    ]
    stray = (653, 752, 700, 762, "F10196096", 0, 0, 0)
    hdr = extraction._partslist_header(header + [stray])
    assert hdr is not None and hdr[6] == "below"  # the trap is set

    assert extraction._partslist_from_words(None, header + [stray]) == []

    # With its ITEM number beside it the same token is a row, and the row strip
    # (the value's own token is the ITEM cell here) recovers the number.
    item = (570, 752, 578, 762, "1", 0, 0, 0)
    monkeypatch.setattr(extraction, "_ocr_strip_tokens",
                        lambda page, rect, psm="7": ["F10196096"])
    assert extraction._partslist_from_words(None, header + [item, stray]) == [
        "F10196096"]


def test_iso_page_scale_and_ocr_render_dpi():
    import types

    from fermi_organizer.extraction import _ocr_render_dpi, iso_page_scale

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
    # Tesseract splits a cramped title-block number into fragments; a shorter
    # fragment must not be fabricated into a wrong number (F10119580 -> F19580).
    assert extraction._ocr_drawing_number("19580") is None
    assert extraction._ocr_drawing_number("F19580") is None
    assert extraction._ocr_drawing_number("F23056") is None
    assert extraction._ocr_drawing_number("F0042432") is None
    assert extraction._ocr_drawing_number("1012805") is None


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


def test_title_value_under_label_region_excludes_left_notes():
    from fermi_organizer.config import TB_NUM_VAL_RE
    # A note on the left repeats 'NUMBER' lower than the title-block label; the
    # label_x_min guard must ignore it and use the title block (right side).
    words = [
        (300, 400, 360, 410, "NUMBER", 0, 0, 0),     # note, left, lower
        (405, 400, 500, 410, "F10154728", 0, 0, 0),  # note's value
        (800, 300, 860, 310, "NUMBER", 0, 0, 0),     # title block, right
        (805, 320, 900, 330, "F10134728", 0, 0, 0),
    ]
    v = extraction._title_value_under(
        words, "NUMBER", TB_NUM_VAL_RE, -40, 170, 40, 40, wide=True,
        convert=extraction._ocr_drawing_number,
        rank=extraction._ocr_number_rank, label_x_min=600)
    assert v == "F10134728"
    # Without the guard the bottom-most (note) label hijacks the field.
    v2 = extraction._title_value_under(
        words, "NUMBER", TB_NUM_VAL_RE, -40, 170, 40, 40, wide=True,
        convert=extraction._ocr_drawing_number,
        rank=extraction._ocr_number_rank)
    assert v2 == "F10154728"


def test_name_plausible_rejects_short_ocr_fragments():
    # A 1-2 letter NAME is OCR noise from a mangled cell (the zoom pass reads
    # the real value); three letters is a real, terse name like 'LUG'.
    assert not extraction._name_plausible(None)
    assert not extraction._name_plausible("")
    assert not extraction._name_plausible("a")
    assert not extraction._name_plausible("er")
    assert extraction._name_plausible("LUG")
    assert extraction._name_plausible("SSR/650 CM LIFTING INTERFACE ASSY")


def test_name_from_anchor_accepts_tall_value_box():
    # The NAME value can be vertically centred in a tall title-block cell: its
    # OCR box then starts ABOVE the label's bottom edge (and reaches well
    # below it). The window must test vertical overlap, not the word's top
    # edge alone, or the value is dropped (F10209823 regressed to a bare
    # drawing-number folder this way).
    words = [
        (3002.0, 2271.0, 3020.0, 2279.0, "NAME", 0, 0, 0),
        (3051.0, 2270.0, 3086.0, 2316.0, "HB650", 0, 0, 0),
        (3090.0, 2270.0, 3118.0, 2316.0, "PROD", 0, 0, 0),
        (3122.0, 2270.0, 3144.0, 2316.0, "CM", 0, 0, 0),
        (3158.0, 2270.0, 3195.0, 2316.0, "PIPING", 0, 0, 0),
        (3201.0, 2270.0, 3292.0, 2316.0, "PROCUREMENT", 0, 0, 0),
    ]
    anchor = extraction._find_name_anchor(words)
    assert anchor is not None
    assert extraction._name_from_anchor(words, anchor) == (
        "HB650 PROD CM PIPING PROCUREMENT")


def test_name_from_anchor_ignores_words_below_the_window():
    # A word clearly past the window (the SCALE / DRAWING NUMBER chips under
    # the NAME cell) stays out.
    words = [
        (3002.0, 2271.0, 3020.0, 2279.0, "NAME", 0, 0, 0),
        (3051.0, 2270.0, 3086.0, 2316.0, "HB650", 0, 0, 0),
        (3100.0, 2360.0, 3160.0, 2375.0, "SCALE", 0, 0, 0),
    ]
    anchor = extraction._find_name_anchor(words)
    assert extraction._name_from_anchor(words, anchor) == "HB650"


def test_name_from_anchor_strict_row_wins_over_tall_noise():
    # The overlap fallback must not hijack a drawing that already reads a NAME
    # on the strict row: a tall box reaching into the window from above is only
    # considered when the strict pass finds nothing (f10157448-style names must
    # stay byte-identical).
    words = [
        (3002.0, 2271.0, 3020.0, 2279.0, "NAME", 0, 0, 0),
        (2990.0, 2100.0, 3030.0, 2280.0, "er", 0, 0, 0),           # overlap-only
        (3060.0, 2285.0, 3260.0, 2295.0, "REAL VALUE", 0, 0, 0),   # strict row
    ]
    anchor = extraction._find_name_anchor(words)
    assert extraction._name_from_anchor(words, anchor) == "REAL VALUE"


def test_name_from_anchor_reads_the_row_left_to_right():
    # F10128174's real OCR word boxes: the fragment '-CONFLAIL,' (OCR of
    # 'MINI-CONFLAT,') starts half a point higher than its neighbours, so the
    # old 'round(y)' sort key bucketed it into an earlier row and emitted it
    # first ('-CONFLAIL, NUT PLATE, MINI xX OQ.'). The name must read in x
    # order; the second line (misread 'THD' as 'TAD') stays out.
    words = [
        (823.5, 729.6, 842.0, 734.4, "NAME", 0, 0, 0),
        (831.6, 747.6, 858.6, 756.8, "NUT", 0, 0, 0),
        (871.1, 747.6, 924.8, 758.6, "PLATE,", 0, 0, 0),
        (940.3, 747.6, 973.8, 756.8, "MINI", 0, 0, 0),
        (980.8, 747.1, 1062.3, 760.6, "-CONFLAIL,", 0, 0, 0),
        (1077.3, 747.6, 1094.5, 756.8, "M4", 0, 0, 0),
        (1106.8, 747.6, 1114.1, 756.8, "xX", 0, 0, 0),
        (1126.4, 747.6, 1153.4, 756.8, "OQ.", 0, 0, 0),
        (979.0, 761.1, 1006.1, 770.4, "TAD", 0, 0, 0),
    ]
    anchor = extraction._find_name_anchor(words)
    assert extraction._name_from_anchor(words, anchor) == (
        "NUT PLATE, MINI -CONFLAIL, xX OQ.")


def test_find_name_anchor_keeps_label_with_distant_part_note():
    # A left-column note ("PART TO BE FREE OF ALL SHARP EDGES") can land on the
    # same visual row as the title-block NAME label. Only an *adjacent* PART
    # word (the "PART NAME" header pair) may hide the label (F10144038).
    words = [
        (100.0, 729.0, 140.0, 739.0, "PART", 0, 0, 0),   # note, far left
        (300.0, 730.0, 360.0, 740.0, "EDGES", 0, 0, 0),
        (823.0, 729.6, 850.0, 740.0, "NAME", 0, 0, 0),   # title-block label
    ]
    anchor = extraction._find_name_anchor(words)
    assert anchor is not None
    assert anchor[4] == "NAME"


def test_find_name_anchor_rejects_adjacent_part_name_header():
    # The parts-list column header "PART NAME" must still be rejected.
    words = [
        (500.0, 200.0, 560.0, 210.0, "PART", 0, 0, 0),
        (566.0, 200.0, 610.0, 210.0, "NAME", 0, 0, 0),
    ]
    assert extraction._find_name_anchor(words) is None


def test_analyze_page_zoom_recovers_name_when_used_on_present(
        tmp_path, make_pdf, monkeypatch):
    # The title-block zoom re-OCR must also run when NAME is missing though
    # USED ON was read - the full-page pass misses the small NAME label
    # (F10112629). OCR is simulated, so no Tesseract is needed.
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    page = fitz.open(str(pdf))[0]

    full_words = [
        (100.0, 500.0, 140.0, 510.0, "USED", 0, 0, 0),
        (146.0, 500.0, 170.0, 510.0, "ON", 0, 0, 0),
        (180.0, 500.0, 240.0, 510.0, "F10126107", 0, 0, 0),
    ]
    zoom_words = [
        (823.0, 729.0, 850.0, 740.0, "NAME", 0, 0, 0),
        (836.0, 746.0, 880.0, 756.0, "FLAT", 0, 0, 0),
        (930.0, 746.0, 970.0, 756.0, "RAIL", 0, 0, 0),
        (979.0, 746.0, 1030.0, 756.0, "SUPPORT", 0, 0, 0),
        (1057.0, 746.0, 1110.0, 756.0, "BLOCK", 0, 0, 0),
    ]
    extraction.OCR.reset(enabled=True)
    monkeypatch.setattr(extraction.OCR, "available", True)
    monkeypatch.setattr(extraction.OCR, "words_cached",
                        lambda *a, **k: full_words)
    monkeypatch.setattr(extraction, "_ocr_words_zoom",
                        lambda *a, **k: zoom_words)

    used, name, _bom = extraction.OCR.analyze_page(page, [], "F10126106")

    assert used == ["F10126107"]
    assert name == "FLAT RAIL SUPPORT BLOCK"


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


def test_short_text_parts_list_survives_ocr_reading_nothing(monkeypatch, tmp_path,
                                                            make_pdf):
    # Review finding (DA escalation): analyze_page memoizes every page it sees,
    # including one where OCR read no words, and extract_bom_from_text skipped
    # the native parser on the memo key alone - so a page under OCR_MIN_CHARS
    # lost its text-layer parts-list row whenever Tesseract was configured but
    # returned nothing. The child edge vanished and the drawing was parked in
    # _orphans/ with exit 0. The autouse OCR-off fixture hid it from the suite.
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART"])
    doc = fitz.open(pdf)
    # Force the image-only branch on a page that does carry a text-layer list.
    monkeypatch.setattr(extraction, "OCR_MIN_CHARS", 10_000)
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "available", True)
    # The OCR engine finds no words: words_cached memoizes an empty word list.
    monkeypatch.setattr(extraction.pymupdf.Page, "get_textpage_ocr",
                        lambda self, **kw: None)

    entries = extraction.extract_bom_from_text(doc, [])

    assert [e[0] for e in entries] == ["F10126107"]
    doc.close()


def test_short_text_parts_list_survives_ocr_used_on_only(monkeypatch, tmp_path,
                                                         make_pdf):
    # Same branch, the other half: OCR read a USED ON value but no rows. That
    # is not a reason to skip the native parser either - and the value OCR
    # called USED ON stays excluded, because it is a title-block parent.
    pdf = make_pdf(tmp_path / "F10126106.pdf",
                   ["FERMI PART LIST", "F10126107 CHILD PART"])
    doc = fitz.open(pdf)
    monkeypatch.setattr(extraction, "OCR_MIN_CHARS", 10_000)
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "available", True)

    monkeypatch.setattr(extraction.OCR, "analyze_page",
                        lambda *a, **k: (["F10150000"], None, []))
    assert [e[0] for e in extraction.extract_bom_from_text(doc, [])] == ["F10126107"]

    # A value OCR identified as USED ON is never re-added as a row.
    monkeypatch.setattr(extraction.OCR, "analyze_page",
                        lambda *a, **k: (["F10126107"], None, []))
    assert extraction.extract_bom_from_text(doc, []) == []
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


def test_used_on_caption_merged_with_note_row_marks_title_block():
    # Row clustering is y-only, so a left-column note line lands in the SAME
    # row as the right-column title-block caption ('BREAK ALL SHARP EDGES,
    # MAX: 0.5 | USED ON | NAME'). The joined text no longer starts with
    # 'USED ON', which let F10153201's USED ON value become a BOM edge and
    # close a 2-node cycle against the parent that lists it.
    rows = {100: [(848, "BREAK ALL SHARP EDGES, MAX:"), (983, "0.5"),
                  (1029, "USED ON"), (1315, "NAME")],
            120: [(848, "1. PART MUST BE FREE OF DIRT")],
            130: [(20, "2"), (1136, "F10110917")]}
    ys = sorted(rows)
    y_idx = ys.index(130)
    assert extraction._positional_row_is_title_block(rows, ys, y_idx, 130)
    # The word path splits the label into two tokens and must reach the same
    # verdict (F10197064: 3 note rows between label and value).
    wrows = {100: [(848, "BREAK"), (900, "ALL"), (983, "0.5"),
                   (1029, "USED"), (1049, "ON"), (1315, "NAME")],
             120: [(848, "1."), (900, "PART")],
             130: [(20, "2"), (1136, "F10119103")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wys)}
    assert extraction._word_row_is_title_block(wrows, wys, 130,
                                               sorted(wrows[130]), y_index)


def test_used_on_label_sits_left_of_its_value():
    # The label's x is its left edge while the value starts past the label
    # text (~55 pt): measured real offsets are 88-116 pt, so an association
    # window of +/-60 missed every one of them (F10205800 et al., where the
    # label sits 4 rows up behind intervening note rows).
    rows = {110: [(1029, "USED"), (1049, "ON")]}
    for y in (120, 130, 140):
        rows[y] = [(848, "NOTE")]
    rows[150] = [(1145, "F10204367")]
    ys = sorted(rows)
    y_idx = ys.index(150)
    assert extraction._positional_row_is_title_block(rows, ys, y_idx, 150)
    wrows = {110: [(1029, "USED"), (1049, "ON")]}
    for y in (120, 130, 140):
        wrows[y] = [(848, "NOTE")]
    wrows[150] = [(1145, "F10204367")]
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wys)}
    assert extraction._word_row_is_title_block(wrows, wys, 150,
                                               sorted(wrows[150]), y_index)


def test_used_on_phrase_in_description_right_of_fermi_is_not_caption():
    # A parts-list description may contain the phrase ('BRACKET USED ON
    # ASSY'). Such text trails the F-number on the row, whereas a title-block
    # label precedes its value - so the x relation separates them and the
    # row below stays a BOM row.
    rows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                  (180, "USED"), (210, "ON")],
            150: [(50, "1"), (70, "F10126107")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 1, 150)
    wrows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                   (180, "USED"), (210, "ON")],
             150: [(50, "1"), (70, "F10126107")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wrows)}
    assert not extraction._word_row_is_title_block(wrows, wys, 150,
                                                   sorted(wrows[150]), y_index)


def test_used_on_anchor_is_fermi_not_row_edge():
    # The anchor must be the F-number's x, not the row's rightmost token: a
    # genuine BOM row carries its description right of the F-number, and the
    # row edge would let a nearby 'USED ON' description suppress the part.
    rows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                  (180, "USED"), (210, "ON")],
            150: [(50, "1"), (70, "F10126107"), (130, "STEEL"),
                  (300, "WASHER"), (500, "LARGE")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 1, 150)
    wrows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                   (180, "USED"), (210, "ON")],
             150: [(50, "1"), (70, "F10126107"), (130, "STEEL"),
                   (300, "WASHER"), (500, "LARGE")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wrows)}
    assert not extraction._word_row_is_title_block(wrows, wys, 150,
                                                   sorted(wrows[150]), y_index)


def test_used_on_prose_tail_is_not_a_label():
    # 'BRACKET USED ON ASSY' is a BOM description, not a caption: the USED/ON
    # pair must not mark the row as a label when BOM prose follows it - even
    # when the candidate F-number sits to its right.
    assert extraction._used_on_label_x(
        [(130, "BRACKET"), (180, "USED"), (210, "ON"), (250, "ASSY")]) is None
    rows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                  (180, "USED"), (210, "ON"), (250, "ASSY")],
            150: [(500, "F10126107")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 1, 150)
    # ...while a merged caption cell ('USED ON | NAME') still marks it.
    assert extraction._used_on_label_x(
        [(848, "BREAK ALL SHARP EDGES, MAX:"), (983, "0.5"),
         (1029, "USED ON"), (1315, "NAME")]) == 1029


def test_word_wide_window_size_token_needs_association():
    # An A0-A9 token in another column, 4 rows up, must not suppress a
    # genuine BOM value; the horizontally associated one still marks it.
    base = {110: [(50, "SPEC")], 120: [(50, "SPEC")], 130: [(50, "SPEC")],
            150: [(50, "1"), (70, "F10126107")]}
    far = {100: [(600, "A3")]} | base
    wys = sorted(far)
    y_index = {y: i for i, y in enumerate(far)}
    assert not extraction._word_row_is_title_block(far, wys, 150,
                                                   sorted(far[150]), y_index)
    near = {100: [(60, "A3")]} | base
    nys = sorted(near)
    n_index = {y: i for i, y in enumerate(near)}
    assert extraction._word_row_is_title_block(near, nys, 150,
                                               sorted(near[150]), n_index)


def test_used_on_label_far_left_column_does_not_suppress():
    # One association rule everywhere: the label must sit within 180pt left
    # of the F-number. A USED ON caption in another column - far left in the
    # tight window, or left of the item column in the wide window - is not
    # this row's title-block cell.
    rows = {120: [(100, "USED"), (120, "ON")],
            150: [(1100, "F10126107")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 1, 150)
    wrows = {110: [(300, "USED"), (320, "ON")],
             120: [(50, "SPEC")], 130: [(50, "SPEC")], 140: [(50, "SPEC")],
             150: [(20, "2"), (1145, "F10126107")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wrows)}
    assert not extraction._word_row_is_title_block(wrows, wys, 150,
                                                   sorted(wrows[150]), y_index)
    # ...while a label just inside the span (all measured real offsets are
    # 88-116pt) still marks the row in both windows.
    near = {120: [(1000, "USED"), (1020, "ON")],
            150: [(1100, "F10126107")]}
    nys = sorted(near)
    assert extraction._positional_row_is_title_block(near, nys, 1, 150)


def test_used_on_label_right_of_fermi_does_not_suppress():
    # The label always precedes its value, so a USED ON caption right of the
    # F-number - a neighbouring column 4-8 rows above - is not this row's
    # title-block cell, in either path.
    wrows = {110: [(200, "USED"), (220, "ON")],
             120: [(50, "SPEC")], 130: [(50, "SPEC")], 140: [(50, "SPEC")],
             150: [(50, "1"), (70, "F10126107")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wrows)}
    assert not extraction._word_row_is_title_block(wrows, wys, 150,
                                                   sorted(wrows[150]), y_index)
    rows = {110: [(200, "USED"), (220, "ON")],
            120: [(50, "SPEC")], 130: [(50, "SPEC")], 140: [(50, "SPEC")],
            150: [(50, "1"), (70, "F10126107")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 4, 150)


def test_used_on_label_on_candidate_row_marks_title_block():
    # A merged USED ON label on the candidate row itself ('2 | USED ON |
    # F10119...') is a title-block value row: neither path used to check the
    # row being evaluated, so an item token left of the value let it pass
    # through as a BOM entry.
    rows = {150: [(20, "2"), (1029, "USED"), (1049, "ON"), (1145, "F10126107")]}
    ys = sorted(rows)
    assert extraction._positional_row_is_title_block(rows, ys, 0, 150)
    wrows = {150: [(20, "2"), (1029, "USED"), (1049, "ON"), (1145, "F10126107")]}
    wys = sorted(wrows)
    y_index = {y: i for i, y in enumerate(wrows)}
    assert extraction._word_row_is_title_block(wrows, wys, 150,
                                               sorted(wrows[150]), y_index)
    # ...while a BOM row carrying the phrase after its F-number stays kept.
    kept = {150: [(50, "1"), (70, "F10126107"), (180, "USED"), (210, "ON")]}
    kys = sorted(kept)
    assert not extraction._positional_row_is_title_block(kept, kys, 0, 150)


def test_caption_row_recognizes_keywords_and_values():
    for text in ("SCALE 1:1", "SHEET 1 OF 2", "USED ON F10126107", "REV",
                 "REV B", "SIZE A0", "DRAWING NUMBER F10126107",
                 "NEXT ASSY F10126107", "  sheet  1  of  2 "):
        assert extraction._is_caption_row(text), text


def test_caption_row_rejects_bom_text():
    for text in ("SHEET METAL BRACKET", "BRACKET USED ON ASSY",
                 "NAME PLATE", "SCALE BAR", "1 F10126107 BRACKET",
                 "ITEM 1 F10126107", "ITEM F10126107"):
        assert not extraction._is_caption_row(text), text


class _CaptionPage:
    def __init__(self, words):
        self._words = words

    def get_text(self, kind="text"):
        assert kind == "words"
        return self._words


def _cword(x0, y0, text):
    return (x0, y0, x0 + 8, y0 + 8, text, 0, 0, 0)


def test_positional_caption_with_value_skips_row_below():
    # 'SCALE 1:1' is a caption even though it is not a bare keyword; the row
    # right below it carries an item number and must still be skipped, while
    # the next row (out of caption reach) is kept.
    page = _CaptionPage([
        _cword(50, 100, "SCALE"), _cword(90, 100, "1:1"),
        _cword(50, 110, "1"), _cword(70, 110, "F10126107"),
        _cword(50, 140, "2"), _cword(70, 140, "F10126108"),
    ])
    found = extraction._extract_bom_positional(page, 1)
    assert [v for v, _, _ in found] == ["F10126108"]


def test_positional_row_with_used_on_words_is_kept():
    # A BOM description may mention USED ON; the row itself is not a caption,
    # so its F-number is kept.
    page = _CaptionPage([
        _cword(50, 100, "1"), _cword(70, 100, "F10126107"),
        _cword(130, 100, "BRACKET"), _cword(180, 100, "USED"),
        _cword(210, 100, "ON"),
        _cword(50, 140, "2"), _cword(70, 140, "F10126108"),
    ])
    found = extraction._extract_bom_positional(page, 1)
    assert sorted(v for v, _, _ in found) == ["F10126107", "F10126108"]


def test_positional_item_row_with_fermi_number_kept():
    # A row 'ITEM 1 F10126107' is a BOM row (item label, item number, part),
    # not a caption: the child must be emitted, not skipped.
    page = _CaptionPage([
        _cword(40, 100, "ITEM"), _cword(70, 100, "1"),
        _cword(90, 100, "F10126107"),
        _cword(70, 140, "2"), _cword(90, 140, "F10126108"),
    ])
    found = extraction._extract_bom_positional(page, 1)
    assert sorted(v for v, _, _ in found) == ["F10126107", "F10126108"]


def test_fermi_line_title_block_used_on_caption_only():
    # A USED ON caption two lines above marks the line as title block...
    assert extraction._fermi_line_is_title_block(
        ["USED ON", "SPEC", "F10126107"], 2)
    # ...but a line merely containing the phrase is BOM text.
    assert not extraction._fermi_line_is_title_block(
        ["BRACKET USED ON ASSY", "SPEC", "F10126107"], 2)


def test_line_bom_stacked_used_on_values_all_title_block():
    # Every value in a USED ON stack is title block, not just the values two
    # lines below the label: the third+ values used to become BOM children.
    lines = ["USED ON", "F10200001", "F10200002", "F10200003"]
    assert extraction._line_bom_entries(lines, 1) == []
    # A bare F-number run away from a USED ON label still parses.
    lines = ["FERMI PART LIST", "F10200001", "F10200002"]
    assert [e[0] for e in extraction._line_bom_entries(lines, 1)] == \
        ["F10200001", "F10200002"]


def test_bad_desc_rejects_labels_not_word_prefixes():
    for text in ("REV A BRACKET", "PART NUMBER", "PARTS LIST", "NOTES:",
                 "FIGURE 3", "TABLE 1", "USED ON", "ITEM 1 BRACKET",
                 "UNLESS OTHERWISE SPECIFIED", "ASSEMBLY", "MATERIALS:",
                 "SUBASSEMBLY DETAIL", "UNITED STATES DEPARTMENT OF ENERGY"):
        assert extraction.BAD_DESC_RE.match(text), text
    for text in ("REVERSE SHAFT", "PARTITION PLATE", "TABLETOP BRACKET",
                 "VIEWPORT COVER", "CODER WHEEL", "PARTICLE BOARD",
                 "ENERGY ABSORBER"):
        assert not extraction.BAD_DESC_RE.match(text), text


def test_table_rows_take_whole_fermi_token():
    # A cell can stack the value and its quantity ('F10112345\n2' used to
    # normalize to the wrong number), carry an item number before the value
    # ('1 F10126108', dropped), or split the value with a space
    # ('F101 26145'): the whole FERMI token wins, split digits re-join.
    data = [["ITEM", "FERMI NO.", "PART NAME"],
            ["1", "F10126107", "BRACKET"],
            ["2", "F10112345\n2", "SHIM"],
            ["3", "1 F10126108", "WASHER"],
            ["4", "F101 26145", "GASKET"]]
    entries = extraction._table_rows_to_entries(data, 0, 1, 1)
    assert [e[0] for e in entries] == \
        ["F10126107", "F10112345", "F10126108", "F10126145"]


def test_drawing_name_skips_parts_list_header_pair(tmp_path, make_pdf):
    # 'PART' above 'NAME' is a parts-list column header; the cell below it is
    # not the drawing name.
    pdf = make_pdf(tmp_path / "F10126106.pdf", [
        "FERMI PART LIST",
        "ITEM", "PART", "NAME", "1",
        "BRACKET",
        "NAME", "ACTUAL BRACKET NAME"])
    doc = fitz.open(pdf)
    assert extraction.extract_drawing_name(doc) == "ACTUAL BRACKET NAME"
    doc.close()


def test_positional_row_own_used_on_text_kept():
    # The row's own text mentioning USED ON must not mark it as title block.
    rows = {100: [(50, "1"), (70, "F10126107"), (130, "BRACKET"),
                  (180, "USED"), (210, "ON")],
            140: [(50, "2"), (70, "F10126108")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 0, 100)


def test_positional_nearby_description_with_used_on_not_caption():
    # A nearby BOM row containing the phrase is not a USED ON caption.
    rows = {120: [(50, "3"), (70, "F10126109"), (130, "BRACKET"),
                  (180, "USED"), (210, "ON")],
            140: [(50, "2"), (70, "F10126108")],
            150: [(50, "1"), (70, "F10126107")]}
    ys = sorted(rows)
    assert not extraction._positional_row_is_title_block(rows, ys, 2, 150)


def test_positional_used_on_caption_still_marks():
    # The genuine caption above a row still marks it as title block.
    rows = {120: [(50, "USED"), (74, "ON")],
            150: [(50, "1"), (70, "F10126107")]}
    ys = sorted(rows)
    assert extraction._positional_row_is_title_block(rows, ys, 1, 150)


def test_ocr_strip_tokens_tempfile_security_and_cleanup(monkeypatch, tmp_path, make_pdf):
    import subprocess as sp

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]

    created_tempfiles = []
    orig_named_temp = extraction.tempfile.NamedTemporaryFile

    def tracking_named_temp(*args, **kwargs):
        tf = orig_named_temp(*args, **kwargs)
        created_tempfiles.append(tf.name)
        return tf

    monkeypatch.setattr(extraction.tempfile, "NamedTemporaryFile", tracking_named_temp)

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, 0, stdout="F10126107", stderr="")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    out = extraction._ocr_strip_tokens(page, fitz.Rect(72, 72, 220, 95), psm="7")

    assert out == ["F10126107"]
    assert len(created_tempfiles) > 0
    for tmp_file in created_tempfiles:
        assert os.path.basename(tmp_file).startswith("fermipdf_ocr_")
        assert tmp_file.endswith(".png")
        assert not os.path.exists(tmp_file), f"Temporary file {tmp_file} was not cleaned up!"
    doc.close()


def test_ocr_words_zoom_tempfile_security_and_cleanup(monkeypatch, tmp_path, make_pdf):
    import subprocess as sp

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]

    created_tempfiles = []
    orig_named_temp = extraction.tempfile.NamedTemporaryFile

    def tracking_named_temp(*args, **kwargs):
        tf = orig_named_temp(*args, **kwargs)
        created_tempfiles.append(tf.name)
        return tf

    monkeypatch.setattr(extraction.tempfile, "NamedTemporaryFile", tracking_named_temp)

    tsv_output = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t10\t10\t50\t20\t95\tNUMBER\n"
    )

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, 0, stdout=tsv_output, stderr="")

    monkeypatch.setattr(extraction.subprocess, "run", fake_run)
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    clip = fitz.Rect(0, 0, 100, 100)
    words = extraction._ocr_words_zoom(page, clip, zoom=8, psm="11")

    assert len(words) == 1
    assert words[0][4] == "NUMBER"
    assert len(created_tempfiles) == 1
    tmp_file = created_tempfiles[0]
    assert os.path.basename(tmp_file).startswith("fermipdf_ocr_")
    assert tmp_file.endswith(".png")
    assert not os.path.exists(tmp_file), f"Temporary file {tmp_file} was not cleaned up!"
    doc.close()


def test_ocr_strip_tokens_cleanup_on_pix_save_failure(monkeypatch, tmp_path, make_pdf):
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["NAME", "Part"])
    doc = fitz.open(pdf)
    page = doc[0]

    created_tempfiles = []
    orig_named_temp = extraction.tempfile.NamedTemporaryFile

    def tracking_named_temp(*args, **kwargs):
        tf = orig_named_temp(*args, **kwargs)
        created_tempfiles.append(tf.name)
        return tf

    monkeypatch.setattr(extraction.tempfile, "NamedTemporaryFile", tracking_named_temp)

    class FakePix:
        def save(self, filename):
            raise RuntimeError("pix.save failed")

    monkeypatch.setattr(page, "get_pixmap", lambda *a, **k: FakePix())
    monkeypatch.setattr(extraction.OCR, "tesseract_path", "tesseract-fake")

    with pytest.raises(RuntimeError, match="pix.save failed"):
        extraction._ocr_strip_tokens(page, fitz.Rect(72, 72, 220, 95), psm="7")

    assert len(created_tempfiles) == 1
    tmp_file = created_tempfiles[0]
    assert not os.path.exists(tmp_file), f"Temporary file {tmp_file} was not cleaned up on pix.save failure!"
    doc.close()


def test_words_cached_normalises_ocr_words_to_iso_points(monkeypatch, tmp_path,
                                                        make_pdf):
    # OCR runs at a render dpi that has nothing to do with page points, so the
    # word boxes are divided by the page's ISO scale before any label-relative
    # window looks at them. Nothing tested this: every OCR test stubs
    # words_cached itself, so an inverted scale stayed green.
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["x"])
    doc = fitz.open(pdf)
    page = doc[0]
    monkeypatch.setattr(extraction.OCR, "enabled", True)
    monkeypatch.setattr(extraction.OCR, "available", True)
    monkeypatch.setattr(extraction, "iso_page_scale", lambda w, h: 2.0)
    monkeypatch.setattr(extraction, "_ocr_render_dpi", lambda p: 100)
    monkeypatch.setattr(extraction.pymupdf.Page, "get_textpage_ocr",
                        lambda self, **kw: object())
    monkeypatch.setattr(extraction.pymupdf.Page, "get_text",
                        lambda self, *a, **k: [
                            (100.0, 200.0, 140.0, 220.0, "HELLO", 0, 0, 0)])

    words = extraction.OCR.words_cached(page)

    assert words[0][:4] == (50.0, 100.0, 70.0, 110.0)
    assert words[0][4] == "HELLO"
    assert extraction.OCR.page_scale[extraction.OCR.doc_key(page)] == 2.0
    doc.close()


def test_words_cached_timeout_is_recorded_not_raised(monkeypatch, tmp_path,
                                                     make_pdf):
    # A hung tesseract must cost one page, not the run: the worker is abandoned,
    # the timeout is reported, and the empty result is memoized so the next
    # caller does not wait again.
    import threading

    pdf = make_pdf(tmp_path / "F10126106.pdf", ["x"])
    doc = fitz.open(pdf)
    page = doc[0]
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(extraction, "OCR_INPROCESS_TIMEOUT_SEC", 0.01)

    def blocked(self, **kw):
        started.set()
        release.wait(5)
        return None

    monkeypatch.setattr(extraction.pymupdf.Page, "get_textpage_ocr", blocked)
    issues = []
    try:
        words = extraction.OCR.words_cached(page, issues, "page 1")
        assert words == []
        assert issues == ["page 1: OCR timed out after 0.01s "
                          "(tesseract process may still finish in background)"]
        key = extraction.OCR.doc_key(page)
        assert extraction.OCR.words_memo[key] == []   # memoized, not retried
    finally:
        # Let the abandoned worker finish before teardown restores the real
        # engine method (it must not call it after the document is closed).
        # Thread.start() returns before run() is entered, so wait for the fake.
        assert started.wait(5)
        release.set()
        for thread in threading.enumerate():
            if thread.name == "ocr-timeout":
                thread.join(timeout=5)
    doc.close()


def test_words_cached_engine_error_is_recorded_as_an_issue(monkeypatch, tmp_path,
                                                           make_pdf):
    # pymupdf surfaces mupdf failures as RuntimeError; they must reach the run's
    # issue list instead of escaping (or silently vanishing).
    pdf = make_pdf(tmp_path / "F10126106.pdf", ["x"])
    doc = fitz.open(pdf)
    page = doc[0]

    def boom(self, **kw):
        raise RuntimeError("engine gone")

    monkeypatch.setattr(extraction.pymupdf.Page, "get_textpage_ocr", boom)
    issues = []

    assert extraction.OCR.words_cached(page, issues, "page 1") == []
    assert issues == ["page 1: OCR failed: engine gone"]
    doc.close()
