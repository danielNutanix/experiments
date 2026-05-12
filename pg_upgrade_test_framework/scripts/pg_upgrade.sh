#!/usr/bin/env bash
# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
# In-place PostgreSQL major-version upgrade for the multi-version golden VM.
# Assumes both OLD_VER and NEW_VER are already installed (binaries + an
# initialised cluster on PORT_BASE+VERSION). Does NOT attach any new disk.
#
# Required env (defaults shown):
#   OLD_VER     (required)
#   NEW_VER     (required, > OLD_VER)
#   PORT_BASE   5400
#   WORKDIR     /tmp/pg_upgrade_workdir

set -euxo pipefail

trap 'rc=$?; echo "[pg_upgrade] FAILED at line ${BASH_LINENO[0]:-?} (cmd: ${BASH_COMMAND:-?}, rc=${rc})" >&2; exit $rc' ERR

OLD_VER="${OLD_VER:?OLD_VER is required}"
NEW_VER="${NEW_VER:?NEW_VER is required}"
PORT_BASE="${PORT_BASE:-5400}"
WORKDIR="${WORKDIR:-/tmp/pg_upgrade_workdir}"

if [ "$OLD_VER" -ge "$NEW_VER" ]; then
    echo "OLD_VER ($OLD_VER) must be less than NEW_VER ($NEW_VER)" >&2
    exit 2
fi

OLD_PORT=$((PORT_BASE + OLD_VER))
NEW_PORT=$((PORT_BASE + NEW_VER))

# ============================================================
# OS-family-specific path resolution
# ============================================================

if command -v dnf >/dev/null 2>&1; then
    OS_FAMILY="dnf"
    OLD_BINDIR="/usr/pgsql-${OLD_VER}/bin"
    NEW_BINDIR="/usr/pgsql-${NEW_VER}/bin"
    OLD_PGDATA="/var/lib/pgsql/${OLD_VER}/data"
    NEW_PGDATA="/var/lib/pgsql/${NEW_VER}/data"
    OLD_SVC="postgresql-${OLD_VER}.service"
    NEW_SVC="postgresql-${NEW_VER}.service"
    PG_OWNER="postgres"
elif command -v apt-get >/dev/null 2>&1; then
    OS_FAMILY="apt"
    OLD_BINDIR="/usr/lib/postgresql/${OLD_VER}/bin"
    NEW_BINDIR="/usr/lib/postgresql/${NEW_VER}/bin"
    OLD_PGDATA="/var/lib/postgresql/${OLD_VER}/main"
    NEW_PGDATA="/var/lib/postgresql/${NEW_VER}/main"
    OLD_SVC="postgresql@${OLD_VER}-main.service"
    NEW_SVC="postgresql@${NEW_VER}-main.service"
    PG_OWNER="postgres"
else
    echo "Unsupported OS: neither dnf nor apt-get found" >&2
    exit 1
fi

for f in "$OLD_BINDIR/pg_ctl" "$NEW_BINDIR/pg_upgrade" "$NEW_BINDIR/initdb"; do
    [ -x "$f" ] || { echo "Required binary missing: $f" >&2; exit 3; }
done

[ -s "${OLD_PGDATA}/PG_VERSION" ] || {
    echo "Source PGDATA missing or empty: ${OLD_PGDATA}" >&2
    exit 4
}

# ============================================================
# Stop both clusters; wipe + reinit the target
# ============================================================

stop_cluster() {
    local svc="$1"
    sudo systemctl stop "$svc" || true
    for _ in $(seq 1 30); do
        if ! systemctl is-active --quiet "$svc"; then
            return 0
        fi
        sleep 1
    done
    echo "Cluster $svc did not stop in time" >&2
    return 5
}

stop_cluster "$OLD_SVC"
stop_cluster "$NEW_SVC"

# pg_upgrade refuses a non-empty target. The bootstrap seeded NEW_VER
# too (so the snapshot is symmetric); we throw that data away here.
sudo rm -rf "${NEW_PGDATA:?}/"*
sudo rm -rf "${NEW_PGDATA:?}/".[!.]* 2>/dev/null || true
sudo install -d -o "$PG_OWNER" -g "$PG_OWNER" -m 0700 "$NEW_PGDATA"

# Re-initdb the target on its assigned port.
sudo -u "$PG_OWNER" "$NEW_BINDIR/initdb" -D "$NEW_PGDATA" \
    --encoding=UTF8 --locale=C
sudo sed -i "s/^[# ]*port[ ]*=.*/port = ${NEW_PORT}/" \
    "${NEW_PGDATA}/postgresql.conf"

# ============================================================
# pg_upgrade
# ============================================================

sudo install -d -o "$PG_OWNER" -g "$PG_OWNER" -m 0700 "$WORKDIR"
sudo -u "$PG_OWNER" rm -rf "${WORKDIR:?}/"*

cd "$WORKDIR"
sudo -u "$PG_OWNER" -H bash -lc "
    set -euxo pipefail
    cd '$WORKDIR'
    '$NEW_BINDIR/pg_upgrade' \
        --old-bindir='$OLD_BINDIR' \
        --new-bindir='$NEW_BINDIR' \
        --old-datadir='$OLD_PGDATA' \
        --new-datadir='$NEW_PGDATA' \
        --old-port=$OLD_PORT \
        --new-port=$NEW_PORT \
        --link
"

# ============================================================
# Start the upgraded cluster + post-upgrade maintenance
# ============================================================

sudo systemctl start "$NEW_SVC"
for _ in $(seq 1 30); do
    if sudo -u "$PG_OWNER" "$NEW_BINDIR/pg_isready" -p "$NEW_PORT" -q; then
        break
    fi
    sleep 1
done
sudo -u "$PG_OWNER" "$NEW_BINDIR/pg_isready" -p "$NEW_PORT"

sudo -u "$PG_OWNER" "$NEW_BINDIR/vacuumdb" -p "$NEW_PORT" \
    --all --analyze-in-stages

echo "[pg_upgrade] Upgrade ${OLD_VER} -> ${NEW_VER} completed successfully."
