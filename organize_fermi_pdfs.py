#!/usr/bin/env python3
"""Launcher: python organize_fermi_pdfs.py <folder> [options...]

Thin shim for direct-script use: prepend this directory to sys.path so the
top-level `fermi_report_xlsx` module and the `fermi_organizer` package resolve
when running `python organize_fermi_pdfs.py` from any working directory, then
delegate to the package CLI.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from fermi_organizer.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
