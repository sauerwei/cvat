#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-${PROJECT_ROOT}/backups}"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_DIR="${BACKUP_ROOT}/cvat_${STAMP}"
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml -f components/serverless/docker-compose.serverless.yml}"
CVAT_HOST_VALUE="${CVAT_HOST:-10.28.252.47}"

compose_args=()
if [ -n "${COMPOSE_FILES}" ]; then
    read -r -a compose_args <<< "${COMPOSE_FILES}"
fi

echo "Using BACKUP_ROOT=${BACKUP_ROOT}"
mkdir -p "${TARGET_DIR}"

# ---------------------------------------------------------------------------
# Health check helpers
# ---------------------------------------------------------------------------
CRITICAL_CONTAINERS=(
    cvat_db
    cvat_redis_inmem
    cvat_redis_ondisk
    cvat_server
    cvat_ui
    cvat_opa
    cvat_clickhouse
    cvat_worker_annotation
)

HEALTHCHECK_WAIT_SECONDS=60
RESTART_WAIT_SECONDS=30
MAX_RESTART_ATTEMPTS=3

_container_status() {
    local name="$1"
    local health
    health=$(docker inspect --format '{{.State.Health.Status}}' "${name}" 2>/dev/null)
    if [ -n "${health}" ]; then
        echo "${health}"
        return
    fi
    local running
    running=$(docker inspect --format '{{.State.Running}}' "${name}" 2>/dev/null)
    if [ "${running}" = "true" ]; then
        echo "running"
    else
        echo "stopped"
    fi
}

_run_healthcheck() {
    echo ""
    echo "Waiting ${HEALTHCHECK_WAIT_SECONDS}s for containers to settle..."
    sleep "${HEALTHCHECK_WAIT_SECONDS}"

    echo "Running container health checks..."
    local all_healthy=true

    for container in "${CRITICAL_CONTAINERS[@]}"; do
        local attempt=0
        while [ "${attempt}" -lt "${MAX_RESTART_ATTEMPTS}" ]; do
            local status
            status=$(_container_status "${container}")
            if [ "${status}" = "healthy" ] || [ "${status}" = "running" ]; then
                echo "  [OK]      ${container} (${status})"
                break
            fi

            attempt=$((attempt + 1))
            if [ "${attempt}" -lt "${MAX_RESTART_ATTEMPTS}" ]; then
                echo "  [RESTART] ${container} is '${status}' — restarting (attempt ${attempt}/${MAX_RESTART_ATTEMPTS})..."
                docker restart "${container}" 2>/dev/null || true
                echo "            Waiting ${RESTART_WAIT_SECONDS}s..."
                sleep "${RESTART_WAIT_SECONDS}"
            else
                echo "  [FAIL]    ${container} still '${status}' after ${MAX_RESTART_ATTEMPTS} attempts"
                all_healthy=false
            fi
        done
    done

    echo ""
    if [ "${all_healthy}" = "true" ]; then
        echo "All critical containers are healthy."
    else
        echo "WARNING: One or more containers failed to recover. Check logs with:"
        echo "  docker compose ${COMPOSE_FILES} logs --tail=50 <container>"
    fi
}

# ---------------------------------------------------------------------------

restore_services=false

cleanup() {
    if [ "${restore_services}" = true ]; then
        echo "Starting CVAT containers with CVAT_HOST=${CVAT_HOST_VALUE}"
        (
            cd "${PROJECT_ROOT}"
            CVAT_HOST="${CVAT_HOST_VALUE}" docker compose "${compose_args[@]}" up -d
        )
        _run_healthcheck
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
