# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Golden VM bootstrap.

Each golden VM must already exist on Prism with:
  * the chosen base OS (RHEL family or Debian/Ubuntu),
  * the matching PostgreSQL major version installed and running,
  * an SSH-reachable user with passwordless sudo.

This module:
  1. Connects to the golden VM,
  2. Uploads ``seed.sql`` and runs it as the postgres user,
  3. Powers the VM off cleanly,
  4. Takes the BASELINE snapshot used by all subsequent test clones.

It is idempotent: re-running on an already-seeded golden VM only refreshes
the snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from .config import FrameworkConfig
from .prism import PrismClient, PrismError
from .ssh_exec import SshExecutor, wait_for_ssh
from .validation import _psql_path  # internal but stable enough for our use


LOGGER = logging.getLogger(__name__)


@dataclass
class GoldenVmStatus:
    version: int
    vm_name: str
    vm_uuid: str
    snapshot_uuid: str
    seeded: bool


def _detect_os_family(ssh: SshExecutor) -> str:
    rc, _, _ = ssh.run("command -v dnf >/dev/null 2>&1", check=False)
    if rc == 0:
        return "rhel"
    rc, _, _ = ssh.run("command -v apt-get >/dev/null 2>&1", check=False)
    if rc == 0:
        return "debian"
    raise RuntimeError("Could not detect OS family on golden VM (no dnf or apt-get)")


def _seed_database(ssh: SshExecutor, version: int, os_family: str,
                   local_seed_sql: str) -> None:
    remote_path = "/tmp/pg_upgrade_test_seed.sql"
    ssh.upload(local_seed_sql, remote_path, mode=0o644)
    psql = _psql_path(version, os_family)
    cmd = (f"sudo -u postgres {psql} -d postgres -X "
           f"-v ON_ERROR_STOP=1 -f {remote_path}")
    ssh.run_streaming(cmd, log_prefix=f"seed:pg{version}")
    ssh.run(f"rm -f {remote_path}", check=False)


def _refresh_snapshot(prism: PrismClient, vm_uuid: str,
                      snapshot_name: str) -> str:
    """Replace any pre-existing snapshot of the same name."""
    existing = prism.find_snapshot(vm_uuid, snapshot_name)
    if existing:
        snap_uuid = existing.get("uuid") or existing.get("snapshot_uuid")
        LOGGER.info("Replacing existing snapshot '%s' (%s)",
                    snapshot_name, snap_uuid)
        if snap_uuid:
            prism.delete_snapshot(snap_uuid)
    return prism.create_snapshot(vm_uuid, snapshot_name)


def bootstrap_one(cfg: FrameworkConfig, version: int,
                  prism: PrismClient) -> GoldenVmStatus:
    """Seed and snapshot a single golden VM."""
    vm_name = cfg.golden_vm_name(version)
    LOGGER.info("Bootstrapping golden VM '%s' for PG%d", vm_name, version)

    vm = prism.get_vm_by_name(vm_name)
    vm_uuid = vm["uuid"]

    if (vm.get("power_state") or "").lower() != "on":
        LOGGER.info("Powering on '%s'", vm_name)
        prism.power_on(vm_uuid)

    ip = prism.wait_for_ip(vm_uuid)
    LOGGER.info("Golden VM '%s' reachable at %s", vm_name, ip)

    wait_for_ssh(ip, cfg.ssh.user, cfg.ssh.password,
                 cfg.ssh.key_path, cfg.ssh.port,
                 timeout=cfg.boot_timeout_secs)

    with SshExecutor(ip, cfg.ssh.user, cfg.ssh.password,
                     cfg.ssh.key_path, cfg.ssh.port) as ssh:
        os_family = _detect_os_family(ssh)
        LOGGER.info("Golden VM '%s' OS family: %s", vm_name, os_family)
        _seed_database(ssh, version, os_family, cfg.seed_sql)

    LOGGER.info("Powering off '%s' before snapshot", vm_name)
    prism.power_off(vm_uuid)

    snap_uuid = _refresh_snapshot(prism, vm_uuid, cfg.baseline_snapshot_name)
    LOGGER.info("Snapshot '%s' for '%s' = %s",
                cfg.baseline_snapshot_name, vm_name, snap_uuid)
    return GoldenVmStatus(version=version, vm_name=vm_name, vm_uuid=vm_uuid,
                          snapshot_uuid=snap_uuid, seeded=True)


def bootstrap_all(cfg: FrameworkConfig) -> List[GoldenVmStatus]:
    prism = PrismClient(cfg.prism.host, cfg.prism.user, cfg.prism.password,
                        cfg.prism.verify_ssl)
    results: List[GoldenVmStatus] = []
    for version in cfg.versions:
        try:
            results.append(bootstrap_one(cfg, version, prism))
        except (PrismError, RuntimeError) as exc:
            LOGGER.error("Bootstrap of PG%d golden VM failed: %s",
                         version, exc)
            results.append(GoldenVmStatus(
                version=version, vm_name=cfg.golden_vm_name(version),
                vm_uuid="", snapshot_uuid="", seeded=False))
    return results


def find_baseline_snapshot(prism: PrismClient, cfg: FrameworkConfig,
                           version: int) -> str:
    """Look up the BASELINE snapshot UUID for a given golden VM version."""
    vm_name = cfg.golden_vm_name(version)
    vm = prism.get_vm_by_name(vm_name)
    snap = prism.find_snapshot(vm["uuid"], cfg.baseline_snapshot_name)
    if not snap:
        raise PrismError(
            f"No '{cfg.baseline_snapshot_name}' snapshot on '{vm_name}'. "
            f"Run `pg-upgrade-test bootstrap` first.")
    return snap.get("uuid") or snap.get("snapshot_uuid") or ""
