"""Fail-closed tests for the Gitolite ACL adapter."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from access import (
    Access,
    apply_access,
    hash_secret,
    key_fingerprint,
    publish_snapshot,
    render_authorized_keys,
    validate_users,
)
from common import RequestError, digest_file


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-access-")
        self.root = Path(self.temp.name).resolve()
        self.repos = self.root / "repos"
        self.compiled = self.root / ".gitolite/conf/gitolite.conf-compiled.pm"
        self.compiled.parent.mkdir(parents=True)
        self.compiled.write_text("%repos = ();\n")
        self.repos.mkdir()
        self.repo = self.repos / "private.git"
        for path in (self.repo / "objects",):
            path.mkdir(parents=True)
        (self.repo / "HEAD").write_text("ref: refs/heads/main\n")
        (self.repo / "gl-conf").write_text("%repos = ();\n")
        self.binary = self.root / "gitolite"
        (self.root / "bindir/lib").mkdir(parents=True)
        self.binary.write_text(
            "#!/bin/sh\n"
            f"test \"$HOME\" = {shlex.quote(str(self.root))} || exit 9\n"
            "if [ \"$1\" = compile ]; then exit 0; fi\n"
            "if [ \"$1\" = query-rc ]; then\n"
            f"  if [ \"$2\" = GL_BINDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'bindir'))}; exit 0; fi\n"
            f"  if [ \"$2\" = GL_LIBDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'bindir/lib'))}; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "if [ \"$2\" = \"%\" ]; then\n"
            "  while IFS= read -r repo; do\n"
            "    if [ \"$repo\" = private ] && [ \"$3\" = reader ] && [ \"$4\" = R ]; then\n"
            "      printf 'private\\t%s\\trepo R reader\\n' \"$3\"\n"
            "    else\n"
            "      printf 'private\\t%s\\tR any private %s DENIED by fallthru\\n' \"$3\" \"$3\"\n"
            "    fi\n"
            "  done\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$3\" = private ] && [ \"$4\" = reader ] && [ \"$5\" = R ]; then exit 0; fi\n"
            "if [ \"$3\" = private ] && [ \"$4\" = writer ] && [ \"$5\" = W ]; then exit 0; fi\n"
            "exit 1\n")
        self.binary.chmod(0o755)
        self.users = {
            "owner": {"enabled": True, "owner": True,
                      "secret_hash": hash_secret(b"owner-test-credential")},
            "reader": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"reader-test-credential")},
            "writer": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"writer-test-credential")},
            "disabled": {"enabled": False, "owner": False,
                         "secret_hash": hash_secret(b"disabled-test-credential")},
        }
        (self.root / "maintenance").write_text("test\n")
        self.generation = publish_snapshot(self.root, self.repos, self.users)
        (self.root / "maintenance").unlink()
        self.cfg = {
            "repos": str(self.repos),
            "access": {"root": str(self.root), "gitolite": str(self.binary)},
        }

    def tearDown(self):
        self.temp.cleanup()

    def rewrite_state(self, **changes):
        state = json.loads((self.root / "identity.json").read_text())
        state.update(changes)
        from access import canonical
        unsigned = {key: value for key, value in state.items() if key != "generation"}
        import hashlib
        state["generation"] = hashlib.sha256(canonical(unsigned)).hexdigest()
        (self.root / "identity.json").write_text(json.dumps(state) + "\n")

    def test_published_snapshot_queries_gitolite_with_contained_home(self):
        authorization = Access(self.cfg)
        self.assertEqual(len(self.generation), 64)
        self.assertTrue(authorization.allowed("private.git", "reader", "R"))
        self.assertFalse(authorization.allowed("private.git", "reader", "W"))
        self.assertTrue(authorization.allowed("private", "writer", "W"))
        self.assertFalse(authorization.allowed("private.git", "disabled", "R"))
        self.assertFalse(authorization.allowed("private.git", "unknown", "R"))
        self.assertFalse(authorization.allowed("private.git", None, "W"))

    def test_authentication_is_hashed_and_rejects_disabled_identities(self):
        authorization = Access(self.cfg)
        self.assertTrue(authorization.authenticate("reader", b"reader-test-credential"))
        self.assertFalse(authorization.authenticate("reader", b"wrong-test-credential"))
        self.assertFalse(authorization.authenticate("disabled", b"disabled-test-credential"))
        self.assertFalse(authorization.authenticate("unknown", b"unknown-test-credential"))

    def test_batch_readability_uses_one_query_and_rejects_unknown_scope(self):
        authorization = Access(self.cfg)
        self.assertEqual(authorization.readable("reader"), {"private"})
        self.assertEqual(authorization.readable("writer"), set())
        self.assertEqual(authorization.readable(None), set())
        with self.assertRaises(RequestError) as unknown:
            authorization.readable("reader", ["other"])
        self.assertEqual(unknown.exception.status, 503)
        self.assertEqual(
            tuple(map(str, authorization.gitolite_directories())),
            (str(self.root / "bindir"), str(self.root / "bindir/lib")),
        )

    def test_require_does_not_disclose_private_repository(self):
        authorization = Access(self.cfg)
        with self.assertRaises(RequestError) as anonymous:
            authorization.require("private", None, "R")
        self.assertEqual(anonymous.exception.status, 401)
        with self.assertRaises(RequestError) as denied:
            authorization.require("private", "reader", "W")
        self.assertEqual(denied.exception.status, 404)

    def test_maintenance_and_policy_tampering_fail_closed(self):
        authorization = Access(self.cfg)
        self.assertTrue(authorization.allowed("private", "reader", "R"))
        original = (self.repo / "gl-conf").read_bytes()
        (self.repo / "gl-conf").write_bytes(original + b"tampered\n")
        with self.assertRaises(RequestError) as tampered:
            authorization.allowed("private", "reader", "R")
        self.assertEqual(tampered.exception.status, 503)
        (self.repo / "gl-conf").write_bytes(original)
        (self.root / "maintenance").touch()
        with self.assertRaises(RequestError) as maintenance:
            Access(self.cfg)
        self.assertEqual(maintenance.exception.status, 503)

    def test_snapshot_requires_maintenance_and_enabled_owner(self):
        state = json.loads((self.root / "identity.json").read_text())
        state["users"]["owner"]["enabled"] = False
        self.rewrite_state(users=state["users"])
        with self.assertRaises(RequestError) as invalid:
            Access(self.cfg)
        self.assertEqual(invalid.exception.status, 503)
        before = digest_file(self.compiled)
        with self.assertRaises(ValueError):
            publish_snapshot(self.root, self.repos, self.users)
        self.assertEqual(digest_file(self.compiled), before)
        with self.assertRaises(ValueError):
            publish_snapshot(Path("relative-access"), self.repos, self.users)

    def test_unknown_compiled_policy_change_is_detected(self):
        authorization = Access(self.cfg)
        self.assertTrue(authorization.allowed("private", "reader", "R"))
        with self.compiled.open("ab") as stream:
            stream.write(b"# changed\n")
        with self.assertRaises(RequestError) as changed:
            authorization.allowed("private", "reader", "R")
        self.assertEqual(changed.exception.status, 503)

    def test_access_root_must_be_absolute(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["access"]["root"] = "relative/access"
        with self.assertRaises(RequestError) as relative:
            Access(cfg)
        self.assertEqual(relative.exception.status, 503)

    def test_identity_validation_rejects_unknown_permission_fields(self):
        users = json.loads(json.dumps(self.users))
        users["reader"]["repositories"] = {"private": "R"}
        with self.assertRaises(ValueError):
            validate_users(users)

    def test_authorized_keys_require_registered_owner_keys(self):
        keydir = self.root / ".gitolite/keydir"
        keydir.mkdir()
        private_key = self.root / "owner-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
                       check=True)
        public = keydir / "owner@laptop.pub"
        public.write_bytes(private_key.with_suffix(".pub").read_bytes())
        users = json.loads(json.dumps(self.users))
        users["owner"]["keys"] = [key_fingerprint(public)]
        (self.root / "maintenance").touch()
        self.assertEqual(render_authorized_keys(
            self.root, "/usr/local/bin/cgit-ssh", users), 1)
        authorized = self.root / "ssh/authorized_keys"
        self.assertEqual(authorized.stat().st_mode & 0o777, 0o600)
        text = authorized.read_text()
        self.assertIn('command="/usr/local/bin/cgit-ssh owner",no-port-forwarding', text)
        self.assertIn("ssh-ed25519 ", text)

        users["owner"]["keys"] = ["SHA256:not-the-key"]
        with self.assertRaises(ValueError):
            render_authorized_keys(self.root, "/usr/local/bin/cgit-ssh", users)

    def test_apply_publishes_keys_tokens_and_removes_maintenance(self):
        keydir = self.root / ".gitolite/keydir"
        keydir.mkdir()
        private_key = self.root / "owner-key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key)],
                       check=True)
        public = keydir / "owner@laptop.pub"
        public.write_bytes(private_key.with_suffix(".pub").read_bytes())
        users = json.loads(json.dumps(self.users))
        users["owner"]["keys"] = [key_fingerprint(public)]
        generation = apply_access(
            self.root, self.repos, users, self.binary, "/usr/local/bin/cgit-ssh")
        self.assertEqual(len(generation), 64)
        self.assertFalse((self.root / "maintenance").exists())
        self.assertTrue((self.root / "ssh/authorized_keys").is_file())
        self.assertTrue((self.root / "lfs-token.key").is_file())
        self.assertTrue(Access(self.cfg).allowed("private", "reader", "R"))


if __name__ == "__main__":
    unittest.main()
