#!/usr/bin/env python3
"""Validate environment and generate runtime files without editing mounted config."""

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit


def flag(name):
    value = os.environ.get(name, "0")
    if value not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


def configure(source_path=Path("/etc/cgitrc"), repos="/repos", lfs="/lfs",
              cgit="/usr/local/libexec/cgit.cgi"):
    base = os.environ.get("CGIT_BASE_URL", "").rstrip("/")
    if base:
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.netloc or url.path
                or url.query or url.fragment or url.username or url.password
                or any(c.isspace() for c in base)):
            raise ValueError("CGIT_BASE_URL must be an http(s) origin, without path or credentials")
    mode = os.environ.get("CGIT_AUTH_MODE", "public")
    if mode not in ("public", "private"):
        raise ValueError("CGIT_AUTH_MODE must be public or private")
    username = os.environ.get("CGIT_USERNAME", "")
    password_file = os.environ.get("CGIT_PASSWORD_FILE", "")
    if bool(username) != bool(password_file):
        raise ValueError("Set both CGIT_USERNAME and CGIT_PASSWORD_FILE")
    if username and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username):
        raise ValueError("CGIT_USERNAME contains unsupported characters")
    push = flag("CGIT_HTTP_PUSH")
    if (mode == "private" or push) and not password_file:
        raise ValueError("Private mode and HTTP push require a configured account")
    if password_file:
        secret = Path(password_file).read_bytes().rstrip(b"\r\n")
        if not secret or b"\n" in secret or b"\r" in secret:
            raise ValueError("Password file must contain one nonempty line")
    max_size = int(os.environ.get("CGIT_LFS_MAX_SIZE", str(10 * 1024**3)))
    if not 1 <= max_size <= 1024**4:
        raise ValueError("CGIT_LFS_MAX_SIZE must be between 1 byte and 1 TiB")
    ssh = os.environ.get("CGIT_SSH_CLONE_URL", "")
    if any(c.isspace() for c in ssh) or (ssh and "$CGIT_REPO_URL" not in ssh):
        raise ValueError("CGIT_SSH_CLONE_URL must contain literal $CGIT_REPO_URL and no whitespace")
    runtime = Path(os.environ.get("CGIT_RUNTIME_DIR", "/var/cache/cgit/runtime"))
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime.parent / "uploads").mkdir(exist_ok=True)
    cfg = dict(repos=repos, lfs=lfs, base_url=base, auth_mode=mode,
               username=username, password_file=password_file, http_push=push,
               max_size=max_size, cgit=cgit,
               cgit_config=str(runtime / "cgitrc"))
    (runtime / "settings.json").write_text(json.dumps(cfg) + "\n")
    # Insert overrides before scan-path: each scanned repo inherits these settings.
    source = source_path.read_text()
    clone_url = (base or "http://$HTTP_HOST") + "/$CGIT_REPO_URL"
    overrides = f"clone-url={clone_url}{' ' + ssh if ssh else ''}\n"
    source = re.sub(r"^clone-url=.*\n?", "", source, flags=re.M)
    # Dynamic object availability must not be hidden behind cgit's HTML cache.
    source = re.sub(r"^cache-size=.*\n?", "", source, flags=re.M)
    (runtime / "cgitrc").write_text("cache-size=0\n" + overrides + source)
    (runtime / "limits.conf").write_text(
        f"server.max-request-size = {max(1024, (max_size + 1023) // 1024)}\n"
    )


if __name__ == "__main__":
    try:
        configure()
    except (ValueError, OSError) as exc:
        sys.exit(f"cgit configuration error: {exc}")
