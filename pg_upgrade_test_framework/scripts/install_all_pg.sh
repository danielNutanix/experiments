#!/usr/bin/env bash
# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
# Bootstrap helper: install every supported PostgreSQL major version on a
# single VM, each on its own deterministic port (5400 + N). Idempotent.
#
# Required env (defaults shown):
#   PG_VERSIONS              "11 12 13 14 15 16 17 18"
#   PORT_BASE                5400
#   SEED_SQL                 unset (seeds are skipped when empty)
#   ALLOW_MISSING_VERSIONS   "0"  (when "1", per-version install failures
#                                  log a warning and continue instead of
#                                  aborting; the run-time matrix later
#                                  skips pairs touching skipped versions)
#   MANIFEST_DIR             /var/lib/pg_upgrade_test
#                            (versions.txt is always written here, listing
#                             whatever ended up successfully installed)

set -euo pipefail

trap 'rc=$?; echo "[install_all_pg] FAILED at line ${BASH_LINENO[0]:-?} (cmd: ${BASH_COMMAND:-?}, rc=${rc})" >&2; exit $rc' ERR

PG_VERSIONS="${PG_VERSIONS:-11 12 13 14 15 16 17 18}"
PORT_BASE="${PORT_BASE:-5400}"
SEED_SQL="${SEED_SQL:-}"
ALLOW_MISSING_VERSIONS="${ALLOW_MISSING_VERSIONS:-0}"
MANIFEST_DIR="${MANIFEST_DIR:-/var/lib/pg_upgrade_test}"
MANIFEST_FILE="${MANIFEST_DIR}/versions.txt"

log() { echo "[install_all_pg] $*"; }

# ============================================================
# OS family detection
# ============================================================

if command -v dnf >/dev/null 2>&1; then
    OS_FAMILY="dnf"
elif command -v apt-get >/dev/null 2>&1; then
    OS_FAMILY="apt"
else
    echo "Unsupported OS: neither dnf nor apt-get found" >&2
    exit 1
fi
log "OS family: $OS_FAMILY"
log "ALLOW_MISSING_VERSIONS=$ALLOW_MISSING_VERSIONS"

# ============================================================
# Repository setup (PGDG)
# ============================================================

setup_repo_apt() {
    log "Configuring PGDG apt repo"
    sudo install -d -m 0755 /etc/apt/keyrings
    if [ ! -s /etc/apt/keyrings/pgdg.gpg ]; then
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
            | sudo gpg --dearmor -o /etc/apt/keyrings/pgdg.gpg
    fi
    local codename
    codename=$(. /etc/os-release; echo "$VERSION_CODENAME")
    if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
        echo "deb [signed-by=/etc/apt/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
            | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
    fi
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y postgresql-common
}

setup_repo_dnf() {
    log "Configuring PGDG dnf repo"
    local rhel_maj
    rhel_maj=$(rpm -E %rhel)
    if ! rpm -q pgdg-redhat-repo >/dev/null 2>&1; then
        sudo dnf install -y \
            "https://download.postgresql.org/pub/repos/yum/reporpms/EL-${rhel_maj}-x86_64/pgdg-redhat-repo-latest.noarch.rpm"
    fi
    sudo dnf -qy module disable postgresql >/dev/null 2>&1 || true
}

# ============================================================
# Per-version install + cluster create on a fixed port
#
# Functions use the (...) subshell form so `set -e` is effective even
# when the call site uses `if FUNC; then` (which would otherwise
# suspend `set -e` for the function body).
# ============================================================

install_version_apt() (
    set -e
    local v="$1"
    local port="$2"
    echo "[install_all_pg] apt: installing postgresql-${v} on port ${port}"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "postgresql-${v}"
    if sudo pg_lsclusters -h 2>/dev/null | awk '{print $1, $2}' \
        | grep -qx "${v} main"; then
        sudo pg_dropcluster --stop "${v}" main || true
    fi
    sudo pg_createcluster --port "${port}" --start "${v}" main
    sudo systemctl enable "postgresql@${v}-main.service" >/dev/null 2>&1 || true
)

install_version_dnf() (
    set -e
    local v="$1"
    local port="$2"
    local pgdata="/var/lib/pgsql/${v}/data"
    echo "[install_all_pg] dnf: installing postgresql${v}-server on port ${port}"
    sudo dnf install -y "postgresql${v}-server" "postgresql${v}-contrib"
    if [ ! -s "${pgdata}/PG_VERSION" ]; then
        sudo "/usr/pgsql-${v}/bin/postgresql-${v}-setup" initdb \
            || sudo -u postgres "/usr/pgsql-${v}/bin/initdb" -D "${pgdata}"
    fi
    sudo sed -i "s/^[# ]*port[ ]*=.*/port = ${port}/" "${pgdata}/postgresql.conf"
    sudo systemctl enable "postgresql-${v}.service"
    sudo systemctl restart "postgresql-${v}.service"
)

# ============================================================
# Seed each cluster (only those that installed successfully)
# ============================================================

psql_for() {
    local v="$1"
    if [ "$OS_FAMILY" = "dnf" ]; then
        echo "/usr/pgsql-${v}/bin/psql"
    else
        echo "/usr/lib/postgresql/${v}/bin/psql"
    fi
}

seed_version() {
    local v="$1"
    local port="$2"
    local psql
    psql=$(psql_for "$v")
    if [ -z "$SEED_SQL" ] || [ ! -f "$SEED_SQL" ]; then
        log "PG${v}: SEED_SQL unset or missing; skipping seed"
        return 0
    fi
    if [ ! -x "$psql" ]; then
        log "PG${v}: $psql not found; skipping seed"
        return 0
    fi
    log "PG${v}: seeding from $SEED_SQL on port ${port}"
    sudo -u postgres "$psql" -p "$port" -d postgres \
        -X -v ON_ERROR_STOP=1 -f "$SEED_SQL"
}

# ============================================================
# Main
# ============================================================

INSTALLED_VERSIONS=()
SKIPPED_VERSIONS=()

main() {
    if [ "$OS_FAMILY" = "apt" ]; then
        setup_repo_apt
        install_func="install_version_apt"
    else
        setup_repo_dnf
        install_func="install_version_dnf"
    fi

    for v in $PG_VERSIONS; do
        port=$((PORT_BASE + v))
        if "$install_func" "$v" "$port"; then
            log "PG${v}: install OK"
            INSTALLED_VERSIONS+=("$v")
        else
            rc=$?
            if [ "$ALLOW_MISSING_VERSIONS" = "1" ]; then
                log "WARN: PG${v} install failed (rc=$rc); skipping"
                SKIPPED_VERSIONS+=("$v")
            else
                log "ERROR: PG${v} install failed (rc=$rc); aborting."
                log "Hint: re-run bootstrap with ALLOW_MISSING_VERSIONS=1 to skip unavailable versions."
                exit "$rc"
            fi
        fi
    done

    for v in "${INSTALLED_VERSIONS[@]:-}"; do
        [ -z "$v" ] && continue
        port=$((PORT_BASE + v))
        seed_version "$v" "$port"
    done

    sudo install -d -m 0755 "$MANIFEST_DIR"
    printf '%s\n' "${INSTALLED_VERSIONS[*]:-}" \
        | sudo tee "$MANIFEST_FILE" >/dev/null

    log ""
    log "=== Bootstrap summary ==="
    log "Installed: ${INSTALLED_VERSIONS[*]:-<none>}"
    log "Skipped  : ${SKIPPED_VERSIONS[*]:-<none>}"
    log "Manifest : $MANIFEST_FILE"
    if command -v pg_lsclusters >/dev/null 2>&1; then
        sudo pg_lsclusters || true
    fi
}

main "$@"
