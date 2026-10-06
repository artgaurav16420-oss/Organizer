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
