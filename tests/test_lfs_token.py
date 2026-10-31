"""Scoped SSH-to-LFS bearer token tests."""

import tempfile
from pathlib import Path
from unittest.mock import patch
import unittest

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from lfs_token import ensure_secret, issue, verify


class LfsTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-lfs-token-")
        self.root = Path(self.temp.name).resolve()
        (self.root / "maintenance").touch()
        self.cfg = {"access": {"root": str(self.root)}}
        ensure_secret(self.root)
        (self.root / "maintenance").unlink()

    def tearDown(self):
        self.temp.cleanup()

    def test_scope_expiry_signature_and_generation(self):
        with patch("lfs_token.time.time", return_value=1_000):
            token, payload = issue(self.cfg, "reader", "group/demo", "download", "a" * 64)
        self.assertEqual(payload["exp"] - payload["iat"], 300)
        with patch("lfs_token.time.time", return_value=1_001):
            self.assertEqual(
                verify(self.cfg, token, "group/demo", "download", "a" * 64),
                "reader",
            )
            for repo, operation, generation in (
                ("other", "download", "a" * 64),
                ("group/demo", "upload", "a" * 64),
                ("group/demo", "download", "b" * 64),
            ):
                with self.subTest(repo=repo, operation=operation):
                    with self.assertRaises(ValueError):
                        verify(self.cfg, token, repo, operation, generation)
        with patch("lfs_token.time.time", return_value=1_301):
            with self.assertRaises(ValueError):
                verify(self.cfg, token, "group/demo", "download", "a" * 64)

    def test_tampered_token_and_missing_key_are_rejected(self):
        token, _ = issue(self.cfg, "reader", "group/demo", "download", "a" * 64)
        version, body, signature = token.split(".")
        invalid = ".".join((version, ("A" if body[0] != "A" else "B") + body[1:], signature))
        with self.assertRaises(ValueError):
            verify(self.cfg, invalid, "group/demo", "download", "a" * 64)
        (self.root / "lfs-token.key").unlink()
        with self.assertRaises(ValueError):
            verify(self.cfg, token, "group/demo", "download", "a" * 64)

    def test_key_creation_requires_maintenance_and_is_private(self):
        (self.root / "lfs-token.key").unlink()
        with self.assertRaises(ValueError):
            ensure_secret(self.root)
        (self.root / "maintenance").touch()
        ensure_secret(self.root)
        self.assertEqual((self.root / "lfs-token.key").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
