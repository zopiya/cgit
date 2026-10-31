"""Linux Compose contract for trusted HTTPS, backend isolation and restart persistence."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import time
import unittest

from contract_support import ACCESS_HELPERS, ACCESS_SECRETS, ENV, Service, run

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    os.environ.get("CGIT_TEST_DRIVER") == "docker"
    and (os.environ.get("CGIT_TEST_GITOLITE") or os.environ.get("CGIT_TEST_GITOLITE_IMAGE")),
    "Docker Compose plus Gitolite are required for the TLS contract",
)
class ComposeTlsContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for executable in ("docker", "curl"):
            if not shutil.which(executable):
                raise RuntimeError(f"{executable} is required for the Compose contract")
        cls.fixture = Service(access=True, start=False)
        cls.project = "cgit-contract-" + re.sub(
            r"[^a-z0-9_-]", "-", cls.fixture.root.name.lower())[-48:]
        cls.https_port = cls._free_port()
        cls.ssh_port = cls._free_port()
        cls.domain = f"git.test:{cls.https_port}"
        cls.override = cls.fixture.root / "compose.contract.yml"
        cls.caddyfile = cls.fixture.root / "Caddyfile.contract"
        cls.headers = cls.fixture.root / "curl.headers"
        cls.body = cls.fixture.root / "curl.body"
        cls.ca = cls.fixture.root / "caddy-root.crt"
        cls.ssh_host_key = cls.fixture.root / "ssh-host-keys/ssh_host_ed25519_key"
        cls._write_configuration()
        try:
            cls.compose("config", "--quiet")
            cls.compose(
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "180",
                timeout=240,
            )
            cls._export_ca()
            cls._wait_for_https()
        except BaseException as exc:
            diagnostics = cls.compose("logs", "--no-color", "--tail", "80", check=False)
            cls.compose("down", "-v", "--remove-orphans", "--timeout", "5", check=False)
            cls.fixture.close()
            raise AssertionError(
                f"Compose fixture startup failed: {exc}\n"
                f"{diagnostics.stdout}{diagnostics.stderr}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.compose("down", "-v", "--remove-orphans", "--timeout", "5", check=False)
        cls.fixture.close()

    @classmethod
    def _free_port(cls):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @classmethod
    def _write_configuration(cls):
        fixture = cls.fixture
        caddy_data = fixture.root / "caddy-data"
        caddy_config = fixture.root / "caddy-config"
        caddy_data.mkdir()
        caddy_config.mkdir()
        cls.ssh_host_key.parent.mkdir(mode=0o700)

        production = (ROOT / "config/Caddyfile").read_text()
        marker = "{$CGIT_DOMAIN} {\n"
        if production.count(marker) != 1:
            raise AssertionError("Production Caddyfile no longer has one site block")
        cls.caddyfile.write_text(
            production.replace(marker, marker + "    tls internal\n", 1))

        def volumes(paths):
            return "\n".join(
                f"      - {json.dumps(str(Path(source).resolve()) + ':' + target + suffix)}"
                for source, target, suffix in paths)

        image = os.environ.get("CGIT_TEST_IMAGE", "cgit:test")
        cls.override.write_text(
            "services:\n"
            "  cgit:\n"
            f"    image: {json.dumps(image)}\n"
            f"    container_name: {json.dumps(cls.project + '-cgit')}\n"
            "    ports: !override []\n"
            "    volumes: !override\n"
            f"{volumes(((fixture.repos, '/repos', ':rw'), (fixture.lfs, '/lfs', ':rw'), (fixture.cache, '/var/cache/cgit', ':rw'), (fixture.access_root, '/access', ':rw')))}\n"
            "    environment:\n"
            f"      CGIT_BASE_URL: {json.dumps('https://' + cls.domain)}\n"
            f"      PUID: {json.dumps(str(os.getuid()))}\n"
            f"      PGID: {json.dumps(str(os.getgid()))}\n"
            "      TZ: UTC\n"
            "  ssh:\n"
            f"    image: {json.dumps(image)}\n"
            "    ports: !override\n"
            f"      - {json.dumps(f'127.0.0.1:{cls.ssh_port}:2222')}\n"
            "    volumes: !override\n"
            f"{volumes(((fixture.repos, '/repos', ':rw'), (fixture.access_root, '/access', ':rw'), (fixture.cache, '/var/cache/cgit', ':rw'), (cls.ssh_host_key.parent, '/ssh-host-keys', ':rw')))}\n"
            "    environment:\n"
            f"      CGIT_BASE_URL: {json.dumps('https://' + cls.domain)}\n"
            f"      PUID: {json.dumps(str(os.getuid()))}\n"
            f"      PGID: {json.dumps(str(os.getgid()))}\n"
            "      TZ: UTC\n"
            "  edge:\n"
            f"    user: {json.dumps(str(os.getuid()) + chr(58) + str(os.getgid()))}\n"
            "    ports: !override\n"
            f"      - {json.dumps(f'127.0.0.1:{cls.https_port}:{cls.https_port}')}\n"
            "    volumes: !override\n"
            f"      - {json.dumps(str(Path(cls.caddyfile).resolve()) + ':/etc/caddy/Caddyfile:ro')}\n"
            f"      - {json.dumps(str(Path(caddy_data).resolve()) + ':/data')}\n"
            f"      - {json.dumps(str(Path(caddy_config).resolve()) + ':/config')}\n"
            "    environment:\n"
            f"      CGIT_DOMAIN: {json.dumps(cls.domain)}\n"
            "      CGIT_ACME_EMAIL: contract@example.invalid\n")

    @classmethod
    def _compose_files(cls):
        return [
            ROOT / "docker-compose.yml",
            ROOT / "docker-compose.access.yml",
            ROOT / "docker-compose.public.yml",
            cls.override,
        ]

    @classmethod
    def compose(cls, *args, check=True, timeout=90):
        command = [
            "docker", "compose", "--profile", "ssh", "--project-directory", str(ROOT),
            "--project-name", cls.project,
        ]
        for path in cls._compose_files():
            command += ["-f", str(path)]
        command += list(args)
        environment = {**ENV, "CGIT_DOMAIN": cls.domain,
                       "CGIT_ACME_EMAIL": "contract@example.invalid"}
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, env=environment)
        if check and result.returncode:
            raise AssertionError(
                f"docker compose {' '.join(args)} failed ({result.returncode}):\n"
                f"{result.stdout}{result.stderr}")
        return result

    @classmethod
    def _container_id(cls, service):
        value = cls.compose("ps", "-q", service).stdout.strip()
        if not value:
            raise AssertionError(f"Compose service is not running: {service}")
        return value

    @classmethod
    def _export_ca(cls):
        edge = cls._container_id("edge")
        for _ in range(100):
            result = subprocess.run(
                ["docker", "cp",
                 f"{edge}:/data/caddy/pki/authorities/local/root.crt", str(cls.ca)],
                capture_output=True, timeout=20,
            )
            if result.returncode == 0:
                return
            time.sleep(.1)
        raise AssertionError("Caddy internal root certificate was not created")

    @classmethod
    def request(cls, path, user=None):
        command = [
            "curl", "--silent", "--show-error", "--connect-timeout", "2",
            "--max-time", "20", "--resolve", f"git.test:{cls.https_port}:127.0.0.1",
            "--cacert", str(cls.ca), "--dump-header", str(cls.headers),
            "--output", str(cls.body), "--write-out", "%{http_code}",
        ]
        if user:
            command += ["--user", f"{user}:{ACCESS_SECRETS[user]}"]
        command.append(f"https://{cls.domain}{path}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(f"HTTPS request failed: {result.stderr}")
        values = {}
        for line in cls.headers.read_text(errors="replace").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip().lower()] = value.strip()
        return int(result.stdout), values, cls.body.read_bytes()

    @classmethod
    def _wait_for_https(cls):
        last = None
        for _ in range(120):
            try:
                status = cls.request("/_health", "reader")[0]
                if status == 200:
                    return
                last = f"unexpected status {status}"
            except (AssertionError, OSError, subprocess.SubprocessError) as exc:
                last = str(exc)
            time.sleep(.25)
        raise AssertionError(f"HTTPS edge did not become ready: {last}")

    @classmethod
    def _git(cls, *args, user=None, env=None, cwd=None):
        command = ["git"]
        if user:
            command += ["-c", f"credential.helper={ACCESS_HELPERS[user]}"]
        command += [
            "-c", f"http.curloptResolve=git.test:{cls.https_port}:127.0.0.1",
            "-c", f"http.sslCAInfo={cls.ca}",
            *args,
        ]
        return run(*command, env=env or ENV, cwd=cwd)

    @classmethod
    def _published_ports(cls, service):
        value = run(
            "docker", "inspect", cls._container_id(service),
            "--format", "{{json .NetworkSettings.Ports}}",
        ).stdout
        return json.loads(value)

    def test_https_clone_push_headers_and_restart(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"href='/public/'", body)
        self.assertNotIn(b"href='/group/demo/'", body)
        denied, challenge, _ = self.request("/group/demo/")
        self.assertEqual(denied, 401)
        self.assertTrue(challenge["www-authenticate"].startswith("Basic "))
        self.assertEqual(headers["strict-transport-security"], "max-age=31536000; includeSubDomains")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

        status, _, body = self.request("/", "reader")
        self.assertEqual(status, 200)
        self.assertIn(b"href='/group/demo/'", body)
        self.assertEqual(self.request("/group/demo/info/refs?service=git-upload-pack")[0], 401)
        self.assertEqual(
            self.request("/group/demo/info/refs?service=git-upload-pack", "reader")[0], 200)

        clone = self.fixture.root / "compose-https-reader"
        self._git("clone", f"https://{self.domain}/group/demo", str(clone), user="reader")
        self.assertEqual((clone / "README.md").read_text().splitlines()[0], "# Contract Demo")
        (clone / "tls.txt").write_text("trusted Compose push\n")
        run("git", "add", ".", cwd=clone)
        run("git", "commit", "-m", "Compose TLS push", cwd=clone)
        self._git("push", "origin", "HEAD", cwd=clone, user="writer")

        ports = self._published_ports("cgit")
        self.assertIn("8080/tcp", ports)
        self.assertIsNone(ports["8080/tcp"])

        self.compose("restart", "edge")
        self._wait_for_https()
        verified = self.fixture.root / "compose-https-verified"
        self._git("clone", f"https://{self.domain}/group/demo", str(verified), user="reader")
        self.assertEqual((verified / "tls.txt").read_text(), "trusted Compose push\n")

    def test_ssh_port_is_published_and_host_key_survives_restart(self):
        ports = self._published_ports("cgit")
        self.assertIsNone(ports.get("8080/tcp"))
        ssh_ports = self._published_ports("ssh")
        self.assertTrue(ssh_ports.get("2222/tcp"))
        self.assertEqual(
            self.compose("port", "ssh", "2222").stdout.strip(),
            f"127.0.0.1:{self.ssh_port}",
        )

        for _ in range(120):
            if self.ssh_host_key.with_suffix(".pub").exists():
                break
            time.sleep(.25)
        else:
            raise AssertionError("SSH host key was not created")
        before = run("ssh-keygen", "-lf", str(self.ssh_host_key.with_suffix(".pub")),
                     "-E", "sha256").stdout.split()[1]
        remote = f"ssh://git@127.0.0.1:{self.ssh_port}/group/demo.git"
        ssh_reader = (
            f"ssh -F /dev/null -i {shlex.quote(str(self.fixture.ssh_private_keys['reader']))} "
            "-o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none "
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR")
        run("git", "clone", remote, str(self.fixture.root / "compose-ssh-reader"),
            env={**ENV, "GIT_SSH_COMMAND": ssh_reader})

        self.compose("restart", "ssh")
        for _ in range(120):
            probe = subprocess.run(
                ["ssh", "-F", "/dev/null",
                 "-i", str(self.fixture.ssh_private_keys["owner"]),
                 "-p", str(self.ssh_port), "-o", "BatchMode=yes",
                 "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=1", "git@127.0.0.1",
                 "git-lfs-authenticate", "group/demo.git", "download"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            )
            if probe.returncode == 0:
                break
            time.sleep(.25)
        else:
            raise AssertionError("SSH service did not recover after Compose restart")
        after = run("ssh-keygen", "-lf", str(self.ssh_host_key.with_suffix(".pub")),
                    "-E", "sha256").stdout.split()[1]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
