Backup and Restore for CVAT
===========================

This document describes the backup and restore flow implemented by
`dev/backup_cvat.sh`. It follows the same volume-based approach as
`site/content/en/docs/administration/community/advanced/backup_guide.md`.
For this installation, the script also starts CVAT with
`CVAT_HOST=10.28.252.47` unless you override `CVAT_HOST` explicitly.

1. What gets backed up
-------------------
The script creates a timestamped folder under `backups/`:

```text
backups/cvat_YYYYMMDD_HHMMSS/
```

It stores these files:

- `cvat_db.tar.gz`
- `cvat_data.tar.gz`
- `cvat_events_db.tar.gz`
- `RESTORE.txt`

The matching source paths are:

- `cvat_db` container: `/var/lib/postgresql/data`
- `cvat_server` container: `/home/django/data`
- `cvat_clickhouse` container: `/var/lib/clickhouse`

2. Create a backup
-------------------
Run the script from the repository root:

```bash
./dev/backup_cvat.sh
```

If your deployment uses additional Compose files, pass them through
`COMPOSE_FILES`:

```bash
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml" \
  ./dev/backup_cvat.sh
```

If you need a different host/IP for this run, override `CVAT_HOST`:

```bash
CVAT_HOST=10.28.252.47 ./dev/backup_cvat.sh
```

The script:

1. Stops the CVAT containers with `docker compose stop`.
2. Creates the backup archives.
3. Starts the CVAT containers again with `docker compose up -d`.

3. Restore CVAT from backup
-------------------
Use exactly the same CVAT version for restore as for backup.

Stop CVAT first:

```bash
CVAT_HOST=10.28.252.47 docker compose stop
```

If you use additional Compose files:

```bash
CVAT_HOST=10.28.252.47 docker compose \
  -f docker-compose.yml -f docker-compose.override.yml stop
```

Change into the backup directory and restore the data:

```bash
cd /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
docker run --rm --name temp_backup --volumes-from cvat_db -v "$(pwd)":/backup ubuntu \
  bash -c "cd /var/lib/postgresql/data && tar -xvf /backup/cvat_db.tar.gz --strip 4"
docker run --rm --name temp_backup --volumes-from cvat_server -v "$(pwd)":/backup ubuntu \
  bash -c "cd /home/django/data && tar -xvf /backup/cvat_data.tar.gz --strip 3"
docker run --rm --name temp_backup --volumes-from cvat_clickhouse -v "$(pwd)":/backup ubuntu \
  bash -c "cd /var/lib/clickhouse && tar -xvf /backup/cvat_events_db.tar.gz --strip 3"
```

Then start CVAT again:

```bash
CVAT_HOST=10.28.252.47 docker compose up -d
```

If you use additional Compose files:

```bash
CVAT_HOST=10.28.252.47 docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

4. Verify the backup
-------------------
Confirm the backup files exist:

```bash
ls /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
```

Expected output:

```text
RESTORE.txt
cvat_data.tar.gz
cvat_db.tar.gz
cvat_events_db.tar.gz
```

5. Scheduling backups
-------------------
Example daily cron job at 04:00:

```cron
0 4 * * * cd /home/devbox/Documents/cvat && \
  CVAT_HOST=10.28.252.47 ./dev/backup_cvat.sh >> /var/log/cvat-backup.log 2>&1
```

If you use extra Compose files:

```cron
0 4 * * * cd /home/devbox/Documents/cvat && \
  CVAT_HOST=10.28.252.47 \
  COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml" \
  ./dev/backup_cvat.sh >> /var/log/cvat-backup.log 2>&1
```

API-based project ZIP backups
-----------------------------
If you also want a daily API backup of all accessible projects as ZIP files,
use `dev/backup_projects_via_api.py`. This does not stop CVAT containers and
uses the same project backup endpoint that the UI triggers.

The script creates a timestamped folder under `backups/`:

```text
backups/projects_api_YYYYMMDD_HHMMSS/
```

Each run contains:

- one ZIP file per project
- `manifest.json` with IDs, filenames, sizes, and failures

Authentication is done with a CVAT Personal Access Token
(recommended) or, if needed, basic auth credentials.

Example manual run:

```bash
cd /home/devbox/Documents/cvat
CVAT_BASE_URL=http://10.28.252.47 \
CVAT_ACCESS_TOKEN='YOUR_PAT_HERE' \
./dev/backup_projects_via_api.py
```

Optional environment variables:

- `CVAT_API_BACKUP_ROOT` to change the backup destination
- `CVAT_API_BACKUP_LIGHTWEIGHT=false` to force full backups where possible
- `CVAT_API_POLL_INTERVAL=5` to slow down request polling
- `CVAT_API_REQUEST_TIMEOUT=3600` to wait longer per project export

Example daily cron job at 02:30:

```cron
30 2 * * * cd /home/devbox/Documents/cvat && \
  CVAT_BASE_URL=http://10.28.252.47 \
  CVAT_ACCESS_TOKEN='YOUR_PAT_HERE' \
  ./dev/backup_projects_via_api.py >> /var/log/cvat-project-api-backup.log 2>&1
```

The script exits with:

- `0` if all project backups were downloaded successfully
- `2` if at least one project failed but the run finished
- `1` on a fatal setup or API error

6. Notes
-------------------
- The script matches the documented volume-backup method and no longer uses `pg_dump`.
- The script backs up the PostgreSQL data directory, the CVAT media data,
  and the ClickHouse events database.
- `RESTORE.txt` is generated into each backup directory with the matching
  restore commands.
- The script defaults `CVAT_HOST` to `10.28.252.47` so the UI remains
  reachable via that IP after `docker compose up -d`.

7. Rollback from backup
-------------------
Use `dev/rollback_cvat.sh` to restore a backup directory created by
`dev/backup_cvat.sh`. The script follows the same restore steps as the
backup guide:

1. Stop CVAT containers.
2. Restore `cvat_db.tar.gz`.
3. Restore `cvat_data.tar.gz`.
4. Restore `cvat_events_db.tar.gz`.
5. Start CVAT again.

Restore the latest backup automatically:

```bash
./dev/rollback_cvat.sh
```

Restore a specific backup directory:

```bash
./dev/rollback_cvat.sh /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
```

If you use additional Compose files:

```bash
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.override.yml" \
  ./dev/rollback_cvat.sh \
  /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
```

The rollback script defaults `CVAT_HOST` to `10.28.252.47`.
Override it for a different host/IP if needed:

```bash
CVAT_HOST=10.28.252.47 ./dev/rollback_cvat.sh \
  /home/devbox/Documents/cvat/backups/cvat_YYYYMMDD_HHMMSS
```
