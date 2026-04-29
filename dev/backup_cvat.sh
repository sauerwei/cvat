#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKUP_ROOT="${PROJECT_ROOT}/backups"
STAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_DIR="${BACKUP_ROOT}/cvat_${STAMP}"

# Configuration: retention in days and optional rsync destination
# Set RETENTION_DAYS to 0 to disable automatic pruning
RETENTION_DAYS="${RETENTION_DAYS:-30}"
# Optional rsync destination, e.g. user@backup.example.com:/path/to/backups
RSYNC_DEST="${RSYNC_DEST:-}"
# Optional GPG settings:
# If GPG_RECIPIENT is set, public-key encrypt files for that recipient.
# If GPG_PASSPHRASE is set, symmetric encryption is used (AES256).
# If ENCRYPTION_KEEP_PLAINTEXT is non-empty, keep plaintext files after encryption.
GPG_RECIPIENT="${GPG_RECIPIENT:-}"
GPG_PASSPHRASE="${GPG_PASSPHRASE:-}"
ENCRYPTION_KEEP_PLAINTEXT="${ENCRYPTION_KEEP_PLAINTEXT:-}"

mkdir -p "${TARGET_DIR}"

echo "Creating backup in ${TARGET_DIR}"

docker exec cvat_db pg_dump -U root -d cvat -Fc > "${TARGET_DIR}/cvat_db.dump"

for vol in cvat_cvat_data cvat_cvat_keys cvat_cvat_logs; do
    docker run --rm \
        -v "${vol}:/source:ro" \
        -v "${TARGET_DIR}:/backup" \
        alpine:3.22 \
        sh -lc "tar -czf /backup/${vol}.tar.gz -C /source ."
done

cat > "${TARGET_DIR}/RESTORE.txt" <<EOF
Backup created: $(date -Iseconds)

Files:
- cvat_db.dump
- cvat_cvat_data.tar.gz
- cvat_cvat_keys.tar.gz
- cvat_cvat_logs.tar.gz

Restore database:
  docker exec -i cvat_db pg_restore -U root -d cvat --clean --if-exists < cvat_db.dump

Restore volume data (example for cvat_cvat_data):
  docker run --rm -v cvat_cvat_data:/target -v \$(pwd):/backup alpine:3.22 sh -lc "cd /target && tar -xzf /backup/cvat_cvat_data.tar.gz"
EOF

echo "Backup completed: ${TARGET_DIR}"

# Prune old backups
if [ "${RETENTION_DAYS}" -gt 0 ]; then
  echo "Pruning backups older than ${RETENTION_DAYS} days"
  find "${BACKUP_ROOT}" -maxdepth 1 -type d -name 'cvat_*' -mtime +${RETENTION_DAYS} -print -exec rm -rf {} \;
fi

# Optional upload via rsync (requires SSH keys / access configured)
if [ -n "${RSYNC_DEST}" ]; then
  echo "Uploading ${TARGET_DIR} to ${RSYNC_DEST} via rsync"
  # If encryption requested, encrypt files before upload
  ENCRYPTED=false
  if command -v gpg >/dev/null 2>&1 && { [ -n "${GPG_RECIPIENT}" ] || [ -n "${GPG_PASSPHRASE}" ]; }; then
    echo "Encrypting backup files with GPG"
    for f in "${TARGET_DIR}"/*; do
      if [ -f "$f" ]; then
        out="$f.gpg"
        if [ -n "${GPG_RECIPIENT}" ]; then
          gpg --batch --yes --output "$out" --encrypt -r "${GPG_RECIPIENT}" "$f"
        else
          gpg --batch --yes --pinentry-mode loopback --passphrase "${GPG_PASSPHRASE}" -c --cipher-algo AES256 --output "$out" "$f"
        fi
      fi
    done
    ENCRYPTED=true
    if [ -z "${ENCRYPTION_KEEP_PLAINTEXT}" ]; then
      echo "Removing plaintext backup files"
      find "${TARGET_DIR}" -maxdepth 1 -type f \( -name '*.dump' -o -name '*.tar.gz' \) -delete || true
    fi
  else
    if [ -n "${GPG_RECIPIENT}" ] || [ -n "${GPG_PASSPHRASE}" ]; then
      echo "Warning: gpg not found or not configured; skipping encryption"
    fi
  fi

  if [ "${ENCRYPTED}" = true ]; then
    rsync -av --delete "${TARGET_DIR}/" "${RSYNC_DEST%/}/$(basename "${TARGET_DIR}")/"
  else
    rsync -av --delete "${TARGET_DIR}/" "${RSYNC_DEST%/}/$(basename "${TARGET_DIR}")/"
  fi
fi
