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


def repository(cfg, name):
    candidates = [name] if name.endswith(".git") else [name + ".git", name]
    for candidate in candidates:
        path = safe_path(cfg["repos"], candidate)
        if path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir():
            # cgit.ignore excludes a repository from *all* our serving paths.
            ignored = git(path, "config", "--bool", "--get", "cgit.ignore", check=False)
            if ignored.stdout.strip() == b"true":
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
