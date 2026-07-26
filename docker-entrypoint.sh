#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /var/cache/cgit /repos

if [ "$(id -u)" = "0" ]; then
    # Reuse an existing group/user already sitting on this gid/uid (e.g. a
    # NAS-assigned id that collides with something already in the image)
    # instead of blindly (re)creating "cgit" and erroring out on conflict.
    group_name="$(getent group "$PGID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$group_name" ]; then
        delgroup cgit 2>/dev/null || true
        addgroup -g "$PGID" cgit
        group_name=cgit
    fi

    user_name="$(getent passwd "$PUID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$user_name" ]; then
        deluser cgit 2>/dev/null || true
        adduser -D -H -u "$PUID" -G "$group_name" cgit
        user_name=cgit
    fi

    chown -R "$PUID:$PGID" /var/cache/cgit

    exec su-exec "$PUID:$PGID" lighttpd -D -f /etc/lighttpd/lighttpd.conf
fi

exec lighttpd -D -f /etc/lighttpd/lighttpd.conf
