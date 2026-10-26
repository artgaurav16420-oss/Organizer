"""CI workflow guarantees that a YAML reader cannot express.

These are the promises the workflows make in comments: every action is pinned
to a commit SHA, a release cannot publish a wheel from a red commit, and the
scripts that live under .github/ are linted like the rest of the code. A
workflow edit that breaks one of them fails here instead of at release time.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_every_action_is_pinned_to_a_commit_sha():
    # A tag is movable; a SHA is not. `uses: actions/checkout@v7` would let the
    # upstream tag be re-pointed at new code without a commit here.
    uses = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*-?\s*uses:\s*(\S+)", line)
            if match:
                uses.append((path.name, match.group(1)))
    assert uses, "no `uses:` lines found - the regex or the workflows changed"
    unpinned = [
        f"{name}: {ref}" for name, ref in uses
        if not re.search(r"@[0-9a-f]{40}$", ref)
    ]
    assert not unpinned, "actions must be SHA-pinned: " + ", ".join(unpinned)


def test_release_build_needs_a_green_test_job():
    # A tag can point at any commit. Without the needs: edge the build job runs
    # in parallel with pytest and publishes the wheel from a red commit.
    text = _text("release.yml")
    assert re.search(r"^  verify:\s*$", text, re.M), "release.yml has no verify job"
    assert re.search(r"^    needs:\s*verify\s*$", text, re.M), (
        "release.yml's build job must need: verify")
    verify = text.split("  verify:", 1)[1].split("\n  build:", 1)[0]
    assert "python -m pytest" in verify, "the release gate must run the test suite"
    # The build must still be gated, not merely ordered after.
    build = text.split("\n  build:", 1)[1]
    assert re.search(r"^    needs:\s*", build, re.M), "build lost its needs: key"


def test_ci_lints_the_github_scripts():
    # cve_watch.py is shipped CI surface: it used to be in no lint or test
    # gate, so a syntax error merged green and only the Monday cron failed.
    text = _text("pytest.yml")
    ruff_line = next(
        (ln for ln in text.splitlines() if "ruff check" in ln), "")
    assert ".github/scripts" in ruff_line, (
        "the ruff step must include .github/scripts")


def test_packaging_job_exercises_the_excel_extra():
    # The wheel's [excel] extra is what a wheel-only user installs to get the
    # workbook; nothing installed it, so a renamed extra would ship unnoticed.
    text = _text("pytest.yml")
    assert "[excel]" in text, "the packaging job must install the excel extra"
    assert "_OPENPYXL_OK" in text, "the extra must be proven to enable the workbook"
