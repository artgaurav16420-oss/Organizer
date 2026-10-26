"""Read-only output-tree scanning: path helpers, link detection, tree walk.

Split out of ``fsops`` so the presentation layer can ask "what is in this
tree?" without importing the module that performs every destructive tree
operation (and, through it, PyMuPDF and the OCR stack). This module imports
`config` only, and nothing here mutates the filesystem.
"""
import os
from pathlib import Path


def native(path):
    r"""Filesystem-op view of `path`: add the Windows extended prefix when long.

    With LongPathsEnabled off, open()/copy2 on an absolute path >= 260 chars
    fails with ENOENT even though the file exists (input trees nest deep;
    the output tree is bounded by MAX_PATH, so only input-derived paths hit
    this - real run: 6 CHK archive copies failed without the prefix).
    Drive paths get `\\?\`; UNC paths need the `\\?\UNC\server\share` form.
    Forward-slash paths are left alone (`\\?\` rejects them; pathlib on
    Windows always yields backslashes).
    """
    s = os.fspath(path)
    if os.name != "nt" or len(s) < 260 or s.startswith("\\\\?\\"):
        return path
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    if os.path.isabs(s) and "/" not in s:
        return "\\\\?\\" + s
    return path


def islink(path):
    """os.path.islink that sees beyond MAX_PATH (Path.is_symlink goes blind)."""
    return os.path.islink(native(path))


def strip_ext_prefix(path):
    r"""Remove the `\\?\` / `\\?\UNC\` prefix `native` adds to long paths.

    Both sides of the link comparison below go through it: `native` prefixes
    only the side that crosses 260 chars, and `os.path.realpath` keeps the
    prefix, so an unprefixed pair would report the first long directory in a
    tree as a link (and prune its PDFs).
    """
    text = os.fspath(path)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\"):]
    return text


def is_dir_link(path):
    """True for a directory that resolves somewhere other than its own name.

    os.path.islink is False for an NTFS junction (a reparse point, not a
    symlink), and os.walk(followlinks=False) does not prune junctions - so a
    junction inside the input folder was traversed and its PDFs indexed as
    drawings of this folder. Both cases are refused here, before the walk
    descends: the documented invariant is that linked folders are never input.

    Resolution is compared against the *resolved* parent, so an input folder
    that itself sits under a junction (or a mapped share) does not mark every
    child as a link, and a reparse point that is not a redirect (a OneDrive
    placeholder) still resolves beside its parent and is kept.
    """
    if islink(path):
        return True
    try:
        parent = strip_ext_prefix(os.path.realpath(native(Path(path).parent)))
        resolved = strip_ext_prefix(os.path.realpath(native(path)))
    except (OSError, ValueError):
        # Unreadable: let the walk decide (an unreadable entry is skipped by
        # the input index like any other).
        return False
    expected = os.path.join(parent, Path(path).name)
    return os.path.normcase(resolved) != os.path.normcase(expected)


def is_system_dir(name):
    """Convention: any top-level directory whose name starts with '_' is a system dir."""
    return name.startswith("_")


def scan_output_tree(output):
    """Live scan of the output folder, categorized by the system-dir convention.

    Returns {'tree': [pdf paths in the organized tree],
             'sup': [pdf paths under _superseded*],
             'orph': [pdf paths under _orphans*]}.
    Any other '_' prefixed top-level name is excluded from all three lists.
    """
    output = Path(output)
    tree, sup, orph = [], [], []
    if output.is_dir():
        out_root = output
        for dirpath, dirnames, filenames in os.walk(out_root):
            # Prune linked folders for the same reason the input scan does: a
            # junction inside the tree would otherwise list PDFs from outside
            # it as tree members (repair mode moves and deletes from this list).
            dirnames[:] = [d for d in dirnames
                           if not is_dir_link(Path(dirpath) / d)]
            for name in filenames:
                if not name.lower().endswith(".pdf"):
                    continue
                p = Path(dirpath) / name
                top = p.relative_to(out_root).parts[0]
                if is_system_dir(top):
                    if top.startswith("_superseded"):
                        sup.append(p)
                    elif top.startswith("_orphans"):
                        orph.append(p)
                else:
                    tree.append(p)
    return {"tree": tree, "sup": sup, "orph": orph}
