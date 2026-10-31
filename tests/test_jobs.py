"""Maintenance job validation, locking, archive, backup and sync tests."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from common import RequestError
from access import hash_secret, publish_snapshot
import jobs

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV,
                          check=True, capture_output=True)


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-jobs-")
        self.root = Path(self.temp.name).resolve()
        self.repos = self.root / "repos"
        self.lfs = self.root / "lfs"
        self.access = self.root / "access"
        self.backups = self.root / "backups"
        for path in (self.repos, self.lfs, self.access, self.backups):
            path.mkdir()
        self.repo = self.repos / "demo.git"
        git("init", "--bare", "-b", "main", str(self.repo))
        self.work = self.root / "work"
        self.work.mkdir()
        git("init", "-b", "main", str(self.work))
        (self.work / "README.md").write_text("# Jobs fixture\n")
        git("add", ".", cwd=self.work)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "Initial", cwd=self.work)
        git("push", str(self.repo), "HEAD", cwd=self.work)
        self.ctx = SimpleNamespace(
            root={"repos": self.repos, "lfs": self.lfs},
            access=self.access,
            backups=self.backups,
            state=self.root / "state",
            work=self.root / "job-work",
            runtime_config=self.root / "settings.json",
        )
        self.ctx.runtime_config.write_text(json.dumps({
            "repos": str(self.repos),
            "lfs": str(self.lfs),
            "auth_mode": "public",
            "username": "",
            "password_file": "",
            "http_push": False,
            "ssh_push": False,
            "cgit_config": str(self.root / "cgitrc"),
        }))

    def tearDown(self):
        self.temp.cleanup()

    def job(self, kind="check", **values):
        job = {"id": "fixture", "type": kind, "cron": "0 3 * * *", "enabled": True,
               "timeout": 30}
        job.update(values)
        return job

    def publish_access(self):
        compiled = self.access / ".gitolite/conf/gitolite.conf-compiled.pm"
        compiled.parent.mkdir(parents=True)
        compiled.write_text("%repos = ();\n")
        (self.repo / "gl-conf").write_text("%repos = ();\n")
        (self.access / "maintenance").write_text("fixture\n")
        publish_snapshot(self.access, self.repos, {
            "owner": {
                "enabled": True,
                "owner": True,
                "secret_hash": hash_secret(b"restore-test-credential"),
            },
        })
        (self.access / "maintenance").unlink()
        (self.access / "ssh").mkdir()
        (self.access / "ssh/authorized_keys").write_text(
            "command=\"/usr/local/bin/cgit-ssh owner\" ssh-ed25519 AAAA fixture\n")
        (self.access / "lfs-token.key").write_bytes(b"k" * 32)

    def test_config_crontab_and_status(self):
        config = self.root / "jobs.json"
        config.write_text(json.dumps({
            "schema_version": 1,
            "timezone": "Asia/Shanghai",
            "jobs": [self.job()],
        }))
        loaded = jobs.load_config(config)
        self.assertEqual(loaded["jobs"][0]["id"], "fixture")
        state = jobs.run_job(self.ctx, self.job(git=True, lfs=True))
        self.assertEqual(state["status"], "success")
        state_file = self.ctx.state / "fixture.json"
        self.assertEqual(json.loads(state_file.read_text())["status"], "success")

    def test_archive_is_complete_and_atomic(self):
        result = jobs.run_job(self.ctx, self.job("archive", retain_count=2))
        archive = self.backups / result["summary"]["archive"]
        self.assertTrue(archive.is_file())
        manifest = json.loads(Path(str(archive) + ".manifest.json").read_text())
        self.assertEqual(manifest["sha256"], result["summary"]["sha256"])
        with tarfile.open(archive, "r:gz") as stream:
            names = stream.getnames()
        self.assertIn("repos/demo.git/HEAD", names)
        self.assertIn("lfs", names)
        self.assertFalse(list(self.backups.glob("*.partial")))

    def test_write_fence_waits_for_inflight_writer(self):
        lock = os.open(self.access / "write.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_SH)
        error = []

        def contender():
            try:
                with jobs.write_fence(self.access, "fixture", .3):
                    pass
            except Exception as exc:
                error.append(exc)

        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=2)
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(error[0], jobs.JobError)
        self.assertFalse((self.access / "maintenance").exists())

    def test_restic_backup_command_and_restore_guard(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "restic"
        snapshot_tree = self.root / "restic-snapshot"
        fake.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *\" backup \"*) printf '%s\\n' '{\"message_type\":\"summary\",\"snapshot_id\":\"abcd\"}' ;;\n"
            "  *\" restore \"*)\n"
            "    target=; while [ \"$#\" -gt 0 ]; do if [ \"$1\" = --target ]; then shift; target=\"$1\"; fi; shift; done\n"
            f"    cp -a {shlex.quote(str(snapshot_tree))}/. \"$target/\" ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n")
        fake.chmod(0o755)
        password = self.root / "restic-password"
        password.write_text("synthetic\n")
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        try:
            result = jobs.run_job(self.ctx, self.job(
                "backup", repository=str(self.backups / "restic"), password_file=str(password)))
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(result["summary"]["snapshot"], "abcd")
        self.publish_access()
        snapshot_tree.mkdir()
        for name in ("repos", "lfs", "access"):
            shutil.copytree(self.root / name, snapshot_tree / name)
        (self.backups / "restic").mkdir(exist_ok=True)
        args = SimpleNamespace(
            repository=str(self.backups / "restic"), password_file=password,
            snapshot="latest", destination=self.root / "restore", timeout=30)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        try:
            restored = jobs.restore(args)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(restored["snapshot"], "latest")
        self.assertEqual(restored["verified"], {"repositories": 1})

    def test_sync_sources_and_git_environment_reject_helpers(self):
        sentinel = self.root / "source-helper-ran"
        for source in (
            f"ext::sh -c 'touch {sentinel}'",
            "file:///tmp/repo.git",
            "https://token@example.test/repo.git",
            "ssh://user:password@example.test/repo.git",
            "git@example.test:repo.git",
            "relative/repo.git",
            "https://example.test/repo.git?token=secret",
        ):
            with self.subTest(source=source), self.assertRaises(jobs.JobError):
                jobs.validate_sync_source(source)
        self.assertFalse(sentinel.exists())
        self.assertEqual(jobs.validate_sync_source(str(self.repo)), str(self.repo))
        self.assertEqual(
            jobs.validate_sync_source("ssh://git@example.test/group/demo.git"),
            "ssh://git@example.test/group/demo.git",
        )
        os.environ["GIT_ALLOW_PROTOCOL"] = "ext"
        os.environ["GIT_CONFIG_COUNT"] = "1"
        try:
            environment = jobs.git_environment()
        finally:
            os.environ.pop("GIT_ALLOW_PROTOCOL")
            os.environ.pop("GIT_CONFIG_COUNT")
        self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "file:https:ssh")
        self.assertNotIn("GIT_CONFIG_COUNT", environment)
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_restore_verification_rejects_policy_and_repository_drift(self):
        self.publish_access()
        restored = self.root / "verified-restore"
        restored.mkdir()
        for name in ("repos", "lfs", "access"):
            shutil.copytree(self.root / name, restored / name)
        self.assertEqual(
            jobs.verify_restored_tree(restored, 30, time.monotonic() + 30),
            {"repositories": 1},
        )

        identity = restored / "access/identity.json"
        state = json.loads(identity.read_text())
        state["generation"] = "0" * 64
        identity.write_text(json.dumps(state))
        with self.assertRaises(jobs.JobError):
            jobs.verify_restored_tree(restored, 30, time.monotonic() + 30)

        identity.write_text(json.dumps(json.loads((self.access / "identity.json").read_text())))
        (restored / "repos/demo.git/HEAD").unlink()
        with self.assertRaises(jobs.JobError):
            jobs.verify_restored_tree(restored, 30, time.monotonic() + 30)

    def test_sync_in_fast_forward_and_rejects_history_rewrite(self):
        source = self.root / "source.git"
        git("init", "--bare", "-b", "main", str(source))
        git("remote", "add", "sync", str(source), cwd=self.work)
        git("push", "sync", "HEAD", cwd=self.work)
        sync_job = self.job("sync-in", source=str(source), repository="demo",
                            lfs=False, allow_rewrite=False, allow_delete=False)
        result = jobs.run_job(self.ctx, sync_job)
        self.assertEqual(result["summary"]["refs_updated"], 1)
        local = git("--git-dir", str(self.repo), "rev-parse", "refs/heads/main").stdout.strip()
        remote = git("--git-dir", str(source), "rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(local, remote)

        divergent = self.root / "divergent"
        git("clone", str(self.repo), str(divergent))
        (divergent / "divergent.txt").write_text("divergent\n")
        git("add", ".", cwd=divergent)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "Divergent", cwd=divergent)
        git("push", "origin", "HEAD", cwd=divergent)

        (self.work / "next.txt").write_text("next\n")
        git("add", ".", cwd=self.work)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "Next", cwd=self.work)
        git("push", "sync", "HEAD", cwd=self.work)
        with self.assertRaises(jobs.JobError):
            jobs.run_job(self.ctx, sync_job)

    def test_sync_with_real_lfs_client(self):
        payload = b"real LFS sync bytes"
        oid = hashlib.sha256(payload).hexdigest()
        (self.work / "asset.bin").write_text(
            f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(payload)}\n")
        git("add", "asset.bin", cwd=self.work)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "LFS pointer", cwd=self.work)
        source = self.root / "real-lfs.git"
        git("clone", "--bare", str(self.work), str(source))
        obj = source / "lfs/objects" / oid[:2] / oid[2:4] / oid
        obj.parent.mkdir(parents=True)
        obj.write_bytes(payload)
        result = jobs.run_job(self.ctx, self.job(
            "sync-in", source=str(source), repository="demo", lfs=True))
        self.assertEqual(result["summary"]["lfs_objects"], 1)
        self.assertEqual((self.lfs / "demo/objects" / oid[:2] / oid[2:4] / oid).read_bytes(), payload)

    def test_sync_rejects_committed_lfsconfig_without_checkout(self):
        (self.work / ".lfsconfig").write_text("[lfs]\nurl = http://127.0.0.1:1/untrusted\n")
        git("add", ".lfsconfig", cwd=self.work)
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "commit", "-m", "Untrusted LFS endpoint", cwd=self.work)
        source = self.root / "untrusted.git"
        git("clone", "--bare", str(self.work), str(source))
        old = git("--git-dir", str(self.repo), "rev-parse", "HEAD").stdout
        with self.assertRaisesRegex(jobs.JobError, "lfsconfig"):
            jobs.run_job(self.ctx, self.job(
                "sync-in", source=str(source), repository="demo", lfs=True))
        self.assertEqual(git("--git-dir", str(self.repo), "rev-parse", "HEAD").stdout, old)

    def test_sync_rejects_symlink_parent(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.copytree(self.repo, outside / "demo.git")
        (self.repos / "group").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((jobs.JobError, RequestError)):
            jobs.run_job(self.ctx, self.job(
                "sync-in", source=str(self.repo), repository="group/demo", lfs=False))

    def test_sync_in_copies_verified_lfs_objects_before_refs(self):
        source = self.root / "lfs-source.git"
        git("init", "--bare", "-b", "main", str(source))
        git("remote", "add", "lfs-sync", str(source), cwd=self.work)
        git("push", "lfs-sync", "HEAD", cwd=self.work)
        payload = b"synthetic remote LFS object"
        oid = hashlib.sha256(payload).hexdigest()
        stored = self.lfs / "demo/objects" / oid[:2] / oid[2:4] / oid
        stored.parent.mkdir(parents=True)
        stored.write_bytes(b"x" * len(payload))
        real_git = shutil.which("git")
        bin_dir = self.root / "lfs-bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = -C ] && [ \"$3\" = lfs ]; then\n"
            "  dir=\"$2\"\n"
            "  case \"$4\" in\n"
            "    fetch)\n"
            f"      obj=\"$dir/.git/lfs/objects/{oid[:2]}/{oid[2:4]}/{oid}\"\n"
            "      mkdir -p \"$(dirname \"$obj\")\"\n"
            f"      printf '%s' {shlex.quote(payload.decode())} > \"$obj\"\n"
            "      ;;\n"
            "    ls-files)\n"
            f"      printf '%s\\n' '{{\"files\":[{{\"oid\":\"{oid}\",\"size\":{len(payload)}}}]}}'\n"
            "      ;;\n"
            "    *) exit 1 ;;\n"
            "  esac\n"
            "  exit 0\n"
            "fi\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n")
        wrapper.chmod(0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        try:
            result = jobs.run_job(self.ctx, self.job(
                "sync-in", source=str(source), repository="demo",
                lfs=True, allow_rewrite=False, allow_delete=False))
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(result["summary"]["lfs_objects"], 1)
        stored = self.lfs / "demo/objects" / oid[:2] / oid[2:4] / oid
        self.assertEqual(stored.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
