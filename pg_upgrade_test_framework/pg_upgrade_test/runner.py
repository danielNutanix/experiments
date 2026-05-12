# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Parallel test matrix runner.

Given a list of ``TestPair`` objects, schedules them across a fixed-size
thread pool. Each pair is a fully isolated workflow (its own clone, IP,
SSH session, snapshot, validation, log collection, delete) so the only
shared state across threads is the ``PrismClient`` (which is itself
backed by a thread-safe ``requests.Session``).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

from .config import FrameworkConfig
from .prism import PrismClient
from .upgrade import TestResult, run_pair


LOGGER = logging.getLogger(__name__)


def run_matrix(cfg: FrameworkConfig) -> List[TestResult]:
    if not cfg.pairs:
        LOGGER.warning("No upgrade pairs requested; nothing to run.")
        return []

    prism = PrismClient(cfg.prism.host, cfg.prism.user, cfg.prism.password,
                        cfg.prism.verify_ssl)

    LOGGER.info("Running %d pair(s) with up to %d in parallel",
                len(cfg.pairs), cfg.max_parallel)

    results: List[TestResult] = []
    with ThreadPoolExecutor(max_workers=cfg.max_parallel,
                            thread_name_prefix="pg-upgrade-test") as pool:
        future_to_pair = {pool.submit(run_pair, cfg, pair, prism): pair
                          for pair in cfg.pairs}
        for future in as_completed(future_to_pair):
            pair = future_to_pair[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — boundary safety
                LOGGER.exception("[%s] Unhandled error in worker", pair.label)
                from .upgrade import TestResult as _TR
                from datetime import datetime, timezone
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                result = _TR(pair=pair, started_at=stamp,
                             finished_at=stamp,
                             status="ERROR",
                             failure_reason=f"{type(exc).__name__}: {exc}")
            LOGGER.info("[%s] -> %s (%.1fs)",
                        pair.label, result.status, result.duration_secs)
            results.append(result)

    results.sort(key=lambda r: (r.pair.source, r.pair.target))
    return results
