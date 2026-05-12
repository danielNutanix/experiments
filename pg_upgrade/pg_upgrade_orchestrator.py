#!/usr/bin/env python3
# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""
PostgreSQL major-version upgrade orchestrator for an NDB-managed DBVM.

Coordinates the end-to-end workflow across NDB, Prism Element and the DBVM:
  1. Pause the Time Machine
  2. Take a recovery snapshot via Prism
  3. Mount a fresh data disk for the new PG binaries
  4. Run pg_upgrade.sh on the DBVM
  5. Resume the Time Machine

Inputs are kept minimal: derivable values (VM UUID, TM ID, cluster IP, etc.)
are looked up at runtime from the VM name.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

import paramiko
import requests
import urllib3


# ============================================================
# Constants
# ============================================================

NDB_API_PREFIX = "/era/v0.9"

# NDB operation status codes — see era_common/.../constants.py
# and EraOperationStatus.java. Only `5` and `17` are non-warning success.
NDB_TERMINAL_STATUSES = {2, 4, 5, 9, 11, 15, 17, 18, 19}
NDB_SUCCESS_STATUSES = {5, 17}

PRISM_DEFAULT_PORT = 9440

# Prism task polling shape varies between endpoints; both keys are accepted.
PRISM_TASK_KEYS = ("taskUuid", "task_uuid")

# Containers we should never use for an ad-hoc upgrade disk.
PRISM_RESERVED_CONTAINERS = (
    "NutanixManagementShare",
    "SelfServiceContainer",
)

GIB = 1024 ** 3

LOGGER = logging.getLogger("pg_upgrade")


# ============================================================
# Configuration & CLI
# ============================================================

@dataclass
class Config:
    """User-supplied inputs for the orchestrator."""

    ndb_host: str
    ndb_user: str
    ndb_password: str

    prism_user: str
    prism_password: str

    vm_name: str

    ssh_user: str
    ssh_password: Optional[str]
    ssh_key_path: Optional[str]
    ssh_port: int

    old_pg_version: int
    new_pg_version: int

    disk_size_gb: int
    snapshot_name: str
    storage_container: Optional[str]
    upgrade_script: str
    mount_point: Optional[str]

    verify_ssl: bool


def _add_credential_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ndb-host", default=os.environ.get("NDB_HOST"),
                        help="NDB server host or IP. Env: NDB_HOST.")
    parser.add_argument("--ndb-user", default=os.environ.get("NDB_USER"),
                        help="NDB username. Env: NDB_USER.")
    parser.add_argument("--ndb-password", default=os.environ.get("NDB_PASSWORD"),
                        help="NDB password. Env: NDB_PASSWORD.")
    parser.add_argument("--prism-user", default=os.environ.get("PRISM_USER"),
                        help="Prism Element username. Env: PRISM_USER.")
    parser.add_argument("--prism-password", default=os.environ.get("PRISM_PASSWORD"),
                        help="Prism Element password. Env: PRISM_PASSWORD.")
    parser.add_argument("--ssh-user", default=os.environ.get("DBVM_SSH_USER", "era"),
                        help="SSH user for the DBVM. Default: era. Env: DBVM_SSH_USER.")
    parser.add_argument("--ssh-password", default=os.environ.get("DBVM_SSH_PASSWORD"),
                        help="SSH password. Env: DBVM_SSH_PASSWORD.")
    parser.add_argument("--ssh-key", default=os.environ.get("DBVM_SSH_KEY"),
                        help="Path to SSH private key. Env: DBVM_SSH_KEY.")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestrate a PostgreSQL major-version upgrade on an NDB DBVM.",
    )
    _add_credential_args(parser)

    parser.add_argument("--vm-name", required=True,
                        help="DBVM name registered in NDB.")
    parser.add_argument("--ssh-port", type=int, default=22, help="DBVM SSH port.")

    parser.add_argument("--old-version", type=int, default=15,
                        help="Source PostgreSQL major version. Default: 15.")
    parser.add_argument("--new-version", type=int, default=17,
                        help="Target PostgreSQL major version. Default: 17.")

    parser.add_argument("--disk-size-gb", type=int, default=20,
                        help="Size in GB of the new disk for PG binaries. Default: 20.")
    parser.add_argument("--snapshot-name", default=None,
                        help="Snapshot name. Default: pg_upgrade_pre_<ts>.")
    parser.add_argument("--storage-container", default=None,
                        help="Storage container name on Prism. Auto-selected if omitted.")
    parser.add_argument("--upgrade-script",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "pg_upgrade.sh"),
                        help="Path to pg_upgrade.sh.")
    parser.add_argument("--mount-point", default=None,
                        help="Mount point for the new disk. Defaults to OS-appropriate "
                             "PG binary path.")

    parser.add_argument("--insecure", dest="verify_ssl", action="store_false",
                        default=True, help="Disable SSL verification (self-signed certs).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")

    return parser


def _resolve_credentials(args: argparse.Namespace) -> Config:
    """Build a Config, prompting for any missing secret."""

    def _required(value: Optional[str], label: str, secret: bool) -> str:
        if value:
            return value
        if secret:
            return getpass.getpass(f"{label}: ")
        return input(f"{label}: ").strip()

    if args.ssh_key and args.ssh_password:
        raise SystemExit("Pass either --ssh-key or --ssh-password, not both.")
    if not args.ssh_key and not args.ssh_password:
        args.ssh_password = getpass.getpass("DBVM SSH password: ")

    return Config(
        ndb_host=_required(args.ndb_host, "NDB host", secret=False),
        ndb_user=_required(args.ndb_user, "NDB user", secret=False),
        ndb_password=_required(args.ndb_password, "NDB password", secret=True),
        prism_user=_required(args.prism_user, "Prism user", secret=False),
        prism_password=_required(args.prism_password, "Prism password", secret=True),
        vm_name=args.vm_name,
        ssh_user=args.ssh_user,
        ssh_password=args.ssh_password,
        ssh_key_path=args.ssh_key,
        ssh_port=args.ssh_port,
        old_pg_version=args.old_version,
        new_pg_version=args.new_version,
        disk_size_gb=args.disk_size_gb,
        snapshot_name=args.snapshot_name or f"pg_upgrade_pre_{int(time.time())}",
        storage_container=args.storage_container,
        upgrade_script=args.upgrade_script,
        mount_point=args.mount_point,
        verify_ssl=args.verify_ssl,
    )


# ============================================================
# HTTP helpers
# ============================================================

class ApiError(RuntimeError):
    """Raised when an HTTP API call returns a non-2xx response."""


def _request(session: requests.Session, method: str, url: str, *,
             timeout: int = 60, **kwargs: Any) -> requests.Response:
    LOGGER.debug("HTTP %s %s", method, url)
    response = session.request(method, url, timeout=timeout, **kwargs)
    LOGGER.debug("HTTP %s %s -> %d\n%s",
                 method, url, response.status_code, _format_body(response))
    if not response.ok:
        body = response.text[:500] if response.text else ""
        raise ApiError(f"{method} {url} -> {response.status_code}: {body}")
    return response


def _format_body(response: requests.Response, *, limit: int = 2000) -> str:
    """Pretty-print the response body for debug logs (JSON when possible)."""
    text = response.text or ""
    if not text:
        return "  <empty body>"
    try:
        pretty = json.dumps(response.json(), indent=2, sort_keys=True)
    except ValueError:
        pretty = text
    if len(pretty) > limit:
        pretty = pretty[:limit] + f"\n... <truncated, total={len(pretty)} bytes>"
    return "\n".join("  " + line for line in pretty.splitlines())


def _json_or_raise(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(f"Expected JSON, got: {response.text[:500]}") from exc


# ============================================================
# NDB client
# ============================================================

class NdbClient:
    """Thin wrapper around the NDB REST API used by this script."""

    def __init__(self, host: str, user: str, password: str, verify_ssl: bool):
        self._base = self._normalize_base(host)
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Content-Type": "application/json"})
        self._session.verify = verify_ssl

    @staticmethod
    def _normalize_base(host: str) -> str:
        host = host.rstrip("/")
        if not host.startswith("http"):
            host = f"https://{host}"
        return f"{host}{NDB_API_PREFIX}"

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def find_dbserver_by_name(self, name: str) -> Dict[str, Any]:
        """Resolve a DBVM by name; returns the full ERADBServerDTO."""
        params = {
            "value-type": "name",
            "value": name,
            "load-databases": "true",
            "detailed": "true",
        }
        resp = _request(self._session, "GET", self._url("/dbservers"), params=params)
        items = _json_or_raise(resp)
        if not items:
            raise ApiError(f"No DBVM named '{name}' found in NDB")
        if isinstance(items, list):
            return items[0]
        return items

    def get_cluster(self, cluster_id: str) -> Dict[str, Any]:
        resp = _request(self._session, "GET", self._url(f"/clusters/{cluster_id}"))
        return _json_or_raise(resp)

    def pause_time_machine(self, tm_id: str, nx_cluster_ids: List[str],
                           reason: str) -> Optional[str]:
        body = {"forced": False, "reason": reason, "nxClusterIds": nx_cluster_ids}
        return self._patch_tm(tm_id, "pause", body)

    def resume_time_machine(self, tm_id: str, nx_cluster_ids: List[str],
                            reason: str) -> Optional[str]:
        body = {
            "resetCapability": False,
            "skipSnapshot": False,
            "skipLogCatchup": False,
            "reason": reason,
            "nxClusterIds": nx_cluster_ids,
        }
        return self._patch_tm(tm_id, "resume", body)

    def _patch_tm(self, tm_id: str, action: str,
                  body: Dict[str, Any]) -> Optional[str]:
        """Pause or resume a TM.

        Returns the dispatched `operationId` to poll, or ``None`` when NDB
        completed the action synchronously (e.g. the TM has no continuous
        recovery enabled, so no log-catchup operation is dispatched). In that
        case the response is a TaskInfoSummary with only a `message` field;
        treating `entityId` (the TM ID) as an operation ID would cause a 500
        from the subsequent `/operations/<id>` GET.
        """
        resp = _request(self._session, "PATCH",
                        self._url(f"/tms/{tm_id}/{action}"),
                        data=json.dumps(body))
        payload = _json_or_raise(resp)
        op_id = payload.get("operationId")
        message = payload.get("message")
        if op_id:
            LOGGER.info("TM %s %s dispatched operation %s", tm_id, action, op_id)
            return op_id
        LOGGER.info("TM %s %s completed synchronously: %s",
                    tm_id, action, message or payload)
        return None

    def wait_for_operation(self, operation_id: str, *,
                           timeout: int = 1800, interval: int = 15) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        url = self._url(f"/operations/{operation_id}")
        while True:
            resp = _request(self._session, "GET", url)
            data = _json_or_raise(resp)
            status = int(data.get("status", 0))
            if status in NDB_TERMINAL_STATUSES:
                if status not in NDB_SUCCESS_STATUSES:
                    raise ApiError(
                        f"NDB operation {operation_id} failed with status {status}: "
                        f"{data.get('message')}")
                return data
            if time.monotonic() > deadline:
                raise ApiError(
                    f"Timed out waiting for NDB operation {operation_id} "
                    f"(last status={status})")
            time.sleep(interval)


# ============================================================
# Prism client
# ============================================================

class PrismClient:
    """Thin wrapper around Prism Element APIs used by this script."""

    def __init__(self, host: str, user: str, password: str, verify_ssl: bool):
        self._host = host
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Content-Type": "application/json"})
        self._session.verify = verify_ssl
        self._cluster_uuid: Optional[str] = None

    def _url(self, path: str) -> str:
        return f"https://{self._host}:{PRISM_DEFAULT_PORT}{path}"

    def cluster_uuid(self) -> str:
        if self._cluster_uuid:
            return self._cluster_uuid
        resp = _request(self._session, "GET",
                        self._url("/PrismGateway/services/rest/v2.0/cluster"))
        data = _json_or_raise(resp)
        uuid = data.get("uuid") or data.get("cluster_uuid")
        if not uuid:
            raise ApiError(f"Could not determine Prism cluster UUID: {data}")
        self._cluster_uuid = uuid
        return uuid

    def get_vm(self, vm_uuid: str, *, include_disks: bool = True) -> Dict[str, Any]:
        params = {"include_vm_disk_config": "true"} if include_disks else {}
        resp = _request(self._session, "GET",
                        self._url(f"/PrismGateway/services/rest/v2.0/vms/{vm_uuid}"),
                        params=params)
        return _json_or_raise(resp)

    def list_storage_containers(self) -> List[Dict[str, Any]]:
        resp = _request(self._session, "GET",
                        self._url("/PrismGateway/services/rest/v2.0/storage_containers/"))
        data = _json_or_raise(resp)
        return data.get("entities", [])

    def pick_storage_container(self, preferred_name: Optional[str]) -> Dict[str, Any]:
        containers = self.list_storage_containers()
        if not containers:
            raise ApiError("No storage containers visible on Prism")
        if preferred_name:
            for container in containers:
                if container.get("name") == preferred_name:
                    return container
            raise ApiError(f"Storage container '{preferred_name}' not found")
        for container in containers:
            name = container.get("name", "")
            if not name.startswith(PRISM_RESERVED_CONTAINERS):
                return container
        return containers[0]

    def take_vm_snapshot(self, vm_uuid: str, snapshot_name: str) -> str:
        body = {
            "snapshotSpecs": [{
                "vmUuid": f"{self.cluster_uuid()}::{vm_uuid}",
                "snapshotName": snapshot_name,
            }],
        }
        resp = _request(self._session, "POST",
                        self._url("/api/nutanix/v0.8/snapshots"),
                        data=json.dumps(body))
        return self._extract_task_uuid(_json_or_raise(resp))

    def attach_disk(self, vm_uuid: str, *, container_uuid: str,
                    size_bytes: int, device_index: int,
                    device_bus: str = "scsi") -> str:
        body = {
            "vm_disks": [{
                "is_cdrom": False,
                "disk_address": {"device_bus": device_bus, "device_index": device_index},
                "vm_disk_create": {
                    "storage_container_uuid": container_uuid,
                    "size": size_bytes,
                },
            }],
        }
        resp = _request(self._session, "POST",
                        self._url(f"/PrismGateway/services/rest/v2.0/vms/"
                                  f"{vm_uuid}/disks/attach"),
                        data=json.dumps(body))
        return self._extract_task_uuid(_json_or_raise(resp))

    def wait_for_task(self, task_uuid: str, *,
                      timeout: int = 600, interval: int = 5) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        url = self._url(f"/PrismGateway/services/rest/v2.0/tasks/{task_uuid}")
        while True:
            resp = _request(self._session, "GET", url)
            data = _json_or_raise(resp)
            percent = int(data.get("percentage_complete", 0))
            status = (data.get("progress_status") or data.get("status") or "").upper()
            if status in {"FAILED", "CANCELED", "ABORTED"}:
                raise ApiError(f"Prism task {task_uuid} ended in status {status}: "
                               f"{data.get('meta_response') or data}")
            if percent >= 100 or status == "SUCCEEDED":
                return data
            if time.monotonic() > deadline:
                raise ApiError(
                    f"Timed out waiting for Prism task {task_uuid} "
                    f"(percent={percent}, status={status})")
            time.sleep(interval)

    @staticmethod
    def _extract_task_uuid(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in PRISM_TASK_KEYS:
                if payload.get(key):
                    return payload[key]
        raise ApiError(f"Prism response missing task UUID: {payload}")


def next_scsi_index(vm_payload: Dict[str, Any]) -> int:
    """Return the next free SCSI device index for a Prism VM."""
    used: List[int] = []
    for disk in vm_payload.get("vm_disk_info", []):
        addr = disk.get("disk_address") or {}
        if addr.get("device_bus", "").lower() == "scsi":
            idx = addr.get("device_index")
            if isinstance(idx, int):
                used.append(idx)
    return (max(used) + 1) if used else 0


# ============================================================
# SSH executor
# ============================================================

class SshExecutor:
    """Streams remote command output and uploads files via SFTP."""

    def __init__(self, host: str, user: str, password: Optional[str],
                 key_path: Optional[str], port: int):
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: Dict[str, Any] = {
            "hostname": host, "port": port, "username": user, "timeout": 30,
        }
        if key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(key_path)
        else:
            connect_kwargs["password"] = password
        LOGGER.info("Connecting via SSH to %s@%s:%d", user, host, port)
        self._client.connect(**connect_kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SshExecutor":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def run(self, command: str, *, check: bool = True,
            input_data: Optional[str] = None) -> Tuple[int, str, str]:
        """Run a single command, capturing stdout/stderr."""
        LOGGER.debug("SSH > %s", command)
        stdin, stdout, stderr = self._client.exec_command(command, get_pty=False)
        if input_data is not None:
            stdin.write(input_data)
            stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(
                f"Remote command failed (rc={rc}): {command}\n"
                f"STDOUT: {out}\nSTDERR: {err}")
        return rc, out, err

    def run_streaming(self, command: str, *, check: bool = True,
                      tail_lines: int = 40) -> Tuple[int, List[str]]:
        """Run a command, stream output, and return (rc, last N output lines).

        The tail is retained so callers can include the actual error context
        in their failure message — useful when the remote script is run with
        ``set -e`` and dies several screens above the exit point.
        """
        LOGGER.info("SSH > %s", command)
        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport is not connected")
        channel = transport.open_session()
        channel.get_pty()
        channel.exec_command(command)

        tail: Deque[str] = deque(maxlen=tail_lines)
        stdout_buf = b""
        stderr_buf = b""
        while True:
            if channel.recv_ready():
                stdout_buf = self._drain(channel.recv, stdout_buf,
                                         sys.stdout, tail)
            if channel.recv_stderr_ready():
                stderr_buf = self._drain(channel.recv_stderr, stderr_buf,
                                         sys.stderr, tail)
            if channel.exit_status_ready() and not channel.recv_ready() \
                    and not channel.recv_stderr_ready():
                break
            time.sleep(0.1)

        rc = channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(self._format_remote_failure(command, rc, tail))
        return rc, list(tail)

    @staticmethod
    def _drain(reader: Any, buffer: bytes, sink: Any,
               tail: Optional[Deque[str]] = None) -> bytes:
        chunk = reader(4096)
        if not chunk:
            return buffer
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            decoded = line.decode("utf-8", errors="replace")
            sink.write(decoded + "\n")
            sink.flush()
            if tail is not None:
                tail.append(decoded)
        return buffer

    @staticmethod
    def _format_remote_failure(command: str, rc: int,
                               tail: Deque[str]) -> str:
        if not tail:
            return (f"Remote command failed (rc={rc}) with no captured output. "
                    f"Command: {command}")
        tail_block = "\n".join("  | " + line for line in tail)
        return (
            f"Remote command failed (rc={rc}): {command}\n"
            f"--- last {len(tail)} lines of remote output ---\n"
            f"{tail_block}\n"
            f"--- end of remote output ---")

    def upload(self, local_path: str, remote_path: str, *,
               mode: int = 0o755) -> None:
        LOGGER.info("Uploading %s -> %s", local_path, remote_path)
        sftp = self._client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()


# ============================================================
# DBVM disk preparation
# ============================================================

@dataclass
class DiskPlan:
    """Resolved values for the new-disk preparation step."""

    device: str          # e.g. /dev/sdg
    mount_point: str     # e.g. /usr/pgsql-17
    blkid_uuid: str = ""


def list_block_devices(ssh: SshExecutor) -> List[str]:
    """Return the set of disk-level (TYPE=disk) device names on the VM."""
    _, out, _ = ssh.run("lsblk -dn -o NAME,TYPE")
    devices = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "disk":
            devices.append(f"/dev/{parts[0]}")
    return sorted(devices)


def detect_os_family(ssh: SshExecutor) -> str:
    rc, _, _ = ssh.run("command -v dnf >/dev/null 2>&1", check=False)
    if rc == 0:
        return "rhel"
    rc, _, _ = ssh.run("command -v apt-get >/dev/null 2>&1", check=False)
    if rc == 0:
        return "debian"
    raise RuntimeError("Could not detect OS family on DBVM (no dnf or apt-get)")


def default_mount_point(os_family: str, new_version: int) -> str:
    if os_family == "rhel":
        return f"/usr/pgsql-{new_version}"
    return f"/usr/lib/postgresql/{new_version}"


def rescan_scsi_hosts(ssh: SshExecutor) -> None:
    """Force every SCSI host to rescan so newly-attached disks surface.

    virtio-scsi hot-plug events on AHV guests are not always delivered to the
    kernel synchronously; without a rescan, ``lsblk`` may not see the new
    device for several seconds (or until a manual rescan).
    """
    cmd = (
        "for host in /sys/class/scsi_host/host*/scan; do "
        "echo '- - -' | sudo tee \"$host\" >/dev/null; "
        "done"
    )
    ssh.run(cmd, check=False)


def wait_for_new_block_device(ssh: SshExecutor, *, before: Iterable[str],
                              timeout: int = 60, interval: int = 2) -> str:
    """Poll ``lsblk`` until a disk appears that wasn't in ``before``."""
    rescan_scsi_hosts(ssh)
    before_set = set(before)
    deadline = time.monotonic() + timeout
    after: List[str] = []
    while True:
        after = list_block_devices(ssh)
        new_devices = sorted(set(after) - before_set)
        if new_devices:
            if len(new_devices) > 1:
                LOGGER.warning("Multiple new devices detected (%s); using first",
                               new_devices)
            return new_devices[0]
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"No new block device detected after {timeout}s. "
                f"Before: {sorted(before)}, After: {after}")
        time.sleep(interval)


def prepare_new_disk(ssh: SshExecutor, *, before: Iterable[str],
                     mount_point: str, settle_timeout: int = 60) -> DiskPlan:
    """Format and mount the freshly-attached disk, returning its details."""

    device = wait_for_new_block_device(ssh, before=before, timeout=settle_timeout)
    LOGGER.info("New disk discovered: %s", device)

    ssh.run(f"sudo wipefs -a {device}")
    ssh.run(f"sudo mkfs.ext4 -F {device}")
    ssh.run(f"sudo mkdir -p {mount_point}")
    ssh.run(f"sudo chown postgres:postgres {mount_point}")
    ssh.run(f"sudo mount {device} {mount_point}")

    _, out, _ = ssh.run(f"sudo blkid -s UUID -o value {device}")
    blk_uuid = out.strip()
    if not blk_uuid:
        raise RuntimeError(f"Could not read blkid UUID for {device}")

    _add_fstab_entry(ssh, blk_uuid, mount_point)
    ssh.run("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT")
    ssh.run(f"mount | grep {mount_point}")

    return DiskPlan(device=device, mount_point=mount_point, blkid_uuid=blk_uuid)


def _add_fstab_entry(ssh: SshExecutor, blk_uuid: str, mount_point: str) -> None:
    """Append a UUID-based fstab entry, idempotently."""
    grep_cmd = f"grep -F 'UUID={blk_uuid}' /etc/fstab"
    rc, _, _ = ssh.run(grep_cmd, check=False)
    if rc == 0:
        LOGGER.info("/etc/fstab already references UUID=%s; skipping append", blk_uuid)
        return
    fstab_line = f"UUID={blk_uuid} {mount_point} ext4 defaults,nofail 0 2"
    ssh.run(f"echo '{fstab_line}' | sudo tee -a /etc/fstab >/dev/null")


# ============================================================
# Upgrade execution
# ============================================================

def run_pg_upgrade_script(ssh: SshExecutor, *, local_script: str,
                          old_ver: int, new_ver: int) -> None:
    """Upload pg_upgrade.sh and execute it under sudo with stdin = script.

    The upgrade script switches to the postgres user via `sudo -iu postgres`
    and continues issuing commands. That pattern only works when bash itself
    is reading from stdin (so the postgres sub-shell inherits the same FD),
    hence the `bash -s < /tmp/pg_upgrade.sh` invocation rather than
    `bash /tmp/pg_upgrade.sh`.
    """
    if not os.path.isfile(local_script):
        raise FileNotFoundError(f"Upgrade script not found: {local_script}")

    remote_path = "/tmp/pg_upgrade.sh"
    ssh.upload(local_script, remote_path, mode=0o755)

    command = (
        f"sudo OLD_VER={old_ver} NEW_VER={new_ver} bash -s < {remote_path}"
    )
    rc, tail = ssh.run_streaming(command, check=False)
    if rc != 0:
        tail_block = "\n".join("  | " + line for line in tail) or "  <empty>"
        raise RuntimeError(
            f"pg_upgrade.sh exited with rc={rc}. The Time Machine will be "
            f"resumed; the recovery snapshot remains available for rollback.\n"
            f"--- last {len(tail)} lines of pg_upgrade.sh output ---\n"
            f"{tail_block}\n"
            f"--- end of pg_upgrade.sh output ---")
    LOGGER.info("pg_upgrade.sh completed successfully")
    ssh.run(f"rm -f {remote_path}", check=False)


# ============================================================
# Lookup helpers
# ============================================================

@dataclass
class Targets:
    """Resolved entities the orchestrator needs across NDB and Prism."""

    dbvm_ip: str
    vm_uuid: str           # Prism VM UUID (vmClusterUuid)
    nx_cluster_id: str     # NDB cluster ID
    prism_ip: str
    tm_ids: List[str] = field(default_factory=list)


def _pick_dbvm_ip(dbserver: Dict[str, Any]) -> str:
    addrs = dbserver.get("ipAddresses") or []
    if not addrs:
        raise ApiError("DBVM has no ipAddresses in NDB metadata")
    return addrs[0]


def _pick_postgres_tm_ids(dbserver: Dict[str, Any]) -> List[str]:
    tm_ids: List[str] = []
    for db in dbserver.get("databases") or []:
        if (db.get("type") or "").lower().startswith("postgres"):
            tm_id = db.get("timeMachineId")
            if tm_id:
                tm_ids.append(tm_id)
    if not tm_ids:
        # Fallback: use the dbserver-level associated TM IDs.
        for tm_id in dbserver.get("associatedTimeMachineIds") or []:
            tm_ids.append(tm_id)
    if not tm_ids:
        raise ApiError("No PostgreSQL Time Machine found on this DBVM")
    return tm_ids


def resolve_targets(ndb: NdbClient, vm_name: str) -> Targets:
    LOGGER.info("Resolving DBVM '%s' in NDB", vm_name)
    dbserver = ndb.find_dbserver_by_name(vm_name)
    nx_cluster_id = dbserver.get("nxClusterId")
    vm_uuid = dbserver.get("vmClusterUuid")
    if not (nx_cluster_id and vm_uuid):
        raise ApiError(f"DBVM payload missing nxClusterId or vmClusterUuid: {dbserver}")

    cluster = ndb.get_cluster(nx_cluster_id)
    cluster_ips = cluster.get("ipAddresses") or []
    if not cluster_ips:
        raise ApiError(f"NDB cluster {nx_cluster_id} has no ipAddresses")

    targets = Targets(
        dbvm_ip=_pick_dbvm_ip(dbserver),
        vm_uuid=vm_uuid,
        nx_cluster_id=nx_cluster_id,
        prism_ip=cluster_ips[0],
        tm_ids=_pick_postgres_tm_ids(dbserver),
    )
    LOGGER.info("Resolved targets: %s", targets)
    return targets


# ============================================================
# Orchestrator
# ============================================================

def banner(msg: str) -> None:
    LOGGER.info("=" * 60)
    LOGGER.info(msg)
    LOGGER.info("=" * 60)


def _pause_all_tms(ndb: NdbClient, tm_ids: List[str], nx_cluster_id: str,
                   reason: str) -> List[str]:
    paused: List[str] = []
    for tm_id in tm_ids:
        banner(f"Step 1: Pausing Time Machine {tm_id}")
        op_id = ndb.pause_time_machine(tm_id, [nx_cluster_id], reason=reason)
        if op_id:
            ndb.wait_for_operation(op_id)
        paused.append(tm_id)
    return paused


def _resume_all_tms(ndb: NdbClient, tm_ids: List[str], nx_cluster_id: str,
                    reason: str) -> None:
    for tm_id in tm_ids:
        banner(f"Step 10: Resuming Time Machine {tm_id}")
        try:
            op_id = ndb.resume_time_machine(tm_id, [nx_cluster_id], reason=reason)
            if op_id:
                ndb.wait_for_operation(op_id)
        except Exception as exc:  # noqa: BLE001 — best-effort resume
            LOGGER.error("Failed to resume TM %s: %s", tm_id, exc)


def _take_recovery_snapshot(prism: PrismClient, vm_uuid: str,
                            snapshot_name: str) -> None:
    banner(f"Step 2: Taking recovery snapshot '{snapshot_name}'")
    task_uuid = prism.take_vm_snapshot(vm_uuid, snapshot_name)
    prism.wait_for_task(task_uuid)


def _attach_upgrade_disk(prism: PrismClient, *, vm_uuid: str,
                         container_name: Optional[str],
                         size_gb: int) -> Tuple[str, int]:
    banner(f"Step 3: Attaching {size_gb}GB disk to VM")
    container = prism.pick_storage_container(container_name)
    container_uuid = container["storage_container_uuid"]
    LOGGER.info("Using storage container: %s (%s)", container.get("name"), container_uuid)

    vm = prism.get_vm(vm_uuid, include_disks=True)
    index = next_scsi_index(vm)
    LOGGER.info("Next SCSI device_index: %d", index)

    task_uuid = prism.attach_disk(
        vm_uuid,
        container_uuid=container_uuid,
        size_bytes=size_gb * GIB,
        device_index=index,
    )
    prism.wait_for_task(task_uuid)
    # The guest-side SCSI rescan + lsblk poll happens in
    # ``wait_for_new_block_device`` after this returns.
    return container_uuid, index


def _prepare_disk_on_dbvm(ssh: SshExecutor, *, before_devices: List[str],
                          new_version: int, mount_override: Optional[str]) -> DiskPlan:
    banner("Step 4: Formatting and mounting the new disk")
    os_family = detect_os_family(ssh)
    LOGGER.info("DBVM OS family: %s", os_family)
    mount_point = mount_override or default_mount_point(os_family, new_version)
    return prepare_new_disk(ssh, before=before_devices, mount_point=mount_point)


def run(cfg: Config) -> None:
    if not cfg.verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ndb = NdbClient(cfg.ndb_host, cfg.ndb_user, cfg.ndb_password, cfg.verify_ssl)
    targets = resolve_targets(ndb, cfg.vm_name)

    prism = PrismClient(targets.prism_ip, cfg.prism_user, cfg.prism_password,
                        cfg.verify_ssl)

    reason = (f"PostgreSQL {cfg.old_pg_version}->{cfg.new_pg_version} upgrade "
              f"orchestrated by pg_upgrade_orchestrator")

    paused_tms: List[str] = []
    try:
        # SSH is opened first so the pre-attach block-device snapshot is taken
        # before Prism creates the new disk; otherwise the lsblk diff is empty
        # and the orchestrator can't identify which device to format.
        with SshExecutor(targets.dbvm_ip, cfg.ssh_user, cfg.ssh_password,
                         cfg.ssh_key_path, cfg.ssh_port) as ssh:
            paused_tms = _pause_all_tms(ndb, targets.tm_ids,
                                        targets.nx_cluster_id, reason)
            _take_recovery_snapshot(prism, targets.vm_uuid, cfg.snapshot_name)

            before_devices = list_block_devices(ssh)
            LOGGER.info("Pre-attach block devices on DBVM: %s", before_devices)

            _attach_upgrade_disk(prism, vm_uuid=targets.vm_uuid,
                                 container_name=cfg.storage_container,
                                 size_gb=cfg.disk_size_gb)

            _prepare_disk_on_dbvm(ssh, before_devices=before_devices,
                                  new_version=cfg.new_pg_version,
                                  mount_override=cfg.mount_point)

            banner("Steps 5-9: Running pg_upgrade.sh on the DBVM")
            run_pg_upgrade_script(ssh, local_script=cfg.upgrade_script,
                                  old_ver=cfg.old_pg_version,
                                  new_ver=cfg.new_pg_version)
    finally:
        if paused_tms:
            _resume_all_tms(ndb, paused_tms, targets.nx_cluster_id, reason)

    banner(f"PostgreSQL upgrade {cfg.old_pg_version}->{cfg.new_pg_version} complete")


# ============================================================
# Entrypoint
# ============================================================

def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    cfg = _resolve_credentials(args)
    try:
        run(cfg)
    except (RuntimeError, paramiko.SSHException) as exc:
        LOGGER.error("Upgrade failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
