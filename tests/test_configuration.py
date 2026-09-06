import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from configure import configure


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.template = self.root / "cgitrc"
        self.template.write_text("clone-url=http://old/$CGIT_REPO_URL\ncache-size=1000\nscan-path=/repos\n")
        self.password = self.root / "password"
        self.password.write_text("synthetic-password\n")
        self.env = {"CGIT_RUNTIME_DIR": str(self.root / "runtime")}

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, **values):
        with patch.dict(os.environ, {**self.env, **values}, clear=True):
            configure(source_path=self.template)
        return json.loads((self.root / "runtime/settings.json").read_text())

    def test_default_read_only(self):
        cfg = self.generate()
        self.assertFalse(cfg["http_push"])
        self.assertEqual(cfg["username"], "")
        self.assertNotIn("password", cfg)

    def test_url_and_ssh_inherited_before_scan(self):
        cfg = self.generate(CGIT_BASE_URL="https://git.example.test/",
                            CGIT_SSH_CLONE_URL="git@nas:/repos/$CGIT_REPO_URL.git")
        text = Path(cfg["cgit_config"]).read_text()
        self.assertIn("clone-url=https://git.example.test/$CGIT_REPO_URL git@nas:/repos/$CGIT_REPO_URL.git\n", text)
        self.assertLess(text.index("clone-url="), text.index("scan-path="))
        self.assertEqual(text.count("clone-url="), 1)
        self.assertNotIn("cache-size=1000", text)

    def test_fail_closed_invalid_config(self):
        for values in ({"CGIT_AUTH_MODE": "private"}, {"CGIT_HTTP_PUSH": "1"},
                       {"CGIT_HTTP_PUSH": "yes"}, {"CGIT_AUTH_MODE": "anything"},
                       {"CGIT_USERNAME": "owner"}, {"CGIT_LFS_MAX_SIZE": "0"},
                       {"CGIT_BASE_URL": "https://example.test/prefix"},
                       {"CGIT_BASE_URL": "https://user:secret@example.test"},
                       {"CGIT_BASE_URL": "https://example.test\nscan-path=/"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.generate(**values)

    def test_private_account_without_persisting_secret(self):
        cfg = self.generate(CGIT_AUTH_MODE="private", CGIT_USERNAME="owner",
                            CGIT_PASSWORD_FILE=str(self.password), CGIT_HTTP_PUSH="1")
        self.assertTrue(cfg["http_push"])
        for file in (self.root / "runtime").iterdir():
            self.assertNotIn("synthetic-password", file.read_text())
