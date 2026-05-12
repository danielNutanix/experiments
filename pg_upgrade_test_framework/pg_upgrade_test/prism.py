# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Minimal Prism Element client for the PG upgrade test framework.

Covers only the operations needed by this framework — VM lookup,
snapshot create/delete, clone, power on/off/reset, IP discovery via the
NIC list, and VM deletion. All operations poll the v2 task endpoint to
completion.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests


LOGGER = logging.getLogger(__name__)

PRISM_PORT = 9440
TASK_KEYS = ("taskUuid", "task_uuid")
TERMINAL_TASK_STATUSES = {"FAILED", "CANCELED", "ABORTED"}


class PrismError(RuntimeError):
    """Raised when a Prism API call returns a non-2xx response."""


# ============================================================
# Generic HTTP helpers
# ============================================================

def _request(session: requests.Session, method: str, url: str,
             *, timeout: int = 60, **kwargs: Any) -> requests.Response:
    LOGGER.debug("HTTP %s %s", method, url)
    response = session.request(method, url, timeout=timeout, **kwargs)
    if not response.ok:
        body = response.text[:500] if response.text else ""
        raise PrismError(f"{method} {url} -> {response.status_code}: {body}")
    return response


def _json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise PrismError(f"Expected JSON, got: {response.text[:500]}") from exc


def _extract_task_uuid(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in TASK_KEYS:
            if payload.get(key):
                return payload[key]
    raise PrismError(f"Prism response missing task UUID: {payload}")


# ============================================================
# Client
# ============================================================

class PrismClient:
    """Thin Prism Element wrapper used by the test framework."""

    def __init__(self, host: str, user: str, password: str,
                 verify_ssl: bool = False):
        self._host = host
        self._session = requests.Session()
        self._session.auth = (user, password)
        self._session.headers.update({"Content-Type": "application/json"})
        self._session.verify = verify_ssl
        self._cluster_uuid: Optional[str] = None

    def _url(self, path: str) -> str:
        return f"https://{self._host}:{PRISM_PORT}{path}"

    # -------- VM lookup --------

    def get_vm_by_name(self, name: str) -> Dict[str, Any]:
        """Return the first VM whose ``name`` matches exactly."""
        resp = _request(self._session, "GET",
                        self._url("/PrismGateway/services/rest/v2.0/vms/"),
                        params={"filter": f"vm_name=={name}",
                                "include_vm_disk_config": "true",
                                "include_vm_nic_config": "true"})
        entities = _json(resp).get("entities", [])
        for vm in entities:
            if vm.get("name") == name:
                return vm
        # Help the operator: surface a few near-name candidates so a typo
        # or forgotten --golden-vm-name flag is obvious from the error.
        all_resp = _request(self._session, "GET",
                            self._url("/PrismGateway/services/rest/v2.0/vms/"))
        candidates = []
        needle = name.lower()
        for vm in _json(all_resp).get("entities", []):
            vm_name = vm.get("name", "")
            if not vm_name:
                continue
            if needle in vm_name.lower() or vm_name.lower() in needle:
                candidates.append(vm_name)
            if len(candidates) >= 5:
                break
        hint = ""
        if candidates:
            hint = (f" Did you mean one of: {', '.join(candidates)}? "
                    f"Pass --golden-vm-name <name> (or set GOLDEN_VM_NAME).")
        raise PrismError(
            f"VM '{name}' not found on Prism {self._host}.{hint}")

    def get_vm(self, vm_uuid: str, *, include_disks: bool = True,
               include_nics: bool = True) -> Dict[str, Any]:
        params: Dict[str, str] = {}
        if include_disks:
            params["include_vm_disk_config"] = "true"
        if include_nics:
            params["include_vm_nic_config"] = "true"
        resp = _request(self._session, "GET",
                        self._url(f"/PrismGateway/services/rest/v2.0/vms/{vm_uuid}"),
                        params=params)
        return _json(resp)

    def cluster_uuid(self) -> str:
        if self._cluster_uuid:
            return self._cluster_uuid
        resp = _request(self._session, "GET",
                        self._url("/PrismGateway/services/rest/v2.0/cluster"))
        data = _json(resp)
        uuid = data.get("uuid") or data.get("cluster_uuid")
        if not uuid:
            raise PrismError(f"Could not determine cluster UUID: {data}")
        self._cluster_uuid = uuid
        return uuid

    # -------- Snapshots --------

    def create_snapshot(self, vm_uuid: str, snapshot_name: str) -> str:
        body = {"snapshot_specs": [{
            "vm_uuid": vm_uuid,
            "snapshot_name": snapshot_name,
        }]}
        resp = _request(self._session, "POST",
                        self._url("/PrismGateway/services/rest/v2.0/snapshots/"),
                        data=json.dumps(body))
        task = _extract_task_uuid(_json(resp))
        result = self.wait_for_task(task)
        return self._snapshot_uuid_from_task(result)

    def find_snapshot(self, vm_uuid: str, snapshot_name: str,
                      ) -> Optional[Dict[str, Any]]:
        resp = _request(self._session, "GET",
                        self._url("/PrismGateway/services/rest/v2.0/snapshots/"),
                        params={"vm_uuid": vm_uuid})
        for snap in _json(resp).get("entities", []):
            if snap.get("snapshot_name") == snapshot_name:
                return snap
        return None

    def delete_snapshot(self, snapshot_uuid: str) -> None:
        resp = _request(self._session, "DELETE",
                        self._url(
                            f"/PrismGateway/services/rest/v2.0/snapshots/"
                            f"{snapshot_uuid}"))
        task = _extract_task_uuid(_json(resp))
        self.wait_for_task(task)

    @staticmethod
    def _snapshot_uuid_from_task(task: Dict[str, Any]) -> str:
        for entity in task.get("entity_list", []):
            if entity.get("entity_type", "").lower() == "snapshot":
                return entity.get("entity_id", "")
        raise PrismError(f"Snapshot task did not list a snapshot entity: {task}")

    # -------- Clone --------

    def clone_vm_from_snapshot(self, snapshot_uuid: str, *,
                               clone_name: str) -> str:
        """Clone a VM from a snapshot. Returns the new VM UUID."""
        body = {"specs": [{"name": clone_name, "snapshot_uuid": snapshot_uuid}]}
        resp = _request(self._session, "POST",
                        self._url("/PrismGateway/services/rest/v2.0/vms/clone"),
                        data=json.dumps(body))
        task = _extract_task_uuid(_json(resp))
        result = self.wait_for_task(task)
        for entity in result.get("entity_list", []):
            if entity.get("entity_type", "").upper() == "VM":
                return entity["entity_id"]
        raise PrismError(f"Clone task did not list a VM entity: {result}")

    # -------- Power --------

    def set_power_state(self, vm_uuid: str, transition: str) -> None:
        body = {"transition": transition}
        resp = _request(self._session, "POST",
                        self._url(
                            f"/PrismGateway/services/rest/v2.0/vms/"
                            f"{vm_uuid}/set_power_state/"),
                        data=json.dumps(body))
        task = _extract_task_uuid(_json(resp))
        self.wait_for_task(task)

    def power_on(self, vm_uuid: str) -> None:
        self.set_power_state(vm_uuid, "ON")

    def power_off(self, vm_uuid: str) -> None:
        self.set_power_state(vm_uuid, "OFF")

    # -------- IP discovery --------

    def wait_for_ip(self, vm_uuid: str, *, timeout: int = 300,
                    interval: int = 5) -> str:
        """Poll until the VM reports a learned IPv4 on at least one NIC."""
        deadline = time.monotonic() + timeout
        while True:
            vm = self.get_vm(vm_uuid, include_disks=False, include_nics=True)
            ip = self._first_ipv4(vm)
            if ip:
                return ip
            if time.monotonic() > deadline:
                raise PrismError(
                    f"VM {vm_uuid} did not report an IP within {timeout}s")
            time.sleep(interval)

    @staticmethod
    def _first_ipv4(vm: Dict[str, Any]) -> Optional[str]:
        for nic in vm.get("vm_nics", []):
            ip = nic.get("ip_address")
            if ip and ":" not in ip:
                return ip
            for entry in nic.get("ip_addresses", []) or []:
                if entry and ":" not in entry:
                    return entry
        return None

    # -------- Delete --------

    def delete_vm(self, vm_uuid: str) -> None:
        resp = _request(self._session, "DELETE",
                        self._url(
                            f"/PrismGateway/services/rest/v2.0/vms/{vm_uuid}"))
        task = _extract_task_uuid(_json(resp))
        self.wait_for_task(task)

    # -------- Snapshot restore --------

    def restore_snapshot(self, vm_uuid: str, snapshot_uuid: str,
                         *, restore_network_configuration: bool = False,
                         ) -> None:
        """Restore a VM's disks from one of its own snapshots.

        Caller is responsible for powering the VM off beforehand and
        powering it back on afterwards — Prism's restore endpoint expects
        the VM to be in the OFF state.
        """
        body = {
            "snapshot_uuid": snapshot_uuid,
            "restore_network_configuration": restore_network_configuration,
        }
        resp = _request(self._session, "POST",
                        self._url(
                            f"/PrismGateway/services/rest/v2.0/vms/"
                            f"{vm_uuid}/restore"),
                        data=json.dumps(body))
        task = _extract_task_uuid(_json(resp))
        self.wait_for_task(task)

    # -------- Task polling --------

    def wait_for_task(self, task_uuid: str, *, timeout: int = 900,
                      interval: int = 5) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        url = self._url(f"/PrismGateway/services/rest/v2.0/tasks/{task_uuid}")
        while True:
            resp = _request(self._session, "GET", url)
            data = _json(resp)
            status = (data.get("progress_status")
                      or data.get("status") or "").upper()
            percent = int(data.get("percentage_complete", 0))
            if status in TERMINAL_TASK_STATUSES:
                raise PrismError(
                    f"Prism task {task_uuid} ended in {status}: "
                    f"{data.get('meta_response') or data}")
            if percent >= 100 or status == "SUCCEEDED":
                return data
            if time.monotonic() > deadline:
                raise PrismError(
                    f"Timed out waiting for Prism task {task_uuid} "
                    f"(percent={percent}, status={status})")
            time.sleep(interval)
