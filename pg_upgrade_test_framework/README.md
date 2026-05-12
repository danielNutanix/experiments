# pg-upgrade-test

Sequential PostgreSQL major-version upgrade compatibility testing on
Nutanix Prism. Validates every `(source, target)` pair in
`{11, 12, 13, 14, 15, 16, 17, 18}` where `source < target` (28 pairs by
default) using **one** multi-version golden VM and Prism snapshot
revert between pairs.

Lives as a fully separate project alongside (but completely independent
of) `../pg_upgrade/` — its own `pyproject.toml`, its own virtualenv, its
own scripts.

## Architecture

```
+----------------------+
|       CLI            |   pg_upgrade_test/cli.py
+----------+-----------+
           |
           v
+----------------------+    +-------------------------+
|  Sequential Runner   |--->| Prism client (REST v2)  |
+----------+-----------+    +-------------------------+
           |
           v
+----------------------+        Per pair:
|   Per-pair workflow  |          1. power off
|                      |          2. restore snapshot
|                      |          3. power on, wait for IP+SSH
|                      |          4. run pg_upgrade.sh in place
|                      |          5. run validation suite
|                      |          6. collect logs
+----------+-----------+
           |
           v
+----------------------+
|   Reporter           |   results.json + matrix.txt + console summary
+----------------------+
```

A single golden VM holds every supported PostgreSQL major version
side-by-side. Each version's cluster lives on a deterministic port:

| PG version | Port | RHEL PGDATA | Debian/Ubuntu PGDATA |
|---|---|---|---|
| 11 | 5411 | `/var/lib/pgsql/11/data` | `/var/lib/postgresql/11/main` |
| 12 | 5412 | `/var/lib/pgsql/12/data` | `/var/lib/postgresql/12/main` |
| ... | ... | ... | ... |
| 18 | 5418 | `/var/lib/pgsql/18/data` | `/var/lib/postgresql/18/main` |

Per-version PGDATA is whatever the OS package manager (`postgresql-N`
on apt, `postgresql{N}-server` on dnf) installs natively, with
`postgresql.conf:port` overridden to `5400 + N`.

## Layout

```
pg_upgrade_test_framework/
├── pyproject.toml
├── README.md
├── pg_upgrade_test/             # Python package
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py                   # `pg-upgrade-test` console script
│   ├── config.py                # version matrix, port convention
│   ├── prism.py                 # Prism Element REST client (incl. restore_snapshot)
│   ├── ssh_exec.py              # paramiko wrapper with streaming + tail
│   ├── golden.py                # one-shot bootstrap of the golden VM
│   ├── upgrade.py               # per-pair workflow (revert -> upgrade -> validate)
│   ├── validation.py            # post-upgrade validation suite
│   ├── runner.py                # sequential matrix runner
│   └── reporter.py              # JSON / matrix / console reports
└── scripts/
    ├── install_all_pg.sh        # PGDG repo + install + seed all versions
    ├── pg_upgrade.sh            # in-place pg_upgrade between two versions
    ├── seed.sql                 # representative dataset
    └── validate.sql             # post-upgrade SQL checks
```

## Prerequisites

1. **One golden VM on Prism**, named `PG_UPGRADE_GOLDEN` by default
   (override with `--golden-vm-name`), with:
   - A supported OS — RHEL/Rocky 8 (`dnf`) or Debian 12 / Ubuntu 22.04 (`apt`).
     RHEL/Rocky 9 only supports PG 12+ via PGDG.
   - SSH reachable as a user with passwordless `sudo`.
   - Outbound network access to PGDG repos
     (`apt.postgresql.org` or `download.postgresql.org`).
2. `uv` installed locally.
3. Network reachability from the host running the framework to **both**
   Prism and the golden VM's learned IP.

## Install

```bash
cd pg_upgrade_test_framework
uv sync
```

## Usage

### 1. Bootstrap the golden VM (one-time, then re-run to refresh)

```bash
uv run pg-upgrade-test bootstrap \
    --prism-host 10.10.0.5 \
    --prism-user admin \
    --golden-vm-name PG_UPGRADE_GOLDEN \
    --ssh-user postgres
```

This boots the VM, installs PG 11–18 from PGDG (each on port `5400+N`),
runs `seed.sql` against every cluster, powers off, and replaces the
`PG_UPGRADE_BASELINE` snapshot.

If the chosen OS doesn't ship every version (e.g. PG 11 isn't packaged
by PGDG for RHEL/Rocky 9), pass `--allow-missing-versions`:

```bash
uv run pg-upgrade-test bootstrap --allow-missing-versions ...
```

The bootstrap then logs a warning per missing version and continues.
The list of versions that actually installed is written to
`/var/lib/pg_upgrade_test/versions.txt` on the VM, captured in the
`PG_UPGRADE_BASELINE` snapshot, and reported in the bootstrap summary
(`installed_versions` / `skipped_versions`). Subsequent `run`
invocations read this manifest automatically and skip pairs that
reference a missing version — no extra flag needed.

### 2. Run the matrix

```bash
# Full default matrix (28 pairs, sequentially):
uv run pg-upgrade-test run \
    --prism-host 10.10.0.5 \
    --prism-user admin

# Subset of pairs:
uv run pg-upgrade-test run --pairs '11->15,13->18' ...

# Subset by source/target filter (repeatable):
uv run pg-upgrade-test run --source 11 --source 12 --target 18 ...
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
| `--ssh-user` | — | — | `postgres` | SSH user on the golden VM |
| `--ssh-password` | `SSH_PASSWORD` | ◐ | prompted | Required if `--ssh-key` not given |
| `--ssh-key` | `SSH_KEY` | ◐ | — | Path to SSH private key |
| `--ssh-port` | — | — | `22` | SSH port |
| `--versions` | — | — | `11,12,13,14,15,16,17,18` | PG versions in scope |
| `--golden-vm-name` | — | — | `PG_UPGRADE_GOLDEN` | Name of the single multi-version VM |
| `--baseline-snapshot` | — | — | `PG_UPGRADE_BASELINE` | Snapshot name on the golden VM |
| `--install-script` | — | — | `scripts/install_all_pg.sh` | Bootstrap helper script |
| `--upgrade-script` | — | — | `scripts/pg_upgrade.sh` | In-place upgrade script |
| `--seed-sql` | — | — | `scripts/seed.sql` | Local path to seed SQL |
| `--validate-sql` | — | — | `scripts/validate.sql` | Local path to validation SQL |
| `--results-dir` | — | — | `./results` | Output root |
| `--boot-timeout` (`run`) | — | — | `300` | Sec to wait for boot/IP/SSH after revert |
| `--upgrade-timeout` (`run`) | — | — | `1800` | Sec to wait for `pg_upgrade.sh` |
| `--pairs` (`run`) | — | — | full matrix | Explicit list, e.g. `11->15,12->18` |
| `--source` (`run`) | — | — | — | Filter by source version (repeatable) |
| `--target` (`run`) | — | — | — | Filter by target version (repeatable) |
| `--allow-missing-versions` (`bootstrap`) | — | — | off | Tolerate per-version install failures (e.g. PG 11 on RHEL 9). `run` then auto-skips affected pairs via the on-VM manifest. |
| `--verbose` | — | — | off | DEBUG logging including HTTP traffic |

## Validation suite

Each post-upgrade run executes against the upgraded cluster on its
deterministic port (`5400 + target_version`):

| Check | What it confirms |
|---|---|
| `server_version` | `SELECT version()` returns the target major |
| `crud` | INSERT/UPDATE/DELETE/SELECT on a temp table |
| `vacuum_analyze` | `VACUUM (ANALYZE)` succeeds on seeded tables |
| `reindex` | `REINDEX SCHEMA upgrade_test` succeeds |
| `validate_sql_q*` | Each statement in `scripts/validate.sql` returns `OK` (catalogs, partitions, JSONB, triggers, view definitions, index validity) |

## Properties

- **Sequential** — one pair at a time; the framework is single-VM by design.
- **Idempotent** — `bootstrap` refreshes the BASELINE snapshot; every
  pair starts from byte-identical disk state via snapshot revert.
- **Deterministic** — same snapshot, same upgrade script, same ports
  every run.
- **Failure-isolated** — a single pair's failure can never bleed into
  the next: the VM is reverted before the next pair runs.
- **Storage-light** — one VM + one snapshot total.
- **Extensible** — adding PG19 means: append `19` to
  `SUPPORTED_VERSIONS`, ensure PGDG ships `postgresql19` for the chosen
  OS, re-run `bootstrap`, then `run`.

## Why sequential?

Single golden VM means a single execution stream. Tradeoffs vs the
fan-out clone-per-pair approach (not implemented):

| | Single VM (this) | Fan-out clones |
|---|---|---|
| Wall-clock for full 28-pair matrix | ~3–4 hr (revert ~3 min × 28) | ~30 min (8 in parallel) |
| Golden images to maintain | 1 | 8+ |
| Storage on Prism | 1 base disk + 1 snapshot | N base disks + clones |
| Test isolation | Snapshot revert | Disposable VMs |
| Setup complexity | Low | High |

Pick this backend when you want minimal setup overhead and don't need
results in under an hour.
