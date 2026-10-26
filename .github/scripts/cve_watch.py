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
PYPROJECT_PIN_RE = re.compile(r'"pymupdf==([^"]+)"', re.M)
LOCK_PIN_RE = re.compile(r"^pymupdf==([^\s\\]+)", re.M)
OPENPYXL_PIN_RE = re.compile(r"^openpyxl==([^\s#]+)", re.M)
OPENPYXL_PYPROJECT_PIN_RE = re.compile(r'"openpyxl==([^"]+)"', re.M)
OPENPYXL_LOCK_PIN_RE = re.compile(r"^openpyxl==([^\s\\]+)", re.M)
TESS_PIN = "5.5.3"
TESS_API = "https://api.github.com/repos/tesseract-ocr/tesseract/releases/latest"
OSV_API = "https://api.osv.dev/v1/query"
LABEL = "cve-watch"


def _get_json(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.load(resp)


def _post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def advisories_for(package, version):
    """[(id, summary)] for advisories covering package==version, via OSV.

    The version-string diff above only notices a *newer release*. An advisory
    against the pinned version itself (CVE-2026-82035 was exactly that) stayed
    invisible, which is the case this query exists to catch.
    """
    payload = {"package": {"name": package, "ecosystem": "PyPI"},
               "version": version}
    try:
        data = _post_json(OSV_API, payload)
    except (OSError, ValueError) as e:
        print(f"WARNING: OSV query failed for {package} {version}: {e}")
        return []
    out = []
    for vuln in data.get("vulns", []) or []:
        summary = (vuln.get("summary") or vuln.get("details") or "").splitlines()
        out.append((vuln.get("id", "?"), summary[0][:160] if summary else ""))
    return out


def check_advisories(open_titles):
    """Open a tracking issue for any advisory that covers a pinned version."""
    pins = {"pymupdf": _pin(PIN_RE, "requirements.txt"),
            "openpyxl": _pin(OPENPYXL_PIN_RE, "requirements.txt")}
    for package, version in pins.items():
        if not version:
            continue
        for vuln_id, summary in advisories_for(package, version):
            print(f"{package} {version}: advisory {vuln_id} - {summary}")
            needle = f"{package} advisory {vuln_id}"
            if any(needle in t for t in open_titles):
                print(f"{package} advisory {vuln_id} already tracked; nothing to do")
                continue
            _track(
                f"{package} advisory {vuln_id} covers the pin ({version})",
                (f"An OSV advisory covers the pinned version.\n\n"
                 f"- Package: `{package}`\n- Pinned: `{version}`\n"
                 f"- Advisory: `{vuln_id}`\n- Summary: {summary}\n\n"
                 "Standing action: confirm the affected code path is unused "
                 "here (see docs/adr/0003-pymupdf-cve-pin.md for the compensating "
                 "control) or bump/mitigate."))


def _gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True,
                          check=False)


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


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


def _pin(pattern, manifest):
    """Version `pattern` captures in `manifest`, or None when it has no pin."""
    match = pattern.search((ROOT / manifest).read_text())
    return match.group(1) if match else None


def check_pymupdf(open_titles):
    pins = {"requirements.txt": _pin(PIN_RE, "requirements.txt"),
            "pyproject.toml": _pin(PYPROJECT_PIN_RE, "pyproject.toml"),
            "requirements.lock": _pin(LOCK_PIN_RE, "requirements.lock")}
    pin = pins["requirements.txt"]
    if pin is None:
        print("WARNING: pymupdf is not pinned in requirements.txt; "
              "skipping the release check")
        return
    if len(set(pins.values())) != 1:
        print("WARNING: pymupdf pins drift between files: " + repr(pins))
    latest = _get_json("https://pypi.org/pypi/pymupdf/json")["info"]["version"]
    print(f"pymupdf pinned=requirements.txt:{pins['requirements.txt']} "
          f"pyproject.toml:{pins['pyproject.toml']} "
          f"requirements.lock:{pins['requirements.lock']} "
          f"latest={latest}")
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


def check_openpyxl(open_titles):
    pins = {"requirements.txt": _pin(OPENPYXL_PIN_RE, "requirements.txt"),
            "pyproject.toml": _pin(OPENPYXL_PYPROJECT_PIN_RE, "pyproject.toml"),
            "requirements.lock": _pin(OPENPYXL_LOCK_PIN_RE, "requirements.lock")}
    pin = pins["requirements.txt"]
    if pin is None:
        print("WARNING: openpyxl is not pinned in requirements.txt; "
              "skipping the release check")
        return
    if pins["pyproject.toml"] is None:
        print("WARNING: openpyxl not declared in pyproject.toml (Excel extra)")
    elif len(set(pins.values())) != 1:
        print("WARNING: openpyxl pins drift between files: " + repr(pins))
    latest = _get_json("https://pypi.org/pypi/openpyxl/json")["info"]["version"]
    print(f"openpyxl pinned=requirements.txt:{pins['requirements.txt']} "
          f"pyproject.toml:{pins['pyproject.toml']} "
          f"requirements.lock:{pins['requirements.lock']} "
          f"latest={latest}")
    if latest == pin:
        print("openpyxl pin is current; nothing to do")
        return
    needle = f"openpyxl {latest} released"
    if any(needle in t for t in open_titles):
        print("openpyxl tracking issue already open; nothing to do")
        return
    _track(
        f"openpyxl {latest} released - re-check (pin is {pin})",
        ("A newer openpyxl release exists on PyPI.\n\n"
         f"- Pinned: `{pin}` (`requirements.txt`)\n"
         f"- Latest: `{latest}`\n\n"
         "Standing action: verify compatibility with the workbook builder "
         "(`fermi_report_xlsx.py`) and bump pins together if safe."))


def main():
    open_titles = _open_watch_titles()
    check_pymupdf(open_titles)
    check_openpyxl(open_titles)
    check_tesseract(open_titles)
    check_advisories(open_titles)


if __name__ == "__main__":
    main()
