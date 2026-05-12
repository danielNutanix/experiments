# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Shared configuration objects, defaults and the PostgreSQL version matrix.

The version matrix is centralised here so adding a new PostgreSQL major
version (e.g. PG19) becomes a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ============================================================
# Version matrix
# ============================================================

SUPPORTED_VERSIONS: Tuple[int, ...] = (11, 12, 13, 14, 15, 16, 17, 18)

# Convention for a per-version golden VM name on Prism. The integer is
# substituted with each supported major version.
GOLDEN_VM_NAME_TEMPLATE = "PG{version}_GOLDEN"

# Snapshot taken on every golden VM after seeding. All test clones come
# from this snapshot so each run starts from byte-identical state.
BASELINE_SNAPSHOT_NAME = "PG_UPGRADE_TEST_BASELINE"


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
    """SSH credentials applied to every cloned test VM.

    The same credentials are used for all clones because they're imaged
    from the same golden VM. Either ``password`` or ``key_path`` must be
    set.
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

    upgrade_script: str
    seed_sql: str
    validate_sql: str

    results_dir: str
    max_parallel: int = 4
    boot_timeout_secs: int = 300
    upgrade_timeout_secs: int = 1800

    versions: Tuple[int, ...] = SUPPORTED_VERSIONS
    golden_vm_name_template: str = GOLDEN_VM_NAME_TEMPLATE
    baseline_snapshot_name: str = BASELINE_SNAPSHOT_NAME

    pairs: List[TestPair] = field(default_factory=list)

    def golden_vm_name(self, version: int) -> str:
        return self.golden_vm_name_template.format(version=version)
