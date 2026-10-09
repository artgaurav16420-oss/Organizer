"""The Tesseract floor must be identical everywhere it is stated.

cve_watch.py compares upstream against TESS_PIN; README promises users a
floor; THIRD_PARTY_NOTICES.md records it. If they drift, the watcher either
cries wolf or stays silent on a real bump.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_tesseract_floor_in_sync():
    script = _read(".github/scripts/cve_watch.py")
    readme = _read("README.md")
    notices = _read("THIRD_PARTY_NOTICES.md")

    script_pin = re.search(r'TESS_PIN = "([^"]+)"', script).group(1)
    readme_floor = re.search(r"(\d+\.\d+\.\d+) or newer recommended",
                             readme).group(1)
    notices_floor = re.search(r"(\d+\.\d+\.\d+)\+ recommended", notices).group(1)

    assert script_pin == readme_floor == notices_floor
