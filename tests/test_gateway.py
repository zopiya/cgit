"""Protocol and data-boundary tests; all repositories/credentials are synthetic.

python3 -m unittest discover -s tests -v
CGIT_TEST_DRIVER=docker CGIT_TEST_IMAGE=cgit:test python3 -m unittest discover -s tests -v
The Docker driver exercises the exact same client flows through real lighttpd.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from access import hash_secret, publish_snapshot
from common import RequestError, object_path, pointer, safe_path
from lfs_token import ensure_secret, issue

AUTH = "Basic " + base64.b64encode(b"owner:fixture-password").decode()
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
       "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}


def run(*args, cwd=None, check=True, **kwargs):
    kwargs.setdefault("timeout", 90)
    result = subprocess.run(args, cwd=cwd, env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    if check and result.returncode:
        raise AssertionError(f"{args[:3]} failed: {result.stderr.decode(errors='replace')}")
    return result


def gitolite_command(root):
    binary = os.environ.get("CGIT_TEST_GITOLITE")
    if binary:
        return Path(binary).resolve()
    image = os.environ.get("CGIT_TEST_GITOLITE_IMAGE")
    if not image:
        return None
    wrapper = Path(root) / "container-gitolite"
    wrapper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "fixture_root=$(dirname \"$HOME\")\n"
        "exec docker run --rm -i --user \"$(id -u):$(id -g)\" "
        "-e HOME=\"$HOME\" -v \"$fixture_root:$fixture_root\" "
        f"--entrypoint /usr/local/bin/gitolite {shlex.quote(image)} \"$@\"\n")
    wrapper.chmod(0o755)
    return wrapper


def basic_auth(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True


class CGIHandler(BaseHTTPRequestHandler):
    """Test-only CGI transport; production uses lighttpd, never this server."""
    def log_message(self, *args):
        pass

    def dispatch(self):
        env = dict(ENV, CGIT_RUNTIME_CONFIG=str(self.server.settings),
                   REQUEST_METHOD=self.command, REQUEST_URI=self.path,
                   QUERY_STRING=self.path.partition("?")[2], SERVER_PROTOCOL="HTTP/1.1",
                   SERVER_NAME="127.0.0.1", SERVER_PORT=str(self.server.server_port),
                   REMOTE_ADDR="127.0.0.1", GATEWAY_INTERFACE="CGI/1.1")
        for key, value in self.headers.items():
            key = key.upper().replace("-", "_")
            env[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = value
        data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        result = subprocess.run([sys.executable, str(ROOT / "runtime/gateway.py")],
                                input=data, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        head, separator, body = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            self.send_error(502, result.stderr.decode(errors="replace"))
            return
        values = [line.decode().split(":", 1) for line in head.split(b"\r\n")]
        status = next((int(value.strip().split()[0]) for key, value in values if key.lower() == "status"), 200)
        self.send_response(status)
        for key, value in values:
            if key.lower() != "status":
                self.send_header(key, value.strip())
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = dispatch


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="cgit-tests-")
        cls.root = Path(cls.temp.name).resolve()
        cls.repos = cls.root / "repos"
        cls.lfs = cls.root / "lfs"
        cls.cache = cls.root / "cache"
        for p in (cls.repos, cls.lfs, cls.cache / "runtime"):
            p.mkdir(parents=True)
        cls.settings = cls.cache / "runtime/settings.json"
        cls.password = cls.root / "password"
        cls.password.write_text("fixture-password\n")
        cls.driver = os.environ.get("CGIT_TEST_DRIVER", "cgi")
        cls.container = "cgit-tests-" + str(os.getpid())
        cls.process = None
        cls.server = None
        if cls.driver == "docker":
            image = os.environ.get("CGIT_TEST_IMAGE", "cgit:test")
            run("docker", "run", "-d", "--name", cls.container,
                "-p", "127.0.0.1::8080", "-e", f"PUID={os.getuid()}", "-e", f"PGID={os.getgid()}",
                "-e", "CGIT_USERNAME=owner", "-e", "CGIT_PASSWORD_FILE=/run/password",
                "-e", "CGIT_HTTP_PUSH=1", "-e", "CGIT_AUTH_MODE=private",
                "-v", f"{cls.repos}:/repos", "-v", f"{cls.lfs}:/lfs", "-v", f"{cls.cache}:/var/cache/cgit",
                "-v", f"{cls.password}:/run/password:ro",
                "-v", f"{cls.root}:{cls.root}", image)
            port = run("docker", "port", cls.container, "8080").stdout.decode().strip().rsplit(":", 1)[1]
            cls.base = "http://127.0.0.1:" + port
            for _ in range(100):
                if cls.settings.exists():
                    try:
                        with urlopen(cls.base + "/cgit.css", timeout=1):
                            break
                    except OSError:
                        pass
                time.sleep(.1)
            else:
                raise AssertionError(run("docker", "logs", cls.container).stdout.decode())
            cls.baseline = json.loads(cls.settings.read_text())
            cls.baseline_cgitrc = (cls.cache / "runtime/cgitrc").read_text()
        else:
            cls.baseline = dict(repos=str(cls.repos), lfs=str(cls.lfs), base_url="",
                                auth_mode="public", username="owner", password_file=str(cls.password),
                                http_push=True, max_size=10 * 1024**3,
                                cgit=os.environ.get("CGIT_TEST_CGIT", "/does-not-exist"),
                                cgit_config=str(cls.cache / "runtime/cgitrc"))
            if cls.driver == "lighttpd":
                cls.start_lighttpd()
            else:
                cls.server = GatewayServer(("127.0.0.1", 0), CGIHandler)
                cls.server.settings = cls.settings
                cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
                cls.thread.start()
                cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def start_lighttpd(cls):
        # Exact production config with filesystem/port substitutions only.
        binary = os.environ["CGIT_TEST_LIGHTTPD"]
        runtime = cls.root / "runtime"
        shutil.copytree(ROOT / "runtime", runtime, ignore=shutil.ignore_patterns("__pycache__"))
        filters = cls.root / "filters"
        shutil.copytree(os.environ["CGIT_TEST_FILTERS"], filters)
        for p in runtime.glob("*.py"):
            text = p.read_text().replace("/usr/local/lib/cgit/filters", str(filters))
            text = text.replace("/var/cache/cgit", str(cls.cache))
            text = text.replace("#!/usr/bin/env python3", "#!" + sys.executable)
            p.write_text(text)
            p.chmod(0o755)
        for p in filters.rglob("*"):
            if p.is_file() and p.read_bytes().startswith(b"#!/usr/bin/env python3"):
                p.write_text(p.read_text().replace("#!/usr/bin/env python3", "#!" + sys.executable))
        docroot = cls.root / "htdocs"
        docroot.mkdir()
        for name in ("cgit.css", "cgit.js", "cgit.png", "favicon.ico", "robots.txt"):
            candidate = Path(os.environ["CGIT_TEST_CGIT"]).parent / name
            if candidate.exists():
                shutil.copy(candidate, docroot / name)
        shutil.copy(ROOT / "config/custom.css", docroot)
        (docroot / "gateway.cgi").symlink_to(runtime / "gateway.py")
        (cls.cache / "uploads").mkdir()
        (cls.cache / "runtime/limits.conf").write_text("server.max-request-size = 10485760\n")
        cgitrc = (ROOT / "config/cgitrc").read_text()
        cgitrc = cgitrc.replace("/usr/local/lib/cgit-personal", str(runtime)).replace("/repos", str(cls.repos))
        cgitrc = cgitrc.replace("/var/cache/cgit", str(cls.cache)).replace("cache-size=1000", "cache-size=0")
        (cls.cache / "runtime/cgitrc").write_text(cgitrc)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        conf = (ROOT / "config/lighttpd.conf").read_text()
        conf = conf.replace('server.port          = 8080', f'server.port = {port}\nserver.bind = "127.0.0.1"')
        conf = conf.replace("/var/www/htdocs/cgit", str(docroot)).replace("/var/cache/cgit", str(cls.cache))
        conf = conf.replace("/usr/local/lib/cgit-personal", str(runtime))
        config_file = cls.root / "lighttpd.conf"
        config_file.write_text(conf)
        cls.log = (cls.root / "lighttpd.log").open("w+")
        cls.process = subprocess.Popen([binary, "-D", "-f", str(config_file)], env=ENV,
                                       stdout=cls.log, stderr=cls.log)
        cls.base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                with urlopen(cls.base + "/cgit.css", timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            cls.log.seek(0)
            raise AssertionError(cls.log.read())

    @classmethod
    def tearDownClass(cls):
        if cls.driver == "docker":
            run("docker", "rm", "-f", cls.container, check=False)
        if cls.process:
            cls.process.terminate()
            cls.process.wait(timeout=10)
            cls.log.close()
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        for root in (self.repos, self.lfs):
            for p in root.iterdir():
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink()
        self.cfg = {**self.baseline, "auth_mode": "public", "http_push": True, "base_url": ""}
        if self.driver == "docker":
            # These white-box tests publish runtime snapshots from the host.
            # Mount the fixture at the same absolute path in both environments.
            self.cfg.update(repos=str(self.repos), lfs=str(self.lfs),
                            cgit_config=str(self.cache / "runtime/cgitrc"))
            cgitrc = self.cache / "runtime/cgitrc"
            cgitrc.write_text(self.baseline_cgitrc.replace("/repos", str(self.repos)))
        self.save_config()
        self.bare = self.repos / "group/demo.git"
        self.bare.parent.mkdir(parents=True, exist_ok=True)
        run("git", "init", "--bare", "-b", "main", str(self.bare))
        self.work = self.root / "work"
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir()
        run("git", "init", "-b", "main", str(self.work))
        (self.work / "hello.py").write_text('print("hello")\n')
        (self.work / "README.md").write_text('# Demo\n\n[Source](hello.py)\n\n![Image](picture.png)\n')
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Initial", cwd=self.work)
        run("git", "push", str(self.bare), "HEAD", cwd=self.work)
        self.remote = self.base + "/group/demo"
        run("git", "remote", "add", "origin", self.remote, cwd=self.work)
        run("git", "config", "credential.helper",
            "!f() { echo username=owner; echo password=fixture-password; }; f", cwd=self.work)

    def save_config(self):
        temporary = self.settings.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.cfg))
        temporary.replace(self.settings)

    def enable_access(self):
        access = self.root / "access"
        if access.exists():
            shutil.rmtree(access)
        (access / ".gitolite/conf").mkdir(parents=True)
        compiled = access / ".gitolite/conf/gitolite.conf-compiled.pm"
        compiled.write_text("%repos = ();\n")
        public = self.repos / "public.git"
        run("git", "init", "--bare", "-b", "main", str(public))
        for repo_name in ("group/demo.git", "public.git"):
            (self.repos / repo_name / "gl-conf").write_text("%repos = ();\n")
        users = {
            "owner": {"enabled": True, "owner": True,
                      "secret_hash": hash_secret(b"owner-access-secret")},
            "reader": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"reader-access-secret")},
            "writer": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"writer-access-secret")},
            "disabled": {"enabled": False, "owner": False,
                         "secret_hash": hash_secret(b"disabled-access-secret")},
        }
        gitolite = self.root / "gitolite"
        (self.root / "gitolite-bin/lib").mkdir(parents=True, exist_ok=True)
        gitolite.write_text(
            "#!/bin/sh\n"
            f"test \"$HOME\" = {shlex.quote(str(access))} || exit 9\n"
            "if [ \"$1\" = query-rc ]; then\n"
            f"  if [ \"$2\" = GL_BINDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'gitolite-bin'))}; exit 0; fi\n"
            f"  if [ \"$2\" = GL_LIBDIR ]; then printf '%s\\n' {shlex.quote(str(self.root / 'gitolite-bin/lib'))}; exit 0; fi\n"
            "  exit 1\n"
            "fi\n"
            "allow() {\n"
            "  case \"$1:$2:$3\" in\n"
            "    public:*:R|public:owner:W) return 0 ;;\n"
            "    group/demo:reader:R|group/demo:writer:R|group/demo:writer:W|group/demo:owner:R|group/demo:owner:W) return 0 ;;\n"
            "  esac\n"
            "  return 1\n"
            "}\n"
            "if [ \"$2\" = \"%\" ]; then\n"
            "  while IFS= read -r repo; do\n"
            "    if allow \"$repo\" \"$3\" \"$4\"; then\n"
            "      printf '%s\\t%s\\trefs/.*\\n' \"$repo\" \"$3\"\n"
            "    else\n"
            "      printf '%s\\t%s\\t%s any %s %s DENIED by fallthru\\n' \"$repo\" \"$3\" \"$4\" \"$repo\" \"$3\"\n"
            "    fi\n"
            "  done\n"
            "  exit 0\n"
            "fi\n"
            "allow \"$3\" \"$4\" \"$5\"\n")
        gitolite.chmod(0o755)
        (access / "maintenance").write_text("test\n")
        publish_snapshot(access, self.repos, users)
        ensure_secret(access)
        (access / "maintenance").unlink()
        self.cfg.update(
            auth_mode="access", username="", password_file="", http_push=True,
            access={"root": str(access), "gitolite": str(gitolite)},
        )
        self.save_config()
        cgitrc = Path(self.cfg["cgit_config"])
        if not cgitrc.exists():
            source = (ROOT / "config/cgitrc").read_text()
            source = source.replace("/repos", str(self.repos))
            source = source.replace("/var/cache/cgit", str(self.cache))
            source = source.replace("/usr/local/lib/cgit-personal", str(ROOT / "runtime"))
            source = source.replace("cache-size=1000", "cache-size=0")
            cgitrc.parent.mkdir(parents=True, exist_ok=True)
            cgitrc.write_text(source)
        return users

    def enable_real_gitolite(self):
        binary = gitolite_command(self.root)
        if binary is None:
            self.fail("A Gitolite test command is required")
        access = self.root / "real-access"
        if access.exists():
            shutil.rmtree(access)
        (access / ".gitolite/conf").mkdir(parents=True)
        (access / ".gitolite/logs").mkdir()
        public = self.repos / "public.git"
        run("git", "init", "--bare", "-b", "main", str(public))
        rc = access / ".gitolite.rc"
        quoted = str(self.repos).replace("\\", "\\\\").replace("'", "\\'")
        rc.write_text(f"%RC = (GL_REPO_BASE => '{quoted}', UMASK => 0022);\n")
        (access / ".gitolite/conf/gitolite.conf").write_text(
            "repo public\n"
            "    R = anonymous reader writer owner\n"
            "    RW+ = owner\n"
            "repo group/demo\n"
            "    R = reader writer owner\n"
            "    RW = writer\n"
            "    RW+ = owner\n")
        env = {**ENV, "HOME": str(access), "LANG": "C", "TZ": "UTC"}
        result = subprocess.run([str(binary), "compile"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        users = {
            "owner": {"enabled": True, "owner": True,
                      "secret_hash": hash_secret(b"owner-access-secret")},
            "reader": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"reader-access-secret")},
            "writer": {"enabled": True, "owner": False,
                       "secret_hash": hash_secret(b"writer-access-secret")},
        }
        (access / "maintenance").write_text("test\n")
        publish_snapshot(access, self.repos, users)
        (access / "maintenance").unlink()
        self.cfg.update(
            auth_mode="access", username="", password_file="", http_push=True,
            access={"root": str(access), "gitolite": (
                "/usr/local/bin/gitolite" if self.driver == "docker" else str(binary))},
        )
        self.save_config()
        return users

    def request(self, path, data=None, method="GET", auth=False, headers=None):
        values = dict(headers or {})
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            values["Content-Type"] = "application/vnd.git-lfs+json"
        if auth:
            values["Authorization"] = AUTH
        request = Request(self.base + path, data=data, method=method, headers=values)
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def put_object(self, data=b"fixture contents", name="group/demo.git"):
        oid = hashlib.sha256(data).hexdigest()
        path = f"/{name}/info/lfs/objects/{oid}"
        response = self.request(path + f"?size={len(data)}", data, "PUT", auth=True)
        self.assertEqual(response[0], 200, response)
        return oid, path

    def test_lfs_batch_upload_download_verify(self):
        data = b"a large synthetic file\n" * 10000
        oid = hashlib.sha256(data).hexdigest()
        batch = {"operation": "upload", "objects": [{"oid": oid, "size": len(data)}]}
        status, _, body = self.request("/group/demo.git/info/lfs/objects/batch", batch, "POST", auth=True)
        self.assertEqual(status, 200, body)
        actions = json.loads(body)["objects"][0]["actions"]
        self.assertIn("verify", actions)
        oid, path = self.put_object(data)
        self.assertEqual(self.request(path)[2], data)
        self.assertEqual(self.request(path + "/verify", {"oid": oid, "size": len(data)}, "POST", True)[0], 200)
        body = self.request("/group/demo.git/info/lfs/objects/batch", batch, "POST", auth=True)[2]
        self.assertNotIn("actions", json.loads(body)["objects"][0])

    def test_upload_rejects_hash_size_and_anonymous(self):
        oid = hashlib.sha256(b"valid").hexdigest()
        path = f"/group/demo/info/lfs/objects/{oid}"
        self.assertEqual(self.request(path + "?size=5", b"valid", "PUT")[0], 401)
        self.assertEqual(self.request(path + "?size=4", b"valid", "PUT", True)[0], 422)
        self.assertEqual(self.request(path + "?size=5", b"wrong", "PUT", True)[0], 422)
        self.assertEqual(self.request(path)[0], 404)
        self.assertFalse(list(self.lfs.rglob(".upload-*")))

    def test_concurrent_uploads_and_empty_object(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.put_object(b"same object"), range(4)))
        self.assertEqual(self.request(results[0][1])[2], b"same object")
        self.assertEqual(self.request(self.put_object(b"")[1])[2], b"")

    def test_ranges_head_and_filename(self):
        _, path = self.put_object(b"0123456789")
        status, headers, body = self.request(path, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body, headers["Content-Range"]), (206, b"2345", "bytes 2-5/10"))
        self.assertEqual(self.request(path, headers={"Range": "bytes=-3"})[2], b"789")
        self.assertEqual(self.request(path, headers={"Range": "bytes=20-"})[0], 416)
        self.assertEqual(self.request(path, headers={"Range": "bytes=-0"})[0], 416)
        self.assertEqual(self.request(path, headers={"Range": "bytes=2-5", "If-Range": '"stale"'})[2], b"0123456789")
        self.assertEqual(self.request(path, method="HEAD")[2], b"")
        self.assertIn("attachment", self.request(path + "?filename=test.svg&inline=1")[1]["Content-Disposition"])

    def test_batch_missing_oversize_and_invalid_payload(self):
        payload = {"operation": "download", "objects": [{"oid": "a" * 64, "size": 2}]}
        response = self.request("/group/demo/info/lfs/objects/batch", payload, "POST")
        self.assertEqual(json.loads(response[2])["objects"][0]["error"]["code"], 404)
        payload["objects"][0]["size"] = self.cfg["max_size"] + 1
        self.assertEqual(self.request("/group/demo/info/lfs/objects/batch", payload, "POST")[0], 413)
        payload["objects"][0]["size"] = True
        self.assertEqual(self.request("/group/demo/info/lfs/objects/batch", payload, "POST")[0], 422)
        self.assertEqual(self.request("/group/demo/info/lfs/objects/batch", b"bad", "POST",
                                      headers={"Content-Type": "application/json"})[0], 400)

    def test_private_auth_covers_git_lfs_cgit(self):
        self.cfg["auth_mode"] = "private"
        self.save_config()
        for path in ("/", "/group/demo/tree/", "/cgit.cgi/group/demo/", "/gateway.cgi/group/demo/",
                     "/group/demo/info/refs?service=git-upload-pack", "/group/demo.git/info/lfs/objects/" + "a" * 64):
            self.assertEqual(self.request(path)[0], 401, path)
        self.assertEqual(self.request("/group/demo/info/refs?service=git-upload-pack", auth=True)[0], 200)
        self.assertEqual(self.request("/group/demo/info/refs?service=git-upload-pack",
                                      headers={"Authorization": "Basic !!!"})[0], 401)

    def test_access_mode_identity_git_and_lfs_matrix(self):
        self.enable_access()
        reader = {"Authorization": basic_auth("reader", "reader-access-secret")}
        writer = {"Authorization": basic_auth("writer", "writer-access-secret")}
        disabled = {"Authorization": basic_auth("disabled", "disabled-access-secret")}
        private_git = "/group/demo/info/refs?service=git-upload-pack"
        self.assertEqual(self.request("/public/info/refs?service=git-upload-pack")[0], 200)
        self.assertEqual(self.request(private_git)[0], 401)
        self.assertEqual(self.request(private_git, headers=reader)[0], 200)
        self.assertEqual(self.request(private_git, headers=writer)[0], 200)
        self.assertEqual(self.request(private_git, headers=disabled)[0], 401)
        self.assertEqual(self.request(
            private_git, headers={"Authorization": basic_auth("reader", "wrong-credential")})[0], 401)

        data = b"writer-owned lfs object"
        oid = hashlib.sha256(data).hexdigest()
        path = f"/group/demo.git/info/lfs/objects/{oid}"
        self.assertEqual(self.request(path + f"?size={len(data)}", data, "PUT", headers=writer)[0], 200)
        self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.request(path, headers=reader)[2], data)
        self.assertEqual(self.request(path, headers=disabled)[0], 401)
        other = b"reader cannot upload"
        other_oid = hashlib.sha256(other).hexdigest()
        other_path = f"/group/demo.git/info/lfs/objects/{other_oid}"
        self.assertEqual(self.request(
            other_path + f"?size={len(other)}", other, "PUT", headers=reader)[0], 404)

        batch = {"operation": "download", "objects": [{"oid": oid, "size": len(data)}]}
        status, _, body = self.request(
            "/group/demo.git/info/lfs/objects/batch", batch, "POST", headers=reader)
        self.assertEqual(status, 200, body)
        self.assertIn("download", json.loads(body)["objects"][0]["actions"])
        self.assertEqual(self.request(
            "/group/demo.git/info/lfs/objects/batch", batch, "POST")[0], 401)

    def test_access_mode_web_visibility_and_direct_urls(self):
        if self.driver == "cgi":
            self.skipTest("Real lighttpd/cgit required for web ACL assertions")
        self.enable_access()
        reader = {"Authorization": basic_auth("reader", "reader-access-secret")}
        status, _, public_index = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"href='/public/'", public_index)
        self.assertNotIn(b"href='/group/demo/'", public_index)
        status, _, reader_index = self.request("/", headers=reader)
        self.assertEqual(status, 200)
        self.assertIn(b"href='/public/'", reader_index)
        self.assertIn(b"href='/group/demo/'", reader_index)
        self.assertEqual(self.request("/group/demo/")[0], 401)
        self.assertEqual(self.request("/group/demo/", headers=reader)[0], 200)
        self.assertEqual(self.request("/public/")[0], 200)
        for path in ("/?r=group/demo&p=summary", "/?url=group/demo/tree/"):
            self.assertEqual(self.request(path)[0], 401, path)
            self.assertEqual(self.request(path, headers=reader)[0], 200, path)

    def test_lfs_bearer_is_repository_operation_and_surface_scoped(self):
        self.enable_access()
        generation = json.loads((self.root / "access/identity.json").read_text())["generation"]
        data = b"bearer scoped object"
        oid = hashlib.sha256(data).hexdigest()
        path = f"/group/demo.git/info/lfs/objects/{oid}"
        writer = {"Authorization": basic_auth("writer", "writer-access-secret")}
        self.assertEqual(self.request(path + f"?size={len(data)}", data, "PUT",
                                      headers=writer)[0], 200)
        download, _ = issue(self.cfg, "reader", "group/demo", "download", generation)
        bearer = {"Authorization": "Bearer " + download}
        self.assertEqual(self.request(path, headers=bearer)[2], data)
        self.assertEqual(self.request("/public/info/refs?service=git-upload-pack",
                                      headers=bearer)[0], 401)
        if self.driver != "cgi" or Path(self.cfg["cgit"]).exists():
            self.assertEqual(self.request("/public/", headers=bearer)[0], 401)
        upload_data = b"bearer upload"
        upload_oid = hashlib.sha256(upload_data).hexdigest()
        upload_path = f"/group/demo.git/info/lfs/objects/{upload_oid}"
        upload, _ = issue(self.cfg, "writer", "group/demo", "upload", generation)
        self.assertEqual(self.request(
            upload_path + f"?size={len(upload_data)}", upload_data, "PUT",
            headers={"Authorization": "Bearer " + upload})[0], 200)
        self.assertEqual(self.request(
            upload_path + f"?size={len(upload_data)}", upload_data, "PUT",
            headers={"Authorization": "Bearer " + download})[0], 401)
        version, body, signature = download.split(".")
        tampered = ".".join(
            (version, ("A" if body[0] != "A" else "B") + body[1:], signature))
        self.assertEqual(self.request(path, headers={
            "Authorization": "Bearer " + tampered})[0], 401)

    def test_access_login_challenge_and_authenticated_home(self):
        self.enable_access()
        status, headers, _ = self.request("/_login")
        self.assertEqual(status, 401)
        self.assertIn('Basic realm="cgit"', headers["WWW-Authenticate"])
        owner = {"Authorization": basic_auth("owner", "owner-access-secret")}
        from urllib.request import build_opener, HTTPRedirectHandler

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with self.assertRaises(HTTPError) as redirected:
            build_opener(NoRedirect).open(Request(self.base + "/_login", headers=owner))
        self.assertEqual(redirected.exception.code, 303)
        self.assertEqual(redirected.exception.headers["Location"], "/")
        redirected.exception.close()
        self.assertEqual(self.request("/_login", method="POST", headers=owner)[0], 405)
        self.assertEqual(self.request("/_login", headers={
            "Authorization": basic_auth("owner", "wrong")})[0], 401)

    def test_real_gitolite_http_hook_enforces_ref_policy(self):
        if not gitolite_command(self.root):
            self.skipTest("CGIT_TEST_GITOLITE is required for real update-hook verification")
        self.enable_real_gitolite()
        writer = {"Authorization": basic_auth("writer", "writer-access-secret")}
        reader = {"Authorization": basic_auth("reader", "reader-access-secret")}
        receive = "/group/demo/info/refs?service=git-receive-pack"
        self.assertEqual(self.request(receive, headers=writer)[0], 200)
        self.assertEqual(self.request(receive, headers=reader)[0], 404)

        client = self.root / "access-client"
        if client.exists():
            shutil.rmtree(client)
        writer_helper = (
            "!f() { echo username=writer; "
            "echo password=writer-access-secret; }; f")
        owner_helper = (
            "!f() { echo username=owner; "
            "echo password=owner-access-secret; }; f")
        reader_helper = (
            "!f() { echo username=reader; "
            "echo password=reader-access-secret; }; f")
        run("git", "-c", f"credential.helper={writer_helper}",
            "clone", self.base + "/group/demo", str(client))
        (client / "writer.txt").write_text("normal write\n")
        run("git", "add", ".", cwd=client)
        run("git", "commit", "-m", "Writer normal push", cwd=client)
        run("git", "-c", f"credential.helper={writer_helper}",
            "push", "origin", "HEAD", cwd=client)

        run("git", "lfs", "install", "--local", cwd=client)
        run("git", "lfs", "track", "*.bin", cwd=client)
        payload = b"access-controlled LFS payload\n" * 1024
        (client / "asset.bin").write_bytes(payload)
        run("git", "add", ".", cwd=client)
        run("git", "commit", "-m", "Writer LFS push", cwd=client)
        run("git", "-c", f"credential.helper={writer_helper}",
            "push", "origin", "HEAD", cwd=client)
        reader_client = self.root / "access-lfs-reader"
        run("git", "-c", f"credential.helper={reader_helper}",
            "-c", "filter.lfs.process=git-lfs filter-process",
            "-c", "filter.lfs.required=true",
            "clone", self.base + "/group/demo", str(reader_client))
        self.assertEqual((reader_client / "asset.bin").read_bytes(), payload)

        run("git", "reset", "--hard", "HEAD~1", cwd=client)
        (client / "divergent.txt").write_text("force attempt\n")
        run("git", "add", ".", cwd=client)
        run("git", "commit", "-m", "Writer force attempt", cwd=client)
        denied = run("git", "-c", f"credential.helper={writer_helper}",
                     "push", "--force", "origin", "HEAD", cwd=client, check=False)
        self.assertNotEqual(denied.returncode, 0, denied.stderr)
        self.assertIn(b"remote rejected", denied.stderr)
        run("git", "-c", f"credential.helper={owner_helper}",
            "push", "--force", "origin", "HEAD", cwd=client)

    def test_no_account_is_read_only_and_push_switch_cannot_be_overridden(self):
        self.cfg["username"] = ""
        self.cfg["password_file"] = ""
        self.save_config()
        data = {"operation": "upload", "objects": []}
        self.assertEqual(self.request("/group/demo/info/lfs/objects/batch", data, "POST")[0], 403)
        self.cfg["http_push"] = False
        self.cfg["username"] = "owner"
        self.cfg["password_file"] = self.baseline["password_file"]
        self.save_config()
        run("git", "--git-dir", str(self.bare), "config", "http.receivepack", "true")
        self.assertEqual(self.request("/group/demo/info/refs?service=git-receive-pack", auth=True)[0], 403)
        self.assertEqual(self.request("/group/demo/git-receive-pack", b"0000", "POST", True)[0], 403)

    def test_repository_isolation_traversal_symlinks_and_ignored_repo(self):
        oid, _ = self.put_object()
        other = self.repos / "other.git"
        run("git", "init", "--bare", str(other))
        self.assertEqual(self.request(f"/other/info/lfs/objects/{oid}")[0], 404)
        self.assertEqual(self.request(f"/missing/info/lfs/objects/{oid}")[0], 404)
        self.assertEqual(self.request(f"/group/%2e%2e/demo/info/lfs/objects/{oid}")[0], 404)
        (self.repos / "link.git").symlink_to(self.bare)
        self.assertEqual(self.request("/link/info/refs?service=git-upload-pack")[0], 404)
        run("git", "--git-dir", str(self.bare), "config", "cgit.ignore", "1")
        self.assertEqual(self.request(f"/group/demo/info/lfs/objects/{oid}")[0], 404)

    def test_excluded_repositories_block_git_lfs_and_browser(self):
        oid, _ = self.put_object()
        paths = ("/group/demo/info/refs?service=git-upload-pack",
                 f"/group/demo/info/lfs/objects/{oid}")
        if self.driver != "cgi" or Path(self.cfg["cgit"]).exists():
            paths += ("/group/demo/", "/?r=group/demo&p=summary", "/?url=group/demo/tree/")
        for rule in ("ignore", "noweb", "local-ignore"):
            with self.subTest(rule=rule):
                if rule == "ignore":
                    run("git", "--git-dir", str(self.bare), "config", "cgit.ignore", "1")
                else:
                    (self.bare / ("noweb" if rule == "noweb" else "cgitrc")).write_text(
                        "" if rule == "noweb" else "ignore=1\n")
                for path in paths:
                    status, _, body = self.request(path)
                    # cgit v1.3.1's legacy r/p form reports "No repository
                    # selected" (400); virtual URLs report Not found (404).
                    self.assertEqual(status, 400 if path.startswith("/?r=") else 404, (rule, path))
                    self.assertNotIn(b"<h1>Demo", body)
                if rule == "ignore":
                    run("git", "--git-dir", str(self.bare), "config", "--unset", "cgit.ignore")
                else:
                    (self.bare / ("noweb" if rule == "noweb" else "cgitrc")).unlink()

    def test_invalid_ignore_fails_closed_before_index_and_git(self):
        run("git", "--git-dir", str(self.bare), "config", "cgit.ignore", "true")
        for path in ("/", "/group/demo/", "/?r=group/demo&p=summary",
                     "/group/demo/info/refs?service=git-upload-pack",
                     "/group/demo/info/lfs/objects/" + "a" * 64):
            self.assertEqual(self.request(path)[0], 500, path)

    def test_doctor_checks_unpublished_repositories(self):
        run("git", "--git-dir", str(self.bare), "config", "cgit.ignore", "1")
        if self.driver == "docker":
            command = ["docker", "exec", self.container, "cgit-doctor", "--git"]
        else:
            command = [sys.executable, str(ROOT / "runtime/doctor.py"), "--git"]
        result = subprocess.run(command, env={**ENV, "CGIT_RUNTIME_CONFIG": str(self.settings)},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("group/demo.git", json.loads(result.stdout)["repositories"])

    def test_project_list_and_export_marker_cover_all_protocols(self):
        oid, _ = self.put_object()
        projects = self.cache / "projects"
        projects.write_text("other.git\n")
        # Container-visible paths differ from host paths in the Docker driver.
        visible_projects = str(Path(self.cfg["cgit_config"]).parent.parent / "projects")
        self.cfg["repository_policy"] = {"project_list": visible_projects, "strict_export": "export-ok"}
        self.save_config()
        cgitrc = self.cache / "runtime/cgitrc"
        original = cgitrc.read_text() if cgitrc.exists() else None
        if original is not None:
            cgitrc.write_text(original.replace("scan-path=", f"project-list={visible_projects}\nstrict-export=export-ok\nscan-path="))
        paths = ["/group/demo/info/refs?service=git-upload-pack",
                 f"/group/demo/info/lfs/objects/{oid}"]
        if self.driver != "cgi" or Path(self.cfg["cgit"]).exists():
            paths += ["/group/demo/", "/?r=group/demo&p=summary", "/?url=group/demo/tree/"]
        try:
            for stage in ("unlisted", "no-marker", "allowed"):
                if stage == "no-marker":
                    projects.write_text("group\n")
                elif stage == "allowed":
                    (self.bare / "export-ok").touch()
                for path in paths:
                    expected = 200 if stage == "allowed" else 400 if path.startswith("/?r=") else 404
                    self.assertEqual(self.request(path)[0], expected,
                                     (stage, path))
        finally:
            if original is not None:
                cgitrc.write_text(original)

    def test_hidden_repo_remains_directly_accessible(self):
        run("git", "--git-dir", str(self.bare), "config", "cgit.hide", "1")
        oid, _ = self.put_object()
        self.assertEqual(self.request("/group/demo/info/refs?service=git-upload-pack")[0], 200)
        self.assertEqual(self.request(f"/group/demo/info/lfs/objects/{oid}")[0], 200)
        if self.driver != "cgi" or Path(self.cfg["cgit"]).exists():
            self.assertEqual(self.request("/group/demo/")[0], 200)
            self.assertNotIn(b"href='/group/demo/'", self.request("/")[2])

    def test_symlink_repo_blocks_browser_scan(self):
        (self.repos / "link.git").symlink_to(self.bare)
        self.assertEqual(self.request("/")[0], 500)

    def test_external_url_ignores_proxy_headers(self):
        self.cfg["base_url"] = "https://git.example.test"
        self.save_config()
        payload = {"operation": "upload", "objects": [{"oid": "a" * 64, "size": 1}]}
        body = self.request("/group/demo/info/lfs/objects/batch", payload, "POST", True,
                            {"X-Forwarded-Host": "evil.test", "X-Forwarded-Proto": "http"})[2]
        self.assertTrue(json.loads(body)["objects"][0]["actions"]["upload"]["href"].startswith("https://git.example.test/"))

    def test_real_git_shallow_clone_fetch_push(self):
        clone = self.root / "clone"
        shutil.rmtree(clone, ignore_errors=True)
        run("git", "clone", "--depth=1", self.remote, str(clone))
        self.assertTrue((clone / ".git/shallow").exists())
        (self.work / "hello.py").write_text('print("updated")\n')
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Update", cwd=self.work)
        run("git", "push", "origin", "HEAD", cwd=self.work)
        run("git", "pull", "--ff-only", cwd=clone)
        self.assertIn("updated", (clone / "hello.py").read_text())
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=clone).stdout,
                         run("git", "rev-parse", "HEAD", cwd=self.work).stdout)

    def test_doctor_detects_missing_and_corrupt_lfs(self):
        if self.driver == "docker":
            command = ["docker", "exec", self.container, "cgit-doctor", "--git", "--lfs"]
        else:
            command = [sys.executable, str(ROOT / "runtime/doctor.py"), "--git", "--lfs"]
        oid, _ = self.put_object(b"valid")
        (self.work / "large.bin").write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 5\n")
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Pointer", cwd=self.work)
        run("git", "push", str(self.bare), "HEAD", cwd=self.work)
        def diagnose():
            result = subprocess.run(command, env={**ENV, "CGIT_RUNTIME_CONFIG": str(self.settings)},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue(result.stdout, result.stderr)
            return result.returncode, json.loads(result.stdout)
        self.assertTrue(diagnose()[1]["ok"])
        path = object_path({**self.cfg, "lfs": str(self.lfs)}, "group/demo.git", oid)
        path.write_bytes(b"wrong")
        self.assertFalse(diagnose()[1]["ok"])
        path.unlink()
        self.assertFalse(diagnose()[1]["ok"])

    def test_real_private_lfs_clone(self):
        self.cfg["auth_mode"] = "private"
        self.save_config()
        run("git", "lfs", "install", "--local", cwd=self.work)
        run("git", "lfs", "track", "*.bin", cwd=self.work)
        (self.work / "private.bin").write_bytes(b"synthetic private object")
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Private LFS", cwd=self.work)
        run("git", "push", "origin", "HEAD", cwd=self.work)
        clone = self.root / "private-clone"
        shutil.rmtree(clone, ignore_errors=True)
        run("git", "clone", "-c", "filter.lfs.process=git-lfs filter-process",
            "-c", "filter.lfs.required=true", "-c", "credential.helper=!f() { echo username=owner; echo password=fixture-password; }; f",
            self.remote, str(clone))
        self.assertEqual((clone / "private.bin").read_bytes(), b"synthetic private object")

    def test_real_lfs_client_round_trip(self):
        run("git", "lfs", "version")
        run("git", "lfs", "install", "--local", cwd=self.work)
        run("git", "lfs", "track", "*.bin", cwd=self.work)
        data = b"synthetic lfs data\x00" * 100000
        (self.work / "asset.bin").write_bytes(data)
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Add LFS asset", cwd=self.work)
        run("git", "push", "origin", "HEAD", cwd=self.work)
        clone = self.root / "lfs-clone"
        shutil.rmtree(clone, ignore_errors=True)
        run("git", "clone", "-c", "filter.lfs.process=git-lfs filter-process",
            "-c", "filter.lfs.required=true", self.remote, str(clone))
        self.assertEqual((clone / "asset.bin").read_bytes(), data)
        run("git", "lfs", "fsck", cwd=clone)
        self.assertEqual(self.request("/group/demo/plain/asset.bin")[2], data)
        oid = hashlib.sha256(data).hexdigest()
        (self.lfs / f"group/demo.git/objects/{oid[:2]}/{oid[2:4]}/{oid}").unlink()
        self.assertEqual(self.request("/group/demo/plain/asset.bin")[0], 404)

    def test_web_gzip_and_lfs_range_download(self):
        if self.driver == "cgi":
            self.skipTest("Compression is provided by lighttpd; use lighttpd or docker driver")
        for path in ("/group/demo/", "/group/demo/about/", "/group/demo/log/", "/group/demo/tree/hello.py"):
            status, headers, body = self.request(path, headers={"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("Content-Encoding"), "gzip", path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)
            self.assertIn(b"<div id='cgit'>", gzip.decompress(body), path)
        data = b"synthetic binary download\x00" * 10000
        _, path = self.put_object(data)
        for path in (path, "/group/demo/plain/large.bin"):
            if "/plain/" in path:
                oid = hashlib.sha256(data).hexdigest()
                (self.work / "large.bin").write_text(
                    f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n")
                run("git", "add", ".", cwd=self.work)
                run("git", "commit", "-m", "Large pointer", cwd=self.work)
                run("git", "push", str(self.bare), "HEAD", cwd=self.work)
            status, headers, body = self.request(path, headers={"Accept-Encoding": "gzip", "Range": "bytes=100-999"})
            self.assertEqual(status, 206, path)
            self.assertIsNone(headers.get("Content-Encoding"), path)
            self.assertEqual(body, data[100:1000], path)

    def test_cgit_browsing_filters_archives_and_dumb_http(self):
        if self.driver == "cgi" and not Path(self.cfg["cgit"]).exists():
            self.skipTest("Requires CGIT_TEST_DRIVER=lighttpd or docker")
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        oid, _ = self.put_object(png)
        (self.work / "picture.png").write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(png)}\n")
        run("git", "add", ".", cwd=self.work)
        run("git", "commit", "-m", "Picture", cwd=self.work)
        run("git", "push", str(self.bare), "HEAD", cwd=self.work)
        for path in ("/", "/group/demo/", "/group/demo/log/", "/group/demo/atom/", "/group/demo/patch/",
                     "/group/demo/snapshot/demo-main.tar.gz", "/group/demo/info/refs", "/group/demo/HEAD"):
            response = self.request(path)
            self.assertEqual(response[0], 200, (path, response[2][:500]))
        about = self.request("/group/demo/about/")[2].decode()
        self.assertEqual(self.request("/group/demo/about/")[1].get("Cache-Control"), "no-store")
        self.assertIn("<h1", about)
        self.assertIn("/group/demo/tree/hello.py?id=", about)
        self.assertIn("/group/demo/plain/picture.png?id=", about)
        self.assertIn(b'class="highlight"', self.request("/group/demo/tree/hello.py")[2])
        card = self.request("/group/demo/tree/picture.png")[2]
        self.assertIn(b"Download original", card)
        self.assertIn(b"snapshots contain", card)
        self.assertEqual(self.request("/group/demo/plain/picture.png")[2], png)
        archive = self.request("/group/demo/snapshot/demo-main.tar.gz")[2]
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            member = next(item for item in tar.getmembers() if item.name.endswith("/picture.png"))
            self.assertTrue(tar.extractfile(member).read().startswith(b"version https://git-lfs.github.com/spec/v1"))
        cfg_local = {**self.cfg, "lfs": str(self.lfs)}
        object_path(cfg_local, "group/demo.git", oid).unlink()
        self.assertIn(b"object missing", self.request("/group/demo/tree/picture.png")[2])


class CommonTests(unittest.TestCase):
    def test_doctor_configuration_failure_is_json(self):
        result = subprocess.run([sys.executable, str(ROOT / "runtime/doctor.py")],
                                env={**ENV, "CGIT_RUNTIME_CONFIG": "/missing/cgit-test-config.json"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_pointer_strictness(self):
        oid = "f" * 64
        self.assertEqual(pointer(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize 0\n".encode()), (oid, 0))
        self.assertIsNone(pointer(b"not a pointer"))

    def test_path_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            for value in ("../escape", "/absolute", "a//b", "a/../b", "a\\b", ".hidden/repo"):
                with self.assertRaises(RequestError):
                    safe_path(directory, value)


if __name__ == "__main__":
    unittest.main()
