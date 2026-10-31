"""Independent checks that the production runtime is Go, without a Python fallback.

Python is the external test driver only. No production implementation is imported.
"""
import os
import json
import tempfile
import sys
from pathlib import Path
import subprocess
import unittest
from contract_support import RUNTIME, ROOT, ENV, run

class GoRuntimeContract(unittest.TestCase):
    def test_go_security_regressions(self):
        # A named suite must actually execute, rather than accepting "no tests".
        result = run("go", "test", "-race", "-count=1", "-v", "./internal/runtime", cwd=ROOT)
        output = result.stdout.decode()
        self.assertNotIn("--- SKIP:", output, "Required Go regressions must not hide skipped recovery tests")
        for name in ("TestTokenScopesAndKeyLifecycle", "TestTokenExpirationAndFutureIssued",
                     "TestCredentialHash", "TestSafePaths", "TestWriteFenceWaitsAndFailurePolicy",
                     "TestSyncRealLFSAndCorruptRepair", "TestSyncRejectsConfigSymlinkAndRewrite"):
            self.assertIn("--- PASS: " + name, output)
        run("go", "test", "./internal/runtime", "-run=^$", "-fuzz=FuzzSafePath",
            "-fuzztime=2s", "-parallel=2", cwd=ROOT)

    def test_vulnerability_gate_rejects_findings_and_incomplete_reports(self):
        with tempfile.TemporaryDirectory(prefix="cgit-vuln-gate-") as directory:
            root = Path(directory)
            source, output = root / "scan.json", root / "gate.json"
            valid = [{"config": {"scanner_name": "govulncheck", "scan_level": "symbol", "scan_mode": "source"}},
                     {"SBOM": {"modules": [{"path": "github.com/zopiya/cgit"}]}}]
            for messages, ok in ((valid, True), ([], False), (valid[:1], False),
                                 (valid + [{"finding": {"osv": "GO-fixture"}}], False)):
                source.write_text("\n".join(json.dumps(m) for m in messages))
                result = run(sys.executable, str(ROOT / "scripts/check_go_vulnerabilities.py"), str(source),
                             "--report", str(output), check=False)
                self.assertEqual(result.returncode == 0, ok)
                self.assertEqual(json.loads(output.read_text())["ok"], ok)
            source.write_text(json.dumps(valid[0]) + "\n{")
            self.assertNotEqual(run(sys.executable, str(ROOT / "scripts/check_go_vulnerabilities.py"), str(source),
                                   "--report", str(output), check=False).returncode, 0)

    @unittest.skipUnless(os.environ.get("CGIT_TEST_DRIVER") == "docker",
                         "Package inventory and Python absence require both Linux images")
    def test_production_images_have_no_python(self):
        for variable in ("CGIT_TEST_IMAGE", "CGIT_TEST_MAINTENANCE_IMAGE"):
            image = os.environ.get(variable)
            self.assertTrue(image, f"{variable} is required")
            result = run("docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image,
                "-ec", "for p in python python3 pypy pypy3 pip pip3; do "
                "if command -v $p; then exit 1; fi; done; "
                "if apk info | grep -E '^(python|py3-|pypy)'; then exit 1; fi; "
                "test -x /usr/local/bin/cgit-runtime; "
                "test ! -d /usr/local/lib/cgit-personal; "
                "test ! -d /usr/local/lib/cgit-maintenance; "
                "if find /usr/local -name '*.py' | grep .; then exit 1; fi")
            self.assertEqual(result.returncode, 0)
