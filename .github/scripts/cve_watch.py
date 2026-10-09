"""Weekly CVE-watch helper: compare the PyMuPDF pin against PyPI latest.

Stdlib only (runs on a bare runner python). Exits 0 in all cases; prints
`pinned=`/`latest=` lines for the log. Opens a GitHub issue (via `gh`,
GH_TOKEN from the workflow) when a newer release exists and no open issue
already tracks it, per the standing re-check action in THIRD_PARTY_NOTICES.md.
"""
import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PIN_RE = re.compile(r"^pymupdf==([^\s#]+)", re.M)


def main():
    pin = PIN_RE.search((ROOT / "requirements.txt").read_text()).group(1)
    with urllib.request.urlopen("https://pypi.org/pypi/pymupdf/json",
                                timeout=30) as resp:
        latest = json.load(resp)["info"]["version"]
    print(f"pinned={pin} latest={latest}")
    if latest == pin:
        print("pin is current; nothing to do")
        return
    title = (f"PyMuPDF {latest} released - re-check CVE-2026-82035 fix status "
             f"(pin is {pin})")
    search = subprocess.run(
        ["gh", "issue", "list", "--state", "open", "--search", title,
         "--json", "number"],
        capture_output=True, text=True, check=False)
    try:
        already = bool(json.loads(search.stdout or "[]"))
    except ValueError:
        already = False
    if already:
        print("tracking issue already open; nothing to do")
        return
    subprocess.run(
        ["gh", "issue", "create", "--title", title, "--label", "cve-watch",
         "--body",
         ("A newer PyMuPDF release exists on PyPI.\n\n"
          f"- Pinned: `{pin}` (`requirements.txt`)\n"
          f"- Latest: `{latest}`\n\n"
          "Standing action (THIRD_PARTY_NOTICES.md): verify whether this "
          "release contains the CVE-2026-82035 fix commit `b2c8f3a`; if so, "
          "bump `pyproject.toml`, `requirements.txt`, and "
          "`requirements.lock` together and re-run the suite.")],
        check=True)
    print("tracking issue created")


if __name__ == "__main__":
    main()
