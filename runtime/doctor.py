#!/usr/bin/env python3
"""Read-only diagnosis; optional complete Git/LFS integrity checks."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from common import RequestError, config, digest_file, git, object_path, pointer, repository


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", action="store_true", help="run git fsck --full for all repositories")
    parser.add_argument("--lfs", action="store_true", help="check reachable LFS pointers and hash stored objects")
    args = parser.parse_args()
    cfg = config()
    findings = []
    repos = []
    for label in ("repos", "lfs"):
        if not Path(cfg[label]).is_dir() or not os.access(cfg[label], os.R_OK | os.X_OK):
            findings.append(f"{label}: directory missing or unreadable")
    if cfg["password_file"] and not os.access(cfg["password_file"], os.R_OK):
        findings.append("password file unreadable by runtime user")
    if cfg["username"] and not os.access(cfg["lfs"], os.W_OK):
        findings.append("LFS store is not writable")
    # Stop descending once a bare repo is found; avoid following symlinks.
    for directory, dirs, files in os.walk(cfg["repos"], followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(directory) / d).is_symlink()]
        if "HEAD" not in files or "objects" not in dirs:
            continue
        dirs[:] = []
        name = str(Path(directory).relative_to(cfg["repos"]))
        try:
            repo, name = repository(cfg, name)
        except RequestError:
            continue
        repos.append(name)
        if cfg["http_push"] and not os.access(repo, os.W_OK):
            findings.append(f"{name}: HTTP push enabled but repository is not writable")
        if args.git:
            result = git(repo, "fsck", "--full", check=False)
            if result.returncode:
                findings.append(f"{name}: git fsck failed: {result.stderr.decode(errors='replace').strip()}")
        if args.lfs:
            listed = git(repo, "rev-list", "--objects", "--all").stdout
            # cat-file interprets each complete line as a revision unless %(rest)
            # is requested; rev-list also includes paths, so pass just the OIDs.
            objects = b"\n".join(line.split(b" ", 1)[0] for line in listed.splitlines()) + b"\n"
            result = subprocess.run(
                ["git", "-c", f"safe.directory={repo}", "--git-dir", str(repo), "cat-file",
                 "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                input=objects if listed else b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            )
            seen = set()
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) != 3 or parts[1] != b"blob" or int(parts[2]) > 1024:
                    continue
                info = pointer(git(repo, "cat-file", "blob", parts[0].decode()).stdout)
                if info and info not in seen:
                    seen.add(info)
                    oid, size = info
                    target = object_path(cfg, name, oid)
                    if not target.is_file() or target.stat().st_size != size:
                        findings.append(f"{name}: missing or wrong size LFS object {oid}")
            root = Path(cfg["lfs"]) / name / "objects"
            for target in root.glob("*/*/*"):
                if target.name.startswith(".upload-"):
                    findings.append(f"{name}: interrupted upload {target.relative_to(root)} (inspect before removal)")
                    continue
                try:
                    checked = object_path(cfg, name, target.name)
                    if checked != target or not target.is_file() or digest_file(target) != target.name:
                        findings.append(f"{name}: corrupt or misplaced LFS object {target.name}")
                except RequestError:
                    findings.append(f"{name}: invalid LFS object path {target.name}")
    print(json.dumps({"ok": not findings, "repositories": repos, "findings": findings,
                      "checks": {"git": args.git, "lfs": args.lfs}}, ensure_ascii=False, indent=2))
    return bool(findings)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RequestError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "findings": [f"Check failed: {exc}"]}, ensure_ascii=False))
        sys.exit(1)
