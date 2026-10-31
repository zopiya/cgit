"""Shared, dependency-free configuration, repository and LFS storage helpers."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

OID = re.compile(r"[0-9a-f]{64}\Z")
POINTER = re.compile(
    rb"version https://git-lfs.github.com/spec/v1\r?\n"
    rb"oid sha256:([0-9a-f]{64})\r?\nsize (0|[1-9][0-9]*)\r?\n?\Z"
)


class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def config():
    with open(os.environ.get("CGIT_RUNTIME_CONFIG", "/var/cache/cgit/runtime/settings.json")) as f:
        return json.load(f)


def pointer(data):
    match = POINTER.fullmatch(data) if len(data) <= 1024 else None
    return (match[1].decode(), int(match[2])) if match else None


def safe_path(root, relative):
    """Reject traversal and symlinks, including internal symlinks, at this boundary."""
    parts = relative.split("/")
    if not relative or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise RequestError(404, "Not found")
    if any(ord(c) < 32 or c == "\\" or ord(c) == 127 for c in relative):
        raise RequestError(404, "Not found")
    base = Path(root).resolve()
    path = base
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise RequestError(404, "Not found")
    if not path.resolve().is_relative_to(base):
        raise RequestError(404, "Not found")
    return path


def cgit_flag(value):
    """Only accept the documented numeric flags; never disagree with cgit's atoi."""
    if value not in ("0", "1"):
        raise ValueError("cgit.ignore must be 0 or 1 (not true/false)")
    return value == "1"


def cgit_settings(source):
    """Match cgit v1.3.1's leading-space and #/; comment rules."""
    if "\0" in source:
        raise ValueError("NUL is not supported in cgit configuration")
    for raw in source.replace("\r\n", "\n").split("\n"):
        line = raw.lstrip(" \t\r\v\f")
        if not line or line.startswith(("#", ";")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ValueError("Malformed cgit configuration line (expected name=value)")
        yield key, value


def repo_ignored(repo):
    result = git(repo, "config", "--get", "cgit.ignore", check=False)
    if result.returncode not in (0, 1):
        raise ValueError(f"Cannot read repository configuration: {repo}")
    ignored = cgit_flag(result.stdout.decode().strip()) if result.returncode == 0 else False
    # cgit reads this file after Git config, with unprefixed repository keys.
    local = repo / "cgitrc"
    if local.exists():
        for key, value in cgit_settings(local.read_text()):
            if key == "ignore":
                ignored = cgit_flag(value)
    return ignored


def validate_scan(root):
    """Fail closed before cgit can scan unsupported paths or ambiguous flags."""
    def unreadable(exc):
        raise exc

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=unreadable):
        path = Path(directory)
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "HEAD" in files and "objects" in dirs:
            repo_ignored(path)
            dirs[:] = []
            continue
        if (path / ".git").exists():
            raise ValueError("Only bare repositories are supported under the repository root")
        if any((path / d).is_symlink() for d in dirs):
            raise ValueError("Symlink directories are not supported under the repository root")


def project_entries(filename):
    entries = [line for line in Path(filename).read_text().splitlines() if line]
    for entry in entries:
        parts = entry.split("/")
        if (not entry or any(not p or p.startswith(".") or "\\" in p
                             or any(ord(c) < 32 for c in p) for p in parts)):
            raise ValueError("project-list requires canonical relative paths without a trailing slash")
    return entries


def repository(cfg, name, *, serving=True):
    candidates = [name] if name.endswith(".git") else [name + ".git", name]
    for candidate in candidates:
        path = safe_path(cfg["repos"], candidate)
        if path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir():
            if serving:
                policy = cfg.get("repository_policy", {})
                projects = policy.get("project_list")
                if projects is not None:
                    entries = project_entries(projects)
                    if not any(candidate == p.rstrip("/") or candidate.startswith(p.rstrip("/") + "/")
                               for p in entries if p):
                        raise RequestError(404, "Not found")
                marker = policy.get("strict_export")
                if ((path / "noweb").exists() or (marker and not (path / marker).exists())
                        or repo_ignored(path)):
                    raise RequestError(404, "Not found")
            return path, candidate
    raise RequestError(404, "Repository not found")


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}", "--git-dir", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check,
    )


def object_path(cfg, repo_name, oid):
    if not isinstance(oid, str) or not OID.fullmatch(oid):
        raise RequestError(422, "Invalid SHA-256 object ID")
    return safe_path(cfg["lfs"], f"{repo_name}/objects/{oid[:2]}/{oid[2:4]}/{oid}")


def digest_file(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
