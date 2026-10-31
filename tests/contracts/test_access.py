"""Public ACL contracts through real HTTP, Git and Git LFS clients."""

import hashlib
import os
import unittest

from contract_support import ACCESS_HELPERS, Service, run


@unittest.skipUnless(
    (os.environ.get("CGIT_TEST_GITOLITE") or os.environ.get("CGIT_TEST_GITOLITE_IMAGE"))
    and os.environ.get("CGIT_TEST_DRIVER", "cgi") in ("lighttpd", "docker"),
    "Real cgit/lighttpd or Docker plus Gitolite are required for ACL contracts",
)
class AccessProtocol(unittest.TestCase):
    def setUp(self):
        self.service = Service(access=True)
        self.addCleanup(self.service.close)

    def test_identity_matrix_across_web_git_lfs_and_update_hook(self):
        service = self.service
        private_git = "/group/demo/info/refs?service=git-upload-pack"
        self.assertEqual(service.request("/public/info/refs?service=git-upload-pack")[0], 200)
        self.assertEqual(service.request(private_git)[0], 401)
        self.assertEqual(service.request(private_git, user="reader")[0], 200)
        self.assertEqual(service.request(private_git, user="writer")[0], 200)
        self.assertEqual(service.request(private_git, user="disabled")[0], 401)
        self.assertEqual(service.request(
            "/gitolite-admin/info/refs?service=git-upload-pack", user="reader")[0], 404)

        status, _, anonymous_index = service.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"href='/public/'", anonymous_index)
        self.assertNotIn(b"href='/group/demo/'", anonymous_index)
        _, _, reader_index = service.request("/", user="reader")
        self.assertIn(b"href='/public/'", reader_index)
        self.assertIn(b"href='/group/demo/'", reader_index)
        self.assertEqual(service.request("/group/demo/")[0], 401)
        self.assertEqual(service.request("/group/demo/", user="reader")[0], 200)
        self.assertEqual(service.request("/gitolite-admin/", user="reader")[0], 404)
        for path in ("/?r=group/demo&p=summary", "/?url=group/demo/tree/"):
            self.assertEqual(service.request(path)[0], 401, path)
            self.assertEqual(service.request(path, user="reader")[0], 200, path)

        payload = b"access contract LFS payload\n" * 1000
        oid = hashlib.sha256(payload).hexdigest()
        object_path = f"/group/demo.git/info/lfs/objects/{oid}"
        self.assertEqual(service.request(
            object_path + f"?size={len(payload)}", payload, "PUT")[0], 401)
        self.assertEqual(service.request(
            object_path + f"?size={len(payload)}", payload, "PUT", user="reader")[0], 404)
        self.assertEqual(service.request(
            object_path + f"?size={len(payload)}", payload, "PUT", user="writer")[0], 200)
        self.assertEqual(service.request(object_path)[0], 401)
        self.assertEqual(service.request(object_path, user="reader")[2], payload)

        reader_client = service.root / "reader-client"
        run("git", "-c", f"credential.helper={ACCESS_HELPERS['reader']}",
            "clone", service.remote, str(reader_client))
        writer_client = service.root / "writer-client"
        run("git", "-c", f"credential.helper={ACCESS_HELPERS['writer']}",
            "clone", service.remote, str(writer_client))
        run("git", "lfs", "install", "--local", cwd=writer_client)
        run("git", "lfs", "track", "*.bin", cwd=writer_client)
        (writer_client / "asset.bin").write_bytes(payload)
        run("git", "add", ".", cwd=writer_client)
        run("git", "commit", "-m", "Writer ACL/LFS commit", cwd=writer_client)
        run("git", "-c", f"credential.helper={ACCESS_HELPERS['writer']}",
            "push", "origin", "HEAD", cwd=writer_client)
        shuffled = service.root / "reader-lfs-client"
        run("git", "-c", f"credential.helper={ACCESS_HELPERS['reader']}",
            "-c", "filter.lfs.process=git-lfs filter-process",
            "-c", "filter.lfs.required=true",
            "clone", service.remote, str(shuffled))
        self.assertEqual((shuffled / "asset.bin").read_bytes(), payload)

        run("git", "reset", "--hard", "HEAD~1", cwd=writer_client)
        (writer_client / "divergent.txt").write_text("force attempt\n")
        run("git", "add", ".", cwd=writer_client)
        run("git", "commit", "-m", "Writer force attempt", cwd=writer_client)
        denied = run("git", "-c", f"credential.helper={ACCESS_HELPERS['writer']}",
                     "push", "--force", "origin", "HEAD", cwd=writer_client, check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn(b"remote rejected", denied.stderr)
        run("git", "-c", f"credential.helper={ACCESS_HELPERS['owner']}",
            "push", "--force", "origin", "HEAD", cwd=writer_client)


if __name__ == "__main__":
    unittest.main()
