#!/usr/bin/env bash
# Nightly database backup (P1.07). Keeps 14 days of gzipped backups on this server.
#
# Staging runs SQLite in the `data` volume (see README, "Full local stack with Docker").
# The SQLite file is copied with SQLite's online backup API inside the web container, so
# it is consistent even while the app is writing. If a `postgres` service is added to
# docker-compose.yml, the script uses pg_dump instead.
#
# Install with cron at 02:00 every night (crontab -e as the deploy user):
#   0 2 * * * /home/deploy/growthstack/deploy/backup.sh >> /var/log/growthstack-backup.log 2>&1
#
# Restore test, SQLite (into a scratch file, never over the live database):
#   gunzip -c /var/backups/growthstack/growthstack-<date>.sqlite.gz > /tmp/scratch.db
#   python3 -c "import sqlite3; print(sqlite3.connect('/tmp/scratch.db').execute('select count(*) from leads').fetchone()[0])"
#   docker compose --env-file ../.env exec -T web python -c "import os, sqlite3; print(sqlite3.connect(os.environ['DATABASE_URL'].split('sqlite:///', 1)[1]).execute('select count(*) from leads').fetchone()[0])"
#   rm /tmp/scratch.db
# The two counts should match.
#
# The backups stay on the same server, so they don't survive losing it: copying them
# off the server is a follow-up.
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/home/deploy/growthstack/deploy}"  # where docker-compose.yml lives
BACKUP_DIR="${BACKUP_DIR:-/var/backups/growthstack}"
KEEP_DAYS="${KEEP_DAYS:-14}"

cd "$DEPLOY_DIR"
# Compose reads ../.env only when told to; without it, it stops on CRAWL4AI_TOKEN.
compose() { docker compose --env-file ../.env "$@"; }
mkdir -p "$BACKUP_DIR"

if compose config --services | grep -qx postgres; then
    out="$BACKUP_DIR/growthstack-$(date +%F).sql.gz"
    dump() { compose exec -T postgres pg_dump -U growthstack growthstack; }
else
    out="$BACKUP_DIR/growthstack-$(date +%F).sqlite.gz"
    dump() {
        compose exec -T web python - <<'PY'
import os, sqlite3, sys, tempfile

path = os.environ["DATABASE_URL"].split("sqlite:///", 1)[1]
fd, tmp = tempfile.mkstemp(suffix=".db")
os.close(fd)
src, dst = sqlite3.connect(path), sqlite3.connect(tmp)
src.backup(dst)
dst.close()
src.close()
with open(tmp, "rb") as f:
    sys.stdout.buffer.write(f.read())
os.remove(tmp)
PY
    }
fi

# Write to a temporary name first so a failed backup never replaces a good one.
tmp="$out.partial"
dump | gzip > "$tmp"
gzip -t "$tmp"
mv "$tmp" "$out"
echo "$(date -Is) backup written: $out ($(du -h "$out" | cut -f1))"

find "$BACKUP_DIR" -name 'growthstack-*.gz' -mtime +"$KEEP_DAYS" -delete
