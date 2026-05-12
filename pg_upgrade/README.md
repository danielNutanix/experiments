# PostgreSQL Upgrade Orchestrator

End-to-end PostgreSQL major-version upgrade for an NDB-managed DBVM.

The orchestrator wraps `pg_upgrade.sh` with the platform-side steps that have
to happen around it:

| # | Step | Where |
|---|------|-------|
| 1 | Pause Time Machine | NDB API |
| 2 | Take recovery snapshot | Prism Element |
| 3 | Attach a fresh data disk | Prism Element |
| 4 | Wipe / `mkfs.ext4` / mount / fstab | DBVM (SSH) |
| 5–9 | Run `pg_upgrade.sh` | DBVM (SSH) |
| 10 | Resume Time Machine | NDB API |

## Install

Dependencies are declared in `pyproject.toml`; `uv sync` creates the venv and
installs everything in one go.

```bash
uv sync
```

## Minimum input

The DBVM name is the only entity-level input. Everything else is derived:

| Derived | Source |
|---------|--------|
| Prism VM UUID | `GET /dbservers?value-type=name&value=<name>` → `vmClusterUuid` |
| NDB cluster ID | same dbserver payload → `nxClusterId` |
| Prism IP | `GET /clusters/{nxClusterId}` → `ipAddresses[0]` |
| Time Machine ID | dbserver payload → `databases[].timeMachineId` (PG only) |
| DBVM IP | dbserver payload → `ipAddresses[0]` |
| Prism cluster UUID | `GET /PrismGateway/services/rest/v2.0/cluster` |
| Storage container | first non-reserved container on the cluster |
| New SCSI device index | `max(existing scsi indexes) + 1` |
| New block device | diff of `lsblk` before/after the attach |
| Mount point | `/usr/pgsql-<NEW_VER>` (RHEL) or `/usr/lib/postgresql/<NEW_VER>` (Debian) |

## Configuration

Every option below can be supplied via a CLI flag **or** the matching
environment variable. Anything still missing at run time is prompted for
interactively (passwords use `getpass`, never echoed, never logged).

Legend: **Req** = ✓ required · ◐ conditionally required · — optional (a
default is applied).

### Credentials

Three independent credential sets — they are *not* shared.

| Req | Purpose | Flag | Env var | Default |
|-----|---------|------|---------|---------|
| ✓ | NDB host (FQDN or IP, no scheme needed) | `--ndb-host` | `NDB_HOST` | — |
| ✓ | NDB username (HTTP Basic) | `--ndb-user` | `NDB_USER` | — |
| ✓ | NDB password (prompted if unset) | `--ndb-password` | `NDB_PASSWORD` | — |
| ✓ | Prism Element username | `--prism-user` | `PRISM_USER` | — |
| ✓ | Prism Element password (prompted if unset) | `--prism-password` | `PRISM_PASSWORD` | — |
| — | DBVM SSH username | `--ssh-user` | `DBVM_SSH_USER` | `era` |
| ◐ | DBVM SSH password (one of `--ssh-password` or `--ssh-key` is required; mutually exclusive) | `--ssh-password` | `DBVM_SSH_PASSWORD` | — |
| ◐ | DBVM SSH private key path (one of `--ssh-password` or `--ssh-key` is required; mutually exclusive) | `--ssh-key` | `DBVM_SSH_KEY` | — |
| — | DBVM SSH port | `--ssh-port` | — | `22` |

The Prism IP itself is **derived** from NDB (`/clusters/{nxClusterId}`),
so you only need Prism *credentials*, not its address.

### Target & versions

| Req | Flag | Default | Description |
|-----|------|---------|-------------|
| ✓ | `--vm-name` | — | DBVM name as registered in NDB. |
| — | `--old-version` | `15` | Source PostgreSQL major version. |
| — | `--new-version` | `17` | Target PostgreSQL major version. |

### Disk, snapshot & storage (all optional)

| Req | Flag | Default | Description |
|-----|------|---------|-------------|
| — | `--disk-size-gb` | `20` | Size of the new disk created for the upgraded PG binaries. |
| — | `--snapshot-name` | `pg_upgrade_pre_<unix_ts>` | Recovery snapshot name on Prism. |
| — | `--storage-container` | auto | Prism storage container name. If omitted, the first non-reserved container (skipping `NutanixManagementShare`, `SelfServiceContainer`) is used. |
| — | `--mount-point` | OS-derived | Where the new disk is mounted. Defaults to `/usr/pgsql-<NEW_VER>` (RHEL) or `/usr/lib/postgresql/<NEW_VER>` (Debian). |

### Execution (all optional)

| Req | Flag | Default | Description |
|-----|------|---------|-------------|
| — | `--upgrade-script` | `./pg_upgrade.sh` next to the orchestrator | Path to the `pg_upgrade.sh` to upload and run on the DBVM. |
| — | `--insecure` | SSL verified | Disable TLS verification — typically needed for self-signed Prism / NDB lab certs. |
| — | `-v`, `--verbose` | INFO | Enable DEBUG logging (including HTTP method/URL traces). |

### Minimum required to launch

| Required from you | Reason |
|-------------------|--------|
| `--ndb-host`, `--ndb-user`, `--ndb-password` | Talk to NDB |
| `--prism-user`, `--prism-password` | Talk to Prism Element (IP is derived) |
| `--ssh-password` **or** `--ssh-key` | Authenticate to the DBVM |
| `--vm-name` | Identify the DBVM to upgrade |

Everything else has a sensible default.

## Run

`pyproject.toml` exposes a `pg-upgrade` console script:

```bash
uv run pg-upgrade                        \
    --ndb-host        <NDB_HOST>         \
    --ndb-user        admin              \
    --prism-user      admin              \
    --vm-name         my-pg-dbvm         \
    --ssh-user        era                \
    --ssh-key         ~/.ssh/era_dbvm    \
    --old-version     15                 \
    --new-version     17                 \
    --insecure
```

Equivalent: `uv run python pg_upgrade_orchestrator.py …`.

### Env-var-only invocation

```bash
export NDB_HOST=10.x.y.z   NDB_USER=admin       NDB_PASSWORD=...
export PRISM_USER=admin    PRISM_PASSWORD=...
export DBVM_SSH_USER=era   DBVM_SSH_KEY=~/.ssh/era_dbvm

uv run pg-upgrade --vm-name my-pg-dbvm --insecure
```

## Notes

- The Time Machine resume runs in a `finally` block, so even if the upgrade
  fails the TM is brought back online. The recovery snapshot from step 2
  remains available for manual rollback.
- `pg_upgrade.sh` switches to the `postgres` user via `sudo -iu postgres`.
  That pattern requires the script to be fed on stdin, which the orchestrator
  does (`bash -s < /tmp/pg_upgrade.sh`).
- The orchestrator does **not** detach the new disk on failure; a failed run
  leaves the disk attached for inspection.
