"""Synthetic service adapters. No production modules are imported by this harness.

CGI is a test transport, not a substitute for lighttpd/container verification.
Runtime commands can be replaced for the Go implementation without changing tests.
"""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pwd
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
USER, SECRET = "owner", "contract-fixture-secret"
AUTH = "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
HELPER = f"!f() {{ echo username={USER}; echo password={SECRET}; }}; f"
ACCESS_SECRETS = {
    "owner": "owner-contract-secret",
    "reader": "reader-contract-secret",
    "writer": "writer-contract-secret",
    "disabled": "disabled-contract-secret",
}
ACCESS_AUTH = {
    user: "Basic " + base64.b64encode(f"{user}:{secret}".encode()).decode()
    for user, secret in ACCESS_SECRETS.items()
}
ACCESS_HELPERS = {
    user: f"!f() {{ echo username={user}; echo password={secret}; }}; f"
    for user, secret in ACCESS_SECRETS.items()
}
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Contract Fixture",
       "GIT_AUTHOR_EMAIL": "contract@localhost", "GIT_COMMITTER_NAME": "Contract Fixture",
       "GIT_COMMITTER_EMAIL": "contract@localhost", "GIT_LFS_SKIP_SMUDGE": "0"}


def command(name, default):
    value = json.loads(os.environ[name]) if name in os.environ else default
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a nonempty JSON string array")
    return value


def run(*args, cwd=None, env=None, check=True):
    result = subprocess.run(args, cwd=cwd, env=env or ENV, capture_output=True, timeout=90)
    if check and result.returncode:
        # This harness only has synthetic data; never print a full environment.
        message = result.stderr.decode(errors="replace").replace(SECRET, "[fixture-secret]")
        raise AssertionError(f"{args[0]} failed ({result.returncode}): {message}")
    return result


class CGIHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def dispatch(self):
        env = {**ENV, "CGIT_RUNTIME_CONFIG": str(self.server.settings),
               "REQUEST_METHOD": self.command, "REQUEST_URI": self.path,
               "QUERY_STRING": self.path.partition("?")[2], "SERVER_PROTOCOL": "HTTP/1.1",
               "SERVER_NAME": "127.0.0.1", "SERVER_PORT": str(self.server.server_port),
               "REMOTE_ADDR": "127.0.0.1", "GATEWAY_INTERFACE": "CGI/1.1"}
        for key, value in self.headers.items():
            key = key.upper().replace("-", "_")
            env[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = value
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        result = subprocess.run(self.server.gateway, input=body, env=env,
                                capture_output=True, timeout=90)
        head, separator, content = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            self.send_error(502, "CGI fixture failed; inspect the runtime separately")
            return
        values = [line.decode().split(":", 1) for line in head.split(b"\r\n")]
        status = next((int(v.strip().split()[0]) for k, v in values if k.lower() == "status"), 200)
        self.send_response(status)
        for key, value in values:
            if key.lower() != "status":
                self.send_header(key, value.strip())
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = dispatch


class Service:
    """Start an isolated service using only CLI arguments, env, volumes and Git."""

    def __init__(self, *, private=False, push=True, access=False, start=True):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-contract-")
        self.root = Path(self.temp.name).resolve()
        self.repos, self.lfs, self.cache = (self.root / n for n in ("repos", "lfs", "cache"))
        for directory in (self.repos, self.lfs, self.cache):
            directory.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.repo = self.repos / "group/demo.git"
        self.repo.parent.mkdir()
        run("git", "init", "--bare", "-b", "main", str(self.repo))
        run("git", "init", "-b", "main", str(self.work))
        (self.work / "README.md").write_text("# Contract Demo\n\n[Source](hello.py)\n")
        (self.work / "hello.py").write_text('print("contract")\n')
        self.commit("Initial contract fixture")
        self.password = self.root / "password"
        self.password.write_text(SECRET + "\n")
        self.settings = self.cache / "runtime/settings.json"
        self.server = self.process = self.log = self.container = self.ssh_container = None
        self.base = None
        self.ssh_process = self.ssh_log = None
        self.ssh_port = None
        self.driver = os.environ.get("CGIT_TEST_DRIVER", "cgi")
        self.access = access
        if self.driver not in ("cgi", "lighttpd", "docker"):
            self.close()
            raise ValueError(f"Unknown CGIT_TEST_DRIVER: {self.driver}")
        self.web_available = self.driver != "cgi"
        if access:
            try:
                self._prepare_access()
            except BaseException:
                self.close()
                raise
        if not start:
            return
        try:
            if self.driver == "docker":
                self.container = "cgit-contract-" + self.root.name.rsplit("-", 1)[-1]
                args = [
                    "docker", "run", "-d", "--name", self.container,
                    "-p", "127.0.0.1::8080", "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}",
                    "-e", f"CGIT_HTTP_PUSH={int(push)}", "-v", f"{self.repos}:/repos",
                    "-v", f"{self.lfs}:/lfs", "-v", f"{self.cache}:/var/cache/cgit",
                ]
                if access:
                    args += ["-e", "CGIT_AUTH_MODE=access", "-e", "CGIT_ACCESS_ROOT=/access",
                             "-e", "CGIT_GITOLITE=/usr/local/bin/gitolite",
                             "-v", f"{self.access_root}:/access"]
                else:
                    args += ["-e", f"CGIT_USERNAME={USER}", "-e", "CGIT_PASSWORD_FILE=/run/password",
                             "-e", f"CGIT_AUTH_MODE={'private' if private else 'public'}",
                             "-v", f"{self.password}:/run/password:ro"]
                args.append(os.environ.get("CGIT_TEST_IMAGE", "cgit:test"))
                run(*args)
                port = run("docker", "port", self.container, "8080").stdout.decode().strip().rsplit(":", 1)[1]
                self.base = "http://127.0.0.1:" + port
            else:
                self._start_local(private, push)
            self.remote = self.base + "/group/demo"
            run("git", "remote", "add", "origin", self.remote, cwd=self.work)
            run("git", "config", "credential.helper", HELPER, cwd=self.work)
            for _ in range(100):
                try:
                    status = self.request("/group/demo/info/refs?service=git-upload-pack",
                                          user="reader" if access else None,
                                          auth=private and not access)[0]
                    if status == 200:
                        break
                except OSError:
                    pass
                time.sleep(.1)
            else:
                raise AssertionError("Service did not become ready for a real Git advertisement")
        except BaseException as exc:
            detail = ""
            if self.container:
                logged = subprocess.run(["docker", "logs", "--tail", "60", self.container],
                                        capture_output=True, timeout=20)
                detail = (logged.stdout + logged.stderr).decode(errors="replace")
            elif self.log:
                self.log.flush()
                self.log.seek(0)
                detail = self.log.read()[-8000:]
            self.close()
            if detail and isinstance(exc, Exception):
                raise RuntimeError(f"{exc}\nService diagnostics:\n{detail.replace(SECRET, '[fixture-secret]')}") from exc
            raise

    def _start_local(self, private, push):
        gateway = command("CGIT_TEST_GATEWAY_COMMAND", [sys.executable, str(ROOT / "runtime/gateway.py")])
        cgit = os.environ.get("CGIT_TEST_CGIT", "/does-not-exist")
        source = (ROOT / "config/cgitrc").read_text().replace("/repos", str(self.repos))
        source = source.replace("/var/cache/cgit", str(self.cache))
        conf = (ROOT / "config/lighttpd.conf").read_text()
        if self.driver == "lighttpd":
            runtime = self.root / "runtime"
            shutil.copytree(ROOT / "runtime", runtime, ignore=shutil.ignore_patterns("__pycache__"))
            filters = self.root / "filters"
            shutil.copytree(os.environ["CGIT_TEST_FILTERS"], filters)
            for file in [*runtime.glob("*.py"), *filters.rglob("*.py")]:
                text = file.read_text().replace("#!/usr/bin/env python3", "#!" + sys.executable)
                text = text.replace("/usr/local/lib/cgit/filters", str(filters))
                text = text.replace("/var/cache/cgit", str(self.cache))
                file.write_text(text)
                file.chmod(0o755)
            source = source.replace("/usr/local/lib/cgit-personal", str(runtime))
            docroot = self.root / "htdocs"
            docroot.mkdir()
            for name in ("cgit.css", "cgit.js", "cgit.png", "favicon.ico", "robots.txt"):
                candidate = Path(cgit).parent / name
                if candidate.exists():
                    shutil.copy(candidate, docroot / name)
            shutil.copy(ROOT / "config/custom.css", docroot)
            (docroot / "gateway.cgi").symlink_to(runtime / "gateway.py")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            conf = conf.replace("server.port          = 8080", f'server.port = {port}\nserver.bind = "127.0.0.1"')
            conf = conf.replace("/var/www/htdocs/cgit", str(docroot)).replace("/var/cache/cgit", str(self.cache))
            conf = conf.replace("/usr/local/lib/cgit-personal", str(runtime))
            self.base = f"http://127.0.0.1:{port}"
        template = self.root / "cgitrc"
        template.write_text(source)
        env = {**ENV, "CGIT_RUNTIME_DIR": str(self.cache / "runtime"),
               "CGIT_HTTP_PUSH": str(int(push)), "CGIT_SSH_PUSH": str(int(push))}
        if self.access:
            env.update(CGIT_AUTH_MODE="access", CGIT_ACCESS_ROOT=str(self.access_root),
                       CGIT_GITOLITE=str(self.gitolite))
            if getattr(self, "base", None):
                env["CGIT_BASE_URL"] = self.base
        else:
            env.update(CGIT_USERNAME=USER, CGIT_PASSWORD_FILE=str(self.password),
                       CGIT_AUTH_MODE="private" if private else "public")
        setup = command("CGIT_TEST_CONFIGURE_COMMAND", [sys.executable, str(ROOT / "runtime/configure.py")])
        run(*setup, "--source", str(template), "--repos", str(self.repos), "--lfs", str(self.lfs),
            "--cgit", cgit, env=env)
        if self.driver == "lighttpd":
            config_file = self.root / "lighttpd.conf"
            config_file.write_text(conf)
            self.log = (self.root / "lighttpd.log").open("w+")
            self.process = subprocess.Popen([os.environ["CGIT_TEST_LIGHTTPD"], "-D", "-f", str(config_file)],
                                            env=ENV, stdout=self.log, stderr=self.log)
        else:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), CGIHandler)
            self.server.daemon_threads = True
            self.server.gateway, self.server.settings = gateway, self.settings
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _prepare_access(self):
        gitolite_value = os.environ.get("CGIT_TEST_GITOLITE")
        gitolite_image = os.environ.get("CGIT_TEST_GITOLITE_IMAGE")
        if gitolite_value:
            self.gitolite = Path(gitolite_value).resolve()
        elif gitolite_image:
            wrapper = self.root / "container-gitolite"
            wrapper.write_text(
                "#!/bin/sh\n"
                "set -eu\n"
                "fixture_root=$(dirname \"$HOME\")\n"
                "exec docker run --rm -i --user \"$(id -u):$(id -g)\" "
                "-e HOME=\"$HOME\" -v \"$fixture_root:$fixture_root\" "
                f"--entrypoint /usr/local/bin/gitolite {shlex.quote(gitolite_image)} \"$@\"\n")
            wrapper.chmod(0o755)
            self.gitolite = wrapper
        else:
            raise RuntimeError(
                "CGIT_TEST_GITOLITE or CGIT_TEST_GITOLITE_IMAGE is required for access-mode contracts")
        if not self.gitolite.is_absolute() or not os.access(self.gitolite, os.X_OK):
            raise RuntimeError("CGIT_TEST_GITOLITE must be executable")
        self.access_root = self.root / "access"
        (self.access_root / ".gitolite/conf").mkdir(parents=True)
        (self.access_root / ".gitolite/logs").mkdir()
        public = self.repos / "public.git"
        run("git", "init", "--bare", "-b", "main", str(public))
        quoted = str(self.repos).replace("\\", "\\\\").replace("'", "\\'")
        (self.access_root / ".gitolite.rc").write_text(
            f"%RC = (GL_REPO_BASE => '{quoted}', UMASK => 0022);\n")
        (self.access_root / ".gitolite/conf/gitolite.conf").write_text(
            "repo gitolite-admin\n"
            "    RW+ = owner\n"
            "repo public\n"
            "    R = anonymous reader writer owner\n"
            "    RW+ = owner\n"
            "repo group/demo\n"
            "    R = reader writer owner\n"
            "    RW = writer\n"
            "    RW+ = owner\n")
        run(str(self.gitolite), "compile",
            env={**ENV, "HOME": str(self.access_root), "LANG": "C", "TZ": "UTC"})

        identities = {}
        keydir = self.access_root / ".gitolite/keydir"
        keydir.mkdir()
        self.ssh_private_keys = {}
        access_command = command(
            "CGIT_TEST_ACCESS_COMMAND",
            [sys.executable, str(ROOT / "runtime/access.py")],
        )
        self.access_command = access_command
        for user, secret in ACCESS_SECRETS.items():
            secret_file = self.root / f"{user}.secret"
            secret_file.write_text(secret)
            hashed = run(*access_command, "hash-secret", "--secret-file", str(secret_file), env=ENV)
            identities[user] = {
                "enabled": user != "disabled",
                "owner": user == "owner",
                "secret_hash": hashed.stdout.decode().strip(),
                "keys": [],
            }
            if user == "disabled":
                continue
            private_key = self.root / f"ssh-client-{user}"
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private_key))
            public_key = keydir / f"{user}@contract.pub"
            public_key.write_bytes(private_key.with_suffix(".pub").read_bytes())
            fingerprint = run("ssh-keygen", "-lf", str(public_key), "-E", "sha256").stdout.decode().split()[1]
            identities[user]["keys"].append(fingerprint)
            self.ssh_private_keys[user] = private_key
        identities_file = self.root / "identities.json"
        identities_file.write_text(json.dumps(identities))
        self.identities = identities
        self.identities_file = identities_file
        if self.driver == "docker":
            self.ssh_command_path = "/usr/local/bin/cgit-ssh"
        else:
            wrapper = self.root / "cgit-ssh"
            wrapper.write_text(
                "#!/bin/sh\n"
                f"export CGIT_RUNTIME_CONFIG={shlex.quote(str(self.settings))}\n"
                f"exec {shlex.quote(sys.executable)} "
                f"{shlex.quote(str(ROOT / 'runtime/ssh_command.py'))} \"$@\"\n")
            wrapper.chmod(0o755)
            self.ssh_command_path = str(wrapper)
        run(*access_command, "apply", "--directory", str(self.access_root),
            "--repos", str(self.repos), "--identities", str(identities_file),
            "--gitolite", str(self.gitolite), "--ssh-command", self.ssh_command_path,
            env=ENV)
        self._finalize_access_storage()

    def _finalize_access_storage(self):
        if self.driver == "docker":
            (self.access_root / ".gitolite.rc").write_text(
                "%RC = (GL_REPO_BASE => '/repos', UMASK => 0022);\n")
            for hook in self.repos.rglob("hooks/*"):
                if hook.is_symlink() and hook.resolve().is_relative_to(self.access_root):
                    relative = hook.resolve().relative_to(self.access_root)
                    hook.unlink()
                    hook.symlink_to(Path("/access") / relative)

    def revoke_identity(self, user):
        quoted = str(self.repos).replace("\\", "\\\\").replace("'", "\\'")
        (self.access_root / ".gitolite.rc").write_text(
            f"%RC = (GL_REPO_BASE => '{quoted}', UMASK => 0022);\n")
        (self.access_root / f".gitolite/keydir/{user}@contract.pub").unlink(missing_ok=True)
        self.identities[user]["enabled"] = False
        self.identities_file.write_text(json.dumps(self.identities))
        run(*self.access_command, "apply", "--directory", str(self.access_root),
            "--repos", str(self.repos), "--identities", str(self.identities_file),
            "--gitolite", str(self.gitolite), "--ssh-command", self.ssh_command_path,
            env=ENV)
        self._finalize_access_storage()

    def commit(self, message):
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", message, cwd=self.work)
        run("git", "push", str(self.repo), "HEAD", cwd=self.work)

    def start_ssh(self):
        if not self.access:
            raise RuntimeError("SSH contract requires access mode")
        if self.driver == "lighttpd":
            return self._start_local_sshd()
        if self.driver != "docker":
            raise RuntimeError("SSH contract requires the lighttpd or Docker driver")
        self.ssh_container = self.container + "-ssh"
        self.ssh_user = "git"
        host_keys = self.root / "ssh-host-keys"
        host_keys.mkdir(mode=0o700)
        self.ssh_host_key = host_keys / "ssh_host_ed25519_key"
        args = [
            "docker", "run", "-d", "--name", self.ssh_container,
            "--entrypoint", "/usr/local/bin/docker-ssh-entrypoint.sh",
            "-p", "127.0.0.1::2222",
            "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}",
            "-e", "CGIT_AUTH_MODE=access", "-e", "CGIT_ACCESS_ROOT=/access",
            "-e", "CGIT_GITOLITE=/usr/local/bin/gitolite", "-e", f"CGIT_BASE_URL={self.base}",
            "-e", "CGIT_HTTP_PUSH=1", "-e", "CGIT_SSH_PUSH=1",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "FOWNER", "--cap-add", "SETGID", "--cap-add", "SETUID",
            "--security-opt", "no-new-privileges:true",
            "--tmpfs", "/run", "--tmpfs", "/tmp",
            "-v", f"{self.repos}:/repos", "-v", f"{self.access_root}:/access",
            "-v", f"{self.cache}:/var/cache/cgit", "-v", f"{host_keys}:/ssh-host-keys",
            os.environ.get("CGIT_TEST_IMAGE", "cgit:test"),
        ]
        run(*args)
        port = run("docker", "port", self.ssh_container, "2222").stdout.decode().strip().rsplit(":", 1)[1]
        self.ssh_port = int(port)
        for _ in range(100):
            probe = subprocess.run(
                ["ssh", "-i", str(self.ssh_private_keys["owner"]), "-p", port,
                 "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=1", "git@127.0.0.1",
                 "git-lfs-authenticate", "group/demo.git", "download"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            )
            if probe.returncode == 0:
                return
            time.sleep(.1)
        logs = subprocess.run(["docker", "logs", "--tail", "80", self.ssh_container],
                              capture_output=True, text=True, timeout=20)
        raise AssertionError(f"SSH service did not become ready\n{logs.stdout}{logs.stderr}")

    def _start_local_sshd(self):
        binary = shutil.which("sshd") or "/usr/sbin/sshd"
        if not Path(binary).is_file():
            raise RuntimeError("sshd is required for local SSH contracts")
        self.ssh_user = pwd.getpwuid(os.getuid()).pw_name
        host_keys = self.root / "ssh-host-keys"
        host_keys.mkdir(mode=0o700, exist_ok=True)
        host_key = host_keys / "ssh_host_ed25519_key"
        self.ssh_host_key = host_key
        if not host_key.exists():
            run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        config = self.root / "sshd.conf"
        config.write_text(
            f"Port {port}\n"
            "ListenAddress 127.0.0.1\n"
            f"HostKey {host_key}\n"
            f"PidFile {self.root / 'sshd.pid'}\n"
            f"AuthorizedKeysFile {self.access_root / 'ssh/authorized_keys'}\n"
            "StrictModes no\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "PermitRootLogin no\n"
            "PubkeyAuthentication yes\n"
            "AuthenticationMethods publickey\n"
            "UsePAM no\n"
            "PermitTTY no\n"
            "PermitUserEnvironment no\n"
            "AllowAgentForwarding no\n"
            "AllowTcpForwarding no\n"
            "X11Forwarding no\n"
            "PermitTunnel no\n"
            "UseDNS no\n"
            "PrintMotd no\n"
            f"AllowUsers {self.ssh_user}\n"
            "LogLevel VERBOSE\n")
        self.ssh_log = (self.root / "sshd.log").open("w+")
        self.ssh_process = subprocess.Popen(
            [binary, "-D", "-e", "-f", str(config)],
            stdout=self.ssh_log, stderr=self.ssh_log,
        )
        self.ssh_port = port
        for _ in range(100):
            probe = subprocess.run(
                ["ssh", "-i", str(self.ssh_private_keys["owner"]), "-p", str(port),
                 "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                 "-o", "ConnectTimeout=1", f"{self.ssh_user}@127.0.0.1",
                 "git-lfs-authenticate", "group/demo.git", "download"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            )
            if probe.returncode == 0:
                return
            time.sleep(.1)
        self.ssh_log.flush()
        self.ssh_log.seek(0)
        raise AssertionError(f"Local sshd did not become ready\n{self.ssh_log.read()}")

    def restart_ssh(self):
        if self.ssh_container:
            run("docker", "restart", self.ssh_container)
            return
        if not self.ssh_process:
            raise RuntimeError("Local SSH service is not running")
        self.ssh_process.terminate()
        self.ssh_process.wait(timeout=10)
        self.ssh_process = None
        self.ssh_log.close()
        self.ssh_log = None
        self._start_local_sshd()

    def request(self, path, data=None, method="GET", auth=False, headers=None, user=None):
        values = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            values["Content-Type"] = "application/vnd.git-lfs+json"
        if user is not None:
            values["Authorization"] = ACCESS_AUTH[user]
        elif auth:
            values["Authorization"] = AUTH
        try:
            response = urlopen(Request(self.base + path, data=data, method=method, headers=values), timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def close(self):
        if self.ssh_container:
            subprocess.run(["docker", "rm", "-f", self.ssh_container], capture_output=True, timeout=30)
            self.ssh_container = None
        if self.container:
            subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=30)
            self.container = None
        if self.ssh_process:
            self.ssh_process.terminate()
            try:
                self.ssh_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ssh_process.kill()
                self.ssh_process.wait()
            self.ssh_process = None
        if self.ssh_log:
            self.ssh_log.close()
            self.ssh_log = None
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=10)
        self.temp.cleanup()
