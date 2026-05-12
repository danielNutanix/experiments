# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Command-line entry point for the PG upgrade compatibility framework.

Subcommands:
  bootstrap   Install every supported PostgreSQL version on the golden
              VM, seed each, and take the BASELINE snapshot.
  run         Execute the upgrade matrix sequentially against the golden
              VM (revert -> upgrade -> validate per pair).
  list-pairs  Print every (source, target) pair that would be exercised
              by the default matrix.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from typing import List, Optional, Sequence, Tuple

import urllib3

from . import __version__
from .config import (
    BASELINE_SNAPSHOT_NAME,
    DEFAULT_GOLDEN_VM_NAME,
    FrameworkConfig,
    PrismCreds,
    SUPPORTED_VERSIONS,
    SshCreds,
    TestPair,
    all_upgrade_pairs,
)
from .golden import bootstrap
from .reporter import (
    print_console_summary,
    write_json_report,
    write_matrix_report,
)
from .runner import run_matrix


LOGGER = logging.getLogger("pg_upgrade_test")

DEFAULT_RESULTS_DIR = "./results"
DEFAULT_BOOT_TIMEOUT = 300
DEFAULT_UPGRADE_TIMEOUT = 1800

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INSTALL_SCRIPT = os.path.join(REPO_ROOT, "scripts", "install_all_pg.sh")
DEFAULT_UPGRADE_SCRIPT = os.path.join(REPO_ROOT, "scripts", "pg_upgrade.sh")
DEFAULT_SEED_SQL = os.path.join(REPO_ROOT, "scripts", "seed.sql")
DEFAULT_VALIDATE_SQL = os.path.join(REPO_ROOT, "scripts", "validate.sql")


# ============================================================
# Logging and warning suppression
# ============================================================

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# Argument parsing
# ============================================================

def _add_common_args(p: argparse.ArgumentParser) -> None:
    g_prism = p.add_argument_group("Prism")
    g_prism.add_argument("--prism-host", required=True,
                         help="Prism Element IP/FQDN")
    g_prism.add_argument("--prism-user", default=os.environ.get("PRISM_USER"),
                         help="Prism username (env: PRISM_USER)")
    g_prism.add_argument("--prism-password",
                         default=os.environ.get("PRISM_PASSWORD"),
                         help="Prism password (env: PRISM_PASSWORD; "
                              "prompted if absent)")
    g_prism.add_argument("--verify-ssl", action="store_true",
                         help="Verify Prism's TLS certificate")

    g_ssh = p.add_argument_group("SSH (golden VM)")
    g_ssh.add_argument("--ssh-user", default="postgres",
                       help="SSH username (default: postgres)")
    g_ssh.add_argument("--ssh-password",
                       default=os.environ.get("SSH_PASSWORD"),
                       help="SSH password (env: SSH_PASSWORD); "
                            "omit when using --ssh-key")
    g_ssh.add_argument("--ssh-key", default=os.environ.get("SSH_KEY"),
                       help="SSH private key path (env: SSH_KEY)")
    g_ssh.add_argument("--ssh-port", type=int, default=22)

    g_versions = p.add_argument_group("Version matrix")
    g_versions.add_argument(
        "--versions",
        help="Comma-separated PostgreSQL versions to consider "
             f"(default: {','.join(str(v) for v in SUPPORTED_VERSIONS)})")
    g_versions.add_argument(
        "--golden-vm-name", default=DEFAULT_GOLDEN_VM_NAME,
        help=f"Name of the single multi-version golden VM "
             f"(default: '{DEFAULT_GOLDEN_VM_NAME}')")
    g_versions.add_argument(
        "--baseline-snapshot", default=BASELINE_SNAPSHOT_NAME,
        help=f"Snapshot name on the golden VM (default: "
             f"'{BASELINE_SNAPSHOT_NAME}')")

    g_paths = p.add_argument_group("Paths")
    g_paths.add_argument("--install-script", default=DEFAULT_INSTALL_SCRIPT,
                         help=f"Local install_all_pg.sh path "
                              f"(default: {DEFAULT_INSTALL_SCRIPT})")
    g_paths.add_argument("--upgrade-script", default=DEFAULT_UPGRADE_SCRIPT,
                         help=f"Local pg_upgrade.sh path "
                              f"(default: {DEFAULT_UPGRADE_SCRIPT})")
    g_paths.add_argument("--seed-sql", default=DEFAULT_SEED_SQL,
                         help=f"Local seed.sql path "
                              f"(default: {DEFAULT_SEED_SQL})")
    g_paths.add_argument("--validate-sql", default=DEFAULT_VALIDATE_SQL,
                         help=f"Local validate.sql path "
                              f"(default: {DEFAULT_VALIDATE_SQL})")
    g_paths.add_argument("--results-dir", default=DEFAULT_RESULTS_DIR,
                         help=f"Local directory for JSON, matrix and "
                              f"per-pair logs (default: {DEFAULT_RESULTS_DIR})")

    p.add_argument("--verbose", action="store_true",
                   help="Enable DEBUG logging including HTTP traffic")


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--pairs",
        help="Comma-separated subset of pairs to run, e.g. '11->15,12->18'. "
             "Default: every (src, tgt) where src < tgt.")
    p.add_argument("--source", type=int, action="append",
                   help="Restrict source major version(s); repeatable")
    p.add_argument("--target", type=int, action="append",
                   help="Restrict target major version(s); repeatable")
    p.add_argument("--boot-timeout", type=int, default=DEFAULT_BOOT_TIMEOUT,
                   help=f"Seconds to wait for boot+SSH+IP after revert "
                        f"(default: {DEFAULT_BOOT_TIMEOUT})")
    p.add_argument("--upgrade-timeout", type=int,
                   default=DEFAULT_UPGRADE_TIMEOUT,
                   help=f"Seconds to wait for pg_upgrade.sh (default: "
                        f"{DEFAULT_UPGRADE_TIMEOUT})")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pg-upgrade-test",
        description="Sequential PostgreSQL major-version upgrade testing on "
                    "Nutanix Prism (single golden VM, snapshot-revert).")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_boot = sub.add_parser(
        "bootstrap",
        help="Install all PG versions on the golden VM and snapshot it",
        description="Install every supported PostgreSQL major version on "
                    "the golden VM (each on port 5400+N), seed each "
                    "cluster, then take the BASELINE snapshot.")
    _add_common_args(p_boot)
    p_boot.add_argument(
        "--allow-missing-versions", action="store_true",
        help="Continue if individual PG versions are unavailable from "
             "PGDG for the chosen OS (e.g. PG11 on RHEL 9). The manifest "
             "of installed versions is always written; `run` reads it "
             "and silently skips pairs that reference a missing version.")

    p_run = sub.add_parser(
        "run", help="Run the upgrade compatibility matrix sequentially",
        description="For each (source, target) pair: revert the golden VM "
                    "to BASELINE, run pg_upgrade in place, run validation, "
                    "collect logs.")
    _add_common_args(p_run)
    _add_run_args(p_run)

    p_list = sub.add_parser(
        "list-pairs",
        help="Print the default upgrade pair list and exit")
    p_list.add_argument(
        "--versions",
        help="Comma-separated versions to use; default is the full matrix")
    return parser


# ============================================================
# Helpers shared between subcommands
# ============================================================

def _parse_versions(value: Optional[str]) -> Tuple[int, ...]:
    if not value:
        return SUPPORTED_VERSIONS
    parsed = tuple(sorted({int(token.strip()) for token in value.split(",")
                           if token.strip()}))
    if not parsed:
        raise SystemExit("--versions provided no valid integers")
    return parsed


def _parse_pairs(value: Optional[str], versions: Tuple[int, ...],
                 sources: Optional[List[int]],
                 targets: Optional[List[int]]) -> List[TestPair]:
    if value:
        pairs = []
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if "->" not in token:
                raise SystemExit(f"--pairs item '{token}' must look like 'src->tgt'")
            src_s, tgt_s = token.split("->", 1)
            src, tgt = int(src_s), int(tgt_s)
            if src >= tgt:
                raise SystemExit(f"Pair {src}->{tgt} requires src < tgt")
            pairs.append(TestPair(source=src, target=tgt))
        return pairs

    pairs = [TestPair(source=s, target=t)
             for (s, t) in all_upgrade_pairs(versions)]
    if sources:
        pairs = [p for p in pairs if p.source in set(sources)]
    if targets:
        pairs = [p for p in pairs if p.target in set(targets)]
    return pairs


def _ensure_password(value: Optional[str], prompt: str) -> str:
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"{prompt} not provided and stdin is not a TTY")
    return getpass.getpass(prompt + ": ")


def _build_config(args: argparse.Namespace,
                  *, pairs: Optional[List[TestPair]] = None) -> FrameworkConfig:
    if not args.prism_user:
        raise SystemExit("--prism-user (or env PRISM_USER) is required")
    prism_password = _ensure_password(args.prism_password,
                                      f"Prism password for {args.prism_user}")
    if not args.ssh_password and not args.ssh_key:
        ssh_password: Optional[str] = _ensure_password(
            None, f"SSH password for {args.ssh_user}")
        ssh_key = None
    else:
        ssh_password = args.ssh_password
        ssh_key = args.ssh_key

    versions = _parse_versions(getattr(args, "versions", None))

    return FrameworkConfig(
        prism=PrismCreds(host=args.prism_host, user=args.prism_user,
                         password=prism_password,
                         verify_ssl=bool(args.verify_ssl)),
        ssh=SshCreds(user=args.ssh_user, password=ssh_password,
                     key_path=ssh_key, port=args.ssh_port),
        install_script=os.path.abspath(args.install_script),
        upgrade_script=os.path.abspath(args.upgrade_script),
        seed_sql=os.path.abspath(args.seed_sql),
        validate_sql=os.path.abspath(args.validate_sql),
        results_dir=os.path.abspath(args.results_dir),
        boot_timeout_secs=getattr(args, "boot_timeout", DEFAULT_BOOT_TIMEOUT),
        upgrade_timeout_secs=getattr(args, "upgrade_timeout",
                                     DEFAULT_UPGRADE_TIMEOUT),
        versions=versions,
        golden_vm_name=args.golden_vm_name,
        baseline_snapshot_name=args.baseline_snapshot,
        allow_missing_versions=bool(getattr(args, "allow_missing_versions",
                                            False)),
        pairs=pairs or [],
    )


# ============================================================
# Subcommand handlers
# ============================================================

def _cmd_bootstrap(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    for path in (cfg.install_script, cfg.upgrade_script,
                 cfg.seed_sql, cfg.validate_sql):
        if not os.path.isfile(path):
            raise SystemExit(f"Required local file missing: {path}")

    status = bootstrap(cfg)
    print()
    installed = ", ".join(str(v) for v in status.installed_versions) or "<none>"
    skipped = ", ".join(str(v) for v in status.skipped_versions) or "<none>"
    if status.succeeded:
        print(f"Bootstrap of '{status.vm_name}' SUCCEEDED.")
        print(f"  vm_uuid            : {status.vm_uuid}")
        print(f"  snapshot_uuid      : {status.snapshot_uuid}")
        print(f"  installed_versions : {installed}")
        print(f"  skipped_versions   : {skipped}")
        return 0
    print(f"Bootstrap of '{status.vm_name}' FAILED: {status.failure_reason}")
    if status.installed_versions:
        print(f"  installed_versions : {installed}")
    return 2


def _cmd_run(args: argparse.Namespace) -> int:
    versions = _parse_versions(args.versions)
    pairs = _parse_pairs(args.pairs, versions, args.source, args.target)
    if not pairs:
        raise SystemExit("No upgrade pairs selected after applying filters")
    cfg = _build_config(args, pairs=pairs)

    for path in (cfg.upgrade_script, cfg.validate_sql):
        if not os.path.isfile(path):
            raise SystemExit(f"Required local file missing: {path}")
    os.makedirs(cfg.results_dir, exist_ok=True)

    LOGGER.info("Pairs to run (%d): %s", len(pairs),
                ", ".join(p.label for p in pairs))

    results = run_matrix(cfg)

    write_json_report(results, os.path.join(cfg.results_dir, "results.json"))
    write_matrix_report(results, os.path.join(cfg.results_dir, "matrix.txt"),
                        versions=cfg.versions)
    print_console_summary(results)

    non_passing = [r for r in results if r.status != "PASSED"]
    return 0 if not non_passing else 1


def _cmd_list_pairs(args: argparse.Namespace) -> int:
    versions = _parse_versions(args.versions)
    pairs = all_upgrade_pairs(versions)
    print(f"Versions: {','.join(str(v) for v in versions)}")
    print(f"Total pairs: {len(pairs)}")
    for src, tgt in pairs:
        print(f"  {src} -> {tgt}")
    return 0


# ============================================================
# Entry point
# ============================================================

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    if args.command == "bootstrap":
        return _cmd_bootstrap(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "list-pairs":
        return _cmd_list_pairs(args)
    parser.error(f"Unknown command: {args.command}")
    return 2  # unreachable
