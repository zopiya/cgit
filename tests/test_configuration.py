import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import subprocess
from contract_support import RUNTIME

def configure(source_path, repos):
    result = subprocess.run([RUNTIME, "configure", "--source", str(source_path), "--repos", repos],
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace"))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repos = self.root / "repos"
        self.repos.mkdir()
        self.template = self.root / "cgitrc"
        self.template.write_text(f"clone-url=http://old/$CGIT_REPO_URL\ncache-size=1000\nscan-path={self.repos}\n")
        self.password = self.root / "password"
        self.password.write_text("synthetic-password\n")
        self.env = {"CGIT_RUNTIME_DIR": str(self.root / "runtime")}

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, **values):
        with patch.dict(os.environ, {**self.env, **values}, clear=True):
            configure(source_path=self.template, repos=str(self.repos))
        return json.loads((self.root / "runtime/settings.json").read_text())

    def test_login_link_is_generated_only_for_access_mode(self):
        access = self.root / "access"
        access.mkdir()
        binary = self.root / "gitolite"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        cfg = self.generate(CGIT_AUTH_MODE="access", CGIT_ACCESS_ROOT=str(access),
                            CGIT_GITOLITE=str(binary))
        config = Path(cfg["cgit_config"]).read_text()
        header = self.root / "runtime/access-header.html"
        self.assertIn(f"header={header}", config)
        self.assertIn('href="/_login"', header.read_text())
        self.assertLess(config.index("header="), config.index("scan-path="))
        cfg = self.generate()
        self.assertNotIn("access-header.html", Path(cfg["cgit_config"]).read_text())

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

    def test_export_policy_and_scan_contract(self):
        projects = self.root / "projects"
        projects.write_text("group/demo.git\ngroup/sub\n")
        self.template.write_text(f"project-list={projects}\nstrict-export=git-daemon-export-ok\nscan-path={self.repos}\n")
        cfg = self.generate()
        self.assertEqual(cfg["repository_policy"], {
            "project_list": str(projects), "strict_export": "git-daemon-export-ok"})
        text = Path(cfg["cgit_config"]).read_text()
        self.assertIn("remove-suffix=1\nenable-git-config=1\nscan-hidden-path=0\n", text)

    def test_unsupported_scan_config_fails_closed(self):
        for source in ("scan-path=/elsewhere\n", "scan-path=/repos\nscan-path=/repos\n",
                       "include=/other\nscan-path=/repos\n", "repo.url=other\nscan-path=/repos\n",
                       "remove-suffix=0\nscan-path=/repos\n", "scan-hidden-path=1\nscan-path=/repos\n",
                       "scan-path=/repos\nstrict-export=marker\n", "  include=/other\nscan-path=/repos\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.template.write_text(source.replace("/repos", str(self.repos)))
                self.generate()

    def test_leading_spaces_comments_and_cache_override(self):
        self.template.write_text(f"  ; comment\n  cache-size=1000\n  scan-path={self.repos}\n")
        cfg = self.generate()
        text = Path(cfg["cgit_config"]).read_text()
        self.assertIn("cache-size=0\n", text)
        self.assertNotIn("cache-size=1000", text)

    def test_noncanonical_project_paths_are_rejected(self):
        projects = self.root / "projects"
        self.template.write_text(f"project-list={projects}\nscan-path={self.repos}\n")
        for value in ("group/", "group//demo.git", "../outside.git", "/absolute.git", ".hidden/demo.git"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                projects.write_text(value + "\n")
                self.generate()

    def test_access_mode_requires_isolated_identity_backend(self):
        access = self.root / "access"
        access.mkdir()
        gitolite = self.root / "gitolite"
        gitolite.write_text("#!/bin/sh\nexit 0\n")
        gitolite.chmod(0o755)
        cfg = self.generate(CGIT_AUTH_MODE="access", CGIT_ACCESS_ROOT=str(access),
                            CGIT_GITOLITE=str(gitolite), CGIT_HTTP_PUSH="1")
        self.assertEqual(cfg["auth_mode"], "access")
        self.assertEqual(cfg["username"], "")
        self.assertEqual(cfg["password_file"], "")
        self.assertEqual(cfg["access"], {"root": str(access), "gitolite": str(gitolite)})

    def test_access_mode_rejects_mixed_or_unsafe_configuration(self):
        access = self.root / "access"
        access.mkdir()
        gitolite = self.root / "gitolite"
        gitolite.write_text("#!/bin/sh\nexit 0\n")
        gitolite.chmod(0o755)
        invalid = (
            {"CGIT_AUTH_MODE": "access", "CGIT_ACCESS_ROOT": str(access)},
            {"CGIT_AUTH_MODE": "access", "CGIT_ACCESS_ROOT": "relative",
             "CGIT_GITOLITE": str(gitolite)},
            {"CGIT_AUTH_MODE": "access", "CGIT_ACCESS_ROOT": str(access),
             "CGIT_GITOLITE": str(gitolite), "CGIT_USERNAME": "owner",
             "CGIT_PASSWORD_FILE": str(self.password)},
            {"CGIT_AUTH_MODE": "public", "CGIT_ACCESS_ROOT": str(access)},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.generate(**values)
