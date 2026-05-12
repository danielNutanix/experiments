# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Shared configuration objects, defaults and the PostgreSQL version matrix.

The framework runs **sequentially** against a **single golden VM** that
has every supported PostgreSQL major version pre-installed (each on its
own port) and seeded. Between pairs the VM is reverted to a fixed
baseline snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ============================================================
# Version matrix
# ============================================================

SUPPORTED_VERSIONS: Tuple[int, ...] = (11, 12, 13, 14, 15, 16, 17, 18)

# Single VM that contains every supported PostgreSQL version.
DEFAULT_GOLDEN_VM_NAME = "PG_UPGRADE_GOLDEN"

# Snapshot taken on the golden VM after all clusters are seeded; every
# pair restores this snapshot before running.
BASELINE_SNAPSHOT_NAME = "PG_UPGRADE_BASELINE"

# Each PostgreSQL cluster runs on a deterministic, predictable port so
# the upgrade and validation tooling never has to guess.
PORT_BASE = 5400


def port_for(version: int) -> int:
    """Return the well-known port assigned to a given PG major version."""
    return PORT_BASE + version


def all_upgrade_pairs(versions: Tuple[int, ...] = SUPPORTED_VERSIONS,
                      ) -> List[Tuple[int, int]]:
    """Return every (source, target) pair where source < target."""
    return [(src, tgt) for src in versions for tgt in versions if src < tgt]


# ============================================================
# Configuration dataclasses
# ============================================================

@dataclass
class PrismCreds:
    """Connection details for Prism Element."""

    host: str
    user: str
    password: str
    verify_ssl: bool = False


@dataclass
class SshCreds:
    """SSH credentials applied to the golden VM.

    Either ``password`` or ``key_path`` must be set.
    """

    user: str = "postgres"
    password: Optional[str] = None
    key_path: Optional[str] = None
    port: int = 22

    def __post_init__(self) -> None:
        if not self.password and not self.key_path:
            raise ValueError("SshCreds requires either `password` or `key_path`")
        if self.password and self.key_path:
            raise ValueError("SshCreds: pass either `password` or `key_path`, not both")


@dataclass
class TestPair:
    """A single (source, target) PostgreSQL upgrade combination to test."""

    source: int
    target: int

    @property
    def label(self) -> str:
        return f"pg{self.source}_to_pg{self.target}"


@dataclass
class FrameworkConfig:
    """Top-level configuration for both `bootstrap` and `run` subcommands."""

    prism: PrismCreds
    ssh: SshCreds

    install_script: str
    upgrade_script: str
    seed_sql: str
    validate_sql: str

    results_dir: str
    boot_timeout_secs: int = 300
    upgrade_timeout_secs: int = 1800
    revert_timeout_secs: int = 600

    versions: Tuple[int, ...] = SUPPORTED_VERSIONS
    golden_vm_name: str = DEFAULT_GOLDEN_VM_NAME
    baseline_snapshot_name: str = BASELINE_SNAPSHOT_NAME

    allow_missing_versions: bool = False

    pairs: List[TestPair] = field(default_factory=list)

    def port_for(self, version: int) -> int:
        return port_for(version)
