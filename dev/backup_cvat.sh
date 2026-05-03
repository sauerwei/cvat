#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${PROJECT_ROOT}/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_DIR="${BACKUP_ROOT}/cvat_${STAMP}"
COMPOSE_FILES="${COMPOSE_FILES:-}"
CVAT_HOST_VALUE="${CVAT_HOST:-10.28.252.47}"

compose_args=()
if [ -n "${COMPOSE_FILES}" ]; then
    read -r -a compose_args <<< "${COMPOSE_FILES}"
fi

echo "Using BACKUP_ROOT=${BACKUP_ROOT}"
mkdir -p "${TARGET_DIR}"

restore_services=false

cleanup() {
    if [ "${restore_services}" = true ]; then
        echo "Starting CVAT containers with CVAT_HOST=${CVAT_HOST_VALUE}"
        (
            cd "${PROJECT_ROOT}"
            CVAT_HOST="${CVAT_HOST_VALUE}" docker compose "${compose_args[@]}" up -d
        )
    fi
}

trap cleanup EXIT

echo "Stopping CVAT containers"
(
    cd "${PROJECT_ROOT}"
    CVAT_HOST="${CVAT_HOST_VALUE}" docker compose "${compose_args[@]}" stop
)
restore_services=true

echo "Creating backup in ${TARGET_DIR}"

docker run --rm --name temp_backup \
    --volumes-from cvat_db \
    -v "${TARGET_DIR}:/backup" \
    ubuntu \
    tar -czvf /backup/cvat_db.tar.gz /var/lib/postgresql/data

docker run --rm --name temp_backup \
    --volumes-from cvat_server \
    -v "${TARGET_DIR}:/backup" \
    ubuntu \
    tar -czvf /backup/cvat_data.tar.gz /home/django/data

docker run --rm --name temp_backup \
    --volumes-from cvat_clickhouse \
    -v "${TARGET_DIR}:/backup" \
    ubuntu \
    tar -czvf /backup/cvat_events_db.tar.gz /var/lib/clickhouse

cat > "${TARGET_DIR}/RESTORE.txt" <<EOF
Backup created: $(date -Iseconds)

Restore CVAT from this backup using the same CVAT version:

1. Stop CVAT containers:
   CVAT_HOST=${CVAT_HOST_VALUE} docker compose ${COMPOSE_FILES} stop

2. Restore data from inside this directory:
   cd ${TARGET_DIR}
   docker run --rm --name temp_backup --volumes-from cvat_db -v \$(pwd):/backup ubuntu bash -c "cd /var/lib/postgresql/data && tar -xvf /backup/cvat_db.tar.gz --strip 4"
   docker run --rm --name temp_backup --volumes-from cvat_server -v \$(pwd):/backup ubuntu bash -c "cd /home/django/data && tar -xvf /backup/cvat_data.tar.gz --strip 3"
   docker run --rm --name temp_backup --volumes-from cvat_clickhouse -v \$(pwd):/backup ubuntu bash -c "cd /var/lib/clickhouse && tar -xvf /backup/cvat_events_db.tar.gz --strip 3"

3. Start CVAT again:
   CVAT_HOST=${CVAT_HOST_VALUE} docker compose ${COMPOSE_FILES} up -d
EOF

echo "Backup completed: ${TARGET_DIR}"
