"""Fermi PDF organizer package.

Imports pymupdf directly (never the legacy `fitz` shim), so no deprecation
notice can be emitted in any process, including multiprocessing workers.
"""
