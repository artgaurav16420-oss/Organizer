"""AST lint: no imported name may be unused in first-party production modules.

Dependency-free (stdlib ast). A binding counts as used when its name appears
anywhere as a Load-context Name in the same module (module-level or inside a
function). Wildcard and __future__ imports are compiler directives, skipped.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_MODULES = sorted(
    list((ROOT / "fermi_organizer").glob("*.py"))
    + [ROOT / "organize_fermi_pdfs.py", ROOT / "fermi_report_xlsx.py"]
)

# Only true re-exports would belong here (module -> allowlisted binding names).
ALLOWLIST = {}

# NODOC gate (T-026): public top-level functions without docstrings in
# fermi_organizer/*.py. Production code is frozen, so the 9 currently
# undocumented helpers stay allowlisted; the gate fails on any NEW one.
NODOC_ALLOWLIST = {
    "cli.py:build_parser",
    "cli.py:run_mode_label",
    "config.py:is_fermi_value",
    "extraction.py:bom_task",
    "extraction.py:org_task",
    "extraction.py:title_task",
    "extraction.py:normalize",
    "fsops.py:find_latest_report",
    "fsops.py:pick_shallowest",
}


def _imported_bindings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != "__future__":
                    yield alias.asname or alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module != "__future__":
                for alias in node.names:
                    if alias.name != "*":
                        yield alias.asname or alias.name, node.lineno


def _referenced_names(tree):
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


@pytest.mark.parametrize("path", PRODUCTION_MODULES, ids=lambda p: p.name)
def test_no_dead_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    referenced = _referenced_names(tree)
    allowed = ALLOWLIST.get(path.name, ())
    dead = [f"{path.name}:{lineno} unused import '{name}'"
            for name, lineno in _imported_bindings(tree)
            if name not in referenced and name not in allowed]
    assert not dead, "dead imports found: " + "; ".join(dead)


def _undocumented_public_functions(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            if ast.get_docstring(node) is None:
                out.append(f"{path.name}:{node.name}")
    return out


def test_no_undocumented_public_functions():
    new_undoc = []
    for path in sorted((ROOT / "fermi_organizer").glob("*.py")):
        for item in _undocumented_public_functions(path):
            if item not in NODOC_ALLOWLIST:
                new_undoc.append(item)
    assert not new_undoc, \
        "undocumented public functions found: " + "; ".join(new_undoc)


def test_no_pymupdf_cli_extract_objects_usage():
    """CVE-2026-82035 (GHSA-434w-92hw-f2m3) is a path traversal in the PyMuPDF
    CLI font path (extract_objects() in src/__main__.py); this project uses the
    library only and must never reference that path."""
    for path in PRODUCTION_MODULES:
        text = path.read_text(encoding="utf-8")
        assert "extract_objects" not in text, path.name
        assert "pymupdf.__main__" not in text, path.name
