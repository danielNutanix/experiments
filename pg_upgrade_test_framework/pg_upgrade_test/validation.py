# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Post-upgrade validation suite executed on each test VM via psql.

Every check is implemented as either a remote shell command (VACUUM,
ANALYZE, REINDEX) or a SQL statement defined in ``scripts/validate.sql``.
Each SQL row must start with the literal ``OK``; anything else is treated
as a failure.
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

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=passed, detail=detail))


# ============================================================
# Helpers
# ============================================================

def _psql_path(version: int, os_family: str) -> str:
    if os_family == "rhel":
        return f"/usr/pgsql-{version}/bin/psql"
    return f"/usr/lib/postgresql/{version}/bin/psql"


def _run_psql(ssh: SshExecutor, version: int, os_family: str,
              sql: str, *, dbname: str = "postgres",
              check: bool = True) -> str:
    psql = _psql_path(version, os_family)
    cmd = (f"sudo -u postgres {psql} -d {dbname} -tA -X -v ON_ERROR_STOP=1 "
           f"-c {_shell_quote(sql)}")
    rc, out, err = ssh.run(cmd, check=False)
    if check and rc != 0:
        raise RuntimeError(f"psql failed (rc={rc}): {err.strip() or out.strip()}")
    return out


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# ============================================================
# Individual checks
# ============================================================

def check_server_version(ssh: SshExecutor, version: int,
                         os_family: str) -> CheckResult:
    out = _run_psql(ssh, version, os_family, "SELECT version();")
    text = out.strip()
    expected = f"PostgreSQL {version}"
    passed = expected in text
    return CheckResult(name="server_version",
                       passed=passed,
                       detail=text if passed else f"expected '{expected}' in: {text}")


def check_crud(ssh: SshExecutor, version: int, os_family: str) -> CheckResult:
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
    out = _run_psql(ssh, version, os_family, sql).strip().splitlines()
    answer = out[-1].strip() if out else ""
    expected = "2:a,B"
    passed = answer == expected
    return CheckResult(name="crud",
                       passed=passed,
                       detail=f"expected={expected} got={answer}")


def check_vacuum_analyze(ssh: SshExecutor, version: int,
                         os_family: str) -> CheckResult:
    try:
        _run_psql(ssh, version, os_family,
                  "VACUUM (ANALYZE) upgrade_test.customers, upgrade_test.orders;")
        return CheckResult(name="vacuum_analyze", passed=True)
    except RuntimeError as exc:
        return CheckResult(name="vacuum_analyze", passed=False, detail=str(exc))


def check_reindex(ssh: SshExecutor, version: int,
                  os_family: str) -> CheckResult:
    try:
        _run_psql(ssh, version, os_family,
                  "REINDEX SCHEMA upgrade_test;")
        return CheckResult(name="reindex", passed=True)
    except RuntimeError as exc:
        return CheckResult(name="reindex", passed=False, detail=str(exc))


def run_validate_sql(ssh: SshExecutor, version: int, os_family: str,
                     remote_sql_path: str) -> List[CheckResult]:
    """Run validate.sql; each statement must produce a row starting with OK."""
    psql = _psql_path(version, os_family)
    cmd = (f"sudo -u postgres {psql} -d postgres -tA -X "
           f"-v ON_ERROR_STOP=1 -f {remote_sql_path}")
    rc, out, err = ssh.run(cmd, check=False)
    results: List[CheckResult] = []
    if rc != 0:
        results.append(CheckResult(
            name="validate_sql",
            passed=False,
            detail=f"psql exited rc={rc}: {err.strip() or out.strip()}"))
        return results
    for idx, raw in enumerate(out.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        first = line.split("|", 1)[0].strip()
        passed = first == "OK"
        results.append(CheckResult(
            name=f"validate_sql_q{idx}",
            passed=passed,
            detail=line))
    if not results:
        results.append(CheckResult(name="validate_sql",
                                   passed=False,
                                   detail="no output rows from validate.sql"))
    return results


# ============================================================
# Top-level entry point
# ============================================================

def run_full_validation(ssh: SshExecutor, target_version: int,
                        os_family: str, remote_validate_sql: str,
                        ) -> ValidationReport:
    """Run every validation check; never raises — returns a report."""
    report = ValidationReport()
    report.checks.append(check_server_version(ssh, target_version, os_family))
    report.checks.append(check_crud(ssh, target_version, os_family))
    report.checks.append(check_vacuum_analyze(ssh, target_version, os_family))
    report.checks.append(check_reindex(ssh, target_version, os_family))
    report.checks.extend(run_validate_sql(ssh, target_version,
                                          os_family, remote_validate_sql))
    return report
