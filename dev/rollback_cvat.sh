#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILES="${COMPOSE_FILES:--f docker-compose.yml -f components/serverless/docker-compose.serverless.yml}"
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

# ---------------------------------------------------------------------------
# Health check: verify critical containers are up; restart any that are not.
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

HEALTHCHECK_WAIT_SECONDS=60   # initial settle time after compose up
RESTART_WAIT_SECONDS=30       # wait after restarting before re-checking
MAX_RESTART_ATTEMPTS=3

_container_status() {
    local name="$1"
    local health
    health=$(docker inspect --format '{{.State.Health.Status}}' "${name}" 2>/dev/null)
    if [ -n "${health}" ]; then
        echo "${health}"
        return
    fi
    # No HEALTHCHECK defined — fall back to running state
    local running
    running=$(docker inspect --format '{{.State.Running}}' "${name}" 2>/dev/null)
    if [ "${running}" = "true" ]; then
        echo "running"
    else
        echo "stopped"
    fi
}

_wait_healthy() {
    local name="$1"
    local timeout="$2"
    local elapsed=0
    while [ "${elapsed}" -lt "${timeout}" ]; do
        local s
        s=$(_container_status "${name}")
        if [ "${s}" = "healthy" ] || [ "${s}" = "running" ]; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

echo ""
echo "Waiting ${HEALTHCHECK_WAIT_SECONDS}s for containers to settle..."
sleep "${HEALTHCHECK_WAIT_SECONDS}"

echo "Running container health checks..."
ALL_HEALTHY=true

for container in "${CRITICAL_CONTAINERS[@]}"; do
    attempt=0
    while [ "${attempt}" -lt "${MAX_RESTART_ATTEMPTS}" ]; do
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
            ALL_HEALTHY=false
        fi
    done
done

echo ""
if [ "${ALL_HEALTHY}" = "true" ]; then
    echo "All critical containers are healthy."
else
    echo "WARNING: One or more containers failed to recover. Check logs with:"
    echo "  docker compose ${COMPOSE_FILES} logs --tail=50 <container>"
    exit 2
fi

echo "Rollback completed: ${BACKUP_DIR}"
