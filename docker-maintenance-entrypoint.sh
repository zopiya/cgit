#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
export HOME=/job-work

mkdir -p /run /job-state /job-work /backups

if [ "$(id -u)" = "0" ]; then
    group_name="$(getent group "$PGID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$group_name" ]; then
        addgroup -g "$PGID" cgit-maintenance
        group_name=cgit-maintenance
    fi
    user_name="$(getent passwd "$PUID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$user_name" ]; then
        adduser -D -H -u "$PUID" -G "$group_name" cgit-maintenance
        user_name=cgit-maintenance
    fi
    chown -R "$PUID:$PGID" /job-state /job-work /run
    chown "$PUID:$PGID" /backups
    su-exec "$PUID:$PGID" cgit-runtime jobs validate
    su-exec "$PUID:$PGID" cgit-runtime jobs \
        crontab --output /run/crontab
    exec su-exec "$PUID:$PGID" supercronic /run/crontab
fi

cgit-runtime jobs validate
cgit-runtime jobs crontab --output /run/crontab
exec supercronic /run/crontab
