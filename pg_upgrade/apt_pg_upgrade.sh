#!/usr/bin/env bash

set -euxo pipefail

# ============================================================
# PostgreSQL ${OLD_VER} -> ${NEW_VER} Upgrade Script
# Ubuntu / Debian (PGDG apt repository)
#
# Custom cluster layout (BASE_PGDATA is auto-discovered, e.g. /pg_data):
#   $BASE_PGDATA/PG${OLD_VER}
#   $BASE_PGDATA/PG${NEW_VER}
#
# Nutanix service:
#   ndb-postgresql.service
# ============================================================


# ============================================================
# Variables
# ============================================================

OLD_VER=17
NEW_VER=18

SERVICE_NAME=ndb-postgresql.service

# Determine BASE_PGDATA from the running PostgreSQL server.
# Falls back to parsing the systemd unit file if psql is unavailable
# (e.g. the service is already stopped from a previous attempt).

BASE_PGDATA=$(sudo -u postgres psql -tA -c "SHOW data_directory;" 2>/dev/null \
  | tr -d '[:space:]')

if [ -z "$BASE_PGDATA" ]; then
  # Look for `Environment=PGDATA=<path>` first (the typical NDB pattern,
  # where ExecStart references ${PGDATA}). Fall back to a literal
  # `-D /<path>` argument on ExecStart for unit files that hardcode it.
  # The leading `/` on the second alternative skips variable references
  # like `-D ${PGDATA}`, which would otherwise match `\S+`.
  BASE_PGDATA=$(grep -oP '^Environment=PGDATA=\K\S+|(?<=-D\s)/\S+' \
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

OLD_BIN=/usr/lib/postgresql/$OLD_VER/bin
NEW_BIN=/usr/lib/postgresql/$NEW_VER/bin


# ============================================================
# Cleanup broken PostgreSQL archive repositories
# (PGDG sometimes leaves stale archive lists that break apt update)
# ============================================================

sudo rm -f /etc/apt/sources.list.d/pgdg-archive.list || true

sudo sed -i '/apt-archive.postgresql.org.*pgdg/d' \
  /etc/apt/sources.list \
  /etc/apt/sources.list.d/* 2>/dev/null || true


# ============================================================
# Install prerequisites
# ============================================================

sudo apt-get update

sudo apt-get install -y \
  wget \
  gnupg2 \
  lsb-release \
  ca-certificates


# ============================================================
# Add PGDG repository
# ============================================================

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


# ============================================================
# Install PostgreSQL packages
# ============================================================

sudo apt-get update

sudo apt-get install -y \
  postgresql-$OLD_VER \
  postgresql-$NEW_VER \
  postgresql-client-$NEW_VER


# ============================================================
# Verify binaries
# ============================================================

$OLD_BIN/postgres --version
$NEW_BIN/postgres --version
$NEW_BIN/pg_upgrade --version
$NEW_BIN/pg_dump --version


# ============================================================
# Remove Ubuntu auto-created cluster
# (apt creates /var/lib/postgresql/$NEW_VER/main; we use a custom layout)
# ============================================================

if pg_lsclusters | grep -q "^$NEW_VER "; then
  sudo pg_dropcluster --stop $NEW_VER main || true
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
# ============================================================

export OLD_VER=17
export NEW_VER=18
export SERVICE_NAME=ndb-postgresql.service

# PostgreSQL is already stopped at this point, so SHOW data_directory
# is not available. Parse the data directory from the systemd unit file:
# prefer `Environment=PGDATA=<path>` (the typical NDB pattern, where
# ExecStart uses `-D ${PGDATA}`), falling back to a literal `-D /<path>`
# on ExecStart. The leading `/` on the second alternative ensures we
# don't accidentally match a variable reference like `-D ${PGDATA}`.

export PGDATA=$(grep -oP '^Environment=PGDATA=\K\S+|(?<=-D\s)/\S+' \
  /etc/systemd/system/$SERVICE_NAME 2>/dev/null \
  | head -n1)

if [ -z "$PGDATA" ]; then
  echo "ERROR: Could not determine PGDATA from systemd unit"
  exit 1
fi

echo "Using PGDATA: $PGDATA"

export OLD_BIN=/usr/lib/postgresql/$OLD_VER/bin
export NEW_BIN=/usr/lib/postgresql/$NEW_VER/bin


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
  "s|/usr/lib/postgresql/$OLD_VER/bin/|/usr/lib/postgresql/$NEW_VER/bin/|g" \
  $SERVICE_FILE


# ============================================================
# Update postgres client in bash_profile
# ============================================================

sudo -iu postgres

OLD_VER=17
NEW_VER=18

if [ -f ~/.bash_profile ]; then
  sed -i \
    "s|/usr/lib/postgresql/$OLD_VER/bin|/usr/lib/postgresql/$NEW_VER/bin|g" \
    ~/.bash_profile
  source ~/.bash_profile
fi

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

export NEW_VER=18

/usr/lib/postgresql/$NEW_VER/bin/psql \
  -U postgres \
  -c "SELECT version();"


# ============================================================
# Post-upgrade maintenance
# ============================================================

/usr/lib/postgresql/$NEW_VER/bin/vacuumdb \
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
