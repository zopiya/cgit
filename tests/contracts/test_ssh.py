"""Real SSH, Git and SSH-authenticated LFS contracts."""

import hashlib
import json
import os
import subprocess
import time
import unittest

from contract_support import ENV, Service, run


@unittest.skipUnless(
    os.environ.get("CGIT_TEST_DRIVER") in ("lighttpd", "docker")
    and (os.environ.get("CGIT_TEST_GITOLITE") or os.environ.get("CGIT_TEST_GITOLITE_IMAGE")),
    "The lighttpd/Docker service plus Gitolite are required for SSH contracts",
)
class SshProtocol(unittest.TestCase):
    def setUp(self):
        self.service = Service(access=True)
        self.addCleanup(self.service.close)
        self.service.start_ssh()

    def ssh_command(self, user):
        return " ".join((
            "ssh",
            "-F", "/dev/null",
            "-i", str(self.service.ssh_private_keys[user]),
            "-p", str(self.service.ssh_port),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
        ))

    def ssh_auth(self, user, operation):
        result = run(
            "ssh", "-F", "/dev/null",
            "-i", str(self.service.ssh_private_keys[user]),
            "-p", str(self.service.ssh_port), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", f"{self.service.ssh_user}@127.0.0.1",
            "git-lfs-authenticate", "group/demo.git", operation,
        )
        return json.loads(result.stdout)

    def test_ssh_clone_push_ref_policy_and_lfs(self):
        service = self.service
        writer_client = service.root / "ssh-writer"
        writer_env = {**ENV, "GIT_SSH_COMMAND": self.ssh_command("writer")}
        remote = f"ssh://{service.ssh_user}@127.0.0.1:{service.ssh_port}/group/demo.git"
        run("git", "clone", remote, str(writer_client), env=writer_env)
        self.assertEqual(
            run("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=writer_client, env=writer_env).stdout.strip(),
            b"main",
        )
        reader_client = service.root / "ssh-reader"
        reader_env = {**ENV, "GIT_SSH_COMMAND": self.ssh_command("reader")}
        run("git", "clone", remote, str(reader_client), env=reader_env)

        run("git", "lfs", "install", "--local", cwd=writer_client)
        run("git", "lfs", "track", "*.bin", cwd=writer_client)
        payload = b"SSH authenticated LFS payload\n" * 1000
        oid = hashlib.sha256(payload).hexdigest()
        upload_auth = self.ssh_auth("writer", "upload")
        batch = {"operation": "upload", "objects": [{"oid": oid, "size": len(payload)}]}
        status, _, body = service.request(
            "/group/demo.git/info/lfs/objects/batch", batch, "POST",
            headers=upload_auth["header"])
        self.assertEqual(status, 200, body)
        (writer_client / "asset.bin").write_bytes(payload)
        run("git", "add", ".", cwd=writer_client)
        run("git", "commit", "-m", "SSH/LFS writer commit", cwd=writer_client)
        run("git", "push", "origin", "HEAD", cwd=writer_client, env=writer_env)
        lfs_reader = service.root / "ssh-lfs-reader"
        run("git", "-c", "filter.lfs.process=git-lfs filter-process",
            "-c", "filter.lfs.required=true", "clone", remote, str(lfs_reader),
            env=reader_env)
        self.assertEqual((lfs_reader / "asset.bin").read_bytes(), payload)

        run("git", "reset", "--hard", "HEAD~1", cwd=writer_client, env=writer_env)
        (writer_client / "divergent.txt").write_text("SSH force attempt\n")
        run("git", "add", ".", cwd=writer_client, env=writer_env)
        run("git", "commit", "-m", "SSH writer force", cwd=writer_client, env=writer_env)
        denied = run("git", "push", "--force", "origin", "HEAD",
                     cwd=writer_client, env=writer_env, check=False)
        self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)
        forwarded = subprocess.run(
            ["ssh", "-F", "/dev/null", "-i", str(service.ssh_private_keys["reader"]),
             "-p", str(service.ssh_port), "-o", "BatchMode=yes",
             "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "ConnectTimeout=2", "-W", "127.0.0.1:9",
             f"{service.ssh_user}@127.0.0.1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
        )
        self.assertNotEqual(forwarded.returncode, 0)
        self.assertIn(b"remote rejected", denied.stderr)
        run("git", "push", "--force", "origin", "HEAD", cwd=writer_client,
            env={**ENV, "GIT_SSH_COMMAND": self.ssh_command("owner")})

    def test_ssh_rejects_shell_and_ssh_lfs_upload_denial(self):
        service = self.service
        reader = run(
            "ssh", "-i", str(service.ssh_private_keys["reader"]),
            "-F", "/dev/null",
            "-p", str(service.ssh_port), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            f"{service.ssh_user}@127.0.0.1", "sh", "-c", "id",
            check=False,
        )
        self.assertNotEqual(reader.returncode, 0)
        denied = run(
            "ssh", "-i", str(service.ssh_private_keys["reader"]),
            "-F", "/dev/null",
            "-p", str(service.ssh_port), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", f"{service.ssh_user}@127.0.0.1",
            "git-lfs-authenticate", "group/demo.git", "upload",
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)

    def test_identity_and_key_revocation_invalidate_existing_credentials(self):
        service = self.service
        auth = self.ssh_auth("reader", "download")
        header = auth["header"]
        data = b"revocation LFS object"
        oid = hashlib.sha256(data).hexdigest()
        path = f"/group/demo.git/info/lfs/objects/{oid}"
        self.assertEqual(service.request(
            path + f"?size={len(data)}", data, "PUT", user="writer")[0], 200)
        self.assertEqual(service.request(path, headers=header)[0], 200)

        service.revoke_identity("reader")
        self.assertEqual(service.request(path, headers=header)[0], 401)
        self.assertEqual(service.request(
            "/group/demo/info/refs?service=git-upload-pack", user="reader")[0], 401)
        denied = run(
            "ssh", "-i", str(service.ssh_private_keys["reader"]),
            "-F", "/dev/null",
            "-p", str(service.ssh_port), "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", f"{service.ssh_user}@127.0.0.1",
            "git-lfs-authenticate", "group/demo.git", "download",
            check=False,
        )
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(service.request(
            "/public/info/refs?service=git-upload-pack")[0], 200)

    def test_host_key_persists_and_service_restarts(self):
        service = self.service
        before = run("ssh-keygen", "-lf", str(service.ssh_host_key.with_suffix(".pub")),
                     "-E", "sha256").stdout
        service.restart_ssh()
        for _ in range(100):
            try:
                self.ssh_auth("owner", "download")
                break
            except (AssertionError, OSError):
                time.sleep(.1)
        else:
            self.fail("SSH service did not recover after restart")
        after = run("ssh-keygen", "-lf", str(service.ssh_host_key.with_suffix(".pub")),
                    "-E", "sha256").stdout
        self.assertEqual(before.split()[1], after.split()[1])


if __name__ == "__main__":
    unittest.main()
