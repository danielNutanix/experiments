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

Passwords for NDB / Prism / SSH can be supplied via env vars
(`NDB_PASSWORD`, `PRISM_PASSWORD`, `DBVM_SSH_PASSWORD`) or interactively at
the prompt — they are never echoed and never logged.

## Notes

- The Time Machine resume runs in a `finally` block, so even if the upgrade
  fails the TM is brought back online. The recovery snapshot from step 2
  remains available for manual rollback.
- `pg_upgrade.sh` switches to the `postgres` user via `sudo -iu postgres`.
  That pattern requires the script to be fed on stdin, which the orchestrator
  does (`bash -s < /tmp/pg_upgrade.sh`).
- The orchestrator does **not** detach the new disk on failure; a failed run
  leaves the disk attached for inspection.
