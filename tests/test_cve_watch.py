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


def _pins(name, patterns):
    """{manifest: version} for `name`; a manifest that omits the pin maps None."""
    found = {}
    for manifest, pattern in patterns.items():
        match = re.search(pattern, _read(manifest), re.M)
        found[manifest] = match.group(1) if match else None
    return found


def test_dependency_pins_agree_across_manifests():
    """requirements.txt, pyproject.toml and the lock must pin the same release.

    CI installs only the hashed lock, so a manifest-only bump (dependabot's
    default) tests the old version and merges drift.
    """
    for name in ("pymupdf", "openpyxl"):
        pins = _pins(name, {
            "requirements.txt": rf"^{name}==([^\s#]+)",
            "pyproject.toml": rf'"{name}==([^"]+)"',
            "requirements.lock": rf"^{name}==([^\s\\]+)",
        })
        missing = sorted(f for f, v in pins.items() if v is None)
        assert not missing, f"{name} is not pinned in {missing}"
        assert len(set(pins.values())) == 1, f"{name} pins drift: {pins}"


def _load_watch():
    """Import .github/scripts/cve_watch.py as a module."""
    import importlib.util
    import sys

    path = ROOT / ".github" / "scripts" / "cve_watch.py"
    spec = importlib.util.spec_from_file_location("cve_watch", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cve_watch"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cve_watch_parses_and_runs_with_stubbed_network():
    # The watcher used to be a file no test ever imported: a syntax error or a
    # refactor break merged green and only the Monday cron failed. Import it and
    # run every check against stubbed endpoints.
    watch = _load_watch()
    tracked = []
    watch._open_watch_titles = lambda: []
    watch._track = lambda title, body: tracked.append(title)
    watch._get_json = lambda url: (
        # A newer release than whatever is pinned: the drift path must stay
        # exercised after a pin bump, so do not hard-code the current pin.
        {"info": {"version": "99.0"}} if "pymupdf" in url
        else {"info": {"version": "3.1.5"}} if "openpyxl" in url
        else {"tag_name": "5.5.3"})
    watch._post_json = lambda url, payload: {"vulns": [
        {"id": "OSV-TEST-1", "summary": "test advisory"}]}
    watch.main()
    # pinned pymupdf vs latest 99.0 -> pin-drift issue; openpyxl current; OSV hits.
    assert any("PyMuPDF 99.0" in t for t in tracked)
    assert any("OSV-TEST-1" in t for t in tracked)
    assert not any("openpyxl 3.1.5 released" in t for t in tracked)


def test_advisory_query_is_best_effort():
    # A failed OSV call warns and returns [], it never aborts the watch.
    watch = _load_watch()

    def boom(url, payload):
        raise OSError("network down")

    watch._post_json = boom
    assert watch.advisories_for("pymupdf", "1.28.2") == []
