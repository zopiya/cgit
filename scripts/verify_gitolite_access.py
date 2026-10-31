#!/usr/bin/env python3
"""Exercise the access adapter against a real, temporary Gitolite v3 installation."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from access import Access, apply_access, hash_secret, key_fingerprint
from common import RequestError
import lfs_token


INITIAL_POLICY = """\
repo public
    R = anonymous reader writer disabled owner
    RW+ = owner

repo private
    R = reader owner
    RW = writer owner
    RW+ = owner
"""

REVOKED_POLICY = """\
repo public
    R = anonymous reader writer disabled owner
    RW+ = owner

repo private
    R = reader owner
    RW+ = owner
"""


def clean_env(home):
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C",
        "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def perl_string(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def compile_policy(binary, home, repos, policy):
    home.mkdir(parents=True, exist_ok=True)
    (home / ".gitolite").mkdir(exist_ok=True)
    (home / ".gitolite/conf").mkdir(exist_ok=True)
    (home / ".gitolite/logs").mkdir(exist_ok=True)
    repos.mkdir(parents=True, exist_ok=True)
    (home / ".gitolite.rc").write_text(
        f"%RC = (GL_REPO_BASE => {perl_string(repos)}, UMASK => 0022);\n")
    (home / ".gitolite/conf/gitolite.conf").write_text(policy)
    result = subprocess.run(
        [str(binary), "compile"],
        env=clean_env(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(
            f"Gitolite compile failed with {result.returncode}: "
            f"{result.stderr or result.stdout}")


def identity_users():
    return {
        "owner": {
            "enabled": True,
            "owner": True,
            "secret_hash": hash_secret(b"owner-test-credential"),
        },
        "reader": {
            "enabled": True,
            "owner": False,
            "secret_hash": hash_secret(b"reader-test-credential"),
        },
        "writer": {
            "enabled": True,
            "owner": False,
            "secret_hash": hash_secret(b"writer-test-credential"),
        },
        "disabled": {
            "enabled": False,
            "owner": False,
            "secret_hash": hash_secret(b"disabled-test-credential"),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitolite", type=Path, required=True,
                        help="Absolute path to Gitolite's executable")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    binary = args.gitolite.resolve()
    if not binary.is_absolute() or not os.access(binary, os.X_OK):
        parser.error("--gitolite must be an executable absolute path")

    checks = []

    def record(name, actual, expected=True):
        checks.append({"name": name, "expected": expected, "actual": actual,
                       "ok": actual == expected})

    def request_error(name, function, status):
        try:
            function()
        except RequestError as exc:
            record(name, exc.status, status)
        else:
            record(name, None, status)

    with tempfile.TemporaryDirectory(prefix="cgit-gitolite-access-") as temp:
        root = Path(temp).resolve()
        home = root / "access"
        repos = root / "repos"
        compile_policy(binary, home, repos, INITIAL_POLICY)
        users = identity_users()
        keydir = home / ".gitolite/keydir"
        keydir.mkdir()
        private_key = root / "owner-key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
            check=True,
        )
        owner_key = keydir / "owner@fixture.pub"
        owner_key.write_bytes(private_key.with_suffix(".pub").read_bytes())
        users["owner"]["keys"] = [key_fingerprint(owner_key)]
        generation = apply_access(
            home, repos, users, binary, "/usr/local/bin/cgit-ssh")
        generation_length = len(generation)
        cfg = {
            "repos": str(repos),
            "access": {"root": str(home), "gitolite": str(binary)},
        }
        authorization = Access(cfg)

        record("compiled_generation", generation_length, 64)
        record("gl_conf_present", (repos / "private.git/gl-conf").is_file())
        record("update_hook_present", (repos / "private.git/hooks/update").is_file())
        authorized = home / "ssh/authorized_keys"
        record("authorized_keys_private", authorized.stat().st_mode & 0o777, 0o600)
        record("authorized_keys_forced",
               'command="/usr/local/bin/cgit-ssh owner",' in authorized.read_text())
        bindir, libdir = authorization.gitolite_directories()
        record("gitolite_bindir", bindir.is_dir())
        record("gitolite_libdir", libdir.is_dir())
        token, _ = lfs_token.issue(
            cfg, "reader", "private", "download", generation)
        record("lfs_token_scope",
               lfs_token.verify(cfg, token, "private", "download", generation), "reader")
        try:
            lfs_token.verify(cfg, token, "public", "download", generation)
        except ValueError:
            record("lfs_token_wrong_repo", True)
        else:
            record("lfs_token_wrong_repo", False)
        record("public_anonymous_R", authorization.allowed("public", None, "R"))
        record("public_anonymous_W", authorization.allowed("public", None, "W"), False)
        record("private_anonymous_R", authorization.allowed("private", None, "R"), False)
        record("private_reader_R", authorization.allowed("private.git", "reader", "R"))
        record("private_reader_W", authorization.allowed("private.git", "reader", "W"), False)
        record("private_writer_R", authorization.allowed("private", "writer", "R"))
        record("private_writer_W", authorization.allowed("private", "writer", "W"))
        record("private_owner_R", authorization.allowed("private", "owner", "R"))
        record("private_owner_W", authorization.allowed("private", "owner", "W"))
        record("private_disabled_R",
               authorization.allowed("private", "disabled", "R"), False)
        record("private_unknown_R",
               authorization.allowed("private", "unknown", "R"), False)
        record("batch_anonymous", sorted(authorization.readable(None)), ["public"])
        record("batch_reader", sorted(authorization.readable("reader")), ["private", "public"])
        record("batch_writer", sorted(authorization.readable("writer")), ["private", "public"])
        record("batch_owner", sorted(authorization.readable("owner")), ["private", "public"])
        record("batch_disabled", sorted(authorization.readable("disabled")), [])
        record("batch_unknown", sorted(authorization.readable("unknown")), [])
        record("reader_secret", authorization.authenticate(
            "reader", b"reader-test-credential"))
        record("reader_wrong_secret", authorization.authenticate(
            "reader", b"wrong-test-credential"), False)
        record("disabled_secret", authorization.authenticate(
            "disabled", b"disabled-test-credential"), False)
        request_error("anonymous_require", lambda: authorization.require(
            "private", None, "R"), 401)
        request_error("unauthorized_require", lambda: authorization.require(
            "private", "reader", "W"), 404)

        compile_policy(binary, home, repos, REVOKED_POLICY)
        apply_access(home, repos, users, binary, "/usr/local/bin/cgit-ssh")
        revoked = Access(cfg)
        record("revoked_writer_W",
               revoked.allowed("private", "writer", "W"), False)
        record("unchanged_reader_R",
               revoked.allowed("private", "reader", "R"))

        gl_conf = repos / "private.git/gl-conf"
        original = gl_conf.read_bytes()
        tampered_instance = Access(cfg)
        record("pre_tamper_query",
               tampered_instance.allowed("private", "reader", "R"))
        gl_conf.write_bytes(original + b"# tampered\n")
        request_error(
            "tampered_repository_policy",
            lambda: tampered_instance.allowed("private", "reader", "R"),
            503,
        )
        gl_conf.write_bytes(original)

        (home / "maintenance").touch()
        request_error("maintenance_blocks_access", lambda: Access(cfg), 503)
        (home / "maintenance").unlink()

    source = binary.parent
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()
    report = {
        "schema_version": 1,
        "ok": all(check["ok"] for check in checks),
        "layer": "native-gitolite-acl-adapter",
        "gitolite": {"executable": str(binary), "commit": commit or None},
        "generation_length": generation_length,
        "checks": checks,
        "production_verified": False,
        "limitations": [
            "Web, Git HTTP, LFS and SSH are not yet routed through this adapter",
            "Branch/ref update hooks and real SSH daemon behavior are not exercised",
            "Linux container and remote CI evidence remain outstanding",
        ],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
