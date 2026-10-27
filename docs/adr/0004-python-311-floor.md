# 4. Python 3.11 floor, and PyMuPDF 2.0 with it

Date: 2026-10-27
Status: Accepted

## Context

Python 3.10 reached end of life on 2026-10-01: it receives no CPython security
fixes, so a tool that advertises `>=3.10` is telling users to run an unsupported
interpreter.

ADR 0003 pinned `pymupdf==1.28.2` — the last release affected by
CVE-2026-82035 — and named two re-open triggers. Trigger 2 ("the 3.10 floor is
dropped") is now being pulled for its own reasons: pymupdf 2.0, the first fixed
release, declares `requires-python >=3.11`.

## Decision

Raise the floor to **Python 3.11** and move to **pymupdf 2.0**:

- `pyproject.toml`: `requires-python = ">=3.11"`, `pymupdf==2.0`.
- CI: Ubuntu 3.11 and 3.12, Windows 3.11. The 3.10 leg is gone.
- `requirements.lock` re-resolved with hashes; the runtime closure grows from
  three distributions to fifteen (pymupdf 2.0 declares nine new mandatory
  dependencies).
- `THIRD_PARTY_NOTICES.md` extended to name every new distribution and its
  licence.

## Consequences

- **No more downgrade side effect.** The wheel no longer forces an exact pin of
  the affected release onto an environment that already has 2.0.
- **Users on 3.10 must upgrade.** 3.10 was EOL, so this is overdue rather than
  early; the packaging metadata now says so.
- **The dependency tree is much bigger.** `onnxruntime` (native, large),
  `numpy`, `networkx`, `protobuf`, `flatbuffers` and four small pure-Python
  packages arrive with PyMuPDF 2.0. That is the real cost of the CVE fix.
- **The layout model is disabled.** pymupdf 2.0 activates an ONNX layout model
  at import and `find_tables()` runs it on every page. Measured on 100 real
  drawings: 55.1 s on 1.28.2, 245.2 s on 2.0, **47.1 s** with
  `pymupdf._get_layout` cleared. `extraction.py` clears it at import (guarded by
  `callable(...)`, since the attribute is private).
- **Text extraction drifts slightly.** Comparing 100 drawings between the two
  engines: 98 identical, one gains a BOM row, one loses one. The difference is
  MuPDF's text geometry, not the layout toggle. Any drawing whose BOM changes
  is surfaced the same way as before — as a placement decision and a missing
  reference — so a re-run after the upgrade is the safe response.

## Alternatives considered

- **Stay on 1.28.2 and keep 3.10.** Rejected: keeps an EOL interpreter and a
  wheel that downgrades 2.0 environments, for a CVE the library surface cannot
  reach.
- **Raise the floor but keep 1.28.2.** Works, but then the CVE pin stays and
  trigger 2 was still consumed; no benefit over moving now.
- **Move to 2.0 and keep the layout model on.** Rejected on measurement: 4.4x
  the extraction cost for table detection this codebase does not need.
