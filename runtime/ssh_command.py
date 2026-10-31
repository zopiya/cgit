#!/usr/bin/env python3
"""Forced SSH command for Git and Git LFS authentication."""

import json
import os
import shlex
import subprocess
import sys
import time
from urllib.parse import quote

from access import clean_environment, session as access_session
from common import RequestError, config, repository
import lfs_token


def fail(message):
    print(message, file=sys.stderr)
    return 1


def git_environment(cfg, authorization, name, username):
    root = str(authorization.root)
    bindir, libdir = authorization.gitolite_directories()
    logical = name[:-4] if name.endswith(".git") else name
    env = clean_environment(authorization.root)
    env.update(HOME=root, GITOLITE_HTTP_HOME=root,
               GL_BINDIR=str(bindir), GL_LIBDIR=str(libdir),
               GL_REPO=logical, GL_USER=username,
               PATH=f"{bindir}:{env['PATH']}")
    if os.environ.get("GIT_PROTOCOL", "") in ("version=0", "version=1", "version=2"):
        env["GIT_PROTOCOL"] = os.environ["GIT_PROTOCOL"]
    return env


def main():
    if len(sys.argv) != 2:
        return fail("cgit SSH: invalid forced command identity")
    username = sys.argv[1]
    original = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if not original or len(original) > 4096 or "\n" in original or "\r" in original:
        return fail("cgit SSH: interactive shell is disabled")
    try:
        arguments = shlex.split(original, posix=True)
    except ValueError:
        return fail("cgit SSH: malformed command")
    if not arguments or arguments[0] not in (
            "git-upload-pack", "git-receive-pack", "git-lfs-authenticate"):
        return fail("cgit SSH: unsupported command")
    cfg = config()
    if cfg.get("auth_mode") != "access":
        return fail("cgit SSH: access mode is not configured")
    authorization = access_session(cfg)
    action = arguments[0]
    if action in ("git-upload-pack", "git-receive-pack"):
        if len(arguments) != 2:
            return fail("cgit SSH: unsupported Git command")
        requested = arguments[1].removeprefix("/")
        try:
            repo, name = repository(cfg, requested)
            operation = "R" if action == "git-upload-pack" else "W"
            if operation == "W" and not cfg.get("ssh_push"):
                return fail("cgit SSH: Git writes are disabled")
            authorization.require(name, username, operation)
            env = git_environment(cfg, authorization, name, username)
            binary = action
            os.execvpe(binary, [binary, str(repo)], env)
        except (RequestError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return fail(f"cgit SSH: {exc}")

    if action == "git-lfs-authenticate":
        if len(arguments) != 3 or arguments[2] not in ("download", "upload"):
            return fail("cgit SSH: invalid git-lfs-authenticate request")
        if not cfg.get("base_url"):
            return fail("cgit SSH: CGIT_BASE_URL is required for LFS authentication")
        try:
            _, name = repository(cfg, arguments[1].removeprefix("/"))
            logical = name[:-4] if name.endswith(".git") else name
            authorization.require(name, username, "R" if arguments[2] == "download" else "W")
            token, payload = lfs_token.issue(
                cfg, username, logical, arguments[2], authorization.state["generation"])
            result = {
                "href": cfg["base_url"] + "/" + quote(logical, safe="/") + ".git/info/lfs",
                "header": {"Authorization": "Bearer " + token},
                "expires_in": payload["exp"] - int(time.time()),
            }
            print(json.dumps(result, separators=(",", ":")))
            return 0
        except (RequestError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return fail(f"cgit SSH: {exc}")
    return fail("cgit SSH: unsupported command")


if __name__ == "__main__":
    sys.exit(main())
