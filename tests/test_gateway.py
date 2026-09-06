"""Protocol and data-boundary tests; all repositories/credentials are synthetic.

python3 -m unittest discover -s tests -v
CGIT_TEST_DRIVER=docker CGIT_TEST_IMAGE=cgit:test python3 -m unittest discover -s tests -v
The Docker driver exercises the exact same client flows through real lighttpd.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
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
from common import RequestError, object_path, pointer, safe_path

AUTH = "Basic " + base64.b64encode(b"owner:fixture-password").decode()
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@localhost",
       "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@localhost"}


def run(*args, cwd=None, check=True, **kwargs):
    result = subprocess.run(args, cwd=cwd, env=ENV, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    if check and result.returncode:
        raise AssertionError(f"{args[:3]} failed: {result.stderr.decode(errors='replace')}")
    return result


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
                "-v", f"{cls.password}:/run/password:ro", image)
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
        run("git", "--git-dir", str(self.bare), "config", "cgit.ignore", "true")
        self.assertEqual(self.request(f"/group/demo/info/lfs/objects/{oid}")[0], 404)

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
