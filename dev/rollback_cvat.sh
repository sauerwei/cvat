#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILES="${COMPOSE_FILES:-}"
CVAT_HOST_VALUE="${CVAT_HOST:-10.28.252.47}"

compose_args=()
if [ -n "${COMPOSE_FILES}" ]; then
    read -r -a compose_args <<< "${COMPOSE_FILES}"
fi

if [ $# -gt 1 ]; then
    echo "Usage: $0 [backup_directory]"
    exit 1
fi

if [ $# -eq 1 ]; then
    BACKUP_DIR="$1"
else
    BACKUP_DIR="$(find "${PROJECT_ROOT}/backups" -mindepth 1 -maxdepth 1 -type d -name 'cvat_*' | sort | tail -n 1)"
fi

if [ -z "${BACKUP_DIR:-}" ] || [ ! -d "${BACKUP_DIR}" ]; then
    echo "Backup directory not found"
    exit 1
fi

for required in cvat_db.tar.gz cvat_data.tar.gz cvat_events_db.tar.gz; do
    if [ ! -f "${BACKUP_DIR}/${required}" ]; then
        echo "Missing required file: ${BACKUP_DIR}/${required}"
        exit 1
    fi
done

echo "Stopping CVAT containers"
(
    cd "${PROJECT_ROOT}"
    CVAT_HOST="${CVAT_HOST_VALUE}" docker compose "${compose_args[@]}" stop
)

echo "Restoring backup from ${BACKUP_DIR}"

docker run --rm --name temp_backup \
    --volumes-from cvat_db \
    -v "${BACKUP_DIR}:/backup" \
    ubuntu \
    bash -c "cd /var/lib/postgresql/data && tar -xvf /backup/cvat_db.tar.gz --strip 4"

docker run --rm --name temp_backup \
    --volumes-from cvat_server \
    -v "${BACKUP_DIR}:/backup" \
    ubuntu \
    bash -c "cd /home/django/data && tar -xvf /backup/cvat_data.tar.gz --strip 3"

docker run --rm --name temp_backup \
    --volumes-from cvat_clickhouse \
    -v "${BACKUP_DIR}:/backup" \
    ubuntu \
    bash -c "cd /var/lib/clickhouse && tar -xvf /backup/cvat_events_db.tar.gz --strip 3"

echo "Starting CVAT containers with CVAT_HOST=${CVAT_HOST_VALUE}"
(
    cd "${PROJECT_ROOT}"
    CVAT_HOST="${CVAT_HOST_VALUE}" docker compose "${compose_args[@]}" up -d
)

echo "Rollback completed: ${BACKUP_DIR}"
