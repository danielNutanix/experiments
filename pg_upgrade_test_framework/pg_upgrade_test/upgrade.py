# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Single (source -> target) upgrade test workflow.

For each pair the framework:

  1. Clones the source golden VM from its BASELINE snapshot,
  2. Powers the clone on and waits for SSH,
  3. Uploads ``pg_upgrade.sh`` and runs it remotely,
  4. Runs the validation suite against the upgraded cluster,
  5. Collects PostgreSQL logs and pg_upgrade logs,
  6. Powers off and deletes the clone (always — even on failure).

The clone naming convention embeds a UTC timestamp + random suffix so
parallel runs never collide.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .config import FrameworkConfig, TestPair
from .golden import find_baseline_snapshot
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
    clone_vm_name: str = ""
    clone_vm_uuid: str = ""
    clone_vm_ip: str = ""
    upgrade_log_tail: List[str] = field(default_factory=list)
    validation: Optional[ValidationReport] = None
    artifact_dir: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "PASSED"


# ============================================================
# Per-pair workflow
# ============================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clone_name(pair: TestPair) -> str:
    suffix = secrets.token_hex(3)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"pgtest_{pair.label}_{stamp}_{suffix}"


def _detect_os_family(ssh: SshExecutor) -> str:
    rc, _, _ = ssh.run("command -v dnf >/dev/null 2>&1", check=False)
    if rc == 0:
        return "rhel"
    rc, _, _ = ssh.run("command -v apt-get >/dev/null 2>&1", check=False)
    if rc == 0:
        return "debian"
    raise RuntimeError("Could not detect OS family on cloned VM")


def _run_upgrade(ssh: SshExecutor, *, local_script: str,
                 pair: TestPair, log_prefix: str,
                 timeout: int) -> Tuple[int, List[str]]:
    ssh.upload(local_script, REMOTE_UPGRADE_SCRIPT, mode=0o755)
    cmd = (f"sudo OLD_VER={pair.source} NEW_VER={pair.target} "
           f"bash -s < {REMOTE_UPGRADE_SCRIPT}")
    return ssh.run_streaming(cmd, check=False, log_prefix=log_prefix)


def _collect_logs(ssh: SshExecutor, *, target_version: int, os_family: str,
                  artifact_dir: str) -> None:
    """Pull pg_upgrade.sh logs and the new server log into ``artifact_dir``."""
    os.makedirs(artifact_dir, exist_ok=True)

    # Discover BASE_PGDATA for the upgraded cluster
    rc, out, _ = ssh.run(
        "sudo -u postgres psql -tA -c 'SHOW data_directory;' 2>/dev/null "
        "|| true", check=False)
    pgdata = out.strip()

    candidates = []
    if pgdata:
        candidates.append(f"{pgdata}/server.log")
        candidates.append(f"{pgdata}/log")
    parent = pgdata.rsplit("/", 1)[0] if pgdata else ""
    if parent:
        candidates.append(f"{parent}/PG{target_version}/server.log")
    candidates.extend([
        f"/var/log/postgresql/postgresql-{target_version}-main.log",
        "/tmp/pg_upgrade_internal.log",
    ])

    # Best-effort: copy each candidate down if it exists.
    for remote in candidates:
        local = os.path.join(artifact_dir, os.path.basename(remote))
        try:
            if ssh.download(remote, local):
                LOGGER.debug("Collected %s -> %s", remote, local)
        except Exception as exc:  # noqa: BLE001 — log collection is best-effort
            LOGGER.debug("Skipping %s: %s", remote, exc)

    # pg_upgrade emits artefacts in the *new* PGDATA's pg_upgrade_*/ dir.
    rc, out, _ = ssh.run(
        f"sudo find / -maxdepth 6 -type d -name 'pg_upgrade_*' 2>/dev/null "
        "| head -5", check=False)
    for remote_dir in out.splitlines():
        remote_dir = remote_dir.strip()
        if not remote_dir:
            continue
        rc, listing, _ = ssh.run(
            f"sudo ls -1 {remote_dir} 2>/dev/null", check=False)
        for fname in listing.splitlines():
            fname = fname.strip()
            if not fname:
                continue
            remote = f"{remote_dir}/{fname}"
            local = os.path.join(artifact_dir,
                                 f"pg_upgrade_{os.path.basename(remote_dir)}_{fname}")
            try:
                ssh.download(remote, local)
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("Skipping %s: %s", remote, exc)


def _destroy_clone(prism: PrismClient, vm_uuid: str, vm_name: str) -> None:
    if not vm_uuid:
        return
    try:
        prism.power_off(vm_uuid)
    except PrismError as exc:
        LOGGER.warning("Power off of %s failed (will still try delete): %s",
                       vm_name, exc)
    try:
        prism.delete_vm(vm_uuid)
        LOGGER.info("Deleted clone %s (%s)", vm_name, vm_uuid)
    except PrismError as exc:
        LOGGER.error("Delete of clone %s failed: %s", vm_name, exc)


def run_pair(cfg: FrameworkConfig, pair: TestPair,
             prism: PrismClient) -> TestResult:
    label = pair.label
    started = _now_iso()
    start_t = time.monotonic()
    artifact_dir = os.path.join(cfg.results_dir, label)
    os.makedirs(artifact_dir, exist_ok=True)
    result = TestResult(pair=pair, started_at=started,
                        artifact_dir=artifact_dir)

    clone_name = _clone_name(pair)
    result.clone_vm_name = clone_name
    clone_uuid = ""
    try:
        snap_uuid = find_baseline_snapshot(prism, cfg, pair.source)
        LOGGER.info("[%s] Cloning PG%d golden snapshot %s -> %s",
                    label, pair.source, snap_uuid, clone_name)
        clone_uuid = prism.clone_vm_from_snapshot(snap_uuid,
                                                  clone_name=clone_name)
        result.clone_vm_uuid = clone_uuid

        prism.power_on(clone_uuid)
        ip = prism.wait_for_ip(clone_uuid, timeout=cfg.boot_timeout_secs)
        result.clone_vm_ip = ip
        LOGGER.info("[%s] Clone reachable at %s", label, ip)

        wait_for_ssh(ip, cfg.ssh.user, cfg.ssh.password,
                     cfg.ssh.key_path, cfg.ssh.port,
                     timeout=cfg.boot_timeout_secs)

        with SshExecutor(ip, cfg.ssh.user, cfg.ssh.password,
                         cfg.ssh.key_path, cfg.ssh.port) as ssh:
            os_family = _detect_os_family(ssh)
            ssh.upload(cfg.validate_sql, REMOTE_VALIDATE_SQL, mode=0o644)

            rc, tail = _run_upgrade(ssh, local_script=cfg.upgrade_script,
                                    pair=pair, log_prefix=label,
                                    timeout=cfg.upgrade_timeout_secs)
            result.upgrade_log_tail = tail
            if rc != 0:
                result.status = "FAILED"
                result.failure_reason = (
                    f"pg_upgrade.sh exited rc={rc}; "
                    f"see upgrade_log_tail in result")
            else:
                result.validation = run_full_validation(
                    ssh, pair.target, os_family, REMOTE_VALIDATE_SQL)
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
        LOGGER.exception("[%s] Test errored", label)

    finally:
        _destroy_clone(prism, clone_uuid, clone_name)
        result.finished_at = _now_iso()
        result.duration_secs = round(time.monotonic() - start_t, 2)

    return result
