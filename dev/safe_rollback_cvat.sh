#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --dump PATH        DB dump to restore (pg_dump -Fc format). If omitted, you will be prompted to choose from backups/.
  --vol-dir PATH     Optional path containing volume archives (cvat_cvat_data.tar.gz etc.).
  --test-restore     Perform a test restore into a temporary Postgres to preview DB contents (diff will be shown).
  --keep-temp        Keep temporary test container after test-restore for inspection.
  --yes              Skip confirmation and perform restore immediately.
  -h, --help         Show this help and exit.

This script will create a timestamped DB dump of the current production DB (rollback safety) before performing the restore.
EOF
}

if [ $# -eq 0 ]; then
  : # continue with defaults; user may be prompted later
fi

DUMP_PATH=""
VOL_DIR=""
TEST_RESTORE=0
KEEP_TEMP=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dump) DUMP_PATH="$2"; shift 2;;
    --vol-dir) VOL_DIR="$2"; shift 2;;
    --test-restore) TEST_RESTORE=1; shift;;
    --keep-temp) KEEP_TEMP=1; shift;;
    --yes) ASSUME_YES=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

# Helper to choose a dump if none provided
if [ -z "$DUMP_PATH" ]; then
  echo "No --dump provided. Available dumps in ${PROJECT_ROOT}/backups/:"
  ls -1t "${PROJECT_ROOT}/backups" | sed -n '1,50p'
  read -rp "Enter dump filename to restore (or full path): " CHOICE
  if [ -z "$CHOICE" ]; then
    echo "Aborting: no dump selected."; exit 1
  fi
  if [ -f "$CHOICE" ]; then
    DUMP_PATH="$CHOICE"
  else
    DUMP_PATH="${PROJECT_ROOT}/backups/$CHOICE"
  fi
fi

if [ ! -f "$DUMP_PATH" ]; then
  echo "Dump file not found: $DUMP_PATH"; exit 1
fi

# Step 1: create safety snapshot of current DB
PREBACK_DIR="${PROJECT_ROOT}/backups"
mkdir -p "$PREBACK_DIR"
PREBACK_FILE="$PREBACK_DIR/pre_rollback_$(date +%Y%m%d_%H%M%S).dump"
echo "Creating safety DB dump: $PREBACK_FILE"
docker exec cvat_db pg_dump -U root -d cvat -Fc > "$PREBACK_FILE"
echo "Safety dump written"

# Optional test-restore into a temp Postgres to preview changes
TEMP_DB_NAME="safe_rb_test_db_$(date +%s)"
cleanup_temp_db() {
  if docker ps -a --format '{{.Names}}' | grep -q "${TEMP_DB_NAME}"; then
    docker rm -f "${TEMP_DB_NAME}" >/dev/null 2>&1 || true
  fi
}

if [ "$TEST_RESTORE" -eq 1 ]; then
  echo "Performing test restore into temporary Postgres container: $TEMP_DB_NAME"
  docker run -d --name "$TEMP_DB_NAME" -e POSTGRES_USER=root -e POSTGRES_PASSWORD=root -e POSTGRES_DB=cvat postgres:15 >/dev/null

  # wait until ready
  for i in {1..60}; do
    docker exec "$TEMP_DB_NAME" pg_isready -U root -d cvat >/dev/null 2>&1 && break
    sleep 1
  done

  echo "Restoring dump into temp DB (this may take a while)"
  cat "$DUMP_PATH" | docker exec -i "$TEMP_DB_NAME" pg_restore -U root -d cvat --clean --if-exists

  echo "Extracting engine_video from test DB and production DB for comparison"
  docker exec "$TEMP_DB_NAME" psql -U root -d cvat -c "\copy (select id, path from engine_video order by id desc limit 200) to stdout csv" > /tmp/safe_rb_test_videos.csv
  docker exec cvat_db psql -U root -d cvat -c "\copy (select id, path from engine_video order by id desc limit 200) to stdout csv" > /tmp/safe_rb_prod_videos.csv

  echo "--- PROD (current) ---"; sed -n '1,200p' /tmp/safe_rb_prod_videos.csv
  echo "--- DUMP (test restore) ---"; sed -n '1,200p' /tmp/safe_rb_test_videos.csv
  echo "--- DIFF ---"
  diff -u /tmp/safe_rb_prod_videos.csv /tmp/safe_rb_test_videos.csv || true

  if [ "$KEEP_TEMP" -eq 0 ]; then
    echo "Cleaning up temporary test DB container"
    cleanup_temp_db
  else
    echo "Temporary test DB container kept: $TEMP_DB_NAME"
  fi
fi

# Confirmation
if [ "$ASSUME_YES" -ne 1 ]; then
  echo
  echo "About to restore DB from: $DUMP_PATH"
  if [ -n "$VOL_DIR" ]; then
    echo "Volume archives will be restored from: $VOL_DIR"
  fi
  read -rp "Proceed with restore to production CVAT DB? (yes/NO) " CONF
  case "$CONF" in
    yes|y|Y) ;;
    *) echo "Abort by user"; exit 1;;
  esac
fi

# Perform volume restore if requested
if [ -n "$VOL_DIR" ]; then
  echo "Restoring volumes from $VOL_DIR"
  for v in cvat_cvat_data cvat_cvat_keys cvat_cvat_logs; do
    if [ -f "$VOL_DIR/${v}.tar.gz" ]; then
      docker run --rm -v "${v}:/target" -v "$VOL_DIR":/backup alpine:3.22 sh -lc "cd /target && tar -xzf /backup/${v}.tar.gz"
      echo "Restored $v"
    else
      echo "Archive missing for $v in $VOL_DIR, skipping"
    fi
  done
fi

# Actual DB restore to production
echo "Restoring DB to production from: $DUMP_PATH"
cat "$DUMP_PATH" | docker exec -i cvat_db pg_restore -U root -d cvat --clean --if-exists
echo "DB restore finished"

echo "Restarting CVAT services"
cd "$PROJECT_ROOT"
docker-compose restart || true

echo "Done. Current engine_video entries:"
docker exec cvat_db psql -U root -d cvat -c "select id, path from engine_video order by id desc limit 200;"

echo "Safety dump retained at: $PREBACK_FILE"
