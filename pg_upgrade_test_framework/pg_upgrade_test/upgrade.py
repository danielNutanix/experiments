# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Per-pair upgrade workflow for the single-VM, sequential-revert backend.

For each pair the framework:

  1. Powers off the golden VM,
  2. Restores it from the BASELINE snapshot,
  3. Powers it on and waits for SSH,
  4. Uploads ``pg_upgrade.sh`` and ``validate.sql``,
  5. Runs the in-place upgrade (binaries already installed for both versions),
  6. Runs the validation suite against the upgraded cluster's port,
  7. Collects PostgreSQL logs and pg_upgrade work directory.

The VM itself is never deleted — only reverted between pairs.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .config import FrameworkConfig, TestPair
from .prism import PrismClient, PrismError
from .ssh_exec import SshExecutor, wait_for_ssh
from .validation import ValidationReport, run_full_validation


LOGGER = logging.getLogger(__name__)

REMOTE_UPGRADE_SCRIPT = "/tmp/pg_upgrade.sh"
REMOTE_VALIDATE_SQL = "/tmp/pg_upgrade_validate.sql"


# ============================================================
# Result containers
# ============================================================

@dataclass
class TestResult:
    pair: TestPair
    started_at: str
    finished_at: str = ""
    duration_secs: float = 0.0
    status: str = "PENDING"  # PENDING | PASSED | FAILED | ERROR
    failure_reason: str = ""
    upgrade_log_tail: List[str] = field(default_factory=list)
    validation: Optional[ValidationReport] = None
    artifact_dir: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"


# ============================================================
# Helpers
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_skipped_result(pair: TestPair, reason: str) -> "TestResult":
    """Construct a SKIPPED result for a pair we won't attempt to run."""
    now = _now_iso()
    return TestResult(pair=pair, started_at=now, finished_at=now,
                      status="SKIPPED", failure_reason=reason)


def _detect_os_family(ssh: SshExecutor) -> str:
    rc, _, _ = ssh.run("command -v dnf >/dev/null 2>&1", check=False)
    if rc == 0:
        return "dnf"
    rc, _, _ = ssh.run("command -v apt-get >/dev/null 2>&1", check=False)
    if rc == 0:
        return "apt"
    raise RuntimeError("Could not detect OS family on golden VM")


def _revert_and_boot(prism: PrismClient, vm_uuid: str,
                     snapshot_uuid: str, *, boot_timeout: int) -> str:
    """Power off, restore snapshot, power on, return the learned IP."""
    LOGGER.info("Powering off VM %s before snapshot revert", vm_uuid)
    try:
        prism.power_off(vm_uuid)
    except PrismError as exc:
        LOGGER.debug("Power off returned (likely already off): %s", exc)

    LOGGER.info("Restoring snapshot %s", snapshot_uuid)
    prism.restore_snapshot(vm_uuid, snapshot_uuid)

    LOGGER.info("Powering VM %s back on", vm_uuid)
    prism.power_on(vm_uuid)
    return prism.wait_for_ip(vm_uuid, timeout=boot_timeout)


def _run_upgrade(ssh: SshExecutor, *, local_script: str,
                 pair: TestPair, log_prefix: str) -> Tuple[int, List[str]]:
    ssh.upload(local_script, REMOTE_UPGRADE_SCRIPT, mode=0o755)
    cmd = (f"sudo OLD_VER={pair.source} NEW_VER={pair.target} "
           f"bash {REMOTE_UPGRADE_SCRIPT}")
    return ssh.run_streaming(cmd, check=False, log_prefix=log_prefix)


def _collect_logs(ssh: SshExecutor, *, target_version: int, os_family: str,
                  artifact_dir: str) -> None:
    """Best-effort download of PostgreSQL logs and pg_upgrade workdir."""
    os.makedirs(artifact_dir, exist_ok=True)

    candidates: List[str] = []
    if os_family == "dnf":
        candidates.append(f"/var/lib/pgsql/{target_version}/data/log")
        candidates.append(f"/var/lib/pgsql/{target_version}/initdb.log")
    else:
        candidates.append(f"/var/log/postgresql/postgresql-{target_version}-main.log")

    for remote in candidates:
        local = os.path.join(artifact_dir, os.path.basename(remote))
        try:
            if ssh.download(remote, local):
                LOGGER.debug("Collected %s -> %s", remote, local)
        except Exception as exc:  # noqa: BLE001 — log collection is best-effort
            LOGGER.debug("Skipping %s: %s", remote, exc)

    rc, listing, _ = ssh.run(
        "sudo ls -1 /tmp/pg_upgrade_workdir 2>/dev/null", check=False)
    for fname in listing.splitlines():
        fname = fname.strip()
        if not fname:
            continue
        remote = f"/tmp/pg_upgrade_workdir/{fname}"
        local = os.path.join(artifact_dir, f"pg_upgrade_{fname}")
        try:
            ssh.download(remote, local)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Skipping %s: %s", remote, exc)


# ============================================================
# Per-pair entry point
# ============================================================

def run_pair(cfg: FrameworkConfig, pair: TestPair, prism: PrismClient,
             vm_uuid: str, snapshot_uuid: str) -> TestResult:
    label = pair.label
    started = _now_iso()
    start_t = time.monotonic()
    artifact_dir = os.path.join(cfg.results_dir, label)
    os.makedirs(artifact_dir, exist_ok=True)
    result = TestResult(pair=pair, started_at=started,
                        artifact_dir=artifact_dir)

    try:
        ip = _revert_and_boot(prism, vm_uuid, snapshot_uuid,
                              boot_timeout=cfg.boot_timeout_secs)
        LOGGER.info("[%s] VM reachable at %s", label, ip)
        wait_for_ssh(ip, cfg.ssh.user, cfg.ssh.password,
                     cfg.ssh.key_path, cfg.ssh.port,
                     timeout=cfg.boot_timeout_secs)

        with SshExecutor(ip, cfg.ssh.user, cfg.ssh.password,
                         cfg.ssh.key_path, cfg.ssh.port) as ssh:
            os_family = _detect_os_family(ssh)
            ssh.upload(cfg.validate_sql, REMOTE_VALIDATE_SQL, mode=0o644)

            rc, tail = _run_upgrade(ssh, local_script=cfg.upgrade_script,
                                    pair=pair, log_prefix=label)
            result.upgrade_log_tail = tail
            if rc != 0:
                result.status = "FAILED"
                result.failure_reason = (
                    f"pg_upgrade.sh exited rc={rc}; "
                    f"see upgrade_log_tail in result")
            else:
                target_port = cfg.port_for(pair.target)
                result.validation = run_full_validation(
                    ssh, pair.target, os_family, target_port,
                    REMOTE_VALIDATE_SQL)
                if result.validation.passed:
                    result.status = "PASSED"
                else:
                    failed = [c.name for c in result.validation.checks
                              if not c.passed]
                    result.status = "FAILED"
                    result.failure_reason = (
                        f"validation failed: {', '.join(failed)}")

            _collect_logs(ssh, target_version=pair.target,
                          os_family=os_family, artifact_dir=artifact_dir)

    except (PrismError, RuntimeError) as exc:
        result.status = "ERROR"
        result.failure_reason = f"{type(exc).__name__}: {exc}"
        LOGGER.exception("[%s] Pair errored", label)

    finally:
        result.finished_at = _now_iso()
        result.duration_secs = round(time.monotonic() - start_t, 2)

    return result
