"""Black-box maintenance CLI, task fencing and isolated restore contracts."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tarfile
import tempfile
import unittest

from contract_support import ENV, run

ROOT = Path(__file__).resolve().parents[2]
JOBS = ROOT / "runtime/jobs.py"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class JobsCliContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-jobs-contract-")
        self.root = Path(self.temp.name).resolve()
        self.repos = self.root / "repos"
        self.lfs = self.root / "lfs"
        self.access = self.root / "access"
        self.backups = self.root / "backups"
        for path in (self.repos, self.lfs, self.access, self.backups):
            path.mkdir()
        self.repo = self.repos / "demo.git"
        self.work = self.root / "work"
        run("git", "init", "--bare", "-b", "main", str(self.repo))
        run("git", "init", "-b", "main", str(self.work))
        (self.work / "README.md").write_text("# Jobs contract\n")
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Initial", cwd=self.work)
        run("git", "push", str(self.repo), "HEAD", cwd=self.work)
        self.config = self.root / "jobs.json"
        self.state = self.root / "state"
        self.job_work = self.root / "job-work"

    def tearDown(self):
        self.temp.cleanup()

    def write_jobs(self, *jobs):
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "timezone": "Asia/Shanghai",
            "jobs": list(jobs),
        }))

    def job(self, job_id, kind, **values):
        value = {
            "id": job_id,
            "type": kind,
            "cron": "0 3 * * *",
            "enabled": True,
            "timeout": 30,
        }
        value.update(values)
        return value

    def cli(self, command, *args, env=None, check=True):
        base = [
            sys.executable, str(JOBS),
            "--config", str(self.config),
            "--state", str(self.state),
            "--work", str(self.job_work),
            "--repos", str(self.repos),
            "--lfs", str(self.lfs),
            "--access", str(self.access),
            "--backups", str(self.backups),
        ]
        return run(*base, command, *args, cwd=self.root, env=env or ENV, check=check)

    def write_access(self):
        compiled = self.access / ".gitolite/conf/gitolite.conf-compiled.pm"
        compiled.parent.mkdir(parents=True)
        compiled.write_text("%repos = ();\n")
        (self.repo / "gl-conf").write_text("%repos = ();\n")
        manifest = {"demo": hashlib.sha256((self.repo / "gl-conf").read_bytes()).hexdigest()}
        state = {
            "schema_version": 1,
            "users": {"owner": {"enabled": True, "owner": True}},
            "common_digest": hashlib.sha256(compiled.read_bytes()).hexdigest(),
            "repositories": manifest,
        }
        state["generation"] = hashlib.sha256(canonical(state)).hexdigest()
        (self.access / "identity.json").write_text(json.dumps(state))
        (self.access / "ssh").mkdir()
        (self.access / "ssh/authorized_keys").write_text(
            "command=\"/usr/local/bin/cgit-ssh owner\" ssh-ed25519 AAAA fixture\n")
        (self.access / "lfs-token.key").write_bytes(b"k" * 32)

    def snapshot(self, name):
        destination = self.root / name
        destination.mkdir()
        for directory in ("repos", "lfs", "access"):
            shutil.copytree(self.root / directory, destination / directory)
        return destination

    def fake_restic(self, snapshot):
        bin_dir = self.root / f"bin-{snapshot.name}"
        bin_dir.mkdir()
        binary = bin_dir / "restic"
        binary.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *\" backup \"*) printf '%s\\n' '{\"message_type\":\"summary\",\"snapshot_id\":\"fixture\"}' ;;\n"
            "  *\" restore \"*)\n"
            "    target=; while [ \"$#\" -gt 0 ]; do if [ \"$1\" = --target ]; then shift; target=\"$1\"; fi; shift; done\n"
            f"    cp -a {shlex.quote(str(snapshot))}/. \"$target/\" ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n")
        binary.chmod(0o755)
        return bin_dir

    def test_cli_inventory_archive_and_backup_are_observable(self):
        password = self.root / "restic-password"
        password.write_text("synthetic\n")
        self.write_jobs(
            self.job("check", "check", git=True, lfs=True),
            self.job("archive", "archive", retain_count=2),
            self.job(
                "backup", "backup",
                repository=str(self.backups / "restic"),
                password_file=str(password),
            ),
        )
        validated = json.loads(self.cli("validate").stdout)
        self.assertEqual(validated, {"ok": True, "jobs": 3})
        status = json.loads(self.cli("status").stdout)
        self.assertEqual(status["jobs"], {"check": None, "archive": None, "backup": None})
        crontab = self.root / "crontab"
        scheduled = json.loads(self.cli("crontab", "--output", str(crontab)).stdout)
        self.assertEqual(scheduled["scheduled"], 3)
        self.assertIn("run --id archive", crontab.read_text())

        result = json.loads(self.cli("run", "--id", "archive").stdout)
        archive = self.backups / result["summary"]["archive"]
        self.assertEqual(result["status"], "success")
        self.assertTrue(archive.is_file())
        with tarfile.open(archive, "r:gz") as stream:
            self.assertIn("repos/demo.git/HEAD", stream.getnames())
        bin_dir = self.fake_restic(self.root / "unused-snapshot")
        env = {**ENV, "PATH": str(bin_dir) + os.pathsep + ENV["PATH"]}
        backup = json.loads(self.cli("run", "--id", "backup", env=env).stdout)
        self.assertEqual(backup["summary"]["snapshot"], "fixture")

    def test_sync_in_is_real_and_rejects_remote_helper_injection(self):
        source = self.root / "source.git"
        source_work = self.root / "source-work"
        run("git", "clone", str(self.repo), str(source_work))
        run("git", "init", "--bare", "-b", "main", str(source))
        (source_work / "synced.txt").write_text("verified source\n")
        run("git", "add", ".", cwd=source_work)
        run("git", "commit", "-m", "Synced", cwd=source_work)
        run("git", "push", str(source), "HEAD", cwd=source_work)
        sync = self.job(
            "sync", "sync-in", source=str(source), repository="demo",
            lfs=False, allow_rewrite=False, allow_delete=False,
        )
        self.write_jobs(sync)
        result = json.loads(self.cli("run", "--id", "sync").stdout)
        self.assertEqual(result["summary"]["refs_updated"], 1)
        local = run("git", "--git-dir", str(self.repo),
                    "rev-parse", "refs/heads/main").stdout.strip()
        remote = run("git", "--git-dir", str(source),
                     "rev-parse", "refs/heads/main").stdout.strip()
        self.assertEqual(local, remote)

        marker = self.root / "helper-ran"
        sync["source"] = f"ext::sh -c 'touch {marker}'"
        self.write_jobs(sync)
        rejected = self.cli("run", "--id", "sync", check=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"Sync source must be", rejected.stderr)
        self.assertFalse(marker.exists())
        states = json.loads(self.cli("status").stdout)
        self.assertEqual(states["jobs"]["sync"]["status"], "failed")

    def test_restore_requires_complete_verified_data(self):
        self.write_access()
        valid = self.snapshot("valid-snapshot")
        self.write_jobs(self.job("check", "check", git=True, lfs=True))
        password = self.root / "restic-password"
        password.write_text("synthetic\n")
        bin_dir = self.fake_restic(valid)
        env = {**ENV, "PATH": str(bin_dir) + os.pathsep + ENV["PATH"]}
        destination = self.root / "restored"
        restored = json.loads(self.cli(
            "restore",
            "--repository", str(self.backups / "restic"),
            "--password-file", str(password),
            "--snapshot", "latest",
            "--destination", str(destination),
            env=env,
        ).stdout)
        self.assertEqual(restored["verified"], {"repositories": 1})

        broken = self.snapshot("broken-snapshot")
        identity = broken / "access/identity.json"
        state = json.loads(identity.read_text())
        state["generation"] = "0" * 64
        identity.write_text(json.dumps(state))
        broken_bin = self.fake_restic(broken)
        broken_env = {**ENV, "PATH": str(broken_bin) + os.pathsep + ENV["PATH"]}
        rejected = self.cli(
            "restore",
            "--repository", str(self.backups / "restic"),
            "--password-file", str(password),
            "--snapshot", "latest",
            "--destination", str(self.root / "broken-restore"),
            env=broken_env,
            check=False,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(b"fingerprint is invalid", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
