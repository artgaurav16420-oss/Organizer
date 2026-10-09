"""Pin-freshness watch: PyMuPDF pin and Tesseract floor vs upstream releases.

Stdlib only (runs on a bare runner python). Prints `pinned=`/`latest=` lines
for the log. Opens a GitHub tracking issue (via `gh`, GH_TOKEN from the
workflow) when a newer release exists and no open `cve-watch` issue already
tracks it, per the standing re-check actions in THIRD_PARTY_NOTICES.md.
Dedupe reads open `cve-watch` issues and matches titles locally (GitHub
search parses version text oddly). Any `gh` failure aborts nonzero (loud red
workflow) instead of risking duplicate or missing issues.
"""
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PIN_RE = re.compile(r"^pymupdf==([^\s#]+)", re.M)
TESS_PIN = "5.5.3"
TESS_API = "https://api.github.com/repos/tesseract-ocr/tesseract/releases/latest"
LABEL = "cve-watch"


def _get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=False)


def _open_watch_titles():
    res = _gh("issue", "list", "--label", LABEL, "--state", "open",
              "--json", "title")
    if res.returncode != 0:
        print(f"issue search failed ({res.returncode}): "
              f"{(res.stderr or '').strip()[:200]}")
        sys.exit(1)
    try:
        return [i["title"] for i in json.loads(res.stdout or "[]")]
    except ValueError:
        print("issue search returned invalid JSON; aborting")
        sys.exit(1)


def _track(title, body):
    res = _gh("issue", "create", "--title", title, "--label", LABEL,
              "--body", body)
    if res.returncode != 0:
        print(f"issue create failed ({res.returncode}): "
              f"{(res.stderr or '').strip()[:200]}")
        sys.exit(1)
    print(f"tracking issue created: {title}")


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check_pymupdf(open_titles):
    pin = PIN_RE.search((ROOT / "requirements.txt").read_text()).group(1)
    latest = _get_json("https://pypi.org/pypi/pymupdf/json")["info"]["version"]
    print(f"pymupdf pinned={pin} latest={latest}")
    if latest == pin:
        print("pymupdf pin is current; nothing to do")
        return
    needle = f"PyMuPDF {latest} released"
    if any(needle in t for t in open_titles):
        print("pymupdf tracking issue already open; nothing to do")
        return
    _track(
        f"PyMuPDF {latest} released - re-check CVE-2026-82035 fix status "
        f"(pin is {pin})",
        ("A newer PyMuPDF release exists on PyPI.\n\n"
         f"- Pinned: `{pin}` (`requirements.txt`)\n"
         f"- Latest: `{latest}`\n\n"
         "Standing action (THIRD_PARTY_NOTICES.md): verify whether this "
         "release contains the CVE-2026-82035 fix commit `b2c8f3a`; if so, "
         "bump `pyproject.toml`, `requirements.txt`, and "
         "`requirements.lock` together and re-run the suite."))


def check_tesseract(open_titles):
    tag = _get_json(TESS_API).get("tag_name", "")
    latest = tag[1:] if tag.startswith("v") else tag
    print(f"tesseract floor={TESS_PIN} latest={latest or '?'}")
    if not latest or _ver_tuple(latest) <= _ver_tuple(TESS_PIN):
        print("tesseract floor is current (or unreadable); nothing to do")
        return
    needle = f"Tesseract {latest} released"
    if any(needle in t for t in open_titles):
        print("tesseract tracking issue already open; nothing to do")
        return
    _track(
        f"Tesseract {latest} released - re-check model-file advisories "
        f"(floor is {TESS_PIN})",
        ("A newer Tesseract release exists upstream.\n\n"
         f"- Floor: `{TESS_PIN}` (README)\n"
         f"- Latest: `{latest}`\n\n"
         "Standing action (THIRD_PARTY_NOTICES.md): verify whether this "
         "release fixes the CVE-2026-88047..-88054 model-file advisories; "
         "if so, raise the floor in README and THIRD_PARTY_NOTICES.md."))


def main():
    open_titles = _open_watch_titles()
    check_pymupdf(open_titles)
    check_tesseract(open_titles)


if __name__ == "__main__":
    main()
