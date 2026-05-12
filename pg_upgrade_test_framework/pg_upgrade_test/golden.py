# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Golden VM bootstrap for the single-VM, sequential-revert architecture.

The golden VM must already exist on Prism with:

* a supported OS (RHEL family with ``dnf`` or Debian/Ubuntu with ``apt``),
* SSH-reachable as a sudoers user,
* network access to PGDG repositories.

This module:
  1. Powers it on,
  2. Uploads ``install_all_pg.sh`` and ``seed.sql`` to it,
  3. Runs the install script: PGDG repo + every PG major version + seed,
  4. Reads back the manifest of versions that actually installed,
  5. Powers it off cleanly,
  6. Replaces the BASELINE snapshot.

It is idempotent: re-running on an already-bootstrapped golden VM
refreshes only the snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config import FrameworkConfig, PORT_BASE
from .prism import PrismClient, PrismError
from .ssh_exec import SshExecutor, wait_for_ssh


LOGGER = logging.getLogger(__name__)

REMOTE_INSTALL_SCRIPT = "/tmp/install_all_pg.sh"
REMOTE_SEED_SQL = "/tmp/pg_upgrade_seed.sql"
REMOTE_MANIFEST_PATH = "/var/lib/pg_upgrade_test/versions.txt"


@dataclass
class BootstrapStatus:
    vm_name: str
    vm_uuid: str
    snapshot_uuid: Optional[str]
    succeeded: bool
    failure_reason: str = ""
    installed_versions: List[int] = field(default_factory=list)
    skipped_versions: List[int] = field(default_factory=list)


def _power_on_if_needed(prism: PrismClient, vm_uuid: str,
                        vm_name: str, current_state: str) -> None:
    if (current_state or "").lower() != "on":
        LOGGER.info("Powering on '%s'", vm_name)
        prism.power_on(vm_uuid)


def _run_install(ssh: SshExecutor, cfg: FrameworkConfig) -> None:
    ssh.upload(cfg.install_script, REMOTE_INSTALL_SCRIPT, mode=0o755)
    ssh.upload(cfg.seed_sql, REMOTE_SEED_SQL, mode=0o644)
    versions = " ".join(str(v) for v in cfg.versions)
    allow = "1" if cfg.allow_missing_versions else "0"
    cmd = (f"sudo PG_VERSIONS='{versions}' "
           f"PORT_BASE={PORT_BASE} "
           f"SEED_SQL={REMOTE_SEED_SQL} "
           f"ALLOW_MISSING_VERSIONS={allow} "
           f"bash {REMOTE_INSTALL_SCRIPT}")
    ssh.run_streaming(cmd, log_prefix="bootstrap")


def read_installed_versions(ssh: SshExecutor) -> Optional[List[int]]:
    """Return the manifest's installed version list, or ``None`` if absent."""
    rc, out, _ = ssh.run(f"cat {REMOTE_MANIFEST_PATH} 2>/dev/null",
                         check=False)
    if rc != 0 or not out.strip():
        return None
    versions: List[int] = []
    for token in out.split():
        token = token.strip()
        if token.isdigit():
            versions.append(int(token))
    return sorted(set(versions))


def _refresh_snapshot(prism: PrismClient, vm_uuid: str,
                      snapshot_name: str) -> str:
    existing = prism.find_snapshot(vm_uuid, snapshot_name)
    if existing:
        snap_uuid = existing.get("uuid") or existing.get("snapshot_uuid")
        LOGGER.info("Replacing existing snapshot '%s' (%s)",
                    snapshot_name, snap_uuid)
        if snap_uuid:
            prism.delete_snapshot(snap_uuid)
    return prism.create_snapshot(vm_uuid, snapshot_name)


def bootstrap(cfg: FrameworkConfig) -> BootstrapStatus:
    """End-to-end golden VM bootstrap."""
    prism = PrismClient(cfg.prism.host, cfg.prism.user, cfg.prism.password,
                        cfg.prism.verify_ssl)
    vm_name = cfg.golden_vm_name
    LOGGER.info("Bootstrapping golden VM '%s' (allow_missing_versions=%s)",
                vm_name, cfg.allow_missing_versions)

    installed: List[int] = []
    try:
        vm = prism.get_vm_by_name(vm_name)
        vm_uuid = vm["uuid"]
        _power_on_if_needed(prism, vm_uuid, vm_name, vm.get("power_state", ""))

        ip = prism.wait_for_ip(vm_uuid, timeout=cfg.boot_timeout_secs)
        LOGGER.info("Golden VM '%s' reachable at %s", vm_name, ip)
        wait_for_ssh(ip, cfg.ssh.user, cfg.ssh.password,
                     cfg.ssh.key_path, cfg.ssh.port,
                     timeout=cfg.boot_timeout_secs)

        with SshExecutor(ip, cfg.ssh.user, cfg.ssh.password,
                         cfg.ssh.key_path, cfg.ssh.port) as ssh:
            _run_install(ssh, cfg)
            installed = read_installed_versions(ssh) or []

        skipped = sorted(set(cfg.versions) - set(installed))

        LOGGER.info("Powering off '%s' before snapshot", vm_name)
        prism.power_off(vm_uuid)

        snap_uuid = _refresh_snapshot(prism, vm_uuid,
                                      cfg.baseline_snapshot_name)
        LOGGER.info("Snapshot '%s' on '%s' = %s",
                    cfg.baseline_snapshot_name, vm_name, snap_uuid)
        return BootstrapStatus(vm_name=vm_name, vm_uuid=vm_uuid,
                               snapshot_uuid=snap_uuid, succeeded=True,
                               installed_versions=installed,
                               skipped_versions=skipped)

    except (PrismError, RuntimeError) as exc:
        LOGGER.exception("Bootstrap of '%s' failed", vm_name)
        return BootstrapStatus(vm_name=vm_name, vm_uuid="",
                               snapshot_uuid=None, succeeded=False,
                               failure_reason=f"{type(exc).__name__}: {exc}",
                               installed_versions=installed,
                               skipped_versions=[])
