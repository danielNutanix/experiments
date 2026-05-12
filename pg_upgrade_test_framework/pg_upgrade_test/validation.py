# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Post-upgrade validation suite executed on the golden VM via psql.

Each cluster runs on a deterministic port (``PORT_BASE + version``), so
all psql invocations are explicit about port and version-specific binary
path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from .ssh_exec import SshExecutor


LOGGER = logging.getLogger(__name__)


# ============================================================
# Result containers
# ============================================================

@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ValidationReport:
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


# ============================================================
# Helpers
# ============================================================

def psql_path(version: int, os_family: str) -> str:
    if os_family == "dnf":
        return f"/usr/pgsql-{version}/bin/psql"
    return f"/usr/lib/postgresql/{version}/bin/psql"


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def run_psql(ssh: SshExecutor, version: int, os_family: str, port: int,
             sql: str, *, dbname: str = "postgres",
             check: bool = True) -> str:
    psql = psql_path(version, os_family)
    cmd = (f"sudo -u postgres {psql} -p {port} -d {dbname} -tA -X "
           f"-v ON_ERROR_STOP=1 -c {_shell_quote(sql)}")
    rc, out, err = ssh.run(cmd, check=False)
    if check and rc != 0:
        raise RuntimeError(f"psql failed (rc={rc}): {err.strip() or out.strip()}")
    return out


# ============================================================
# Individual checks
# ============================================================

def check_server_version(ssh: SshExecutor, version: int, os_family: str,
                         port: int) -> CheckResult:
    out = run_psql(ssh, version, os_family, port, "SELECT version();")
    text = out.strip()
    expected = f"PostgreSQL {version}"
    passed = expected in text
    return CheckResult(name="server_version", passed=passed,
                       detail=text if passed else f"expected '{expected}' in: {text}")


def check_crud(ssh: SshExecutor, version: int, os_family: str,
               port: int) -> CheckResult:
    sql = (
        "BEGIN; "
        "CREATE TEMP TABLE _u_probe (id INT, payload TEXT); "
        "INSERT INTO _u_probe VALUES (1,'a'),(2,'b'),(3,'c'); "
        "UPDATE _u_probe SET payload = upper(payload) WHERE id = 2; "
        "DELETE FROM _u_probe WHERE id = 3; "
        "SELECT count(*) || ':' || string_agg(payload, ',' ORDER BY id) "
        "FROM _u_probe; "
        "ROLLBACK;"
    )
    out = run_psql(ssh, version, os_family, port, sql).strip().splitlines()
    answer = out[-1].strip() if out else ""
    expected = "2:a,B"
    passed = answer == expected
    return CheckResult(name="crud", passed=passed,
                       detail=f"expected={expected} got={answer}")


def check_vacuum_analyze(ssh: SshExecutor, version: int, os_family: str,
                         port: int) -> CheckResult:
    try:
        run_psql(ssh, version, os_family, port,
                 "VACUUM (ANALYZE) upgrade_test.customers, upgrade_test.orders;")
        return CheckResult(name="vacuum_analyze", passed=True)
    except RuntimeError as exc:
        return CheckResult(name="vacuum_analyze", passed=False, detail=str(exc))


def check_reindex(ssh: SshExecutor, version: int, os_family: str,
                  port: int) -> CheckResult:
    try:
        run_psql(ssh, version, os_family, port,
                 "REINDEX SCHEMA upgrade_test;")
        return CheckResult(name="reindex", passed=True)
    except RuntimeError as exc:
        return CheckResult(name="reindex", passed=False, detail=str(exc))


def run_validate_sql(ssh: SshExecutor, version: int, os_family: str,
                     port: int, remote_sql_path: str) -> List[CheckResult]:
    """Run validate.sql; each statement must produce a row starting with OK."""
    psql = psql_path(version, os_family)
    cmd = (f"sudo -u postgres {psql} -p {port} -d postgres -tA -X "
           f"-v ON_ERROR_STOP=1 -f {remote_sql_path}")
    rc, out, err = ssh.run(cmd, check=False)
    results: List[CheckResult] = []
    if rc != 0:
        results.append(CheckResult(name="validate_sql", passed=False,
                                   detail=f"psql exited rc={rc}: "
                                          f"{err.strip() or out.strip()}"))
        return results
    for idx, raw in enumerate(out.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        first = line.split("|", 1)[0].strip()
        results.append(CheckResult(name=f"validate_sql_q{idx}",
                                   passed=(first == "OK"), detail=line))
    if not results:
        results.append(CheckResult(name="validate_sql", passed=False,
                                   detail="no output rows from validate.sql"))
    return results


# ============================================================
# Top-level entry point
# ============================================================

def run_full_validation(ssh: SshExecutor, target_version: int,
                        os_family: str, port: int,
                        remote_validate_sql: str) -> ValidationReport:
    report = ValidationReport()
    report.checks.append(check_server_version(ssh, target_version,
                                              os_family, port))
    report.checks.append(check_crud(ssh, target_version, os_family, port))
    report.checks.append(check_vacuum_analyze(ssh, target_version,
                                              os_family, port))
    report.checks.append(check_reindex(ssh, target_version, os_family, port))
    report.checks.extend(run_validate_sql(ssh, target_version, os_family,
                                          port, remote_validate_sql))
    return report
