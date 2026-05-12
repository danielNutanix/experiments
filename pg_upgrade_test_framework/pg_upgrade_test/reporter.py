# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Result aggregation and reporting.

Produces three artefacts per run:

  1. ``<results_dir>/results.json`` — full structured results,
  2. ``<results_dir>/matrix.txt``   — compact source-vs-target grid,
  3. a human-readable summary printed to stdout.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Dict, List, Tuple

from .config import SUPPORTED_VERSIONS
from .upgrade import TestResult


LOGGER = logging.getLogger(__name__)

STATUS_GLYPH = {
    "PASSED": "P",
    "FAILED": "F",
    "ERROR":  "E",
    "PENDING": "?",
    "SKIPPED": "-",
}


def write_json_report(results: List[TestResult], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "summary": _summary(results),
        "results": [_serialise(r) for r in results],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    LOGGER.info("Wrote JSON report to %s", path)


def write_matrix_report(results: List[TestResult], path: str,
                        versions: Tuple[int, ...] = SUPPORTED_VERSIONS,
                        ) -> None:
    grid: Dict[Tuple[int, int], str] = {
        (r.pair.source, r.pair.target): r.status for r in results
    }
    lines: List[str] = []
    header = "src\\tgt | " + " ".join(f"{v:>3}" for v in versions)
    lines.append(header)
    lines.append("-" * len(header))
    for src in versions:
        cells = []
        for tgt in versions:
            if src >= tgt:
                cells.append("  .")
            else:
                status = grid.get((src, tgt), "SKIPPED")
                cells.append(f"  {STATUS_GLYPH.get(status, '?')}")
        lines.append(f"{src:>5}   |" + " ".join(cells))
    lines.append("")
    lines.append("Legend: P=passed  F=failed  E=error  -=skipped  .=N/A")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    LOGGER.info("Wrote matrix report to %s", path)


def print_console_summary(results: List[TestResult]) -> None:
    summary = _summary(results)
    print()
    print("=" * 72)
    print("PostgreSQL upgrade compatibility matrix — summary")
    print("=" * 72)
    print(f"  total:  {summary['total']}")
    print(f"  passed: {summary['passed']}")
    print(f"  failed: {summary['failed']}")
    print(f"  error:  {summary['error']}")
    print()
    if summary["failed"] or summary["error"]:
        print("Non-passing pairs:")
        for r in results:
            if r.status not in ("PASSED",):
                print(f"  - {r.pair.label:>14}: {r.status}: {r.failure_reason}")
        print()


# ============================================================
# Internals
# ============================================================

def _summary(results: List[TestResult]) -> Dict[str, int]:
    counts = {"total": len(results), "passed": 0, "failed": 0, "error": 0}
    for r in results:
        if r.status == "PASSED":
            counts["passed"] += 1
        elif r.status == "FAILED":
            counts["failed"] += 1
        elif r.status == "ERROR":
            counts["error"] += 1
    return counts


def _serialise(result: TestResult) -> Dict[str, object]:
    payload = asdict(result)
    if result.validation is not None:
        payload["validation"] = {
            "passed": result.validation.passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in result.validation.checks
            ],
        }
    payload["pair"] = {"source": result.pair.source, "target": result.pair.target,
                       "label": result.pair.label}
    return payload
