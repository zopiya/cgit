#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
ACCESS_ROOT="${CGIT_ACCESS_ROOT:-/access}"

mkdir -p /run/cgit-ssh /ssh-host-keys "$ACCESS_ROOT/ssh" /var/cache/cgit

if [ "$(id -u)" = "0" ]; then
    group_name="$(getent group "$PGID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$group_name" ]; then
        addgroup -g "$PGID" cgit-ssh
        group_name=cgit-ssh
    fi

    user_name="$(getent passwd "$PUID" 2>/dev/null | cut -d: -f1 || true)"
    if [ -z "$user_name" ]; then
        adduser -D -H -h "$ACCESS_ROOT" -u "$PUID" -G "$group_name" \
            -s /bin/sh git
        user_name=git
    fi

    if [ ! -f /ssh-host-keys/ssh_host_ed25519_key ]; then
        ssh-keygen -q -t ed25519 -N "" -f /ssh-host-keys/ssh_host_ed25519_key
    fi
    chown -R "$PUID:$PGID" /run/cgit-ssh /var/cache/cgit
    chmod 700 /ssh-host-keys "$ACCESS_ROOT/ssh"
    chown -R "$PUID:$PGID" "$ACCESS_ROOT/ssh"
    if [ ! -s "$ACCESS_ROOT/ssh/authorized_keys" ]; then
        echo "SSH access keys are missing; publish Gitolite policy and run authorized-keys first" >&2
        exit 1
    fi
    chown "$PUID:$PGID" "$ACCESS_ROOT/ssh/authorized_keys"

    sed "s/^AllowUsers git$/AllowUsers $user_name/" \
        /etc/ssh/cgit_sshd_config > /run/cgit-ssh/sshd_config
    su-exec "$PUID:$PGID" python3 /usr/local/lib/cgit-personal/configure.py
    exec /usr/sbin/sshd -D -e -f /run/cgit-ssh/sshd_config
fi

python3 /usr/local/lib/cgit-personal/configure.py
exec /usr/sbin/sshd -D -e -f /etc/ssh/cgit_sshd_config
