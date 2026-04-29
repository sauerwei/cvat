Backup and Restore for CVAT
==========================

This document describes how to restore CVAT from backups created by `dev/backup_cvat.sh`, how to run a test-restore, how to configure scheduled backups, retention, and optional remote upload.

1) Locate a backup
-------------------
Backups are stored in the repository under `backups/` with folders named `cvat_YYYYMMDD_HHMMSS`.
Example latest backup path:

  /home/devbox/Documents/cvat/backups/cvat_20260429_142848

2) Restore to a running CVAT instance (production)
--------------------------------------------------
If CVAT and its containers are running (or you restarted with `docker-compose up -d`), you can restore volumes and DB from a backup directory. Replace the timestamp with your backup folder.

Volumes (example):

```bash
cd /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
docker run --rm -v cvat_cvat_data:/target -v "$(pwd)":/backup alpine:3.22 sh -lc "cd /target && tar -xzf /backup/cvat_cvat_data.tar.gz"
docker run --rm -v cvat_cvat_keys:/target -v "$(pwd)":/backup alpine:3.22 sh -lc "cd /target && tar -xzf /backup/cvat_cvat_keys.tar.gz"
docker run --rm -v cvat_cvat_logs:/target -v "$(pwd)":/backup alpine:3.22 sh -lc "cd /target && tar -xzf /backup/cvat_cvat_logs.tar.gz"
```

Database:

```bash
cd /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
docker exec -i cvat_db pg_restore -U root -d cvat --clean --if-exists < cvat_db.dump
```

After restoring, restart CVAT services:

```bash
docker-compose restart
```

3) Test-restore in an isolated environment (recommended)
-------------------------------------------------------
Use the following steps to verify a backup without touching production services. Replace BACKUP_DIR with the absolute path to the backup folder.

```bash
BACKUP_DIR=/home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS

# create temporary volumes
docker volume create test_cvat_data
docker volume create test_cvat_keys
docker volume create test_cvat_logs

# extract archives into the temporary volumes (use absolute host path)
docker run --rm -v test_cvat_data:/target -v "$BACKUP_DIR":/backup alpine:3.22 \
  sh -lc "cd /target && tar -xzf /backup/cvat_cvat_data.tar.gz"
docker run --rm -v test_cvat_keys:/target -v "$BACKUP_DIR":/backup alpine:3.22 \
  sh -lc "cd /target && tar -xzf /backup/cvat_cvat_keys.tar.gz"
docker run --rm -v test_cvat_logs:/target -v "$BACKUP_DIR":/backup alpine:3.22 \
  sh -lc "cd /target && tar -xzf /backup/cvat_cvat_logs.tar.gz"

# run a temporary Postgres with the same DB/user used by CVAT
docker run -d --name test_cvat_db -e POSTGRES_USER=root -e POSTGRES_PASSWORD=root -e POSTGRES_DB=cvat postgres:15

# wait until Postgres is ready
until docker exec test_cvat_db pg_isready -U root -d cvat >/dev/null 2>&1; do sleep 1; done

# restore the DB
cat "$BACKUP_DIR/cvat_db.dump" | docker exec -i test_cvat_db pg_restore -U root -d cvat --clean --if-exists

# verify (list tables)
docker exec test_cvat_db psql -U root -d cvat -c "\dt"

# cleanup (remove test container and volumes)
docker rm -f test_cvat_db
docker volume rm test_cvat_data test_cvat_keys test_cvat_logs
```

4) Scheduling backups (cron)
----------------------------
Open `crontab -e` for the user running CVAT and add (daily at 02:00):

```
0 2 * * * /home/devbox/Documents/cvat/dev/backup_cvat.sh >> /var/log/cvat-backup.log 2>&1
```

5) Retention and remote upload
-------------------------------
The backup script supports two optional environment variables:

- `RETENTION_DAYS` (default `30`) — backups older than this are pruned after a run. Set to `0` to disable pruning.
- `RSYNC_DEST` — optional `rsync` destination (e.g. `user@host:/path`) to upload each new backup. Ensure SSH keys are configured for the target host.

Examples:

```bash
# keep backups for 7 days and upload to remote host
RETENTION_DAYS=7 RSYNC_DEST="backup@example.com:/srv/backups/cvat" /home/devbox/Documents/cvat/dev/backup_cvat.sh
```

Recommended routine backup setup
-------------------------------
For routine backups, use one of these two patterns:

1. Local cron on the VM plus remote copy

```bash
# daily at 02:00, keep 30 days, copy to external backup host
0 2 * * * cd /home/devbox/Documents/cvat && RETENTION_DAYS=30 RSYNC_DEST="backup@example.com:/srv/backups/cvat" /home/devbox/Documents/cvat/dev/backup_cvat.sh >> /var/log/cvat-backup.log 2>&1
```

2. Local cron on the VM plus encrypted remote copy

```bash
# public-key encryption before upload
0 2 * * * cd /home/devbox/Documents/cvat && RETENTION_DAYS=30 GPG_RECIPIENT="backup@company.com" RSYNC_DEST="backup@example.com:/srv/backups/cvat" /home/devbox/Documents/cvat/dev/backup_cvat.sh >> /var/log/cvat-backup.log 2>&1
```

Notes:
- Prefer an external backup host, NAS, or object storage over storing archives in a database.
- If you really need a central storage system, use it as a file target (for example via `rsync`, NFS, S3, or GitLab artifacts), not as the place where the SQL dump itself is stored.
- If you want no local cron, the same command can be scheduled with `systemd timer` or GitLab scheduled pipelines.

Encryption before upload
------------------------
The backup script can optionally encrypt backup files before uploading them. Two modes are supported:

- Public-key encryption: set `GPG_RECIPIENT` to the GPG key ID or user id of the recipient (their public key must be present in the GPG keyring on the machine running the backup).
- Symmetric encryption: set `GPG_PASSPHRASE` to a passphrase (the script uses AES256 symmetric encryption).

Examples:

```bash
# Public-key encryption and rsync upload
GPG_RECIPIENT="backup@company.com" RSYNC_DEST="backup@example.com:/srv/backups/cvat" ./dev/backup_cvat.sh

# Symmetric encryption (passphrase in env) and keep plaintext locally
GPG_PASSPHRASE="s3cret" ENCRYPTION_KEEP_PLAINTEXT=1 RSYNC_DEST="backup@example.com:/srv/backups/cvat" ./dev/backup_cvat.sh
```

Notes:
- `gpg` must be installed on the machine running the backup.
- If `ENCRYPTION_KEEP_PLAINTEXT` is unset, the script will remove plaintext `.dump` and `.tar.gz` files from the backup directory before uploading.
- Store passphrases and private keys securely (use system secrets or CI secrets for automated runs).

6) Security notes
-----------------
- Backups contain database dumps and keys. Limit filesystem access to `backups/` and the remote target.
- Consider encrypting archives (e.g. `gpg`) before sending them off-site.

7) Troubleshooting
------------------
- If `docker run -v "$BACKUP_DIR":/backup` fails, ensure you use an absolute path for `$BACKUP_DIR`.
- If `pg_restore` reports permission errors, confirm the DB user/password in the target Postgres container and adapt the commands.

If you want, I can also add automatic GPG encryption before uploading, or add an `--upload` flag to the script.

8) Safe rollback (recommended)
--------------------------------
Use the provided interactive safe rollback helper to avoid accidental data loss and to preview the restore before applying it to production.

- Script: `dev/safe_rollback_cvat.sh`
- Features:
  - creates a timestamped safety DB dump before any restore
  - optional `--test-restore` to load the chosen dump into a temporary Postgres and show a diff of `engine_video` entries
  - optional `--vol-dir` to restore volumes from a folder of archives
  - confirmation prompt; use `--yes` to skip confirmation

Example: test then restore (interactive):

```bash
./dev/safe_rollback_cvat.sh --dump backups/cvat_20260429_140058/cvat_db.dump --vol-dir backups/cvat_20260429_140058 --test-restore
```

Example: non-interactive restore from a dump and volumes:

```bash
./dev/safe_rollback_cvat.sh --dump /path/to/dump.dump --vol-dir /path/to/backup_dir --yes
```

Notes:
- The script writes a safety dump to `backups/pre_rollback_YYYYMMDD_HHMMSS.dump` which you can use to rollback again if needed.
- After restore the script restarts Compose and prints current `engine_video` entries for quick verification.

9) Make CVAT reachable from other devices on the network
-------------------------------------------------------
If other people should open CVAT from another device, start Compose with the VM's IP address as `CVAT_HOST` and make sure port `8080` is reachable.

Find the current VM IP:

```bash
hostname -I
# or more specific:
ip addr show | grep 'inet ' | grep -v '127.0.0.1'
```

Export the IP and start CVAT with it:

```bash
export CVAT_HOST=10.28.252.47
docker compose up -d
```

If you want the IP to be reused in your shell session, you can also set it in one line:

```bash
export CVAT_HOST="$(hostname -I | awk '{print $1}')"
docker compose up -d
```

Then open CVAT from another device in the same network via:

```text
http://10.28.252.47:8080/
```

If the page still is not reachable, allow the port in your firewall on the VM host:

```bash
sudo ufw allow 8080/tcp
```

If your VM IP changes often, consider making it static in your VM/network settings or use a local DNS name.

10) Quick verification commands
--------------------------------
- List backups:

```bash
ls -lh backups/
```

- Show RESTORE instructions for a specific backup:

```bash
cat backups/cvat_YYYYMMDD_HHMMSS/RESTORE.txt
```

- Check CVAT UI (replace with your host):

```bash
curl -I -H "Host: 10.28.252.47" http://127.0.0.1:8080/
```

