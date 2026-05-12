# pg-upgrade-test

Automated PostgreSQL major-version upgrade compatibility testing on
Nutanix Prism. Validates every `(source, target)` pair in
`{11, 12, 13, 14, 15, 16, 17, 18}` where `source < target` (28 pairs by
default) by cloning a per-version golden VM, performing an in-place
`pg_upgrade`, and running a validation suite — all in parallel.

Lives as a **fully separate project** alongside (but completely
independent of) `../pg_upgrade/` — its own `pyproject.toml`, its own
virtualenv, its own copy of `pg_upgrade.sh`.

## Architecture

```
+----------------------+
|       CLI            |   pg_upgrade_test/cli.py
+----------+-----------+
           |
           v
+----------------------+    +-------------------------+
|  Matrix Runner       |--->| Prism client (REST v2)  |
|  (ThreadPoolExecutor)|    +-------------------------+
+----------+-----------+
           |
           v
+----------------------+    +-------------------------+
|   Per-pair workflow  |--->| SSH executor (paramiko) |
|  clone -> boot ->    |    +-------------------------+
|  upgrade -> validate |
|  -> log -> destroy   |
+----------+-----------+
           |
           v
+----------------------+
|   Reporter           |   results.json + matrix.txt + console summary
+----------------------+
```

## Layout

```
pg_upgrade_test_framework/
├── pyproject.toml
├── README.md
├── pg_upgrade_test/             # Python package
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                   # `pg-upgrade-test` console script
│   ├── config.py                # version matrix, dataclasses
│   ├── prism.py                 # Prism Element REST client
│   ├── ssh_exec.py              # paramiko wrapper with streaming + tail
│   ├── golden.py                # bootstrap golden VMs + BASELINE snapshot
│   ├── upgrade.py               # per-pair workflow
│   ├── validation.py            # post-upgrade validation suite
│   ├── runner.py                # parallel matrix runner
│   └── reporter.py              # JSON / matrix / console reports
└── scripts/
    ├── pg_upgrade.sh            # in-place pg_upgrade driver (RHEL & Debian)
    ├── seed.sql                 # representative dataset
    └── validate.sql             # post-upgrade SQL checks
```

## Prerequisites

1. **One golden VM per supported PostgreSQL major version** on Prism,
   each named `PG{N}_GOLDEN` (configurable via
   `--golden-vm-template`). Each must have:
   - OS installed (RHEL family with `dnf`, or Debian/Ubuntu with `apt`),
   - PostgreSQL `{N}` installed and the cluster initialised,
   - SSH reachable as a user with passwordless `sudo`.
2. `uv` installed locally (or any tool that understands `pyproject.toml`).
3. Network reachability from the host running the framework to **both**
   the Prism endpoint and to the cloned VMs' learned IPs.

## Install

```bash
cd pg_upgrade_test_framework
uv sync
```

## Usage

### 1. Bootstrap golden VMs (one-time per version)

```bash
uv run pg-upgrade-test bootstrap \
    --prism-host 10.10.0.5 \
    --prism-user admin \
    --ssh-user postgres
```

This boots each `PG{N}_GOLDEN` VM, runs `seed.sql`, powers it off, and
takes the `PG_UPGRADE_TEST_BASELINE` snapshot. Re-running it just
refreshes the snapshot. To bootstrap a single version:

```bash
uv run pg-upgrade-test bootstrap --only 11 ...
```

### 2. Run the matrix

```bash
# Full default matrix (28 pairs), 4 in parallel:
uv run pg-upgrade-test run \
    --prism-host 10.10.0.5 \
    --prism-user admin

# Subset of pairs:
uv run pg-upgrade-test run --pairs '11->15,13->18' ...

# Subset by source/target filter (repeatable):
uv run pg-upgrade-test run --source 11 --source 12 --target 18 ...

# Higher concurrency:
uv run pg-upgrade-test run --max-parallel 8 ...
```

Outputs land in `--results-dir` (default `./results/`):

- `results.json` — full structured results with per-check details,
- `matrix.txt`   — compact source-vs-target grid,
- `<src->tgt>/`  — per-pair PostgreSQL/pg_upgrade logs.

### 3. Inspect the matrix

```bash
uv run pg-upgrade-test list-pairs
uv run pg-upgrade-test list-pairs --versions 11,15,18
```

## Configuration reference

All flags accept their value either via CLI argument or env var (where
shown). Required = ✓, conditionally required = ◐, optional = —.

| Flag | Env var | Req | Default | Purpose |
|---|---|---|---|---|
| `--prism-host` | — | ✓ | — | Prism Element IP/FQDN |
| `--prism-user` | `PRISM_USER` | ✓ | — | Prism username |
| `--prism-password` | `PRISM_PASSWORD` | ◐ | prompted | Prism password |
| `--verify-ssl` | — | — | off | Verify Prism's TLS cert |
| `--ssh-user` | — | — | `postgres` | SSH user on golden VMs and clones |
| `--ssh-password` | `SSH_PASSWORD` | ◐ | prompted | Required if `--ssh-key` not given |
| `--ssh-key` | `SSH_KEY` | ◐ | — | Path to SSH private key |
| `--ssh-port` | — | — | `22` | SSH port |
| `--versions` | — | — | `11,12,13,14,15,16,17,18` | PG versions in scope |
| `--golden-vm-template` | — | — | `PG{version}_GOLDEN` | Naming convention for golden VMs |
| `--baseline-snapshot` | — | — | `PG_UPGRADE_TEST_BASELINE` | Snapshot taken on each golden VM |
| `--upgrade-script` | — | — | `scripts/pg_upgrade.sh` | Local path to upgrade driver |
| `--seed-sql` | — | — | `scripts/seed.sql` | Local path to seed SQL |
| `--validate-sql` | — | — | `scripts/validate.sql` | Local path to validation SQL |
| `--results-dir` | — | — | `./results` | Output root |
| `--max-parallel` (`run`) | — | — | `4` | Max concurrent test workers |
| `--boot-timeout` (`run`) | — | — | `300` | Sec to wait for boot/IP/SSH |
| `--upgrade-timeout` (`run`) | — | — | `1800` | Sec to wait for `pg_upgrade.sh` |
| `--pairs` (`run`) | — | — | full matrix | Explicit list, e.g. `11->15,12->18` |
| `--source` (`run`) | — | — | — | Filter by source version (repeatable) |
| `--target` (`run`) | — | — | — | Filter by target version (repeatable) |
| `--only` (`bootstrap`) | — | — | all versions | Bootstrap a single version |
| `--verbose` | — | — | off | DEBUG logging including HTTP traffic |

## Validation suite

Each post-upgrade run executes against the upgraded cluster:

| Check | What it confirms |
|---|---|
| `server_version` | `SELECT version()` returns the target major |
| `crud` | INSERT/UPDATE/DELETE/SELECT on a temp table |
| `vacuum_analyze` | `VACUUM (ANALYZE)` succeeds on seeded tables |
| `reindex` | `REINDEX SCHEMA upgrade_test` succeeds |
| `validate_sql_q*` | Each statement in `scripts/validate.sql` returns `OK` (catalogs, partitions, JSONB, triggers, view definitions, index validity) |

## Non-functional properties

- **Parallel** — pairs run concurrently in a thread pool; each pair
  has its own clone, IP, SSH session, and snapshot.
- **Idempotent** — `bootstrap` refreshes the BASELINE snapshot; `run`
  always destroys clones in `finally`.
- **Deterministic** — clones boot from the same byte-identical
  snapshot, so failures reproduce.
- **Failure-isolated** — a single pair's failure never blocks others;
  results are aggregated at the end.
- **Storage-light** — relies on Prism snapshots/clones rather than full
  copies.
- **Extensible** — adding PG19 means: provision `PG19_GOLDEN`, append
  `19` to `SUPPORTED_VERSIONS`, run `bootstrap --only 19`, then `run`.
