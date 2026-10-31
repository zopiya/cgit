"""Forced SSH command and SSH-to-LFS authentication tests."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from access import hash_secret, publish_snapshot
from lfs_token import ensure_secret, verify

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


class SshCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-ssh-command-")
        self.root = Path(self.temp.name).resolve()
        self.repos = self.root / "repos"
        self.lfs = self.root / "lfs"
        self.access = self.root / "access"
        for path in (self.repos / "group/demo.git/objects", self.lfs,
                     self.access / ".gitolite/conf", self.root / "bindir/lib"):
            path.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", "-b", "main",
                        str(self.repos / "group/demo.git")], check=True, capture_output=True,
                       env=GIT_ENV)
        work = self.root / "work"
        subprocess.run(["git", "init", "-b", "main", str(work)], check=True,
                       capture_output=True, env=GIT_ENV)
        (work / "README.md").write_text("# SSH command fixture\n")
        subprocess.run(["git", "-C", str(work), "add", "."], check=True,
                       capture_output=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(work), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@localhost", "commit", "-m", "Initial"],
                       check=True, capture_output=True, env=GIT_ENV)
        subprocess.run(["git", "-C", str(work), "push",
                        str(self.repos / "group/demo.git"), "HEAD"], check=True,
                       capture_output=True, env=GIT_ENV)
        (self.repos / "group/demo.git/gl-conf").write_text("%repos = ();\n")
        compiled = self.access / ".gitolite/conf/gitolite.conf-compiled.pm"
        compiled.write_text("%repos = ();\n")
        self.gitolite = self.root / "gitolite"
        self.gitolite.write_text(
            "#!/bin/sh\n"
            f"test \"$HOME\" = {shlex.quote(str(self.access))} || exit 9\n"
            "if [ \"$1\" = query-rc ]; then\n"
            f"  if [ \"$2\" = GL_BINDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'bindir'))}; exit 0; fi\n"
            f"  if [ \"$2\" = GL_LIBDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'bindir/lib'))}; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "if [ \"$3\" = group/demo ] && [ \"$5\" = R ]; then exit 0; fi\n"
            "exit 1\n")
        self.gitolite.chmod(0o755)
        users = {
            "owner": {"enabled": True, "owner": True,
                      "secret_hash": hash_secret(b"owner-ssh-command-secret")},
            "reader": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"reader-ssh-command-secret")},
        }
        (self.access / "maintenance").write_text("test\n")
        publish_snapshot(self.access, self.repos, users)
        ensure_secret(self.access)
        (self.access / "maintenance").unlink()
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({
            "repos": str(self.repos),
            "lfs": str(self.lfs),
            "base_url": "https://git.example.test",
            "auth_mode": "access",
            "http_push": True,
            "ssh_push": True,
            "access": {"root": str(self.access), "gitolite": str(self.gitolite)},
        }))
        self.env = {
            **GIT_ENV,
            "CGIT_RUNTIME_CONFIG": str(self.settings),
        }

    def tearDown(self):
        self.temp.cleanup()

    def command(self, user, value, data=b""):
        return subprocess.run(
            [sys.executable, str(ROOT / "runtime/ssh_command.py"), user],
            input=data,
            env={**self.env, "SSH_ORIGINAL_COMMAND": value},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )

    def test_lfs_authentication_returns_scoped_bearer(self):
        result = self.command("reader", "git-lfs-authenticate group/demo.git download")
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["href"], "https://git.example.test/group/demo.git/info/lfs")
        self.assertGreater(response["expires_in"], 0)
        token = response["header"]["Authorization"].removeprefix("Bearer ")
        generation = json.loads((self.access / "identity.json").read_text())["generation"]
        self.assertEqual(verify(
            {"access": {"root": str(self.access)}},
            token, "group/demo", "download", generation), "reader")
        denied = self.command("reader", "git-lfs-authenticate group/demo.git upload")
        self.assertNotEqual(denied.returncode, 0)

    def test_git_upload_pack_and_shell_denial(self):
        allowed = self.command("reader", "git-upload-pack 'group/demo.git'", b"0000")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertIn(b"refs/heads/main", allowed.stdout)
        denied = self.command("reader", "git-upload-pack '/other.git'")
        self.assertNotEqual(denied.returncode, 0)
        shell = self.command("reader", "sh -c id")
        self.assertNotEqual(shell.returncode, 0)
        self.assertIn(b"unsupported command", shell.stderr)


if __name__ == "__main__":
    unittest.main()
