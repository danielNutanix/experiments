#!/usr/bin/env bash

set -euxo pipefail

# Surface the failing line + command + exit code before bash exits.
# Without this, `set -e` failures (especially inside command substitutions
# whose stderr is redirected) leave no trace beyond `rc=N`.
# `:-?` defaults are required because `set -u` treats unset array elements
# as unbound — accessing BASH_LINENO[0] bare can itself trigger an error
# inside the trap and mask the real failure.
trap 'ec=$?; printf "\n!!! pg_upgrade.sh FAILED at line %s: command \"%s\" exited with status %s !!!\n" \
  "${BASH_LINENO[0]:-?}" "${BASH_COMMAND:-?}" "${ec}" >&2; exit "${ec}"' ERR

# ============================================================
# PostgreSQL ${OLD_VER} -> ${NEW_VER} Upgrade Script
#
# Supports:
#   - Debian / Ubuntu       (apt + PGDG apt repo)
#   - RHEL / Rocky / Alma   (dnf + PGDG yum repo)
#
# Custom cluster layout (BASE_PGDATA is auto-discovered, e.g. /pg_data):
#   $BASE_PGDATA/PG${OLD_VER}
#   $BASE_PGDATA/PG${NEW_VER}
#
# Override defaults via environment variables:
#   OLD_VER        e.g. 15
#   NEW_VER        e.g. 17
#   SERVICE_NAME   e.g. era_postgres.service or ndb-postgresql.service
# ============================================================


# ============================================================
# Inputs
# ============================================================

OLD_VER=${OLD_VER:-15}
NEW_VER=${NEW_VER:-17}


# ============================================================
# Detect OS family
# ============================================================

if [ -r /etc/os-release ]; then
  . /etc/os-release
fi

OS_FAMILY=
case "${ID:-}:${ID_LIKE:-}" in
  *debian*|*ubuntu*)
    OS_FAMILY=debian
    ;;
  *rhel*|*centos*|*fedora*|*rocky*|*alma*)
    OS_FAMILY=rhel
    ;;
  *)
    if command -v apt-get >/dev/null 2>&1; then
      OS_FAMILY=debian
    elif command -v dnf >/dev/null 2>&1; then
      OS_FAMILY=rhel
    else
      echo "ERROR: Unsupported OS (no apt-get or dnf found)"
      exit 1
    fi
    ;;
esac

echo "Detected OS family: $OS_FAMILY"


# ============================================================
# OS-specific defaults
# ============================================================

case "$OS_FAMILY" in
  debian)
    : "${SERVICE_NAME:=ndb-postgresql.service}"
    OLD_BIN=/usr/lib/postgresql/$OLD_VER/bin
    NEW_BIN=/usr/lib/postgresql/$NEW_VER/bin
    ;;
  rhel)
    : "${SERVICE_NAME:=era_postgres.service}"
    OLD_BIN=/usr/pgsql-$OLD_VER/bin
    NEW_BIN=/usr/pgsql-$NEW_VER/bin
    ;;
esac

echo "Using SERVICE_NAME: $SERVICE_NAME"


# ============================================================
# Determine BASE_PGDATA
#
# Prefer querying the running server. Fall back to parsing the
# systemd unit file (Environment=PGDATA= first, then -D /<path>;
# the leading `/` skips variable references like `-D ${PGDATA}`).
# ============================================================

# `|| true` lets the assignments fall through gracefully when psql can't
# connect or the systemd unit doesn't match either pattern. Without it,
# `set -e + pipefail` kills the script silently on the failing pipeline.
BASE_PGDATA=$(sudo -u postgres psql -tA -c "SHOW data_directory;" 2>/dev/null \
  | tr -d '[:space:]' || true)

if [ -z "$BASE_PGDATA" ]; then
  BASE_PGDATA=$(grep -oP '^Environment=PGDATA=\K\S+|(?<=-D\s)/\S+' \
    "/etc/systemd/system/$SERVICE_NAME" 2>/dev/null \
    | head -n1 || true)
fi

if [ -z "$BASE_PGDATA" ]; then
  echo "ERROR: Could not determine PostgreSQL data directory"
  exit 1
fi

echo "Discovered BASE_PGDATA: $BASE_PGDATA"

OLD_PGDATA=$BASE_PGDATA/PG$OLD_VER
NEW_PGDATA=$BASE_PGDATA/PG$NEW_VER


# ============================================================
# Persist inputs for `sudo -iu postgres` shells
#
# `sudo -iu` starts a fresh login shell that does not inherit env
# from this script. Each postgres-side block sources this file
# instead of re-declaring the values.
# ============================================================

PG_UPGRADE_ENV=/tmp/pg_upgrade.env

cat > "$PG_UPGRADE_ENV" <<EOF
export OS_FAMILY=$OS_FAMILY
export OLD_VER=$OLD_VER
export NEW_VER=$NEW_VER
export SERVICE_NAME=$SERVICE_NAME
export OLD_BIN=$OLD_BIN
export NEW_BIN=$NEW_BIN
EOF

chmod 644 "$PG_UPGRADE_ENV"


# ============================================================
# Install PGDG repository
# ============================================================

case "$OS_FAMILY" in
  debian)
    # Cleanup stale PGDG archive lists that can break apt-get update
    sudo rm -f /etc/apt/sources.list.d/pgdg-archive.list || true
    sudo sed -i '/apt-archive.postgresql.org.*pgdg/d' \
      /etc/apt/sources.list \
      /etc/apt/sources.list.d/* 2>/dev/null || true

    sudo apt-get update

    sudo apt-get install -y \
      wget \
      gnupg2 \
      lsb-release \
      ca-certificates

    if [ ! -f /usr/share/keyrings/postgresql.gpg ]; then
      wget --quiet -O - \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | sudo gpg --dearmor \
            -o /usr/share/keyrings/postgresql.gpg
    fi

    echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] \
http://apt.postgresql.org/pub/repos/apt \
$(lsb_release -cs)-pgdg main" \
      | sudo tee /etc/apt/sources.list.d/pgdg.list

    sudo apt-get update
    ;;

  rhel)
    sudo dnf install -y \
      https://download.postgresql.org/pub/repos/yum/reporpms/EL-$(rpm -E %rhel)-$(uname -m)/pgdg-redhat-repo-latest.noarch.rpm

    sudo dnf -qy module disable postgresql
    ;;
esac


# ============================================================
# Install PostgreSQL packages
# ============================================================

# The orchestrator may mount a fresh filesystem on /usr/pgsql-$NEW_VER (or
# /usr/lib/postgresql/$NEW_VER) BEFORE this step runs. If the new-version
# packages were already installed earlier, the rpm/dpkg DB still says
# "installed" and a plain install becomes a no-op, leaving the mount empty.
# Reinstall the new-version packages so files are re-deposited onto the
# freshly mounted filesystem.
case "$OS_FAMILY" in
  debian)
    sudo apt-get install -y \
      postgresql-$OLD_VER \
      postgresql-$NEW_VER \
      postgresql-client-$NEW_VER
    sudo apt-get install -y --reinstall \
      postgresql-$NEW_VER \
      postgresql-client-$NEW_VER
    ;;
  rhel)
    sudo dnf install -y \
      postgresql$OLD_VER \
      postgresql$OLD_VER-server \
      postgresql$NEW_VER \
      postgresql$NEW_VER-server
    sudo dnf reinstall -y \
      postgresql$NEW_VER \
      postgresql$NEW_VER-server
    ;;
esac


# ============================================================
# Verify binaries
# ============================================================

$OLD_BIN/postgres --version
$NEW_BIN/postgres --version
$NEW_BIN/pg_upgrade --version
$NEW_BIN/pg_dump --version


# ============================================================
# Remove auto-created cluster (Debian/Ubuntu only)
# (apt creates /var/lib/postgresql/$NEW_VER/main; we use a custom layout)
# ============================================================

if [ "$OS_FAMILY" = "debian" ] && command -v pg_lsclusters >/dev/null 2>&1; then
  if pg_lsclusters | grep -q "^$NEW_VER "; then
    sudo pg_dropcluster --stop $NEW_VER main || true
  fi
fi


# ============================================================
# Stop Nutanix PostgreSQL service
# ============================================================

sudo systemctl stop $SERVICE_NAME || true

sleep 5


# ============================================================
# Ensure postgres fully stopped
# ============================================================

if ps -ef | grep '[p]ostgres' | grep -v exporter | grep -v grep; then
  echo "ERROR: postgres processes still running"
  exit 1
fi


# ============================================================
# Switch to postgres user
# ============================================================

sudo -iu postgres


# ============================================================
# Variables under postgres user
# (sourced from the env file written by the parent shell)
# ============================================================

. /tmp/pg_upgrade.env

# Postgres is stopped here, so SHOW data_directory is unavailable.
# Parse PGDATA from the systemd unit file instead.

export PGDATA=$(grep -oP '^Environment=PGDATA=\K\S+|(?<=-D\s)/\S+' \
  /etc/systemd/system/$SERVICE_NAME 2>/dev/null \
  | head -n1 || true)

if [ -z "$PGDATA" ]; then
  echo "ERROR: Could not determine PGDATA from systemd unit"
  exit 1
fi

echo "Using PGDATA: $PGDATA"


# ============================================================
# Create versioned directories
# ============================================================

mkdir -p $PGDATA/PG$OLD_VER
mkdir -p $PGDATA/PG$NEW_VER


# ============================================================
# Move existing cluster into PG${OLD_VER}/
#
# Only if root PGDATA still contains PG_VERSION
# ============================================================

if [ -f "$PGDATA/PG_VERSION" ]; then

  find "$PGDATA" -mindepth 1 -maxdepth 1 \
    ! -name PG$OLD_VER \
    ! -name PG$NEW_VER \
    -exec mv {} "$PGDATA/PG$OLD_VER/" \;

fi


# ============================================================
# Permissions
# ============================================================

chown -R postgres:postgres \
  $PGDATA/PG$OLD_VER \
  $PGDATA/PG$NEW_VER

chmod 700 $PGDATA/PG$OLD_VER
chmod 700 $PGDATA/PG$NEW_VER


# ============================================================
# Cleanup failed PG${NEW_VER} attempts
# ============================================================

rm -rf $PGDATA/PG$NEW_VER/*


# ============================================================
# Validate source cluster exists
# ============================================================

if [ ! -f "$PGDATA/PG$OLD_VER/PG_VERSION" ]; then

  echo "ERROR: Source PG$OLD_VER cluster not found"
  exit 1

fi


# ============================================================
# Initialize PG${NEW_VER} cluster
# ============================================================

$NEW_BIN/initdb \
  -D $PGDATA/PG$NEW_VER


# ============================================================
# Verify initialization
# ============================================================

cat $PGDATA/PG$NEW_VER/PG_VERSION


# ============================================================
# Backup pg_hba.conf
# ============================================================

cp \
  $PGDATA/PG$OLD_VER/pg_hba.conf \
  $PGDATA/PG$OLD_VER/pg_hba.conf.bak


# ============================================================
# Temporary pg_hba.conf for pg_upgrade
#
# pg_hba.conf is evaluated top-to-bottom and the first matching rule
# wins. Prepend a permissive `local all all peer` rule so pg_upgrade
# can connect over the Unix socket as the postgres OS user without a
# password. Any existing `local all all <method>` line below becomes
# inert. Original is preserved at pg_hba.conf.bak (from the previous
# step) and restored after pg_upgrade completes.
# ============================================================

sed -i '1ilocal all all peer' $PGDATA/PG$OLD_VER/pg_hba.conf

head -n5 $PGDATA/PG$OLD_VER/pg_hba.conf


# ============================================================
# Run pg_upgrade
# ============================================================

$NEW_BIN/pg_upgrade \
  -b $OLD_BIN \
  -B $NEW_BIN \
  -d $PGDATA/PG$OLD_VER \
  -D $PGDATA/PG$NEW_VER \
  -U postgres \
  --link


# ============================================================
# Start upgraded cluster manually
# ============================================================

$NEW_BIN/pg_ctl \
  -D $PGDATA/PG$NEW_VER \
  -l $PGDATA/PG$NEW_VER/server.log \
  start


sleep 5


# ============================================================
# Validate upgraded cluster
# ============================================================

$NEW_BIN/psql \
  -U postgres \
  -c "SELECT version();"


$NEW_BIN/psql -l


# ============================================================
# Stop manually started cluster
# ============================================================

$NEW_BIN/pg_ctl \
  -D $PGDATA/PG$NEW_VER \
  stop


# ============================================================
# Restore original pg_hba.conf
# ============================================================

mv \
  $PGDATA/PG$OLD_VER/pg_hba.conf.bak \
  $PGDATA/PG$OLD_VER/pg_hba.conf


# ============================================================
# Exit postgres user
# ============================================================

exit


# ============================================================
# Inspect Nutanix service
# ============================================================

sudo systemctl cat $SERVICE_NAME || true


# ============================================================
# Update service file
# ============================================================

SERVICE_FILE=/etc/systemd/system/$SERVICE_NAME

sudo cp \
  $SERVICE_FILE \
  ${SERVICE_FILE}.bak


# ============================================================
# Update postgres binary path in service file
# (Both OS path patterns are tried; non-matching pattern is a no-op)
# ============================================================

sudo sed -i \
  -e "s|/usr/lib/postgresql/$OLD_VER/bin|/usr/lib/postgresql/$NEW_VER/bin|g" \
  -e "s|/usr/pgsql-$OLD_VER/bin|/usr/pgsql-$NEW_VER/bin|g" \
  $SERVICE_FILE


# ============================================================
# Update postgres binary path in postgres user's ~/.bash_profile
# ============================================================

sudo -iu postgres

. /tmp/pg_upgrade.env

if [ -f ~/.bash_profile ]; then
  sed -i \
    -e "s|/usr/lib/postgresql/$OLD_VER/bin|/usr/lib/postgresql/$NEW_VER/bin|g" \
    -e "s|/usr/pgsql-$OLD_VER/bin|/usr/pgsql-$NEW_VER/bin|g" \
    ~/.bash_profile
  source ~/.bash_profile
fi

exit


# ============================================================
# Update PGDATA path in service file
# ============================================================

sudo sed -i \
  "s|$BASE_PGDATA|$BASE_PGDATA/PG$NEW_VER|g" \
  $SERVICE_FILE


# ============================================================
# Replace deprecated postmaster references
# ============================================================

sudo sed -i \
  "s|postmaster|postgres|g" \
  $SERVICE_FILE


# ============================================================
# Reload systemd
# ============================================================

sudo systemctl daemon-reexec

sudo systemctl daemon-reload


# ============================================================
# Start upgraded PostgreSQL service
# ============================================================

sudo systemctl start $SERVICE_NAME


# ============================================================
# Verify service
# ============================================================

sudo systemctl status $SERVICE_NAME --no-pager -l


# ============================================================
# Verify PostgreSQL version
# ============================================================

sudo -iu postgres

. /tmp/pg_upgrade.env

$NEW_BIN/psql \
  -U postgres \
  -c "SELECT version();"


# ============================================================
# Post-upgrade maintenance
# ============================================================

$NEW_BIN/vacuumdb \
  --all \
  --analyze-in-stages


# ============================================================
# Final verification
# ============================================================

ps -ef | grep '[p]ostgres'

# Exit the postgres login shell (back to the original user) before cleanup.
exit


# ============================================================
# Cleanup
# ============================================================

rm -f /tmp/pg_upgrade.env


echo
echo "============================================================"
echo "PostgreSQL upgrade to version $NEW_VER completed successfully"
echo "============================================================"
