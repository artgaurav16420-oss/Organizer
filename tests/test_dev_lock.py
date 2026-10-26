"""The dev lock must match its .in source.

CI installs requirements-dev.txt only, so a bump written into
requirements-dev.in (a ruff or pytest security release) never reaches the lint
and test gates until somebody remembers to recompile. A hand edit of the .txt
is silently reverted by the next compile. Both drifts are invisible to every
other gate, so pin them here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", re.M)


def _pins(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    return {name_: version for name_, version in PIN_RE.findall(text)}


def test_dev_pins_match_the_compiled_lock():
    declared = _pins("requirements-dev.in")
    compiled = _pins("requirements-dev.txt")
    assert declared, "requirements-dev.in has no pins to compare"

    drift = {
        pkg: (version, compiled.get(pkg))
        for pkg, version in declared.items()
        if compiled.get(pkg) != version
    }
    assert not drift, (
        "requirements-dev.in and requirements-dev.txt disagree "
        f"(in, txt): {drift} - recompile with "
        "`uv pip compile requirements-dev.in -o requirements-dev.txt`"
    )


def test_every_dev_pin_is_in_the_lock():
    # A dev tool present in the .in file but absent from the .txt would never
    # be installed by CI, so the gate silently does not run it.
    declared = _pins("requirements-dev.in")
    compiled = _pins("requirements-dev.txt")
    missing = sorted(pkg for pkg in declared if pkg not in compiled)
    assert not missing, f"dev tools missing from the compiled lock: {missing}"
