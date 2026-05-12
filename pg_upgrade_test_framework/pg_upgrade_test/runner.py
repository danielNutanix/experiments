# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Sequential matrix runner.

Single golden VM means single-stream execution. Pairs are scheduled in
ascending ``(source, target)`` order so the matrix file fills top-down.

Before running anything the runner probes the golden VM for the
``versions.txt`` manifest left by ``install_all_pg.sh`` (when
``--allow-missing-versions`` was used during bootstrap). Pairs that
reference a version not in the manifest are reported as ``SKIPPED``
rather than executed — saving a wasted revert+boot cycle.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .config import FrameworkConfig, TestPair
from .golden import read_installed_versions
from .prism import PrismClient, PrismError
from .ssh_exec import SshExecutor, wait_for_ssh
from .upgrade import TestResult, make_skipped_result, run_pair


LOGGER = logging.getLogger(__name__)


def _resolve_baseline(prism: PrismClient, cfg: FrameworkConfig
                      ) -> Tuple[str, str]:
    vm = prism.get_vm_by_name(cfg.golden_vm_name)
    snap = prism.find_snapshot(vm["uuid"], cfg.baseline_snapshot_name)
    if not snap:
        raise PrismError(
            f"No '{cfg.baseline_snapshot_name}' snapshot on "
            f"'{cfg.golden_vm_name}'. Run `pg-upgrade-test bootstrap` first.")
    snap_uuid = snap.get("uuid") or snap.get("snapshot_uuid")
    if not snap_uuid:
        raise PrismError(f"Snapshot lookup returned no UUID: {snap}")
    return vm["uuid"], snap_uuid


def _probe_manifest(prism: PrismClient, cfg: FrameworkConfig,
                    vm_uuid: str) -> Optional[List[int]]:
    """Best-effort: power on (if needed), SSH in, read the manifest."""
    try:
        vm = prism.get_vm(vm_uuid, include_disks=False, include_nics=False)
        state = (vm.get("power_state") or "").lower()
        if state != "on":
            LOGGER.info("Probe: powering on golden VM to read manifest")
            prism.power_on(vm_uuid)
        ip = prism.wait_for_ip(vm_uuid, timeout=cfg.boot_timeout_secs)
        wait_for_ssh(ip, cfg.ssh.user, cfg.ssh.password,
                     cfg.ssh.key_path, cfg.ssh.port,
                     timeout=cfg.boot_timeout_secs)
        with SshExecutor(ip, cfg.ssh.user, cfg.ssh.password,
                         cfg.ssh.key_path, cfg.ssh.port) as ssh:
            return read_installed_versions(ssh)
    except (PrismError, RuntimeError) as exc:
        LOGGER.warning("Could not probe installed-versions manifest: %s", exc)
        return None


def _split_runnable(pairs: List[TestPair],
                    installed: Optional[List[int]],
                    ) -> Tuple[List[TestPair], List[TestResult]]:
    """Split requested pairs into (runnable, pre-built SKIPPED results)."""
    if installed is None:
        return pairs, []
    inst = set(installed)
    runnable: List[TestPair] = []
    skipped: List[TestResult] = []
    for pair in pairs:
        missing = sorted({pair.source, pair.target} - inst)
        if not missing:
            runnable.append(pair)
        else:
            reason = (f"unavailable PG version(s) on golden VM: "
                      f"{', '.join(str(v) for v in missing)}")
            LOGGER.info("[%s] SKIPPED — %s", pair.label, reason)
            skipped.append(make_skipped_result(pair, reason))
    return runnable, skipped


def run_matrix(cfg: FrameworkConfig) -> List[TestResult]:
    if not cfg.pairs:
        LOGGER.warning("No upgrade pairs requested; nothing to run.")
        return []

    prism = PrismClient(cfg.prism.host, cfg.prism.user, cfg.prism.password,
                        cfg.prism.verify_ssl)
    vm_uuid, snapshot_uuid = _resolve_baseline(prism, cfg)
    LOGGER.info("Using golden VM '%s' (uuid=%s) snapshot %s",
                cfg.golden_vm_name, vm_uuid, snapshot_uuid)

    installed = _probe_manifest(prism, cfg, vm_uuid)
    if installed is not None:
        LOGGER.info("Golden VM advertises installed versions: %s",
                    ", ".join(str(v) for v in installed))
    else:
        LOGGER.info("No installed-versions manifest found; running every "
                    "requested pair (failures will surface at upgrade time).")

    pairs_sorted = sorted(cfg.pairs, key=lambda p: (p.source, p.target))
    runnable, skipped_results = _split_runnable(pairs_sorted, installed)
    LOGGER.info("Pairs: %d requested, %d runnable, %d skipped",
                len(pairs_sorted), len(runnable), len(skipped_results))

    results: List[TestResult] = list(skipped_results)
    for index, pair in enumerate(runnable, start=1):
        LOGGER.info("[%d/%d] starting pair %s", index, len(runnable),
                    pair.label)
        result = run_pair(cfg, pair, prism, vm_uuid, snapshot_uuid)
        LOGGER.info("[%d/%d] %s -> %s (%.1fs)",
                    index, len(runnable), pair.label,
                    result.status, result.duration_secs)
        results.append(result)

    results.sort(key=lambda r: (r.pair.source, r.pair.target))
    return results
