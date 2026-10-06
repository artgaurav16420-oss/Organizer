# Codebase Health Report — PDF Organizer

> **SUPERSEDED — 2026-09-30 (re-review #1).** Most findings below are already fixed. Current state: YELLOW, 0 CRITICAL / 6 HIGH / 20 MEDIUM / 13 LOW. Historical snapshot only — verify against code before acting.

_Generated 2026-09-30 via Complete Codebase Review (7 specialist agents + Devil's Advocate + Roadmap)._
_Target: `D:\Software Development\PDF Organizer`_

## Executive Summary

- **Overall Health**: YELLOW
- **Codebase Size**: ~2,800 LOC, 8 Python files, 7 modules
- **Critical Issues**: 1 (P0 OCR crash)
- **Tech Debt**: 6.5h (Phase 1) / ~35h (full backlog)
- **Priority Areas**: Code Quality (runtime crash + complexity), Architecture (duplication + god module), Process Quality (no tests)

## Per-Domain Scores

| Domain | Score (/10) | Critical | High | Medium | Low |
|--------|-------------|----------|------|--------|-----|
| Architecture | 5 | 0 | 2 | 4 | 3 |
| Code Quality | 4 | 1 | 3 | 8 | 3 |
| Security | 7 | 0 | 0 | 1 | 3 |
| Tech Debt | 4 | 0 | 3 | 5 | 8 |
| Dependencies | 5 | 0 | 2 | 1 | 3 |
| Documentation | 3 | 0 | 3 | 1 | 4 |
| Process Quality | 4 | 0 | 2 | 3 | 4 |
| **Overall** | **4.6** | **1** | **15** | **23** | **28** |

## Detailed Findings

### Code Quality

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| Missing `datetime` import — NameError kills any OCR run (extraction.py:246,270) | CRITICAL | 0.5h | CONFIRMED |
| `extract_bom_from_text` cyclomatic complexity ~40+ (extraction.py:335-502) | HIGH | 2h | CONFIRMED |
| `run_incremental` 505 lines, complexity ~30+ (runmodes.py:284-789) | HIGH | 2h | CONFIRMED |
| `_extract_bom_positional` complexity ~25+ (extraction.py:505-598) | HIGH | 2h | CONFIRMED |
| Dead code: `NAVY_F if False else GRN_F` (fermi_report_xlsx.py:217; `NAVY_F` undefined) | MEDIUM | 0.5h | CONFIRMED |
| Dead code: config.py OCR state never imported (config.py:32-36) | MEDIUM | 1h | CONFIRMED |
| Duplication: Format 4 vs `_extract_bom_positional` (~80 lines shared logic) | MEDIUM | 2h | CONFIRMED |
| Duplication: BFS reachability check (runmodes.py:136-147, 632-643) | MEDIUM | 1h | CONFIRMED |
| Duplication: CHK reporting block ×3 (runmodes.py:93-98, 358-363, 414-419) | MEDIUM | 1h | CONFIRMED |
| Silent `except Exception: pass` (fermi_report_xlsx.py:86-87, report_glue.py:147-148, runmodes.py:347-348) | MEDIUM | 3h | CONFIRMED |
| Global `warnings.filterwarnings("ignore")` (cli.py:25) | LOW | 1h | CONFIRMED |
| `QTY_RE` and `ITEM_RE` identical (config.py:37-38); naming/style issues | LOW | 1h | CONFIRMED |

### Architecture

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| 4 functions duplicated verbatim graph.py ↔ runmodes.py (`find_organized_pdfs`, `pick_shallowest`, `find_latest_report`, `copy_superseded`) | HIGH | 4h | CONFIRMED |
| Log-as-API: report_glue.py regex-parses human-readable log text into Excel context | HIGH | 4h | CONFIRMED |
| OCR runtime state duplicated config.py ↔ extraction.py (two sources of truth) | MEDIUM | 1.5h | CONFIRMED |
| cli.py directly imports external top-level `fermi_report_xlsx` module | MEDIUM | 1h | CONFIRMED |
| runmodes.py god module (789 lines; full + incremental + supersede + orphan logic) | MEDIUM | 4h | CONFIRMED |
| graph.py mixes pure graph algorithms with filesystem side effects | MEDIUM | 2h | CONFIRMED |
| config.py mixes constants with mutable runtime state | MEDIUM | 1h | CONFIRMED |

### Security

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| `shutil.copy2` / `shutil.move` follow symlinks, no guard (graph.py, runmodes.py) | MEDIUM | 1h | PLAUSIBLE |
| No dependency version pinning (PyMuPDF has known CVEs, e.g. CVE-2023-30565) — **CORRECTED 2026-09-30**: CVE-2023-30565 is unrelated to PyMuPDF (Becton Dickinson CQI Reporter). Current PyMuPDF advisory tracking: CVE-2026-82035 is CLI-path-only / unreachable here; no fixed release as of 2026-09-30 | LOW | 0.5h | CONFIRMED |
| `TESSERACT_EXE` env var trusts arbitrary executable | LOW | — | PLAUSIBLE |
| Positive: subprocess list-form (no `shell=True`), path sanitization, atomic `os.replace`, correct `tempfile` use | INFO | — | CONFIRMED |

### Tech Debt

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| No test coverage (6 modules; extraction/graph logic highly testable) | HIGH | 24h+ | CONFIRMED |
| `fitz.TOOLS.mupdf_display_errors(False)` called per-document, comment says "call once" | MEDIUM | 1h | CONFIRMED |
| Hardcoded magic numbers throughout OCR geometry (40, 340, 80, 360, 135, …) | MEDIUM | 1h | CONFIRMED |
| History cap `200` hardcoded (fermi_report_xlsx.py:85, 265) | MEDIUM | 0.5h | CONFIRMED |
| Iteration cap `200` in `build_folder_names` (graph.py:68) | MEDIUM | 0.5h | CONFIRMED |
| Review markers (`LOW #n`, `MEDIUM #n`) left in code; `sys.path` shim; `os.environ["PATH"]` mutation; `id()` cache keys | LOW | 3h | CONFIRMED |

### Dependencies

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| No dependency declaration file (no requirements.txt / pyproject.toml / setup.py) | HIGH | 0.5h | CONFIRMED |
| PyMuPDF hard undeclared dependency, no fallback, no minimum version | HIGH | 1h | CONFIRMED |
| No version pinning anywhere (PyMuPDF table/OCR APIs drift between minors) — **FIXED 2026-09-30**: requirements.txt now pins `pymupdf==1.28.2` and `openpyxl==3.1.5` | MEDIUM | 0.5h | CONFIRMED |
| Tesseract undeclared external binary; no ≥5.0 version check | MEDIUM | 1h | CONFIRMED |
| openpyxl optional-but-undeclared; no lock file; no Python version constraint | LOW | 1h | CONFIRMED |

### Documentation

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| No README.md (no project description, quickstart, feature list) | HIGH | 2h | CONFIRMED |
| No setup/installation instructions | HIGH | 1h | CONFIRMED |
| No usage examples for CLI flags | HIGH | 1h | CONFIRMED |
| No API docs (`build_workbook` ctx dict has 10+ undocumented keys) | MEDIUM | 2.5h | CONFIRMED |
| No architecture/design doc (BOM→graph→placement, OCR strategy, cycle-breaking) | MEDIUM | 2h | CONFIRMED |
| Missing docstrings on `run_full`, `run_incremental`, `place_files`, `build_pdf_index`, BOM extractors | MEDIUM | 1h | CONFIRMED |
| No CHANGELOG / CONTRIBUTING / .gitignore (`__pycache__/` committed) | LOW | 1h | CONFIRMED |

### Process Quality (Karpathy Compliance: 4/10)

| Finding | Severity | Est. Hours | DA Verdict |
|---------|----------|------------|------------|
| No automated test suite (RULE_4; manual `--dry-run` is not verification) | HIGH | 24h+ | CONFIRMED |
| Log format as de facto API (RULE_2/RULE_6; fragile coupling) | HIGH | 4h | CONFIRMED |
| Excel subsystem 462 lines for non-core reporting (YAGNI) | MEDIUM | 4h | PLAUSIBLE |
| OCR fallback ~250 lines rivalling primary path (YAGNI) | MEDIUM | 4h | PLAUSIBLE |
| Incremental mode 500+ lines of edge cases (YAGNI) | MEDIUM | 4h | PLAUSIBLE |
| Overlapping FERMI regexes (8+ patterns); patch-comment accumulation | LOW | 2h | CONFIRMED |

### DA Escalations (found by Devil's Advocate, missed by specialists)

| Finding | Severity | DA Verdict |
|---------|----------|------------|
| P0 crash framing: any scanned PDF + working Tesseract aborts the whole run (`NameError: datetime`) | CRITICAL | CONFIRMED |
| `cli.py:51` `_OCR_STATE.get('path','')` — key `path` never set, "tesseract: …" log line always blank, hiding misconfiguration | HIGH | CONFIRMED |
| Unbounded recursion (`break_cycles→dfs`, `place_files→dfs`, `count_copies`, `mark_reachable`) — deep/degenerate DAG → `RecursionError`, no depth guard | HIGH | CONFIRMED |
| Shell injection via TESSERACT_EXE | — | REJECTED (list-form calls, `os.path.isfile` gate — env-controlled exe by design) |

## Cross-Domain Impact

| Root cause | Domains | Note |
|------------|---------|------|
| graph.py ↔ runmodes.py duplication | Architecture, Code Quality, Tech Debt, Process Quality | Single fix resolves 4 agents' findings |
| Log-as-API | Architecture, Process Quality | Log wording change silently breaks workbook |
| No tests | Tech Debt, Process Quality | Blocks safe refactoring of everything else |
| No dependency declaration | Dependencies, Security, Process Quality | Supply-chain risk + reproducibility gap |
| OCR state duplication | Architecture, Process Quality | Two sources of truth |
| Warning suppression | Code Quality, Security | Masks security-relevant warnings |

## Improvement Roadmap

### Phase 1 — Now (6.5h)

| Task | Item | Hours |
|------|------|-------|
| T-001 | Fix missing `datetime` import (P0 OCR crash) — extraction.py | 0.5h |
| T-002 | Fix `cli.py:51` never-set `'path'` key (blank Tesseract log line) | 1h |
| T-003 | Minimal recursion depth guard in dfs / place_files / count_copies / mark_reachable — **PARTIALLY FIXED**: recursion guards added for break_cycles/place_files; count_copies/mark_reachable still unguarded (re-review F-38) | 1.5h |
| T-004 | Remove dead `NAVY_F if False` branch — fermi_report_xlsx.py:217 | 0.5h |
| T-005 | Scope global `warnings.filterwarnings("ignore")` — cli.py:25 | 1h |
| T-006 | Add requirements.txt (pin PyMuPDF, openpyxl) | 0.5h |
| T-007 | Single-source OCR state (config XOR extraction) | 1.5h |

### Phase 2 — Next (17h)

| Task | Item | Hours |
|------|------|-------|
| T-008 | Consolidate 4 duplicated functions into graph.py | 4h |
| T-009 | Split `run_incremental` (~505L) + `extract_bom_from_text` (~168L) | 6h |
| T-010 | Decouple log-as-API (structured context object + log-format compat shim) | 5h |
| T-011 | Iterative DFS/placement rewrite if guard proves insufficient | 2h |

### Phase 3 — Backlog (10.5h min / 38.5h full)

| Task | Item | Hours |
|------|------|-------|
| T-012 | README.md (run, flags, output tree) | 1.5h |
| T-013 | `shutil` symlink guard | 1h |
| T-014 | Minimal test harness (BOM fixture + dry-run smoke) | 8h |
| T-015 | Full per-module coverage (aspirational) | 28h |

## Tech Debt Summary

- **Total estimated**: 6.5h (Phase 1) / ~35h (Phases 1+2+3-min) / ~63h (incl. full coverage)
- **Trend**: First baseline — no trend data (baseline saved to `.code-review-cache/ccr-baseline.json` in system temp dir)

## Agent Status

- Completed: 7/7 specialists + synthesis + DA + roadmap
- Excluded dimensions (not applicable): Test Health (no tests), Build & CI (no CI), Performance, Database, UI/UX, DevOps
- Report verified by devil's advocate: 11 CONFIRMED, 1 PLAUSIBLE, 1 REJECTED + 3 DA-ESCALATIONS (2 confirmed as new findings)
