# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Streaming SSH executor with SFTP upload support.

Adapted from the single-VM orchestrator. Streams remote stdout/stderr
to the local logger and retains a rolling tail so failure messages can
include the actual error context (not just an exit code).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from collections import deque
from typing import Any, Deque, List, Optional, Tuple

import paramiko


LOGGER = logging.getLogger(__name__)


class SshExecutor:
    """Connect once, run many commands; safe as a context manager."""

    def __init__(self, host: str, user: str, password: Optional[str],
                 key_path: Optional[str], port: int = 22,
                 *, connect_timeout: int = 30):
        self._host = host
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs: dict = {
            "hostname": host, "port": port, "username": user,
            "timeout": connect_timeout,
        }
        if key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(key_path)
        else:
            connect_kwargs["password"] = password
        LOGGER.info("SSH connect %s@%s:%d", user, host, port)
        self._client.connect(**connect_kwargs)

    def __enter__(self) -> "SshExecutor":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run(self, command: str, *, check: bool = True,
            timeout: Optional[int] = None) -> Tuple[int, str, str]:
        LOGGER.debug("[%s] SSH > %s", self._host, command)
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(
                f"Remote command failed (rc={rc}) on {self._host}: {command}\n"
                f"STDOUT: {out}\nSTDERR: {err}")
        return rc, out, err

    def run_streaming(self, command: str, *, check: bool = True,
                      tail_lines: int = 60,
                      log_prefix: str = "") -> Tuple[int, List[str]]:
        """Run a command, stream output, and return (rc, last N lines)."""
        prefix = log_prefix or self._host
        LOGGER.info("[%s] SSH > %s", prefix, command)
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
                                         sys.stdout, tail, prefix)
            if channel.recv_stderr_ready():
                stderr_buf = self._drain(channel.recv_stderr, stderr_buf,
                                         sys.stderr, tail, prefix)
            if channel.exit_status_ready() and not channel.recv_ready() \
                    and not channel.recv_stderr_ready():
                break
            time.sleep(0.05)

        rc = channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(
                self._format_failure(prefix, command, rc, tail))
        return rc, list(tail)

    @staticmethod
    def _drain(reader: Any, buffer: bytes, sink: Any,
               tail: Deque[str], prefix: str) -> bytes:
        chunk = reader(4096)
        if not chunk:
            return buffer
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            decoded = line.decode("utf-8", errors="replace")
            sink.write(f"[{prefix}] {decoded}\n")
            sink.flush()
            tail.append(decoded)
        return buffer

    @staticmethod
    def _format_failure(prefix: str, command: str, rc: int,
                        tail: Deque[str]) -> str:
        if not tail:
            return (f"[{prefix}] Remote command failed (rc={rc}) "
                    f"with no captured output: {command}")
        body = "\n".join(f"  | {line}" for line in tail)
        return (f"[{prefix}] Remote command failed (rc={rc}): {command}\n"
                f"--- last {len(tail)} lines ---\n{body}\n--- end ---")

    # ------------------------------------------------------------------
    # File transfer
    # ------------------------------------------------------------------

    def upload(self, local_path: str, remote_path: str,
               *, mode: int = 0o755) -> None:
        LOGGER.info("[%s] SFTP put %s -> %s", self._host, local_path, remote_path)
        sftp = self._client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()

    def download(self, remote_path: str, local_path: str) -> bool:
        """Best-effort download. Returns True if the remote file existed."""
        sftp = self._client.open_sftp()
        try:
            try:
                sftp.stat(remote_path)
            except IOError:
                return False
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            sftp.get(remote_path, local_path)
            return True
        finally:
            sftp.close()


def wait_for_ssh(host: str, user: str, password: Optional[str],
                 key_path: Optional[str], port: int = 22,
                 *, timeout: int = 300, interval: int = 5) -> None:
    """Block until SSH on ``host:port`` accepts the given credentials."""
    deadline = time.monotonic() + timeout
    last_err: Optional[BaseException] = None
    while True:
        try:
            with SshExecutor(host, user, password, key_path, port,
                             connect_timeout=10):
                return
        except (paramiko.SSHException, OSError) as exc:
            last_err = exc
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"SSH on {user}@{host}:{port} did not become reachable "
                    f"within {timeout}s. Last error: {exc}") from last_err
            time.sleep(interval)
