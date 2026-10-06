"""Shared constants and regexes for the Fermi PDF organizer."""
import re

FERMI_ANY_RE = re.compile(r"^F[0-9A-Za-z]")
FERMI_VAL_RE = re.compile(r"^F(?!C)[0-9][0-9A-Za-z]*$")


def is_fermi_value(val):
    return bool(val) and bool(FERMI_VAL_RE.match(val))


def is_processable_ref(val):
    """True if val is a FERMI part reference the graph should process.

    FC-prefixed common components are tracked separately (skipped_fc) and
    never become graph edges, so they are filtered out here.
    """
    return bool(val) and is_fermi_value(val) and not val.startswith("FC")


# Shared regex patterns
FERMI_RE = re.compile(r"^(F\d{1,8}[A-Za-z]?|FC\d{1,8}[A-Za-z]?)$")
FERMI_RE_SEARCH = re.compile(r"\bF\d{1,8}[A-Za-z]?\b|\bFC\d{1,8}[A-Za-z]?\b")
FERMI_HEADER_RE = re.compile(r"^FERMI(?:PART)?(?:#|NO\.?|NUM(?:BER)?)\.?$", re.IGNORECASE)
# Canonical part-stem filter: F (+ optional C for common components) + 5-8
# digits, then only [A-Z0-9_] to the end. Hyphens are never part of a
# canonical stem. Accepts real stems like F10038961___DWG1 and
# F10126513___DWG1_XML2347; rejects copy-suffix variants ('- COPY', ' (1)').
PART_STEM_RE = re.compile(r"^F(?:C)?\d{5,8}(?![0-9])[A-Z0-9_]*$")

# Raw filename scanner: finds the part token in real-world drawing names.
# Tolerates numeric prefixes ('2.20.3.9 F10196150--CHK'), hyphen separators
# ('F10187622-B-CHK', 'F10052259---DWG1') and trailing title text
# ('F10137913 KIT, ...' -> 'F10137913'); stops at spaces/other characters.
PART_NAME_RE = re.compile(r"(?<![A-Z0-9])F(?:C)?\d{5,8}(?![0-9])[A-Z0-9_-]*",
                          re.IGNORECASE)


def canonical_stem(name):
    """Canonical uppercase stem for a PDF filename (no extension), or None.

    Hyphen separators become underscores ('F10187622-B-CHK' ->
    'F10187622_B_CHK') so hyphen/underscore copies of one drawing share a
    stem. Returns None for non-part documents (specs, annexures, ...).
    """
    m = PART_NAME_RE.search(name or "")
    if not m:
        return None
    stem = m.group(0).upper().replace("-", "_")
    return stem if PART_STEM_RE.match(stem) else None


def filename_title(name):
    """Title text after the part token in a filename, or None.

    'F10137913 KIT, MAGNETIC SHIELD COUPLER' -> 'KIT, MAGNETIC SHIELD COUPLER'.
    Names whose tail is only separators ('F10187624---DWG1') -> None.
    """
    m = PART_NAME_RE.search(name or "")
    if not m:
        return None
    tail = (name[m.end():] or "").strip(" -_.")
    return tail or None


# ---------------------------------------------------------------------------
# Title-block check: the drawing number and revision printed in the drawing's
# own title block (authoritative) are compared against the filename. The value
# word sits just below its label; the bottom-most label is the title block
# (the revision-history RCD block sits higher on the sheet).
# ---------------------------------------------------------------------------
TB_REV_VAL_RE = re.compile(r"^[A-Z]$|^-$")
TB_NUM_VAL_RE = re.compile(r"^F(?:C)?\d{5,8}$")
TB_REV_DX_LEFT, TB_REV_DX_RIGHT, TB_REV_DY_BOTTOM = -10, 50, 14
TB_NUM_DX_LEFT, TB_NUM_DX_RIGHT, TB_NUM_DY_BOTTOM = -40, 170, 14
# OCR word boxes can be offset by a row: search a symmetric band around the
# label instead of only below it.
TB_OCR_DY_TOP, TB_OCR_DY_BOTTOM = 40, 40
# Single safety limit for all path-length guards (Windows MAX_PATH = 260, margin below)
MAX_PATH = 250
# Windows mkdir limit on the directory path only (~248; keep margin)
MAX_DIR = 240
# Recursion depth guard for the cycle-search (graph) and placement (fsops) DFS
TREE_MAX_DEPTH = 500
# Bound for the folder-name shortening loop (naming.build_folder_names)
NAME_SHORTEN_MAX_PASSES = 200

# ---------------------------------------------------------------------------
# OCR support for scanned (image-only) PDFs.  All geometry is derived from
# OCR'd label anchors ("ITEM", "FERMI #", "USED ON", "NAME") relative to the
# page, never from absolute coordinates, so it works on any paper size.
# OCR only runs on pages whose text layer is shorter than OCR_MIN_CHARS.
# ---------------------------------------------------------------------------
OCR_MIN_CHARS = 50
OCR_MAIN_DPI = 300
# Pixel cap for the full-page OCR render. Sheets are A0-A4, but scans are
# stored at the scanner's dpi (1 px = 1 pt), so an A0 scan at 153 dpi is a
# 7152x5051 pt page: at 300 dpi that is 627 MP and PyMuPDF's OCR fails with
# "Overly large image" (silently yielding zero words). 300 MP keeps 300 dpi
# for A4-A1 and lands the biggest A0 scans near 207 dpi, which OCRs well.
OCR_MAX_RENDER_MP = 300
OCR_STRIP_DPI = 6   # zoom factor for the parts-list row re-OCR (432 dpi)
OCR_VAL_RE = re.compile(r"^F(C?)\d{5,8}$")
OCR_CORR = str.maketrans("OLISB|", "011581")

# OCR geometry (points). Every window/tolerance is anchored to an OCR'd label
# ("USED ON", "NAME", "FERMI #", "ITEM") or to the row's own bbox - never to
# absolute page coordinates - so any paper size works.
OCR_ROW_TOL = 6              # same visual row: max y drift
OCR_USED_ON_GAP_MAX = 40     # "USED" -> "ON" label: max x gap
OCR_USED_ON_DX_LEFT = 30     # value window around the USED ON label
OCR_USED_ON_DY_TOP = 3
OCR_USED_ON_DX_RIGHT = 340
OCR_USED_ON_DY_BOTTOM = 80
OCR_NAME_DX_LEFT = 25        # value window around the NAME label
OCR_NAME_DY_TOP = 2
OCR_NAME_DX_RIGHT = 360
OCR_NAME_DY_BOTTOM = 30
OCR_NAME_LINE_TOL = 9        # NAME is a single visual line: max y drift
OCR_FERMI_SCAN_DX_LEFT = 15  # FERMI# header column scan: x window around label
OCR_FERMI_SCAN_DX_RIGHT = 90
OCR_HDR_ROW_TOL = 5          # FERMI# header vs ITEM label: same-row y tolerance
OCR_ITEM_FERMI_GAP_MAX = 90  # ITEM label -> FERMI# header: max x gap
OCR_HDR_ROW_GAP = 4          # rows above the FERMI# header still belong to the list
OCR_FERMI_COL_DX_LEFT = 18   # parts-list FERMI column x tolerance
OCR_FERMI_COL_DX_RIGHT = 135
OCR_ITEM_COL_DX_LEFT = 35    # parts-list ITEM column x tolerance
OCR_ITEM_FERMI_MIN_GAP = 10  # ITEM candidate must sit this far left of FERMI#
OCR_ITEM_X_FALLBACK = 60     # item-column x fallback when no ITEM label is found
OCR_ROW_STRIP_DY_TOP = 3     # parts-list row strip: y margins around the row
OCR_ROW_STRIP_DY_BOTTOM = 20

# ---------------------------------------------------------------------------
# Watermark detection. Text phrases that mark a watermark (word-bounded so the
# title-block 'Drafting' label and 'REFERENCE ONLY' drawing notes don't match).
# Visual signatures (transparency, diagonal/light large text, watermark layers)
# are checked in extraction.detect_watermark.
# ---------------------------------------------------------------------------
WATERMARK_TEXT_RE = re.compile(
    r"\b(?:UNCONTROLLED(?:\s+COPY)?|NOT\s+FOR\s+CONSTRUCTION|PRELIMINARY|"
    r"PRE-?RELEASED|WORKING\s+COPY|DRAFT\s+COPY|FOR\s+REVIEW\s+ONLY|"
    r"DO\s+NOT\s+COPY|DRAFT|VOID|VOIDED|SUPERSEDED|CANCELLED)\b",
    re.IGNORECASE,
)
WATERMARK_MAX_PAGES = 3          # pages scanned per PDF for watermark evidence
WATERMARK_TRANSPARENT_OPACITY = 0.99
WATERMARK_TRANSPARENT_MIN_SIZE = 8
WATERMARK_DIAGONAL_MIN_SIZE = 24  # small diagonal dimension labels don't count
WATERMARK_LIGHT_MIN_SIZE = 40
WATERMARK_LIGHT_MIN_CHANNEL = 0.6

ITEM_RE = re.compile(r"^\d{1,3}$")
BAD_DESC_RE = re.compile(
    r"^(?:NOTE|REV|PAGE|FIG|TABLE|SECTION|DETAIL|VIEW|SCALE|DRAWN|CHECKED|"
    r"APPROVED|DATE|DWG|TITLE|NUMBER|QUANTITY|ITEM|PART|MATERIAL|DESCRIPTION|"
    r"ASSEMBLY|SUB-?ASSEMBLY|NEXT\s+ASSY|USED\s+ON|FERMI|PARTS?|LIST|BOM|"
    r"REVISION|CONTROL|DOCUMENT|SIGNATURES?|TEMPLATE|UNLESS|"
    r"OTHERWISE|SPECIFIED|PROJECT|CATEGORY|GROUP|CAGE|CODE|UNITED|STATES|"
    r"DEPARTMENT|ENERGY|NATIONAL|ACCELERATOR|LABORATORY|FERMILAB)",
    re.IGNORECASE
)
SIZE_RE = re.compile(r"^A\d$", re.IGNORECASE)
SINGLE_LINE_RE = re.compile(
    r"^(F\d{1,8}[A-Za-z]?|FC\d{1,8}[A-Za-z]?)\s+"
    r"([A-Za-z][A-Za-z0-9\s\-_/\\.]{3,})"
)
TOKEN_RE = re.compile(r"\b(F\d{1,8}[A-Za-z]?|FC\d{1,8}[A-Za-z]?)(?![\dA-Za-z])")
USED_RE = re.compile(r"^USED\s*ON\s*$", re.IGNORECASE)
STOP_RE = re.compile(
    r"^(?:MATERIAL|DESCRIPTION|PROJECT|CATEGORY|GROUP|DRAWING|SHEET|REV|"
    r"SCALE|SIZE|DRAWN|CHECKED|APPROVED|DATE|NAME|TITLE|NUMBER|NOTES?|"
    r"ITEM|PARTS?|FERMI|PARTS?\s+LIST|NEXT\s+ASSY)",
    re.IGNORECASE,
)
TITLE_BLOCK_KEYWORDS = {
    "USED ON", "MATERIAL", "SIZE", "SCALE", "DRAWING NUMBER",
    "SHEET", "REV", "DRAWN", "CHECKED", "APPROVED", "DATE",
    "NAME", "TITLE", "NUMBER", "NOTES", "UNLESS", "OTHERWISE",
    "SPECIFIED", "PROJECT", "CATEGORY", "GROUP", "CAGE", "CODE",
    "UNITED", "STATES", "DEPARTMENT", "ENERGY", "NATIONAL",
    "ACCELERATOR", "LABORATORY", "FERMILAB", "TEMPLATE", "VERSION",
    "DESCRIPTION", "PARTS LIST", "BOM", "REVISION", "CONTROL",
    "DOCUMENT", "SIGNATURES", "ITEM", "PARTS", "FERMI",
    "NEXT ASSY", "DRAWING",
}
