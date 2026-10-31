"""Public Git/LFS/HTTP contracts. No imports from the implementation under test."""

from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import unittest

from contract_support import ENV, HELPER, SECRET, Service, run


class PublicProtocol(unittest.TestCase):
    def setUp(self):
        self.service = Service()
        self.addCleanup(self.service.close)

    def upload(self, data=b"contract object"):
        oid = hashlib.sha256(data).hexdigest()
        path = f"/group/demo.git/info/lfs/objects/{oid}"
        status, _, body = self.service.request(path + f"?size={len(data)}", data, "PUT", auth=True)
        self.assertEqual(status, 200, body)
        return oid, path

    def test_lfs_free_space_admission_rejects_without_publishing(self):
        service = self.service
        settings = json.loads(service.settings.read_text())
        settings["lfs_min_free_bytes"] = 1 << 50
        service.settings.write_text(json.dumps(settings))
        data = b"storage admission fixture"
        oid = hashlib.sha256(data).hexdigest()
        path = f"/group/demo.git/info/lfs/objects/{oid}?size={len(data)}"
        status, _, _ = service.request(path, data, "PUT", auth=True)
        self.assertEqual(status, 507)
        self.assertFalse(list(service.lfs.rglob(oid)))
        self.assertFalse(list(service.lfs.rglob(".upload-*")))
        settings["lfs_min_free_bytes"] = 0
        service.settings.write_text(json.dumps(settings))
        self.upload(data)

    def test_smart_git_clone_fetch_and_authenticated_push(self):
        s = self.service
        status, headers, body = s.request("/group/demo/info/refs?service=git-upload-pack")
        self.assertEqual(status, 200)
        self.assertIn("application/x-git-upload-pack-advertisement", headers["Content-Type"])
        self.assertIn(b"# service=git-upload-pack", body)
        clone = s.root / "client"
        run("git", "-c", f"credential.helper={HELPER}", "clone", "--depth=1", s.remote, str(clone))
        self.assertEqual((clone / "hello.py").read_bytes(), b'print("contract")\n')
        self.assertEqual(run("git", "rev-parse", "--is-shallow-repository", cwd=clone).stdout.strip(), b"true")
        (s.work / "next.txt").write_text("next revision\n")
        s.commit("Next contract revision")
        run("git", "fetch", cwd=clone)
        run("git", "reset", "--hard", "origin/main", cwd=clone)
        self.assertEqual((clone / "next.txt").read_text(), "next revision\n")
        run("git", "config", "credential.helper", HELPER, cwd=clone)
        (clone / "client.txt").write_text("client push\n")
        run("git", "add", ".", cwd=clone)
        run("git", "commit", "-m", "Client push", cwd=clone)
        run("git", "push", "origin", "HEAD", cwd=clone)
        verify = s.root / "verify"
        run("git", "clone", s.remote, str(verify))
        self.assertEqual((verify / "client.txt").read_text(), "client push\n")

    def test_lfs_batch_hash_range_head_and_verify(self):
        s = self.service
        data = b"0123456789"
        oid = hashlib.sha256(data).hexdigest()
        batch = {"operation": "upload", "objects": [{"oid": oid, "size": len(data)}]}
        status, _, response = s.request("/group/demo.git/info/lfs/objects/batch", batch, "POST", True)
        self.assertEqual(status, 200, response)
        actions = json.loads(response)["objects"][0]["actions"]
        self.assertIn("upload", actions)
        self.assertTrue(actions["upload"]["href"].startswith(s.base + "/group/demo.git/"))
        _, path = self.upload(data)
        self.assertEqual(s.request(path)[2], data)
        status, headers, body = s.request(path, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, headers["Content-Range"], body), (206, "bytes 2-5/10", b"2345"))
        status, headers, body = s.request(path, method="HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, "10", b""))
        self.assertEqual(s.request(path, headers={"Range": "bytes=100-"})[0], 416)
        self.assertEqual(s.request(path + "/verify", {"oid": oid, "size": len(data)}, "POST", True)[0], 200)
        wrong = hashlib.sha256(b"good").hexdigest()
        target = f"/group/demo.git/info/lfs/objects/{wrong}"
        self.assertEqual(s.request(target + "?size=4", b"evil", "PUT", True)[0], 422)
        self.assertEqual(s.request(target)[0], 404)

    def test_anonymous_write_and_repository_isolation(self):
        s = self.service
        oid, path = self.upload()
        self.assertEqual(s.request(path + "?size=15", b"contract object", "PUT")[0], 401)
        self.assertEqual(s.request("/group/demo/info/refs?service=git-receive-pack")[0], 401)
        other = s.repos / "other.git"
        run("git", "init", "--bare", "-b", "main", str(other))
        self.assertEqual(s.request(f"/other/info/lfs/objects/{oid}")[0], 404)
        self.assertEqual(s.request(f"/group/%2e%2e/demo/info/lfs/objects/{oid}")[0], 404)
        self.assertEqual(s.request("/missing/info/refs?service=git-upload-pack")[0], 404)

    def test_exclusion_rules_block_git_and_lfs(self):
        s = self.service
        oid, _ = self.upload()
        for rule in ("git-ignore", "noweb", "local-ignore"):
            with self.subTest(rule=rule):
                if rule == "git-ignore":
                    run("git", "--git-dir", str(s.repo), "config", "cgit.ignore", "1")
                else:
                    (s.repo / ("noweb" if rule == "noweb" else "cgitrc")).write_text(
                        "" if rule == "noweb" else "ignore=1\n")
                try:
                    self.assertEqual(s.request("/group/demo/info/refs?service=git-upload-pack")[0], 404)
                    self.assertEqual(s.request(f"/group/demo.git/info/lfs/objects/{oid}")[0], 404)
                finally:
                    if rule == "git-ignore":
                        run("git", "--git-dir", str(s.repo), "config", "--unset", "cgit.ignore")
                    else:
                        (s.repo / ("noweb" if rule == "noweb" else "cgitrc")).unlink()

    def test_concurrent_and_empty_lfs_objects(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            objects = list(pool.map(lambda _: self.upload(b"parallel"), range(4)))
        self.assertEqual(self.service.request(objects[0][1])[2], b"parallel")
        self.assertEqual(self.service.request(self.upload(b"")[1])[2], b"")

    def test_real_lfs_client_round_trip(self):
        s = self.service
        run("git", "lfs", "install", "--local", cwd=s.work)
        run("git", "lfs", "track", "*.bin", cwd=s.work)
        payload = b"independent LFS client payload\n" * 1000
        (s.work / "asset.bin").write_bytes(payload)
        run("git", "add", ".", cwd=s.work)
        run("git", "commit", "-m", "LFS contract payload", cwd=s.work)
        run("git", "push", "origin", "HEAD", cwd=s.work)
        clone = s.root / "lfs-client"
        run("git", "-c", f"credential.helper={HELPER}",
            "-c", "filter.lfs.process=git-lfs filter-process", "-c", "filter.lfs.required=true",
            "clone", s.remote, str(clone))
        self.assertEqual((clone / "asset.bin").read_bytes(), payload)
        self.assertIn(b"oid sha256:", run("git", "show", "HEAD:asset.bin", cwd=clone).stdout)


class PrivateProtocol(unittest.TestCase):
    def setUp(self):
        self.service = Service(private=True)
        self.addCleanup(self.service.close)

    def test_private_every_dynamic_read_requires_authentication(self):
        s = self.service
        paths = ("/", "/group/demo/", "/group/demo/tree/", "/?r=group/demo&p=summary",
                 "/?url=group/demo/tree/", "/group/demo/info/refs?service=git-upload-pack",
                 "/group/demo.git/info/lfs/objects/" + "a" * 64, "/_health")
        for path in paths:
            with self.subTest(path=path):
                status, headers, body = s.request(path)
                self.assertEqual(status, 401, path)
                self.assertIn("Basic", headers["WWW-Authenticate"])
                self.assertNotIn(b"Contract Demo", body)
                self.assertNotIn(SECRET.encode(), body)
        self.assertEqual(s.request(paths[5], auth=True)[0], 200)
        self.assertEqual(s.request(paths[5], headers={"Authorization": "Basic !!!"})[0], 401)
        clone = s.root / "private-client"
        run("git", "-c", f"credential.helper={HELPER}", "clone", s.remote, str(clone))
        self.assertEqual((clone / "README.md").read_text().splitlines()[0], "# Contract Demo")


class ReadOnlyProtocol(unittest.TestCase):
    def test_switch_blocks_receive_pack_even_with_repository_override(self):
        service = Service(push=False)
        self.addCleanup(service.close)
        run("git", "--git-dir", str(service.repo), "config", "http.receivepack", "true")
        for path, data, method in (("/group/demo/info/refs?service=git-receive-pack", None, "GET"),
                                   ("/group/demo/git-receive-pack", b"0000", "POST")):
            self.assertEqual(service.request(path, data, method, True)[0], 403)
        self.assertEqual(service.request("/group/demo/info/refs?service=git-upload-pack")[0], 200)


class WebProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get("CGIT_TEST_DRIVER", "cgi") == "cgi":
            raise unittest.SkipTest("Real cgit/lighttpd required; local CGI is partial protocol evidence")

    def setUp(self):
        self.service = Service()
        self.addCleanup(self.service.close)

    def test_browsing_filters_gzip_and_archive(self):
        s = self.service
        for path in ("/", "/group/demo/", "/group/demo/log/", "/group/demo/refs/",
                     "/group/demo/commit/", "/group/demo/diff/", "/group/demo/atom/"):
            self.assertEqual(s.request(path)[0], 200, path)
        status, headers, body = s.request("/group/demo/about/", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"Contract Demo", gzip.decompress(body))
        self.assertIn(b"/group/demo/tree/hello.py?id=", gzip.decompress(body))
        self.assertIn(b'class="highlight"', s.request("/group/demo/tree/hello.py")[2])
        data = b"LFS archive fixture"
        oid = hashlib.sha256(data).hexdigest()
        (s.work / "asset.bin").write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n")
        s.commit("Archive LFS pointer")
        archive = s.request("/group/demo/snapshot/demo-main.tar.gz")
        self.assertEqual(archive[0], 200)
        with tarfile.open(fileobj=io.BytesIO(archive[2]), mode="r:gz") as tar:
            item = next(v for v in tar.getmembers() if v.name.endswith("/asset.bin"))
            self.assertTrue(tar.extractfile(item).read().startswith(b"version https://git-lfs.github.com/spec/v1"))

    def test_excluded_repository_not_in_index_or_legacy_links(self):
        s = self.service
        for rule in ("git-ignore", "noweb", "local-ignore"):
            if rule == "git-ignore":
                run("git", "--git-dir", str(s.repo), "config", "cgit.ignore", "1")
            else:
                (s.repo / ("noweb" if rule == "noweb" else "cgitrc")).write_text(
                    "" if rule == "noweb" else "ignore=1\n")
            try:
                self.assertNotIn(b"href='/group/demo/'", s.request("/")[2])
                for path in ("/group/demo/", "/?r=group/demo&p=summary", "/?url=group/demo/tree/"):
                    status, _, body = s.request(path)
                    self.assertEqual(status, 400 if path.startswith("/?r=") else 404, (rule, path))
                    self.assertNotIn(b"Contract Demo", body)
            finally:
                if rule == "git-ignore":
                    run("git", "--git-dir", str(s.repo), "config", "--unset", "cgit.ignore")
                else:
                    (s.repo / ("noweb" if rule == "noweb" else "cgitrc")).unlink()
