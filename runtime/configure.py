#!/usr/bin/env python3
"""Validate environment and generate runtime files without editing mounted config."""

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from common import cgit_settings, project_entries, validate_scan


def repository_policy(source, repos):
    """Keep the supported scan contract explicit instead of silently bypassing it."""
    policy = {}
    scans = 0
    for key, value in cgit_settings(source):
        if key == "include" or key.startswith("repo."):
            raise ValueError("Use scan-path and repository Git config; include/repo.* are not supported")
        if key == "scan-path":
            scans += 1
            if value != repos:
                raise ValueError(f"scan-path must be {repos}")
        if key in ("project-list", "strict-export", "remove-suffix", "enable-git-config", "scan-hidden-path"):
            if scans:
                raise ValueError(f"{key} must appear before scan-path")
            if key == "project-list":
                if not Path(value).is_absolute() or "$" in value:
                    raise ValueError("project-list must be an absolute path without macros")
                project_entries(value)
                policy["project_list"] = value
            elif key == "strict-export":
                if not value or "/" in value or "\\" in value or value in (".", "..") or "$" in value:
                    raise ValueError("strict-export must be a marker filename")
                policy["strict_export"] = value
            elif value != ("0" if key == "scan-hidden-path" else "1"):
                raise ValueError(f"{key} must be {'0' if key == 'scan-hidden-path' else '1'}")
    if scans != 1:
        raise ValueError("Exactly one scan-path is required")
    return policy


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
    if mode not in ("public", "private", "access"):
        raise ValueError("CGIT_AUTH_MODE must be public, private or access")
    username = os.environ.get("CGIT_USERNAME", "")
    password_file = os.environ.get("CGIT_PASSWORD_FILE", "")
    access_root = os.environ.get("CGIT_ACCESS_ROOT", "")
    gitolite = os.environ.get("CGIT_GITOLITE", "")
    if mode == "access":
        if username or password_file:
            raise ValueError("Access mode uses identity.json; do not set CGIT_USERNAME/CGIT_PASSWORD_FILE")
        if not access_root or not Path(access_root).is_absolute():
            raise ValueError("Access mode requires an absolute CGIT_ACCESS_ROOT")
        if not Path(access_root).is_dir():
            raise ValueError("CGIT_ACCESS_ROOT must be an existing directory")
        if not gitolite or not Path(gitolite).is_absolute() or not os.access(gitolite, os.X_OK):
            raise ValueError("Access mode requires an executable absolute CGIT_GITOLITE")
    else:
        if access_root or gitolite:
            raise ValueError("CGIT_ACCESS_ROOT/CGIT_GITOLITE require CGIT_AUTH_MODE=access")
        if bool(username) != bool(password_file):
            raise ValueError("Set both CGIT_USERNAME and CGIT_PASSWORD_FILE")
        if username and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username):
            raise ValueError("CGIT_USERNAME contains unsupported characters")
    push = flag("CGIT_HTTP_PUSH")
    ssh_push = flag("CGIT_SSH_PUSH")
    if mode != "access" and (mode == "private" or push) and not password_file:
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
    source = "".join(f"{key}={value}\n" for key, value in cgit_settings(source_path.read_text()))
    policy = repository_policy(source, repos)
    validate_scan(repos)
    cfg = dict(repos=repos, lfs=lfs, base_url=base, auth_mode=mode,
               username=username, password_file=password_file, http_push=push,
               ssh_push=ssh_push,
               max_size=max_size, cgit=cgit,
               cgit_config=str(runtime / "cgitrc"), repository_policy=policy)
    if mode == "access":
        cfg["access"] = {"root": access_root, "gitolite": gitolite}
    (runtime / "settings.json").write_text(json.dumps(cfg) + "\n")
    # Insert overrides before scan-path: each scanned repo inherits these settings.
    clone_url = (base or "http://$HTTP_HOST") + "/$CGIT_REPO_URL"
    login_header = ""
    if mode == "access":
        # A top-level challenge establishes the Basic protection space at /,
        # rather than at a particular private repository's directory.
        header_file = runtime / "access-header.html"
        header_file.write_text(
            '<nav aria-label="Account"><a href="/_login">登录 / Sign in</a></nav>\n')
        login_header = f"header={header_file}\n"
    overrides = (login_header + "remove-suffix=1\nenable-git-config=1\nscan-hidden-path=0\n"
                 f"clone-url={clone_url}{' ' + ssh if ssh else ''}\n")
    source = re.sub(r"^clone-url=.*\n?", "", source, flags=re.M)
    # Dynamic object availability must not be hidden behind cgit's HTML cache.
    source = re.sub(r"^cache-size=.*\n?", "", source, flags=re.M)
    (runtime / "cgitrc").write_text("cache-size=0\n" + overrides + source)
    (runtime / "limits.conf").write_text(
        f"server.max-request-size = {max(1024, (max_size + 1023) // 1024)}\n"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/etc/cgitrc"))
    parser.add_argument("--repos", default="/repos")
    parser.add_argument("--lfs", default="/lfs")
    parser.add_argument("--cgit", default="/usr/local/libexec/cgit.cgi")
    args = parser.parse_args()
    try:
        configure(source_path=args.source, repos=args.repos, lfs=args.lfs, cgit=args.cgit)
    except (ValueError, OSError) as exc:
        sys.exit(f"cgit configuration error: {exc}")
