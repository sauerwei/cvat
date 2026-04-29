#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Usage: rollback_cvat.sh [db_dump_path] [backup_volumes_dir]
# If db_dump_path is omitted, uses pre_restore_*.dump if present in backups/

DEFAULT_DUMP="${PROJECT_ROOT}/backups/pre_restore_20260429_144129.dump"
DUMP_PATH="${1:-$DEFAULT_DUMP}"
BACKUP_VOL_DIR="${2:-}"

if [ ! -f "$DUMP_PATH" ]; then
  echo "DB dump not found: $DUMP_PATH"
  exit 1
fi

echo "Restoring DB from: $DUMP_PATH"
cat "$DUMP_PATH" | docker exec -i cvat_db pg_restore -U root -d cvat --clean --if-exists
echo "DB restore finished"

if [ -n "$BACKUP_VOL_DIR" ]; then
  echo "Restoring volumes from: $BACKUP_VOL_DIR"
  for v in cvat_cvat_data cvat_cvat_keys cvat_cvat_logs; do
    if [ -f "$BACKUP_VOL_DIR/${v}.tar.gz" ]; then
      docker run --rm -v "${v}:/target" -v "$BACKUP_VOL_DIR":/backup alpine:3.22 \
        sh -lc "cd /target && tar -xzf /backup/${v}.tar.gz"
      echo "Restored volume: $v"
    else
      echo "Warning: archive not found for $v in $BACKUP_VOL_DIR"
    fi
  done
fi

echo "Restarting CVAT services"
cd "$PROJECT_ROOT"
docker-compose restart || true

echo "Verification: latest engine_video entries"
docker exec cvat_db psql -U root -d cvat -c "select v.id, s.file, v.path from engine_video v left join engine_data d on v.data_id=d.id left join engine_serverfile s on s.data_id=d.id order by v.id desc limit 50;"

echo "Rollback script completed"
