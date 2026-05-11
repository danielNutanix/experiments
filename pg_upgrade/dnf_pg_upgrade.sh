#!/usr/bin/env bash

set -euxo pipefail

# ============================================================
# PostgreSQL ${OLD_VER} -> ${NEW_VER} Upgrade Script
# RHEL / Rocky / Alma / CentOS
#
# Custom cluster layout (BASE_PGDATA is auto-discovered, e.g. /pg_data):
#   $BASE_PGDATA/PG${OLD_VER}
#   $BASE_PGDATA/PG${NEW_VER}
#
# Nutanix service:
#   era_postgres.service
# ============================================================


# ============================================================
# Variables
# ============================================================

OLD_VER=15
NEW_VER=17

SERVICE_NAME=era_postgres.service

# Determine BASE_PGDATA from the running PostgreSQL server.
# Falls back to parsing the systemd unit file if psql is unavailable
# (e.g. the service is already stopped from a previous attempt).

BASE_PGDATA=$(sudo -u postgres psql -tA -c "SHOW data_directory;" 2>/dev/null \
  | tr -d '[:space:]')

if [ -z "$BASE_PGDATA" ]; then
  BASE_PGDATA=$(grep -oP '(?<=-D\s)\S+' \
    "/etc/systemd/system/$SERVICE_NAME" 2>/dev/null \
    | head -n1)
fi

if [ -z "$BASE_PGDATA" ]; then
  echo "ERROR: Could not determine PostgreSQL data directory"
  exit 1
fi

echo "Discovered BASE_PGDATA: $BASE_PGDATA"

OLD_PGDATA=$BASE_PGDATA/PG$OLD_VER
NEW_PGDATA=$BASE_PGDATA/PG$NEW_VER

OLD_BIN=/usr/pgsql-$OLD_VER/bin
NEW_BIN=/usr/pgsql-$NEW_VER/bin


# ============================================================
# Install PGDG repository
# ============================================================

sudo dnf install -y \
  https://download.postgresql.org/pub/repos/yum/reporpms/EL-$(rpm -E %rhel)-$(uname -m)/pgdg-redhat-repo-latest.noarch.rpm


# ============================================================
# Disable built-in PostgreSQL module
# ============================================================

sudo dnf -qy module disable postgresql


# ============================================================
# Install PostgreSQL packages
# ============================================================

sudo dnf install -y \
  postgresql$OLD_VER \
  postgresql$OLD_VER-server \
  postgresql$NEW_VER \
  postgresql$NEW_VER-server


# ============================================================
# Verify binaries
# ============================================================

$OLD_BIN/postgres --version
$NEW_BIN/postgres --version
$NEW_BIN/pg_upgrade --version
$NEW_BIN/pg_dump --version


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
# ============================================================

export OLD_VER=15
export NEW_VER=17
export SERVICE_NAME=era_postgres.service

# PostgreSQL is already stopped at this point, so SHOW data_directory
# is not available. Parse the data directory from the systemd unit file.

export PGDATA=$(grep -oP '(?<=-D\s)\S+' \
  /etc/systemd/system/$SERVICE_NAME 2>/dev/null \
  | head -n1)

if [ -z "$PGDATA" ]; then
  echo "ERROR: Could not determine PGDATA from systemd unit"
  exit 1
fi

echo "Using PGDATA: $PGDATA"

export OLD_BIN=/usr/pgsql-$OLD_VER/bin
export NEW_BIN=/usr/pgsql-$NEW_VER/bin


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
# ============================================================

# pg_hba.conf is evaluated top-to-bottom and the first matching rule
# wins. Prepend a permissive `local all all peer` rule so pg_upgrade can
# connect over the Unix socket as the postgres OS user without a password.
# Any existing `local all all <method>` line below becomes inert.
# Original file is preserved at pg_hba.conf.bak (from the previous step)
# and restored after pg_upgrade completes.

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
# Update postgres binary path
# ============================================================

sudo sed -i \
  "s|/usr/pgsql-$OLD_VER/bin/|/usr/pgsql-$NEW_VER/bin/|g" \
  $SERVICE_FILE


# ============================================================
# Update postgres client in bash_profile
# ============================================================
sudo -iu postgres

OLD_VER=15
NEW_VER=17
sed -i \
  "s|/usr/pgsql-$OLD_VER/bin|/usr/pgsql-$NEW_VER/bin|g" \
  ~/.bash_profile
source ~/.bash_profile

exit

# ============================================================
# Update PGDATA path
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

export NEW_VER=17

/usr/pgsql-$NEW_VER/bin/psql \
  -U postgres \
  -c "SELECT version();"


# ============================================================
# Post-upgrade maintenance
# ============================================================

/usr/pgsql-$NEW_VER/bin/vacuumdb \
  --all \
  --analyze-in-stages


# ============================================================
# Final verification
# ============================================================

ps -ef | grep '[p]ostgres'


echo
echo "============================================================"
echo "PostgreSQL upgrade to version $NEW_VER completed successfully"
echo "============================================================"
