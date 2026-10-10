#!/usr/bin/env python3
"""Content extraction: BOM tables/text, USED ON, NAME, OCR fallback."""
import multiprocessing
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

# Import pymupdf, never the legacy `fitz` shim: the shim prints its own
# deprecation notice via pymupdf's captured stdout handle, so the print
# leaks whenever pymupdf was imported before this module (the old
# redirect_stdout workaround only worked in the lucky import order).
import pymupdf

if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()

from .config import (canonical_stem,
                     TB_REV_VAL_RE, TB_NUM_VAL_RE,
                     TB_REV_DX_LEFT, TB_REV_DX_RIGHT, TB_REV_DY_BOTTOM,
                     TB_NUM_DX_LEFT, TB_NUM_DX_RIGHT, TB_NUM_DY_BOTTOM,
                     TB_OCR_DY_TOP, TB_OCR_DY_BOTTOM,
                     FERMI_RE, FERMI_RE_SEARCH,
                     FERMI_HEADER_RE, OCR_MIN_CHARS, OCR_MAIN_DPI, OCR_STRIP_DPI,
                     OCR_VAL_RE, OCR_CORR, ITEM_RE, BAD_DESC_RE, SIZE_RE,
                     SINGLE_LINE_RE, TOKEN_RE, USED_RE, STOP_RE,
                     PARTS_HEADER_RE, PARTS_HEADER_COMBO_RE,
                     TITLE_BLOCK_KEYWORDS, WATERMARK_TEXT_RE, WATERMARK_MAX_PAGES,
                     WATERMARK_TRANSPARENT_OPACITY, WATERMARK_TRANSPARENT_MIN_SIZE,
                     WATERMARK_DIAGONAL_MIN_SIZE, WATERMARK_LIGHT_MIN_SIZE,
                     WATERMARK_LIGHT_MIN_CHANNEL,
                     OCR_ROW_TOL, OCR_USED_ON_GAP_MAX,
                     OCR_USED_ON_DX_LEFT, OCR_USED_ON_DY_TOP,
                     OCR_USED_ON_DX_RIGHT, OCR_USED_ON_DY_BOTTOM,
                     OCR_NAME_DX_LEFT, OCR_NAME_DY_TOP, OCR_NAME_DX_RIGHT,
                     OCR_NAME_DY_BOTTOM, OCR_NAME_LINE_TOL,
                     OCR_FERMI_SCAN_DX_LEFT, OCR_FERMI_SCAN_DX_RIGHT,
                     OCR_HDR_ROW_TOL, OCR_ITEM_FERMI_GAP_MAX, OCR_HDR_ROW_GAP,
                     OCR_FERMI_COL_DX_LEFT, OCR_FERMI_COL_DX_RIGHT,
                      OCR_ITEM_COL_DX_LEFT, OCR_ITEM_FERMI_MIN_GAP,
                      OCR_ITEM_X_FALLBACK, OCR_ROW_STRIP_DY_TOP,
                      OCR_ROW_STRIP_DY_BOTTOM, OCR_MAX_RENDER_MP)

# Resource-exhaustion guards + OCR memo bound.
# OCR_MIN_CHARS itself lives in config.py; these thresholds sit here so
# extraction.py stays the only touched module. Thresholds are best-judgment
# values ([uncertain] per fix-plan: 500MB / 200 pages / 512 memo entries).
MAX_PDF_BYTES = 500 * 1024 * 1024  # skip-with-issue files over ~500MB
MAX_PDF_PAGES = 200  # skip-with-issue documents beyond 200 pages
_OCR_MEMO_MAX_ENTRIES = 512  # oldest-first cap per memo dict (never events)


def _memo_put(memo, key, value):
    """Store + oldest-first evict so run-scoped OCR memos stay bounded."""
    memo[key] = value
    while len(memo) > _OCR_MEMO_MAX_ENTRIES:
        memo.pop(next(iter(memo)))
    return value


# ISO A-series landscape page sizes in points (72 dpi). A sheet is stored
# either at its ISO size or as the same sheet at the scanner's dpi (1 px =
# 1 pt), so the page scale tells us how many points per ISO point.
_ISO_PAGE_PTS = ((3370, 2384), (2384, 1684), (1684, 1191), (1191, 842),
                 (842, 595))


def iso_page_scale(width, height):
    """Page points per ISO A-size point (1.0 for a sheet stored at 72 dpi)."""
    w, h = max(width, height), min(width, height)
    if w <= 0 or h <= 0:
        return 1.0
    best = min(_ISO_PAGE_PTS, key=lambda s: abs(w / s[0] - 1.0))
    return w / best[0]


def _ocr_render_dpi(page):
    """Full-page OCR render dpi, capped so huge sheets stay OCR-able."""
    w, h = page.rect.width, page.rect.height
    if w <= 0 or h <= 0:
        return OCR_MAIN_DPI
    dpi = (OCR_MAX_RENDER_MP * 1e6 * 72 * 72 / (w * h)) ** 0.5
    return min(OCR_MAIN_DPI, max(72, int(dpi)))


# OCR runtime state (run-scoped context: availability + memos + events)
class _OcrContext:
    """Run-scoped OCR runtime state shared by the serial and parallel paths."""

    def __init__(self):
        self.enabled = True
        self.available = False
        self.reason = None
        self.tesseract_path = None
        self.install_dir = None
        self._exported_dir = None
        self.version = None
        self.events = {}
        self.page_memo = {}
        self.words_memo = {}
        self.page_scale = {}
        self._doc_seq = 0

    def reset(self, enabled=True):
        """Start a fresh run: clear events/memos/availability and set enabled."""
        self.enabled = bool(enabled)
        self.available = False
        self.reason = None
        self.tesseract_path = None
        self.install_dir = None
        self.version = None
        self.events = {}
        self.page_memo = {}
        self.words_memo = {}
        self.page_scale = {}
        self._doc_seq = 0

    def _export_for_pymupdf(self, install_dir):
        """Export a common-dir Tesseract install to the process environment.

        This global mutation exists for exactly one consumer: PyMuPDF's
        in-process OCR (`get_textpage_ocr`) resolves the tesseract binary and
        TESSDATA_PREFIX from the process environment at OCR time, with no API
        to pass them explicitly. Our own CLI invocations do NOT rely on it —
        they get an explicit env via subprocess_env(). The PATH prepend is
        idempotent: repeated detection/reset cycles must not grow PATH with
        the same directory again.
        """
        exported = getattr(self, "_exported_dir", None)
        if exported and exported != install_dir:
            # Install switched mid-process (e.g. TESSERACT_EXE changed between
            # runs): drop the stale prefix, or later OCR would resolve the
            # old tessdata through the leftover PATH/TESSDATA_PREFIX.
            parts = [p for p in os.environ.get("PATH", "").split(os.pathsep)
                     if p != exported]
            os.environ["PATH"] = os.pathsep.join(parts)
            if os.environ.get("TESSDATA_PREFIX") == os.path.join(
                    exported, "tessdata"):
                del os.environ["TESSDATA_PREFIX"]
        self._exported_dir = install_dir
        if install_dir not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = (install_dir + os.pathsep
                                  + os.environ.get("PATH", ""))
        os.environ.setdefault("TESSDATA_PREFIX",
                              os.path.join(install_dir, "tessdata"))

    def subprocess_env(self):
        """Explicit env for OUR tesseract child processes (never the global).

        The exe is always invoked by absolute path, so only TESSDATA_PREFIX
        can matter; when detection found no common-dir install there is
        nothing to add and None inherits the process env unchanged.
        """
        if not self.install_dir:
            return None
        env = dict(os.environ)
        env.setdefault("TESSDATA_PREFIX",
                       os.path.join(self.install_dir, "tessdata"))
        return env

    def ensure_tesseract(self):
        """Locate Tesseract once (PATH, TESSERACT_EXE, common Windows dirs)."""
        if self.tesseract_path is not None:
            return self.tesseract_path if self.available else None
        exe = os.environ.get("TESSERACT_EXE")
        if exe:
            # [SECURITY] TESSERACT_EXE is an operator-controlled env var; the exe
            # is canonicalized via realpath + validated via isfile below, invoked
            # list-form with shell=False. No allowlist is feasible (any installed
            # Tesseract version must work), so safe-wrapper exemption E2 is
            # consciously accepted.
            exe = os.path.realpath(exe)
        if not exe or not os.path.isfile(exe):
            exe = shutil.which("tesseract")
        if not exe:
            cand_dirs = [
                r"C:\Program Files\Tesseract-OCR",
                r"C:\Program Files (x86)\Tesseract-OCR",
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Tesseract-OCR"),
            ]
            for d in cand_dirs:
                if d and os.path.isfile(os.path.join(d, "tesseract.exe")):
                    exe = os.path.join(d, "tesseract.exe")
                    self.install_dir = d
                    self._export_for_pymupdf(d)
                    break
        if not exe or not os.path.isfile(exe):
            if self.reason is None:
                self.reason = "tesseract not found"
            return None
        try:
            ver = subprocess.run([exe, "--version"], capture_output=True,
                                 timeout=20, env=self.subprocess_env())
        except (OSError, subprocess.SubprocessError) as e:
            self.reason = f"tesseract not runnable ({exe}): {e}"
            return None
        if ver.returncode != 0:
            # A non-zero --version means the install is broken (missing/invalid
            # tessdata, engine init failure); treating it as available would
            # silently degrade every later OCR pass to "blank strip".
            detail = ((ver.stderr or b"") + (ver.stdout or b""))
            detail = detail.decode("utf-8", "replace").strip()[:160]
            self.reason = (f"tesseract not runnable ({exe}): --version "
                           f"exit {ver.returncode}: {detail}")
            return None
        self.tesseract_path = exe
        self.available = True
        # First X.Y.Z token in the --version output; None when it cannot be
        # parsed (the CLI then skips the version note/warning).
        m = re.search(rb"(\d+)\.(\d+)\.(\d+)", (ver.stdout or b"") + (ver.stderr or b""))
        self.version = m.group(0).decode() if m else None
        return exe

    def doc_key(self, page):
        parent = getattr(page, "parent", None)
        name = str(getattr(parent, "name", "") or getattr(parent, "metadata", {}).get("name", "") or "")
        if not name:
            # In-memory docs can share the empty-name fallback; give each a
            # per-run sequence number so memos never collide across documents.
            self._doc_seq += 1
            name = f"doc-{self._doc_seq}"
        return os.path.normcase(name), page.number

    def doc_scale(self, page):
        """Page points per ISO A-size point for this document's page."""
        key = self.doc_key(page)
        if key in self.page_scale:
            return self.page_scale[key]
        return iso_page_scale(page.rect.width, page.rect.height)

    def words_cached(self, page, issues=None, page_label=""):
        key = self.doc_key(page)
        if key in self.words_memo:
            return self.words_memo[key]
        words = []
        try:
            scale = iso_page_scale(page.rect.width, page.rect.height)
            tp = page.get_textpage_ocr(dpi=_ocr_render_dpi(page), full=True)
            words = page.get_text("words", textpage=tp)
            if scale != 1.0:
                # Normalize to ISO page points so the label-relative windows
                # (tuned for 72 dpi pages) also fit scans stored at scanner dpi.
                words = [(w[0] / scale, w[1] / scale, w[2] / scale,
                          w[3] / scale) + tuple(w[4:]) for w in words]
            _memo_put(self.page_scale, key, scale)
        except (RuntimeError, OSError, ValueError) as e:
            # RuntimeError covers pymupdf's mupdf errors (FileDataError /
            # EmptyFileError subclass RuntimeError; no generic pymupdf.Error
            # exists in 1.28.2).
            if issues is not None:
                issues.append(f"{page_label}: OCR failed: {e}")
        _memo_put(self.words_memo, key, words)
        return words

    def analyze_page(self, page, issues=None, page_label=""):
        """One OCR pass per image-only page: title fields + parts-list tokens (memoized).

        Every page routed here is image-only (scanned); it is recorded in
        `events` even when OCR is unavailable, so the workbook can list scanned
        drawings for --no-ocr runs too.
        """
        key = self.doc_key(page)
        if key in self.page_memo:
            return self.page_memo[key]
        result = ([], None, [])
        ev = self.events.setdefault(key[0], {"pages": set(), "used_on": None,
                                             "name": None, "bom": [], "secs": 0.0})
        ev["pages"].add(page.number)
        if self.available and self.enabled:
            t0 = datetime.now()
            words = self.words_cached(page, issues, page_label)
            if words:
                used, name, used_anchor = _title_fields_from_words(words)
                bom = _partslist_from_words(page, words)
                # Tesseract's full-page pass sometimes never reads the USED ON
                # value, though it reads the label.  Strip re-OCR the label region
                # (same trick as the parts-list rows).
                if used_anchor is not None and not used:
                    rect = pymupdf.Rect(used_anchor[0] - OCR_USED_ON_DX_LEFT,
                                     used_anchor[1] - OCR_USED_ON_DY_TOP,
                                     used_anchor[0] + OCR_USED_ON_DX_RIGHT,
                                     used_anchor[1] + OCR_USED_ON_DY_BOTTOM)
                    used = _ocr_strip_tokens(page, rect, psm="6")
                if not used:
                    # Scaled-down scans: the small title-block labels are often
                    # missed at full-page DPI - re-OCR the bottom-right corner
                    # at high zoom.
                    try:
                        clip = pymupdf.Rect(page.rect.width * 0.55,
                                         page.rect.height * 0.85,
                                         page.rect.width, page.rect.height)
                        zoom_words = _ocr_words_zoom(page, clip, 8, "6")
                        z_used, z_name, _ = _title_fields_from_words(zoom_words)
                        if z_used:
                            used = z_used
                            name = z_name or name
                    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                        if issues is not None:
                            issues.append(f"{page_label}: title-block zoom OCR failed: {e}")
                result = (used, name, bom)
                if not ev["used_on"] and used:
                    ev["used_on"] = list(used)
                if not ev["name"] and name:
                    ev["name"] = name
                for v in bom:
                    if v not in ev["bom"]:
                        ev["bom"].append(v)
            ev["secs"] += (datetime.now() - t0).total_seconds()
        _memo_put(self.page_memo, key, result)
        return result

    def report_lines(self):
        lines = []
        total_pages, total_secs = 0, 0.0
        for path, ev in sorted(self.events.items()):
            stem = Path(path).name
            pages = ", ".join(str(p + 1) for p in sorted(ev["pages"]))
            fields = []
            if ev["used_on"]:
                fields.append("USED ON=" + ",".join(ev["used_on"]))
            if ev["name"]:
                fields.append("NAME=" + repr(ev["name"]))
            fields.append("BOM=" + (",".join(ev["bom"]) if ev["bom"] else "none"))
            lines.append(f"  {stem}: page(s) {pages}: " + "; ".join(fields))
            total_pages += len(ev["pages"])
            total_secs += ev["secs"]
        lines.append(f"  OCR total: {len(self.events)} pdf(s), {total_pages} page(s), "
                     f"{total_secs:.1f}s")
        return lines

    def scanned_stems(self):
        """(stem, sorted 1-based page numbers) for image-only PDFs seen this run.

        Populated for every image-only page routed to OCR, even when OCR is
        disabled or unavailable, so the workbook can always list scanned parts.
        """
        out = []
        for path, ev in sorted(self.events.items()):
            stem = canonical_stem(Path(path).stem) if path else None
            pages = sorted(p + 1 for p in ev.get("pages", ()))
            if stem and pages:
                out.append((stem, pages))
        return out

    def event_delta(self, before_keys):
        delta = {}
        for k in self.events:
            if k in before_keys:
                continue
            ev = self.events[k]
            delta[k] = {"pages": sorted(ev.get("pages", ())),
                        "used_on": list(ev.get("used_on") or []),
                        "name": ev.get("name"),
                        "bom": list(ev.get("bom", [])),
                        "secs": float(ev.get("secs", 0.0))}
        return delta

    def merge_events(self, delta):
        for k, ev in (delta or {}).items():
            cur = self.events.setdefault(k, {"pages": set(), "used_on": None,
                                             "name": None, "bom": [], "secs": 0.0})
            cur["pages"].update(ev.get("pages", ()))
            if not cur["used_on"] and ev.get("used_on"):
                cur["used_on"] = list(ev["used_on"])
            if not cur["name"] and ev.get("name"):
                cur["name"] = ev["name"]
            for v in ev.get("bom", ()):
                if v not in cur["bom"]:
                    cur["bom"].append(v)
            cur["secs"] += ev.get("secs", 0.0)


# Run-scoped singleton (deliberate, not hidden global state): cli resets it
# once per run and worker processes re-init it per process.
OCR = _OcrContext()

# Test seam: monkeypatch to intercept PDF opening in the worker tasks.
_open_pdf = pymupdf.open


def normalize(s):
    return re.sub(r"\s+", "", str(s) if s is not None else "").upper().strip()


# ---------------------------------------------------------------------------
# OCR (scanned / image-only PDFs)
# ---------------------------------------------------------------------------
def _ocr_tok(word):
    """Validate/normalize an OCR'd word into a FERMI part value (O→0, I/L→1, S→5, B→8, |→1).

    Tesseract often glues table borders onto cell text ('|F10126513]SSR2',
    'FLO|44633'), so every alphanumeric run inside the word is tried, not just
    the whole word. The confusion map is applied before splitting so a pipe
    read as the digit 1 survives inside the token. FC-prefixed common
    components are rejected - the pipeline skips them anyway.
    """
    src = word.upper()
    # Corrected runs first (a pipe *inside* a token is a misread 1), then the
    # raw runs (a leading/trailing table border must be stripped, not turned
    # into a leading 1).
    cands = re.split(r"[^A-Z0-9]+", src.translate(OCR_CORR))
    cands += re.split(r"[^A-Z0-9]+", src)
    for part in cands:
        if not part.startswith("F"):
            continue
        c = part.translate(OCR_CORR)
        if OCR_VAL_RE.match(c) and not c.startswith("FC"):
            return c
    return None


def _is_fermi_label(text):
    """True for OCR variants of the parts-list FERMI# header ('_FERML#', 'FERM1')."""
    t = re.sub(r"[^A-Z0-9]", "", text.upper()).replace("1", "I").replace("L", "I")
    return t == "FERMI"


def _find_used_anchor(words):
    """First 'USED'+'ON' label pair ('ON' immediately right, same visual row)."""
    # Row index of ON candidates (same tolerance-3 clustering as
    # _words_by_row) so each USED word scans only nearby rows, not the full
    # word list. Identical results: the row prefilter (TOL+3) admits every
    # word the exact check (TOL) could match (triangle inequality), and the
    # exact x-gap / y-band check is re-applied per word; outer order (first
    # USED in words order) is preserved.
    on_rows = {}
    for w2 in words:
        t2 = re.sub(r"[^A-Za-z]", "", w2[4].upper()) if len(w2[4]) <= 4 else ""
        if t2 != "ON":
            continue
        y0 = w2[1]
        placed = False
        for ry in on_rows:
            if abs(ry - y0) < 3:
                on_rows[ry].append(w2)
                placed = True
                break
        if not placed:
            on_rows[y0] = [w2]
    for w in words:
        t = re.sub(r"[^A-Z]", "", w[4].upper())  # OCR glues borders: '«USED' -> USED
        if t != "USED":
            continue
        for ry, members in on_rows.items():
            if abs(ry - w[1]) >= OCR_ROW_TOL + 3:
                continue
            for w2 in members:
                if (0 <= w2[0] - w[2] < OCR_USED_ON_GAP_MAX
                        and abs(w2[1] - w[1]) < OCR_ROW_TOL):
                    return w
    return None


def _find_name_anchor(words):
    """Last standalone NAME label (a PART word to its left marks the header row)."""
    name_anchor = None
    for w in words:
        t = re.sub(r"[^A-Z]", "", w[4].upper())
        if t == "NAME":
            # the parts-list header row reads "ITEM | FERMI # | PART NAME";
            # only a standalone NAME (no PART before it on the same visual row)
            # is the title-block NAME label.
            left = [w2 for w2 in words
                    if w2[0] < w[0] and abs(w2[1] - w[1]) < OCR_ROW_TOL]
            if any(w2[4].upper().startswith("PART") for w2 in left):
                continue
            name_anchor = w
    return name_anchor


def _used_values_around_anchor(words, used_anchor):
    used_vals, seen = [], set()
    if used_anchor is not None:
        ux, uy = used_anchor[0], used_anchor[1]
        for w in words:
            v = _ocr_tok(w[4])
            if (v and v not in seen
                    and ux - OCR_USED_ON_DX_LEFT <= w[0] <= ux + OCR_USED_ON_DX_RIGHT
                    and uy - OCR_USED_ON_DY_TOP <= w[1] <= uy + OCR_USED_ON_DY_BOTTOM):
                seen.add(v)
                used_vals.append(v)
    return used_vals


_BAD_NAME_WORDS = frozenset({"ENERGY", "LABORATORY", "NATIONAL", "UNITED",
                             "STATES", "DEPARTMENT", "ACCELERATOR"})


def _name_from_anchor(words, name_anchor):
    if name_anchor is None:
        return None
    nx, ny1 = name_anchor[0], name_anchor[3]
    picked = [w for w in words
              if w is not name_anchor
              and nx - OCR_NAME_DX_LEFT <= w[0] <= nx + OCR_NAME_DX_RIGHT
              and ny1 - OCR_NAME_DY_TOP <= w[1] <= ny1 + OCR_NAME_DY_BOTTOM
              and len(re.sub(r"[^A-Za-z]", "", w[4])) >= 2
              and not _ocr_tok(w[4])
              and w[4].upper().strip(",:;") not in _BAD_NAME_WORDS]
    picked.sort(key=lambda w: (round(w[1]), w[0]))
    if picked:
        # NAME is a single line; anything in the next visual row (SCALE,
        # DRAWING NUMBER chips OCR'd as junk) is dropped.
        y_first = (picked[0][1] + picked[0][3]) / 2
        picked = [w for w in picked if abs((w[1] + w[3]) / 2 - y_first) < OCR_NAME_LINE_TOL]
    if picked:
        return " ".join(w[4] for w in picked)
    return None


def _join_split_f_tokens(words):
    """Join Tesseract's split F tokens ('F' + '10187546') into one word."""
    out = []
    used = set()
    for i, w in enumerate(words):
        if i in used:
            continue
        if re.fullmatch(r"FC?", w[4].strip().upper()):
            for j, q in enumerate(words):
                if j == i or j in used:
                    continue
                if (re.fullmatch(r"\d{5,8}", q[4].strip())
                        and 0 <= q[0] - w[2] <= 12
                        and abs(q[1] - w[1]) <= 4):
                    used.add(j)
                    out.append((w[0], min(w[1], q[1]), q[2],
                                max(w[3], q[3]), w[4].strip() + q[4].strip(),
                                0, 0, 0))
                    break
            else:
                out.append(w)
            continue
        out.append(w)
    return out


def _title_fields_from_words(words):
    """USED ON + NAME from OCR word geometry (label-anchored, no absolute coords)."""
    words = _join_split_f_tokens(words)
    used_anchor = _find_used_anchor(words)
    name_anchor = _find_name_anchor(words)
    used_vals = _used_values_around_anchor(words, used_anchor)
    name = _name_from_anchor(words, name_anchor)
    return used_vals, name, used_anchor


def _join_split_f_strings(tokens):
    """Join Tesseract's split F tokens ('F' + '10187546') in a token list."""
    out = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if (re.fullmatch(r"FC?", t) and i + 1 < len(tokens)
                and re.fullmatch(r"\d{5,8}", tokens[i + 1])):
            out.append(t + tokens[i + 1])
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _tesseract_run(args, timeout):
    """Invoke the Tesseract CLI for OCR output; raise on a failed run.

    Non-zero exit (bad tessdata, engine init crash) leaves stdout empty:
    callers must not read that as a genuinely blank region, so a failed
    run surfaces as an extraction error instead of silent accuracy loss.
    The child gets an explicit env (OCR.subprocess_env), never relying on
    the process-global export that exists only for PyMuPDF's in-process OCR.
    """
    r = subprocess.run(args, capture_output=True, text=True,
                       # UTF-8, not the locale codepage: Tesseract output
                       # contains multi-byte characters (e.g. curly quotes)
                       # that cp1252 cannot decode - the reader thread then
                       # dies and r.stdout is None.
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=OCR.subprocess_env())
    if r.returncode != 0:
        err = (r.stderr or "").strip().replace("\n", " ")[:160]
        raise RuntimeError(f"tesseract exit {r.returncode}: {err}")
    return r


def _ocr_strip_tokens(page, rect, psm="7"):
    """OCR of one small strip via the Tesseract CLI, with consensus voting.

    Single-pass OCR is knife-edge on the smallest text of scanner-dpi sheets:
    an F-number flips between reads at different zoom/psm/oem combinations. A
    few cheap passes are voted - a value read by two passes wins, otherwise
    the first pass that yields values is used. psm 7 (single line) can glue
    the row's item digit into the FERMI token ('-4_1F10197133'), which no
    longer parses; psm 6 (uniform block) and lower zooms are among the
    fallbacks.
    """
    exe = OCR.tesseract_path
    if not exe:
        return []
    scale = OCR.doc_scale(page)
    # Target the zoom that renders the strip's glyphs at a Tesseract-friendly
    # pixel height (~36 px) using the word heights the full-page pass already
    # measured (normalized ISO points). The readable zoom window on
    # scanner-dpi sheets is narrow, so a slightly higher zoom is also tried.
    med_h = 0.0
    if psm == "6":
        near = [w[3] - w[1] for w in OCR.words_cached(page)
                if rect.y0 <= w[1] <= rect.y1 + 40]
        if near:
            med_h = sorted(near)[len(near) // 2]
    if scale != 1.0:
        # Rects are built from ISO-normalized word coordinates.
        rect = pymupdf.Rect(rect.x0 * scale, rect.y0 * scale,
                         rect.x1 * scale, rect.y1 * scale)
    # Pages stored at scanner dpi (scale > 1) are already magnified; a fixed
    # strip zoom over-magnifies and Tesseract garbles the value. The USED ON
    # strip reads best at the lower zooms on these sheets, the parts-list rows
    # at the higher one.
    zoom_hi = OCR_STRIP_DPI / min(scale, 3.0)
    zoom_lo = max(1.0, zoom_hi / 1.9)
    if psm == "6":
        zoom_t = 36.0 / med_h if med_h else zoom_lo
        zoom_t = min(max(zoom_t, zoom_lo * 0.9), zoom_hi)
        passes = ((zoom_lo, "6", "1"), (zoom_t * 1.07, "6", "1"),
                  (zoom_t, "6", "3"), (zoom_hi, "6", "3"))
    else:
        passes = ((zoom_hi, "7", "3"), (zoom_lo, "6", "1"), (zoom_hi, "6", "3"))
    pngs = {}
    try:
        results = []
        failed = 0
        last_err = None
        for zoom, mode, oem in passes:
            if zoom not in pngs:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
                fd, png = tempfile.mkstemp(prefix="fermipdf_ocr_", suffix=".png")
                os.close(fd)
                pix.save(png)
                pngs[zoom] = png
            try:
                r = _tesseract_run(
                    [exe, pngs[zoom], "stdout", "--psm", mode, "--oem", oem,
                     "--dpi", str(int(72 * zoom))], 120)
            except (RuntimeError, subprocess.SubprocessError) as e:
                # One failing pass (hung engine on this strip) must not kill
                # the vote: the remaining passes still run. Only when every
                # pass fails does the broken engine surface (re-raised below).
                failed += 1
                last_err = e
                results.append([])
                continue
            # Glue pipe-separated fragments back together ('FLO |44633'): the
            # pipe is Tesseract's border/1 confusion and OCR_CORR maps it to 1.
            text = re.sub(r"\s*\|\s*", "|", (r.stdout or "").upper())
            toks = re.sub(r"[^A-Z0-9|]+", " ", text).split()
            vals = []
            for tok_src in _join_split_f_strings(toks):
                v = _ocr_tok(tok_src)
                if v and v not in vals:
                    vals.append(v)
            # Consensus: two passes read the same value set.
            if vals and any(vals == prev for prev in results):
                return vals
            results.append(vals)
        votes = {}
        for vals in results:
            for v in vals:
                votes[v] = votes.get(v, 0) + 1
        if votes:
            best, n = max(votes.items(), key=lambda kv: kv[1])
            if n >= 2:
                for vals in results:
                    if best in vals:
                        return vals
        for vals in results:
            if vals:
                return vals
        if failed == len(passes):
            raise last_err
        return []
    finally:
        for png in pngs.values():
            try:
                os.unlink(png)
            except OSError:
                # Best-effort temp cleanup: a locked or already-removed file
                # must not fail the OCR path.
                pass


def _partslist_header(words):
    """Best FERMI# header anchor: (score, item_x, fer_x, fer_x1, hdr_y1, hdr_y, side).

    Fermilab sheets draw the parts list bottom-up: the ITEM/FERMI# header row
    sits BELOW its rows (item 1 just above the header). The side with the most
    FERMI-shaped tokens aligned in the label's column wins, so the title-block
    'FERMI NATIONAL ACCELERATOR LABORATORY' text (no aligned F-tokens) never
    wins the anchor search.
    """
    hdr_best = None
    for w in words:
        if not _is_fermi_label(w[4]):
            continue
        x0, x1, y1 = w[0], w[2], w[3]
        col = [q for q in words
               if x0 - OCR_FERMI_SCAN_DX_LEFT <= q[0] <= x0 + OCR_FERMI_SCAN_DX_RIGHT
               and _ocr_tok(q[4])]
        n_above = sum(1 for q in col if q[1] < w[1] - OCR_HDR_ROW_GAP)
        n_below = sum(1 for q in col if q[1] > y1)
        item = next((w2 for w2 in words
                     if "ITEM" in w2[4].upper()
                     and abs(w2[1] - w[1]) < OCR_HDR_ROW_TOL
                     and 0 < w[0] - w2[2] < OCR_ITEM_FERMI_GAP_MAX), None)
        # A real parts list has an ITEM label on the header row or several
        # aligned rows; a lone F-token next to the title-block 'FERMI NATIONAL
        # ACCELERATOR LABORATORY' text is not a parts list.
        if item is None and max(n_above, n_below) < 2:
            continue
        side = "above" if n_above >= n_below else "below"
        score = max(n_above, n_below) + (2 if item else 0)
        if hdr_best is None or score > hdr_best[0]:
            hdr_best = (score, item[0] if item else w[0] - OCR_ITEM_X_FALLBACK,
                        x0, x1, y1, w[1], side)
    return hdr_best


def _partslist_candidate_rows(words, item_x, fer_x, fer_x1, hdr_y, side):
    """Candidate rows: ITEM digits OR bare FERMI tokens on the header's side."""
    cand_rows = []
    for w in words:
        x, y0 = w[0], w[1]
        if side == "above":
            if y0 >= hdr_y - OCR_HDR_ROW_GAP:
                continue
        elif y0 <= hdr_y + OCR_HDR_ROW_GAP:
            continue
        if "RCD" in w[4].upper():
            continue  # revision-control document numbers are not parts
        v = _ocr_tok(w[4])
        label = re.fullmatch(r"\d{1,3}\|?", w[4])
        # Tesseract sometimes splits the F from its digits ('F' + '10187546');
        # the row strip re-OCR recovers the full value.
        partial_f = re.fullmatch(r"F\|?", w[4].upper())
        in_fermi_col = fer_x - OCR_FERMI_COL_DX_LEFT <= x <= fer_x1 + OCR_FERMI_COL_DX_RIGHT
        if (((v or partial_f) and in_fermi_col)
                or (label and item_x - OCR_ITEM_COL_DX_LEFT <= x < fer_x - OCR_ITEM_FERMI_MIN_GAP)):
            cand_rows.append((y0, y0, w))
    return cand_rows


def _dedupe_row_candidates(cand_rows):
    """Keep the topmost candidate per visual row (tolerance 3).

    Scaled-down scans pack parts-list rows ~5px apart; a looser tolerance
    merges two real rows and silently drops one of their values.
    """
    rows = []
    for y0, y1, w in sorted(cand_rows):
        if rows and abs(y0 - rows[-1][0]) < 3:
            continue
        rows.append((y0, y1, w))
    return rows


def _partslist_from_words(page, words):
    """Parts-list FERMI values: label-anchored column + per-row strip re-OCR."""
    if not words:
        return []
    words = _join_split_f_tokens(words)
    hdr_best = _partslist_header(words)
    if hdr_best is None:
        return []
    _, item_x, fer_x, fer_x1, hdr_y1, hdr_y, side = hdr_best
    rows = _dedupe_row_candidates(
        _partslist_candidate_rows(words, item_x, fer_x, fer_x1, hdr_y, side))
    vals = []
    for y0, y1, w in rows:
        v = _ocr_tok(w[4])
        if v:
            # The row's own token is reliable; a strip rect can span
            # neighbouring rows on dense scaled-down scans.
            if v not in vals:
                vals.append(v)
            continue
        rect = pymupdf.Rect(item_x - OCR_ITEM_COL_DX_LEFT, y0 - OCR_ROW_STRIP_DY_TOP,
                         fer_x1 + OCR_FERMI_COL_DX_RIGHT, y1 + OCR_ROW_STRIP_DY_BOTTOM)
        for sv in dict.fromkeys(_ocr_strip_tokens(page, rect)):
            if sv not in vals:
                vals.append(sv)
    return vals


def _table_fermi_column(data):
    """First of the top 3 rows with a FERMI header -> (hdr_idx, fermi_col).

    (None, None) when no header row is found.
    """
    for row_idx in range(min(3, len(data))):
        header_candidate = [normalize(c) for c in data[row_idx]]
        fermi_col = next((i for i, h in enumerate(header_candidate)
                          if FERMI_HEADER_RE.match(h)), None)
        if fermi_col is not None:
            return row_idx, fermi_col
    return None, None


def _table_rows_to_entries(data, hdr_idx, fermi_col, page_num):
    entries = []
    for row in data[hdr_idx + 1:]:
        if fermi_col >= len(row):
            continue
        # A cell can carry more than the value: a quantity stacked under it
        # ('F10126145\n2' used to normalize to 'F101261452' - a wrong part
        # number) or an item number before it ('1 F10126108', dropped). Take
        # the first whitespace-separated token that is a whole FERMI value.
        for token in re.split(r"\s+", str(row[fermi_col] or "")):
            val = normalize(token)
            if FERMI_RE.match(val):
                entries.append((val, page_num, "table"))
                break
    return entries


def extract_bom_from_tables(doc, issues=None):
    """Table-based BOM entries: [(value, page_num, "table"), ...] from an open doc.

    Scans the first 3 rows of each detected table for a FERMI# header column
    (header gate); per-page detection/extraction errors go to `issues`.
    """
    entries = []
    # Silence MuPDF warnings on stderr; re-setting the global flag once per
    # document is harmless (this runs for every PDF).
    pymupdf.TOOLS.mupdf_display_errors(False)
    for page_num, page in enumerate(doc, 1):
        try:
            tables = page.find_tables().tables
        except (RuntimeError, ValueError) as e:
            if issues is not None:
                issues.append(f"page {page_num}: table detection failed: {e}")
            tables = []
        for table in tables:
            try:
                data = table.extract()
            except (RuntimeError, ValueError) as e:
                if issues is not None:
                    issues.append(f"page {page_num}: table extract failed: {e}")
                continue
            if not data:
                continue
            hdr_idx, fermi_col = _table_fermi_column(data)
            if fermi_col is None:
                continue
            entries.extend(_table_rows_to_entries(data, hdr_idx, fermi_col, page_num))
    return entries


def _fermi_line_is_title_block(lines, i):
    """Format 3: whether a lone FERMI# line belongs to the title block."""
    is_title_block = False
    if i > 0:
        prev_up = lines[i - 1].upper()
        if prev_up in TITLE_BLOCK_KEYWORDS:
            is_title_block = True
        elif SIZE_RE.match(prev_up) and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if re.match(r"^1\s+OF\s*\d+", nxt, re.IGNORECASE):
                is_title_block = True
    if not is_title_block and i + 1 < len(lines):
        nxt_up = lines[i + 1].upper()
        if nxt_up in TITLE_BLOCK_KEYWORDS:
            is_title_block = True
        elif re.match(r"^1\s+OF\s*\d+", nxt_up, re.IGNORECASE):
            is_title_block = True
    if not is_title_block and i > 1:
        if _is_used_on_caption(lines[i - 2]):
            is_title_block = True
    if not is_title_block:
        # A stack of USED ON values ('USED ON' then several F-number lines)
        # is all title block: walk up over the consecutive run of F-number
        # lines to its label, so every value in the stack is skipped, not
        # just the two nearest the label.
        j = i - 1
        while j >= 0 and FERMI_RE.match(lines[j].strip()):
            j -= 1
        if j >= 0 and _is_used_on_caption(lines[j]):
            is_title_block = True
    return is_title_block


def _fermi_line_has_context(lines, i):
    # A lone FERMI# line only counts as BOM when other BOM content is nearby.
    for dy in range(-3, 4):
        if dy == 0:
            continue
        j = i + dy
        if 0 <= j < len(lines):
            if FERMI_RE.match(lines[j]) or ITEM_RE.match(lines[j]):
                return True
    return False


_CROSS_REF_WORDS = frozenset(
    {"AND", "OR", "&", "W/", "WITH", "PER", "SEE", "REF", "+"})


def _desc_is_cross_reference(desc):
    """A 'description' that only points at other drawings (e.g. the wrapped
    fabrication note 'F10112550 AND F10118731.') is a cross-reference, not a
    part name. No genuine part name consists solely of drawing references
    and conjunctions, so rejecting these can only drop bogus edges (the part
    then surfaces as an orphan/missing reference, visibly)."""
    text = FERMI_RE_SEARCH.sub(" ", desc.upper())
    words = [w.strip(".,;:()") for w in text.split()]
    words = [w for w in words if w]
    return bool(words) and all(w in _CROSS_REF_WORDS for w in words)


def _line_bom_entries(lines, page_num):
    """Formats 1-3 over the page's text lines, in exact scan order."""
    entries = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Format 1: Multi-line (item number on the line above; FERMI# and description below)
        if FERMI_RE.match(line) and i > 0 and ITEM_RE.match(lines[i - 1]):
            desc_candidate = lines[i + 1] if i + 1 < len(lines) else ""
            if desc_candidate and not BAD_DESC_RE.match(desc_candidate):
                if _desc_is_cross_reference(desc_candidate):
                    # Cross-reference, not a part: skip the row entirely.
                    # Without this the FERMI line falls through to Format 3
                    # below, whose context check the ITEM line above
                    # satisfies - emitting the rejected drawing anyway.
                    # (BAD_DESC rejections keep the old fallthrough.)
                    i += 1
                    continue
                val = normalize(line)
                entries.append((val, page_num, "text-fallback", desc_candidate.strip()))
                i += 2
                # Skip optional note line(s) until we hit qty or next item
                while i < len(lines):
                    if ITEM_RE.match(lines[i]):
                        i += 1
                        break
                    if ITEM_RE.match(lines[i]) or FERMI_RE.match(lines[i]):
                        break
                    i += 1
                continue
        # Format 2: Single-line (FERMI# + description on same line). The
        # description must read like a part name: a wrapped fabrication note
        # ("F10112550 AND F10118731.") matches the shape but only points at
        # other drawings, so cross-references are rejected (neighboring bare
        # dimension lines satisfy ITEM_RE, so a context gate would not save
        # us here - and isolated single-line rows are legitimate BOM content
        # per test_single_line_bom_detection, so none is required).
        m = SINGLE_LINE_RE.match(line)
        if m:
            val = normalize(m.group(1))
            desc = m.group(2).strip()
            if not BAD_DESC_RE.match(desc) and not _desc_is_cross_reference(desc):
                entries.append((val, page_num, "text-fallback", desc))
                i += 1
                continue
        # Format 3: FERMI# as anchor - check if in BOM context
        if FERMI_RE.match(line):
            if not _fermi_line_is_title_block(lines, i) and _fermi_line_has_context(lines, i):
                entries.append((normalize(line), page_num, "text-fallback"))
        i += 1
    return entries


def _dict_row_text(rows, y):
    return " ".join(t for _, t in sorted(rows[y]))


def _positional_row_is_title_block(rows, sorted_ys, y_idx, y):
    if _is_caption_row(_dict_row_text(rows, y)):
        return True
    for _, t in rows[y]:
        t_up = t.upper()
        if t_up in TITLE_BLOCK_KEYWORDS:
            return True
        if re.match(r"^1\s+OF\s*\d+", t_up):
            return True
        if SIZE_RE.match(t_up):
            return True
    for dy in range(1, 4):
        if y_idx - dy < 0:
            break
        prev_y = sorted_ys[y_idx - dy]
        if prev_y < y - 30:
            break
        if _is_used_on_caption(_dict_row_text(rows, prev_y)):
            return True
    # A tall title-block cell can park the USED ON value several rows below
    # its label (F10205800: 3 note rows intervene, label 4 rows back), which
    # the tight loop above never reaches - and a USED ON value is an F-number
    # by design, i.e. a certain false BOM edge. Only USED ON gets the wide
    # window; other keywords stay tight so genuine rows near spec text keep
    # working.
    # The wide window must also be horizontally associated with this row: a
    # USED ON label in another column (or a neighbouring drawing's block) is
    # not this row's title-block cell.
    row_xs = [x for x, _ in rows[y]]
    lo, hi = min(row_xs) - 60, max(row_xs) + 60
    for dy in range(4, 9):
        if y_idx - dy < 0:
            break
        prev_y = sorted_ys[y_idx - dy]
        if prev_y < y - 100:
            break
        if _is_used_on_caption(_dict_row_text(rows, prev_y)):
            label_xs = [x for x, t in rows[prev_y]
                        if "USED" in t.upper() or t.upper() == "ON"]
            if any(lo <= x <= hi for x in label_xs):
                return True
    return False


def _positional_row_has_context(rows, sorted_ys, y_idx, y):
    # BOM context: the row's own ITEM segment or BOM content in rows above.
    for _, t in rows[y]:
        if ITEM_RE.match(t.strip()):
            return True
    for dy in range(1, 4):
        if y_idx - dy < 0:
            break
        prev_y = sorted_ys[y_idx - dy]
        if prev_y < y - 30:
            break
        prev_text = _dict_row_text(rows, prev_y)
        if FERMI_RE_SEARCH.search(prev_text) or any(ITEM_RE.match(w) for _, w in rows[prev_y]):
            return True
    return False


def _positional_row_entries(page, page_num, entries, issues):
    """Format 4: Positional text grouping; appends into entries (set-deduped)."""
    try:
        blocks = page.get_text("dict")["blocks"]
        rows = {}
        for b in blocks:
            if "lines" not in b:
                continue
            for line in b["lines"]:
                y = line["bbox"][1]
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                found = False
                for ry in rows:
                    if abs(ry - y) < 3:
                        rows[ry].append((line["bbox"][0], text))
                        found = True
                        break
                if not found:
                    rows[y] = [(line["bbox"][0], text)]
        sorted_ys = sorted(rows)
        existing = {e[0] for e in entries}
        for y_idx, y in enumerate(sorted_ys):
            row_text = _dict_row_text(rows, y)
            # The row text embeds the FERMI# among other tokens: search, don't match.
            if not FERMI_RE_SEARCH.search(row_text):
                continue
            if _positional_row_is_title_block(rows, sorted_ys, y_idx, y):
                continue
            if not _positional_row_has_context(rows, sorted_ys, y_idx, y):
                continue
            for _, t in rows[y]:
                t_stripped = t.strip()
                if FERMI_RE.match(t_stripped):
                    # A bare FERMI token takes its description from the row
                    # below (Format-1 stack shape): a pure cross-reference
                    # there ("AND F10126108.") rejects the row, mirroring the
                    # line parser - otherwise the positional path re-adds
                    # what the line path just rejected.
                    if y_idx + 1 < len(sorted_ys) and _desc_is_cross_reference(
                            _dict_row_text(rows, sorted_ys[y_idx + 1])):
                        continue
                    val = normalize(t_stripped)
                    if val not in existing:
                        entries.append((val, page_num, "text-positional"))
                        existing.add(val)
    except (RuntimeError, ValueError, KeyError, TypeError) as e:
        if issues is not None:
            issues.append(f"page {page_num}: positional grouping failed: {e}")


def _doc_has_parts_header(doc):
    """Whether any page prints a parts-list header: a standalone caption
    ("PARTS LIST", "BOM", ...), the column combo on one line ("ITEM ...
    FERMI ..."), or the column labels on adjacent lines (PDF text extraction
    often emits one label per line). Gates the line-based BOM fallback (see
    extract_bom_from_text): without a header anywhere, F-numbered text lines
    are notes/title-block content. Table and positional parsers carry their
    own structural gates and are unaffected."""
    try:
        texts = [page.get_text() for page in doc]
    except (RuntimeError, ValueError):
        return False
    for text in texts:
        lines = [raw.strip() for raw in text.splitlines()]
        for i, line in enumerate(lines):
            if PARTS_HEADER_RE.match(line) or PARTS_HEADER_COMBO_RE.search(line):
                return True
            if ITEM_RE.match(line):
                for dy in (-2, -1, 1, 2):
                    j = i + dy
                    if 0 <= j < len(lines) and FERMI_HEADER_RE.match(lines[j]):
                        return True
    return False


def extract_bom_from_text(doc, issues=None):
    """Text-based BOM entries from an open doc (line + positional fallbacks).

    Image-only pages (< OCR_MIN_CHARS of text) go through the OCR context when
    enabled; the native line parser never sees unstructured OCR text. The
    line parser additionally requires a parts-list header somewhere in the
    document: on a headerless sheet every F-numbered line is a note, a
    reference, or title-block content.
    """
    entries = []
    header = _doc_has_parts_header(doc)
    for page_num, page in enumerate(doc, 1):
        text = page.get_text()
        if len(text.strip()) < OCR_MIN_CHARS:
            # Scanned (image-only) page: OCR decides its content.  If strip
            # re-OCR found parts-list values they are added directly; the
            # native line parser never sees the unstructured OCR text.
            used_vals, _ocr_name, ocr_bom = OCR.analyze_page(page, issues, f"page {page_num}")
            if ocr_bom:
                entries.extend((v, page_num, "ocr") for v in ocr_bom)
            if used_vals or ocr_bom or OCR.doc_key(page) in OCR.words_memo:
                continue
        if "FERMI" not in text.upper():
            continue
        lines = [l.strip() for l in text.splitlines()]
        lines = [l for l in lines if l]
        if header:
            entries.extend(_line_bom_entries(lines, page_num))
        _positional_row_entries(page, page_num, entries, issues)
        entries.extend(_extract_bom_positional(page, page_num, issues))
    return entries


def _words_by_row(words):
    # Same tolerance-3 row clustering as Format 4.
    rows = {}
    for w in words:
        x0, y0, word = w[0], w[1], w[4]
        found_row = False
        for ry in rows:
            if abs(ry - y0) < 3:
                rows[ry].append((x0, word))
                found_row = True
                break
        if not found_row:
            rows[y0] = [(x0, word)]
    return rows


def _fermi_word_positions(rows):
    positions = []
    for y_key in sorted(rows.keys()):
        row_words = sorted(rows[y_key], key=lambda t: t[0])
        for x, word in row_words:
            if FERMI_RE.match(word):
                positions.append((x, y_key, word))
    return positions


def _x_clusters(x_values):
    clusters = []
    for x in x_values:
        if clusters and x - clusters[-1][-1] <= 15:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    return clusters


def _word_row_has_item_before(row_words, x):
    for wx, w in row_words:
        if wx >= x:
            break
        if ITEM_RE.match(w):
            return True
    return False


_CAPTION_VALUE_RE = re.compile(r"(?:\d+(?::\d+)?|A\d|OF|[A-Z])")
_CAPTION_FERMI_VALUE_RE = re.compile(r"F[C]?\d+[A-Za-z]?")
# Fields whose caption value is an F-number by design. Other keywords must not
# let an F-number pass as their value: 'ITEM 1 F10126107' is a BOM row (item
# number + part), not a caption, and skipping it would drop a real child.
_CAPTION_FERMI_FIELDS = frozenset(
    {"USED ON", "DRAWING NUMBER", "NUMBER", "NEXT ASSY"})
_CAPTION_PHRASES = sorted(TITLE_BLOCK_KEYWORDS, key=len, reverse=True)


def _is_caption_row(text):
    """True when a row reads as a title-block caption: one of the configured
    keywords (multi-word phrases included) followed only by value tokens -
    'SCALE 1:1', 'SHEET 1 OF 2', 'USED ON F10126107', bare 'REV'. An F-number
    is a value only for the fields that hold one (see _CAPTION_FERMI_FIELDS).
    A row that merely starts with or contains a keyword word as ordinary text
    ('SHEET METAL BRACKET', 'BRACKET USED ON ASSY') is BOM content, not a
    caption."""
    text = " ".join((text or "").upper().split())
    if not text:
        return False
    for kw in _CAPTION_PHRASES:
        if text == kw:
            return True
        if text.startswith(kw + " "):
            rest = text[len(kw) + 1:].split()
            fermi_ok = kw in _CAPTION_FERMI_FIELDS
            if rest and all(
                    _CAPTION_VALUE_RE.fullmatch(t)
                    or (fermi_ok and _CAPTION_FERMI_VALUE_RE.fullmatch(t))
                    for t in rest):
                return True
    return False


def _is_used_on_caption(text):
    """True when a row is a USED ON caption ('USED ON', 'USED ON F1...'),
    not BOM text merely containing the phrase ('BRACKET USED ON ASSY')."""
    text = " ".join((text or "").upper().split())
    return text.startswith("USED ON") and _is_caption_row(text)


def _word_row_is_title_block(rows, sorted_y_keys, y_key, row_words, y_index):
    # Same row: a drawing-size token (A0-A9) marks the title block.
    for _, w in row_words:
        if SIZE_RE.match(w):
            return True
    # Nearby rows: title-block captions or size tokens. A caption is a
    # keyword row, optionally with its value ('SCALE 1:1', 'USED ON F1...');
    # a keyword word inside a BOM row's text must not disable the rows below.
    y_idx = y_index[y_key]
    for dy in range(1, 4):
        if y_idx - dy < 0:
            break
        prev_y = sorted_y_keys[y_idx - dy]
        if prev_y < y_key - 30:
            break
        prev_text = " ".join(w for _, w in sorted(rows[prev_y], key=lambda t: t[0]))
        if _is_caption_row(prev_text):
            return True
        for _, w in rows[prev_y]:
            if SIZE_RE.match(w):
                return True
    return False


def _extract_bom_positional(page, page_num, issues=None):
    found = []
    try:
        words = page.get_text("words")
    except (RuntimeError, ValueError) as e:
        if issues is not None:
            issues.append(f"page {page_num}: word extraction failed: {e}")
        return found
    if not words:
        return found
    rows = _words_by_row(words)
    fermi_positions = _fermi_word_positions(rows)
    if len(fermi_positions) < 2:
        return found
    x_values = sorted(x for x, _, _ in fermi_positions)
    best_cluster = max(_x_clusters(x_values), key=len)
    if len(best_cluster) < 2:
        return found
    best_x_min = min(best_cluster) - 10
    best_x_max = max(best_cluster) + 10
    sorted_y_keys = sorted(rows.keys())
    y_index = {y: i for i, y in enumerate(sorted_y_keys)}
    for x, y_key, word in fermi_positions:
        if not best_x_min <= x <= best_x_max:
            continue
        row_words = sorted(rows[y_key], key=lambda t: t[0])
        row_text = " ".join(w for _, w in row_words)
        if _is_caption_row(row_text):
            continue
        if _word_row_is_title_block(rows, sorted_y_keys, y_key, row_words, y_index):
            continue
        if _word_row_has_item_before(row_words, x):
            val = normalize(word)
            found.append((val, page_num, "positional"))
    return found


def merge_bom_entries(table_entries, text_entries):
    """Union of table + text entries, deduped by FERMI value (first occurrence wins)."""
    seen = set()
    merged = []
    for e in table_entries + text_entries:
        if e[0] not in seen:
            seen.add(e[0])
            merged.append(e)
    return merged


def extract_bom_from_doc(doc, issues):
    """BOM entries from an open doc: merge table + text extraction (union, not either/or)."""
    table_entries = extract_bom_from_tables(doc, issues)
    text_entries = extract_bom_from_text(doc, issues)
    if table_entries and text_entries:
        method = "merged"
    elif table_entries:
        method = "table"
    else:
        method = "text-fallback"
    return merge_bom_entries(table_entries, text_entries), method


def extract_bom_entries(pdf_path):
    """Returns (entries, method, issues)."""
    issues = []
    size_msg = _pdf_size_issue(pdf_path)
    if size_msg is not None:
        issues.append(size_msg)
        return [], "skipped", issues
    with _open_pdf(pdf_path) as doc:
        pages_msg = _doc_pages_issue(doc)
        if pages_msg is not None:
            issues.append(pages_msg)
            return [], "skipped", issues
        entries, method = extract_bom_from_doc(doc, issues)
    return entries, method, issues


def extract_used_on(doc):
    """Extract USED ON value(s) from the title block on the first page."""
    values = []
    if doc.page_count == 0:
        return values
    if len(doc[0].get_text().strip()) < OCR_MIN_CHARS:
        ocr_used, _ocr_name, _ocr_bom = OCR.analyze_page(doc[0])
        if ocr_used:
            return ocr_used
    lines = [l.strip() for l in doc[0].get_text().splitlines()]
    for i, line in enumerate(lines):
        if not USED_RE.match(line):
            continue
        for follow in lines[i + 1:]:
            if not follow:
                continue
            if STOP_RE.match(follow):
                break
            toks = TOKEN_RE.findall(follow)
            if toks:
                values.extend(normalize(t) for t in toks)
            else:
                break
    return values


def extract_drawing_name(doc):
    """Extract the drawing NAME from the title block (first page)."""
    if doc.page_count == 0:
        return None
    if len(doc[0].get_text().strip()) < OCR_MIN_CHARS:
        _ocr_used, ocr_name, _ocr_bom = OCR.analyze_page(doc[0])
        if ocr_name:
            return ocr_name
    lines = [l.strip() for l in doc[0].get_text().splitlines()]
    lines = [l for l in lines if l]
    skip_words = {"SHEET", "REV", "DATE", "SCALE", "SIZE", "TITLE", "NUMBER",
                  "DRAWING NUMBER", "MATERIAL", "N/A", "USED ON", "1 OF 1"}
    for i, line in enumerate(lines):
        if line.upper() == "NAME" and i + 1 < len(lines):
            # A parts-list header pair ('PART' line directly above 'NAME')
            # labels a BOM column: the line below is a cell, not the name.
            prev = lines[i - 1].upper() if i > 0 else ""
            if prev == "PART" or prev.startswith("PART "):
                continue
            candidate = lines[i + 1]
            if candidate.upper() not in skip_words:
                return candidate
    return None


def _ocr_words_zoom(page, clip, zoom=8, psm="11"):
    """OCR a page crop at high zoom via the Tesseract CLI (TSV).

    Returns word tuples in page coordinates. Used for the small title-block
    labels that the full-page OCR pass misses on scaled-down scans.
    """
    exe = OCR.tesseract_path
    if not exe:
        return []
    scale = OCR.doc_scale(page)
    if scale > 1.0:
        zoom = zoom / min(scale, 3.0)
    if clip.width > 0 and clip.height > 0:
        max_zoom = (OCR_MAX_RENDER_MP * 1e6 / (clip.width * clip.height)) ** 0.5
        zoom = min(zoom, max(1.0, max_zoom))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
    fd, png = tempfile.mkstemp(prefix="fermipdf_ocr_", suffix=".png")
    os.close(fd)
    try:
        pix.save(png)
        r = _tesseract_run([exe, png, "stdout", "tsv", "--psm", psm], 180)
        words = []
        for line in (r.stdout or "").splitlines()[1:]:
            f = line.split("\t")
            if len(f) < 12 or f[0] != "5":
                continue
            left, top, w, h = int(f[6]), int(f[7]), int(f[8]), int(f[9])
            text = f[11].strip()
            if not text:
                continue
            x0, y0 = clip.x0 + left / zoom, clip.y0 + top / zoom
            if scale != 1.0:
                # Match the ISO-normalized full-page word coordinates.
                x0, y0 = x0 / scale, y0 / scale
                w, h = w / zoom / scale, h / zoom / scale
            else:
                w, h = w / zoom, h / zoom
            words.append((x0, y0, x0 + w, y0 + h, text, 0, 0, 0))
        return words
    finally:
        try:
            os.unlink(png)
        except OSError:
            pass


def _ocr_drawing_number(raw):
    """Drawing number from an OCR word, or None.

    Handles 'F10197062', corrections ('Floos2259' -> F10052259), and the
    leading F dropped by Tesseract ('10197062' -> F10197062).
    """
    t = raw.strip().upper()
    if TB_NUM_VAL_RE.match(t):
        return t
    v = _ocr_tok(t)
    if v and TB_NUM_VAL_RE.match(v):
        return v
    if re.fullmatch(r"\d{5,8}", t):
        return "F" + t
    return None


def _ocr_number_rank(raw):
    """Candidate priority: exact F-number, OCR-corrected, bare digits."""
    t = raw.strip().upper()
    if TB_NUM_VAL_RE.match(t):
        return 0
    v = _ocr_tok(t)
    if v and TB_NUM_VAL_RE.match(v):
        return 1
    return 2


def _title_value_under(words, label, val_re, dx_left, dx_right,
                       dy_top, dy_bottom, wide=False, convert=None, rank=None):
    """Value word near the bottom-most `label` word (title block).

    The bottom-most label is the title block field; the same label in the
    revision-history (RCD) block sits higher on the sheet. Text layers put the
    value just below the label (`wide=False`); OCR word boxes can be offset by
    a row, so OCR uses a generous symmetric window (`wide=True`). `convert`
    maps raw OCR words to a value (None rejects); `rank` orders candidates
    (lower wins, then distance to the label, then x).
    """
    labs = [w for w in words if w[4].strip().upper() == label]
    if not labs:
        return None
    lab = max(labs, key=lambda w: w[1])
    x0, y0, y1 = lab[0], lab[1], lab[3]
    y_min = y0 - dy_top if wide else y1 - dy_top
    y_max = y1 + dy_bottom
    vals = []
    for q in words:
        if not (x0 + dx_left <= q[0] <= x0 + dx_right
                and y_min <= q[1] <= y_max):
            continue
        raw = q[4].strip().upper()
        v = convert(raw) if convert else (raw if val_re.match(raw) else None)
        if v and val_re.match(v):
            key = rank(raw) if rank else 0
            vals.append((key, abs(q[1] - y0), q[0], v))
    vals.sort(key=lambda t: (t[0], t[1], t[2]))
    return vals[0][3] if vals else None


def extract_title_block(doc, issues=None):
    """(drawing number, revision) from the title block on page 1.

    The revision is a single letter ('-' when the drawing has none); both are
    None when the field cannot be read. Scanned pages reuse the memoized OCR
    words, so this adds no second OCR pass. OCR failures are recorded into
    `issues` when provided (a failed read must not pass as "no title block"
    downstream), mirroring the USED ON zoom path.
    """
    if doc.page_count == 0:
        return None, None
    page = doc[0]
    if len(page.get_text().strip()) < OCR_MIN_CHARS:
        if not (OCR.available and OCR.enabled):
            return None, None
        try:
            words = OCR.words_cached(page)
        except (RuntimeError, OSError, ValueError) as e:
            if issues is not None:
                issues.append(f"title-block OCR failed: {e}")
            return None, None

        def from_words(ws):
            return (
                _title_value_under(ws, "NUMBER", TB_NUM_VAL_RE,
                                   TB_NUM_DX_LEFT, TB_NUM_DX_RIGHT,
                                   TB_OCR_DY_TOP, TB_OCR_DY_BOTTOM, wide=True,
                                   convert=_ocr_drawing_number,
                                   rank=_ocr_number_rank),
                _title_value_under(ws, "REV", TB_REV_VAL_RE,
                                   TB_REV_DX_LEFT, TB_REV_DX_RIGHT,
                                   TB_OCR_DY_TOP, TB_OCR_DY_BOTTOM, wide=True),
            )

        number, rev = from_words(words)
        if number is None and rev is None:
            # Scaled-down scans: the small title-block labels are often missed
            # at full-page DPI - re-OCR the bottom-right corner at high zoom.
            clip = pymupdf.Rect(page.rect.width * 0.55, page.rect.height * 0.85,
                             page.rect.width, page.rect.height)
            zoom_words = []
            for psm in ("11", "6"):
                try:
                    zoom_words += _ocr_words_zoom(page, clip, 8, psm)
                except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                    if issues is not None:
                        issues.append(f"title-block zoom OCR failed: {e}")
                    break
                number, rev = from_words(zoom_words)
                if number and rev:
                    break
    else:
        words = page.get_text("words")
        number = _title_value_under(words, "NUMBER", TB_NUM_VAL_RE,
                                    TB_NUM_DX_LEFT, TB_NUM_DX_RIGHT,
                                    2, TB_NUM_DY_BOTTOM)
        rev = _title_value_under(words, "REV", TB_REV_VAL_RE,
                                 TB_REV_DX_LEFT, TB_REV_DX_RIGHT,
                                 2, TB_REV_DY_BOTTOM)
    return number, rev


# ---------------------------------------------------------------------------
# Watermark detection
# ---------------------------------------------------------------------------
def _span_chars(span):
    """Text of a get_texttrace() span (chars are (ucs, gid, origin, bbox))."""
    return "".join(chr(c[0]) for c in span.get("chars", ()))


def detect_watermark(doc, issues=None):
    """Best-effort watermark detection for one open document.

    Returns a short evidence string (e.g. "watermark text: PRELIMINARY",
    "transparent text: 'UNCONTROLLED COPY'", "watermark layer: Watermark")
    or None. Scans at most WATERMARK_MAX_PAGES pages. Heuristics target the
    common watermark signatures (transparency, diagonal or light large text,
    watermark layers, stamp annotations, keyword phrases) while ignoring
    template text such as the rotated title-block 'TEMPLATE VERSION' or the
    'Drafting' label. Check failures still return None (fail-open) but are
    recorded into `issues` when provided, so a failed check never silently
    passes as clean in runs that pass their issues list.
    """
    try:
        for v in (doc.get_ocgs() or {}).values():
            name = v.get("name") or ""
            if "watermark" in name.lower():
                return f"watermark layer: {name}"
    except (RuntimeError, ValueError, AttributeError) as e:
        if issues is not None:
            issues.append(f"watermark layer check failed: {e}")
    for page in list(doc)[:WATERMARK_MAX_PAGES]:
        try:
            m = WATERMARK_TEXT_RE.search(page.get_text())
        except (RuntimeError, ValueError) as e:
            if issues is not None:
                issues.append("watermark text check failed: "
                              f"{e}")
            m = None
        if m:
            return f"watermark text: {m.group(0).upper()}"
        try:
            annots = page.annots()
        except (RuntimeError, ValueError) as e:
            if issues is not None:
                issues.append(f"watermark annotation check failed: {e}")
            annots = None
        for a in annots or ():
            content = ((a.info or {}).get("content") or "").strip()
            if WATERMARK_TEXT_RE.search(content):
                return f"watermark annotation: {content[:40]}"
        try:
            spans = page.get_texttrace()
        except (RuntimeError, ValueError) as e:
            if issues is not None:
                issues.append(f"watermark visual check failed: {e}")
            spans = ()
        for span in spans:
            text = _span_chars(span).strip()
            if not text:
                continue
            size = float(span.get("size") or 0)
            opacity = span.get("opacity")
            if (opacity is not None and float(opacity) < WATERMARK_TRANSPARENT_OPACITY
                    and size >= WATERMARK_TRANSPARENT_MIN_SIZE):
                return f"transparent text: {text[:40]}"
            d = span.get("dir") or (1.0, 0.0)
            if (abs(d[0]) > 0.05 and abs(d[1]) > 0.05
                    and size >= WATERMARK_DIAGONAL_MIN_SIZE):
                return f"diagonal text: {text[:40]}"
            col = span.get("color")
            if (size >= WATERMARK_LIGHT_MIN_SIZE
                    and isinstance(col, (list, tuple)) and len(col) == 3):
                r, g, b = (float(x) for x in col)
                if (min(r, g, b) > WATERMARK_LIGHT_MIN_CHANNEL
                        and max(r, g, b) < 1.0):
                    return f"light large text: {text[:40]}"
    return None


# ---------------------------------------------------------------------------
# Parallel execution: every PDF extracts independently in a worker process;
# the caller assembles results in input order so logs stay deterministic.
# OCR events found in workers are merged back so the parent OCR report stays
# complete. resolve_jobs(0/None) = auto = CPU count; 1 = serial.
# ---------------------------------------------------------------------------
def _ocr_worker_init(ocr_enabled):
    OCR.reset(enabled=ocr_enabled)
    if ocr_enabled:
        try:
            OCR.ensure_tesseract()
        except (OSError, subprocess.SubprocessError, RuntimeError) as e:
            OCR.reason = f"tesseract check failed: {e}"


def _strip_self_ref(used, pdf_path):
    """Drop the drawing's own number from its USED ON values.

    The USED ON box sits next to the DRAWING NUMBER box; OCR (and some text
    layers) pick up the drawing's own number, which is never a real parent.
    """
    stem = canonical_stem(Path(pdf_path).name)
    own = stem.split("_")[0] if stem else None
    if not own:
        return used
    return [v for v in used if v != own]


def _strip_self_bom(entries, pdf_path):
    """Drop BOM rows carrying the drawing's own number.

    OCR parts-list reads sometimes pick up the title block's own FERMI number
    as a row; a drawing never lists itself, and a self-entry would make a
    childless leaf look like a root assembly.
    """
    stem = canonical_stem(Path(pdf_path).name)
    own = stem.split("_")[0] if stem else None
    if not own:
        return entries
    return [e for e in entries if e[0] != own]


def _doc_is_scanned(doc):
    """True when the first page has no usable text layer (image-only)."""
    if doc.page_count == 0:
        return False
    return len(doc[0].get_text().strip()) < OCR_MIN_CHARS


def _pdf_size_issue(pdf_path):
    """File-size guard message, or None (stat failures fall through to open)."""
    try:
        size = os.path.getsize(pdf_path)
    except OSError:
        return None
    if size > MAX_PDF_BYTES:
        return (f"skipped: file size {size / (1024 * 1024):.0f}MB exceeds "
                f"{MAX_PDF_BYTES // (1024 * 1024)}MB cap")
    return None


def _doc_pages_issue(doc):
    """Page-count guard message, or None."""
    try:
        count = doc.page_count
    except (RuntimeError, ValueError, AttributeError):
        return None
    if count > MAX_PDF_PAGES:
        return f"skipped: {count} pages exceeds {MAX_PDF_PAGES}-page cap"
    return None


def bom_task(pdf_path):
    before = set(OCR.events)
    try:
        issues = []
        size_msg = _pdf_size_issue(pdf_path)
        if size_msg is not None:
            issues.append(size_msg)
            out = ("ok", [], "skipped", issues, None, None, None, None, False)
        else:
            with _open_pdf(pdf_path) as doc:
                pages_msg = _doc_pages_issue(doc)
                if pages_msg is not None:
                    issues.append(pages_msg)
                    out = ("ok", [], "skipped", issues, None, None, None,
                           None, False)
                else:
                    entries, method = extract_bom_from_doc(doc, issues)
                    entries = _strip_self_bom(entries, pdf_path)
                    watermark = detect_watermark(doc, issues)
                    number, rev = extract_title_block(doc, issues)
                    name = extract_drawing_name(doc)
                    scanned = _doc_is_scanned(doc)
                    out = ("ok", entries, method, issues, watermark, number,
                           rev, name, scanned)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        out = ("error", str(e), None, None, None, None, None, None, None)
    return out + (OCR.event_delta(before),)


def title_task(pdf_path):
    before = set(OCR.events)
    try:
        # No issues slot in this task's return shape; oversized documents
        # surface as error tuples so the run log still shows them.
        size_msg = _pdf_size_issue(pdf_path)
        if size_msg is not None:
            out = ("error", size_msg, None)
        else:
            with _open_pdf(pdf_path) as doc:
                pages_msg = _doc_pages_issue(doc)
                if pages_msg is not None:
                    out = ("error", pages_msg, None)
                else:
                    out = ("ok", _strip_self_ref(extract_used_on(doc), pdf_path),
                           extract_drawing_name(doc))
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        out = ("error", str(e), None)
    return out + (OCR.event_delta(before),)


def org_task(pdf_path):
    before = set(OCR.events)
    try:
        issues = []
        size_msg = _pdf_size_issue(pdf_path)
        if size_msg is not None:
            issues.append(size_msg)
            out = ("ok", [], "skipped", [], issues, None, None, None, None,
                   False)
        else:
            with _open_pdf(pdf_path) as doc:
                pages_msg = _doc_pages_issue(doc)
                if pages_msg is not None:
                    issues.append(pages_msg)
                    out = ("ok", [], "skipped", [], issues, None, None, None,
                           None, False)
                else:
                    entries, method = extract_bom_from_doc(doc, issues)
                    entries = _strip_self_bom(entries, pdf_path)
                    used = _strip_self_ref(extract_used_on(doc), pdf_path)
                    watermark = detect_watermark(doc, issues)
                    number, rev = extract_title_block(doc, issues)
                    name = extract_drawing_name(doc)
                    scanned = _doc_is_scanned(doc)
                    out = ("ok", entries, method, used, issues, watermark,
                           number, rev, name, scanned)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        out = ("error", str(e), None, None, None, None, None, None, None, None)
    return out + (OCR.event_delta(before),)


def dup_meta_task(pdf_path):
    """Watermark + text-layer scan for duplicate candidates (no BOM/USED ON work).

    Returns ("ok", watermark evidence, scanned) where scanned is the
    first-page text-layer check (_doc_is_scanned) - no OCR runs, so this
    stays cheap under run_parallel(..., ocr_enabled=False).
    """
    before = set(OCR.events)
    try:
        # No issues slot in this task's return shape; oversized documents
        # surface as error tuples so the run log still shows them.
        size_msg = _pdf_size_issue(pdf_path)
        if size_msg is not None:
            out = ("error", size_msg, None)
        else:
            with _open_pdf(pdf_path) as doc:
                pages_msg = _doc_pages_issue(doc)
                if pages_msg is not None:
                    out = ("error", pages_msg, None)
                else:
                    out = ("ok", detect_watermark(doc), _doc_is_scanned(doc))
    except (RuntimeError, OSError, ValueError) as e:
        out = ("error", str(e), None)
    return out + (OCR.event_delta(before),)


def resolve_jobs(n=None):
    """Worker count: n > 0 as given; 0/None/invalid = CPU count (4 if unknown)."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return n
    return os.cpu_count() or 4


def run_parallel(task, paths, jobs=0, ocr_enabled=True):
    """Task results (OCR event delta stripped) for every path, in input order.

    jobs <= 1 runs serially in-process; otherwise a process pool. Worker OCR
    events are merged back into the parent OCR context so the OCR report
    stays complete.
    """
    jobs = resolve_jobs(jobs)
    if jobs <= 1:
        return [r[:-1] for r in (task(p) for p in paths)]
    from concurrent.futures import ProcessPoolExecutor
    # Explicit spawn context: fork is unsafe in a multi-threaded parent
    # (DeprecationWarning since 3.10, default changed on newer Python) and the
    # start method would otherwise differ across the 3.10/3.11 CI matrix.
    # Spawn matches the long-standing Windows behavior; worker startup cost is
    # negligible next to OCR.
    with ProcessPoolExecutor(max_workers=jobs,
                             mp_context=multiprocessing.get_context("spawn"),
                             initializer=_ocr_worker_init,
                             initargs=(ocr_enabled,)) as ex:
        raw = list(ex.map(task, paths))
    for r in raw:
        OCR.merge_events(r[-1])
    return [r[:-1] for r in raw]
