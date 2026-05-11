# ============================================================
# Install PostgreSQL 17 binaries
# ============================================================

sudo apt update

sudo apt install -y \
  wget \
  gnupg2 \
  lsb-release \
  ca-certificates

wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
sudo gpg --dearmor -o /usr/share/keyrings/postgresql.gpg

echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] \
http://apt.postgresql.org/pub/repos/apt \
$(lsb_release -cs)-pgdg main" | \
sudo tee /etc/apt/sources.list.d/pgdg.list

# START HERE
sudo apt update

sudo apt install -y \
  postgresql-16 \
  postgresql-17


# ============================================================
# Verify binaries
# ============================================================

/usr/lib/postgresql/16/bin/postgres --version
/usr/lib/postgresql/17/bin/postgres --version


# ============================================================
# Check existing cluster directories
# ============================================================

sudo find /pg_data -maxdepth 2 -name PG_VERSION

# Expected:
# /pg_data/PG16/PG_VERSION


# ============================================================
# Stop Nutanix postgres service
# ============================================================

sudo systemctl stop ndb-postgresql.service

ps -ef | grep postgres

# Ensure no postgres processes remain


# ============================================================
# Inspect Nutanix service config
# ============================================================

systemctl cat ndb-postgresql.service


# ============================================================
# Prepare PG17 target directory
# ============================================================

sudo -iu postgres

rm -rf /pg_data/PG17

mkdir -p /pg_data/PG17

chmod 700 /pg_data/PG17


# ============================================================
# Initialize empty PG17 cluster
# ============================================================

/usr/lib/postgresql/17/bin/initdb \
  -D /pg_data/PG17


# ============================================================
# Verify initialization
# ============================================================

cat /pg_data/PG17/PG_VERSION

# Expected:
# 17


# ============================================================
# Optional: temporarily relax local auth for pg_upgrade
# ============================================================

vi /pg_data/PG16/pg_hba.conf

# Temporarily ensure:
#
# local   all   all   peer


# ============================================================
# Run pg_upgrade
# ============================================================

/usr/lib/postgresql/17/bin/pg_upgrade \
  -b /usr/lib/postgresql/16/bin/ \
  -B /usr/lib/postgresql/17/bin/ \
  -d /pg_data/PG16 \
  -D /pg_data/PG17 \
  -U postgres \
  --link


# ============================================================
# Start upgraded PG17 cluster manually first
# ============================================================

/usr/lib/postgresql/17/bin/pg_ctl \
  -D /pg_data/PG17 \
  -l /pg_data/PG17/server.log \
  start


# ============================================================
# Validate upgrade
# ============================================================

/usr/lib/postgresql/17/bin/psql -U postgres -c "SELECT version();"

/usr/lib/postgresql/17/bin/psql -l


# ============================================================
# Stop manually started cluster
# ============================================================

/usr/lib/postgresql/17/bin/pg_ctl \
  -D /pg_data/PG17 \
  stop


# ============================================================
# Update Nutanix systemd service
# ============================================================

exit

sudo vi /etc/systemd/system/ndb-postgresql.service


# Update ExecStart from PG16 to PG17:
#
# OLD:
# ExecStart=/usr/lib/postgresql/16/bin/postgres -D /pg_data/PG16
#
# NEW:
# ExecStart=/usr/lib/postgresql/17/bin/postgres -D /pg_data/PG17
#
# Also update:
#
# Environment=PGDATA=/pg_data/PG17
#
# Ensure:
# - uses postgres (not postmaster)
# - uses PG17 binaries
# - uses PG17 data directory


# ============================================================
# Reload systemd
# ============================================================

sudo systemctl daemon-reexec

sudo systemctl daemon-reload


# ============================================================
# Start upgraded service
# ============================================================

sudo systemctl start ndb-postgresql.service

sudo systemctl status ndb-postgresql.service


# ============================================================
# Verify running PG17
# ============================================================

/usr/lib/postgresql/17/bin/psql -U postgres -c "SELECT version();"


# ============================================================
# Post-upgrade maintenance
# ============================================================

sudo -iu postgres

/usr/lib/postgresql/17/bin/vacuumdb \
  --all \
  --analyze-in-stages


# ============================================================
# Restore original pg_hba.conf auth if modified
# ============================================================

vi /pg_data/PG16/pg_hba.conf

# Restore original auth, for example:
#
# local   all   all   scram-sha-256


# ============================================================
# Restart service after pg_hba restore
# ============================================================

exit

sudo systemctl restart ndb-postgresql.service

sudo systemctl status ndb-postgresql.service


# ============================================================
# Final verification
# ============================================================

ps -ef | grep postgres

/usr/lib/postgresql/17/bin/psql -U postgres -c "SELECT version();"






#!/usr/bin/env bash

set -euxo pipefail

# ============================================================
# PostgreSQL 16 -> 17 Upgrade Script
# Ubuntu 22.04 / Debian
#
# Custom cluster layout:
#   /pg_data/PG16
#   /pg_data/PG17
#
# Nutanix service:
#   ndb-postgresql.service
# ============================================================


# ============================================================
# Variables
# ============================================================

OLD_VER=16
NEW_VER=17

PGDATA=/pg_data

OLD_PGDATA=$PGDATA/PG16
NEW_PGDATA=$PGDATA/PG17

OLD_BIN=/usr/lib/postgresql/$OLD_VER/bin
NEW_BIN=/usr/lib/postgresql/$NEW_VER/bin

SERVICE_NAME=ndb-postgresql.service


# ============================================================
# Cleanup broken PostgreSQL archive repositories
# ============================================================

sudo rm -f /etc/apt/sources.list.d/pgdg-archive.list || true

sudo sed -i '/apt-archive.postgresql.org.*jammy-pgdg/d' \
  /etc/apt/sources.list \
  /etc/apt/sources.list.d/* || true


# ============================================================
# Install PostgreSQL repository
# ============================================================

sudo apt update

sudo apt install -y \
  wget \
  gnupg2 \
  lsb-release \
  ca-certificates


# ============================================================
# Add PGDG repository
# ============================================================

if [ ! -f /usr/share/keyrings/postgresql.gpg ]; then

  wget --quiet -O - \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc | \
    sudo gpg --dearmor \
      -o /usr/share/keyrings/postgresql.gpg

fi


echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] \
http://apt.postgresql.org/pub/repos/apt \
$(lsb_release -cs)-pgdg main" | \
sudo tee /etc/apt/sources.list.d/pgdg.list


# ============================================================
# Install PostgreSQL packages
# ============================================================

sudo apt update

sudo apt install --reinstall -y \
  postgresql-16 \
  postgresql-17 \
  postgresql-client-17


# ============================================================
# Verify binary versions
# ============================================================

$OLD_BIN/postgres --version
$NEW_BIN/postgres --version
$NEW_BIN/pg_upgrade --version
$NEW_BIN/pg_dump --version


# ============================================================
# Remove Ubuntu auto-created cluster
# ============================================================

if pg_lsclusters | grep "^17"; then

  sudo pg_dropcluster --stop 17 main || true

fi


# ============================================================
# Stop Nutanix PostgreSQL service
# ============================================================

sudo systemctl stop $SERVICE_NAME || true

sleep 5


# ============================================================
# Ensure postgres fully stopped
# ============================================================

if ps -ef | grep '[p]ostgres'; then
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

export PGDATA=/pg_data

export OLD_BIN=/usr/lib/postgresql/16/bin
export NEW_BIN=/usr/lib/postgresql/17/bin


# ============================================================
# Create versioned directories
# ============================================================

mkdir -p $PGDATA/PG16
mkdir -p $PGDATA/PG17


# ============================================================
# Move existing cluster into PG16/
#
# Only if root PGDATA still contains PG_VERSION
# ============================================================

if [ -f "$PGDATA/PG_VERSION" ]; then

  find "$PGDATA" -mindepth 1 -maxdepth 1 \
    ! -name PG16 \
    ! -name PG17 \
    -exec mv {} "$PGDATA/PG16/" \;

fi


# ============================================================
# Permissions
# ============================================================

chown -R postgres:postgres \
  $PGDATA/PG16 \
  $PGDATA/PG17

chmod 700 $PGDATA/PG16
chmod 700 $PGDATA/PG17


# ============================================================
# Cleanup failed PG17 attempts
# ============================================================

rm -rf $PGDATA/PG17/*


# ============================================================
# Validate source cluster exists
# ============================================================

if [ ! -f "$PGDATA/PG16/PG_VERSION" ]; then

  echo "ERROR: Source PG16 cluster not found"
  exit 1

fi


# ============================================================
# Initialize PG17 cluster
# ============================================================

$NEW_BIN/initdb \
  -D $PGDATA/PG17


# ============================================================
# Verify initialization
# ============================================================

cat $PGDATA/PG17/PG_VERSION


# ============================================================
# Backup pg_hba.conf
# ============================================================

cp \
  $PGDATA/PG16/pg_hba.conf \
  $PGDATA/PG16/pg_hba.conf.bak


# ============================================================
# Temporary pg_hba.conf for pg_upgrade
# ============================================================

cat > $PGDATA/PG16/pg_hba.conf

# Paste below content manually, save and exit:
#
# local   all   all                     peer
# host    all   all   127.0.0.1/32      trust
# host    all   all   ::1/128           trust


# ============================================================
# Run pg_upgrade
# ============================================================

$NEW_BIN/pg_upgrade \
  -b $OLD_BIN \
  -B $NEW_BIN \
  -d $PGDATA/PG16 \
  -D $PGDATA/PG17 \
  -U postgres \
  --link


# ============================================================
# Start upgraded cluster manually
# ============================================================

$NEW_BIN/pg_ctl \
  -D $PGDATA/PG17 \
  -l $PGDATA/PG17/server.log \
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
  -D $PGDATA/PG17 \
  stop


# ============================================================
# Restore original pg_hba.conf
# ============================================================

mv \
  $PGDATA/PG16/pg_hba.conf.bak \
  $PGDATA/PG16/pg_hba.conf


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

sudo vi /etc/systemd/system/ndb-postgresql.service

sudo cp \
  $SERVICE_FILE \
  ${SERVICE_FILE}.bak


# ============================================================
# Update postgres binary path
# ============================================================

sudo sed -i \
  "s|/usr/lib/postgresql/16/bin/postgres|/usr/lib/postgresql/17/bin/postgres|g" \
  $SERVICE_FILE
# ExecStart=/usr/lib/postgresql/17/bin/postgres -D ${PGDATA}
# Environment=PGDATA=/pg_data/PG17
# ============================================================
# Update PGDATA path
# ============================================================

sudo sed -i \
  "s|/pg_data/PG16|/pg_data/PG17|g" \
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

sudo systemctl status $SERVICE_NAME --no-pager


# ============================================================
# Verify PostgreSQL version
# ============================================================

sudo -iu postgres

/usr/lib/postgresql/17/bin/psql \
  -U postgres \
  -c "SELECT version();"


# ============================================================
# Post-upgrade maintenance
# ============================================================

/usr/lib/postgresql/17/bin/vacuumdb \
  --all \
  --analyze-in-stages


# ============================================================
# Final verification
# ============================================================

ps -ef | grep '[p]ostgres'


echo
echo "============================================================"
echo "PostgreSQL upgrade to version 17 completed successfully"
echo "============================================================"


