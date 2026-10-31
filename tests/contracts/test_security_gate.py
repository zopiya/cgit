"""Black-box security evidence gate contracts."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts/security_gate.py"


def read_json(path):
    return json.loads(Path(path).read_text())


class SecurityGateContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cgit-security-gate-")
        self.root = Path(self.temp.name)
        runtime = read_json(ROOT / "contracts/runtime.json")
        security = read_json(ROOT / "contracts/security.json")
        contract_tests = {}
        for requirement in runtime["requirements"]:
            tests = requirement.get("tests")
            if tests is None:
                single = requirement.get("test")
                tests = [] if single is None else [single]
            contract_tests[requirement["id"]] = tests
        self.required = set()
        for control in security["controls"].values():
            self.required.update(control.get("tests", []))
            for contract_id in control.get("contracts", []):
                self.required.update(contract_tests[contract_id])
        self.test_report = self.root / "container.json"
        self.image_report = self.root / "image.json"
        self.secret_report = self.root / "secrets.json"
        self.output = self.root / "security.json"
        self.write_evidence()

    def tearDown(self):
        self.temp.cleanup()

    def write_evidence(self, skipped=None):
        cases = [
            {
                "id": test_id,
                "outcome": "skipped" if test_id == skipped else "passed",
                "seconds": 0.01,
            }
            for test_id in sorted(self.required)
        ]
        self.test_report.write_text(json.dumps({
            "schema_version": 1,
            "driver": "docker",
            "suite": "all",
            "layer": "linux-container",
            "ok": skipped is None,
            "complete": skipped is None,
            "production_verified": False,
            "tests_run": len(cases),
            "failures": 0,
            "errors": 0,
            "skipped": [] if skipped is None else [{"id": skipped, "reason": "fixture"}],
            "cases": cases,
        }))
        self.image_report.write_text(json.dumps({
            "schema_version": 1,
            "layer": "linux-container-empty-startup",
            "ok": True,
            "image_id": "sha256:" + "1" * 64,
            "cgit": "cgit v1.3.1",
            "git": "git version 2.54.0",
            "health": "healthy",
        }))
        self.secret_report.write_text("[]\n")

    def gate(self, *extra, root=None, test_report=None, image_report=None, secret_report=None):
        command = [
            sys.executable, str(GATE),
            "--root", str(root or ROOT),
            "--manifest", str(ROOT / "contracts/security.json"),
            "--test-report", str(test_report or self.test_report),
            "--image-report", str(image_report or self.image_report),
            "--secret-report", str(secret_report or self.secret_report),
            "--report", str(self.output),
            *extra,
        ]
        return subprocess.run(command, capture_output=True, text=True, timeout=30)

    def test_gate_is_fail_closed_and_reports_manual_boundary(self):
        result = self.gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = read_json(self.output)
        self.assertTrue(report["automated_ok"])
        self.assertFalse(report["complete"])
        self.assertFalse(report["production_verified"])
        self.assertTrue(report["manual_pending"])
        self.assertEqual(set(report["gates"]), {f"G{i}" for i in range(1, 9)})
        self.assertTrue(all(gate["automated_ok"] for gate in report["gates"].values()))

        required = self.gate("--require-complete")
        self.assertEqual(required.returncode, 2)
        self.assertFalse(read_json(self.output)["complete"])

    def test_gate_rejects_skips_missing_layers_and_findings(self):
        self.write_evidence(skipped=sorted(self.required)[0])
        skipped = self.gate()
        self.assertNotEqual(skipped.returncode, 0)
        self.assertFalse(read_json(self.output)["automated_ok"])

        self.write_evidence()
        self.image_report.unlink()
        missing = self.gate()
        self.assertNotEqual(missing.returncode, 0)
        self.assertFalse(read_json(self.output)["automated_ok"])

        self.image_report.write_text(json.dumps({
            "schema_version": 1,
            "layer": "linux-container-empty-startup",
            "ok": True,
            "image_id": "sha256:" + "2" * 64,
            "cgit": "cgit v1.3.1",
            "git": "git version 2.54.0",
            "health": "healthy",
        }))
        self.secret_report.write_text('[{"RuleID":"fixture","File":"fixture"}]\n')
        findings = self.gate()
        self.assertNotEqual(findings.returncode, 0)
        self.assertFalse(read_json(self.output)["automated_ok"])

    def test_gate_cannot_borrow_passes_from_a_partial_report(self):
        complete = read_json(self.test_report)
        partial = dict(complete, driver="cgi", layer="local-protocol-partial", complete=False)
        self.test_report.write_text(json.dumps(dict(complete, cases=complete["cases"][1:],
                                                    tests_run=len(complete["cases"]) - 1)))
        other = self.root / "partial.json"
        other.write_text(json.dumps(partial))
        result = self.gate("--test-report", str(other))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(read_json(self.output)["automated_ok"])

    def test_gate_rejects_inconsistent_complete_reports(self):
        original = read_json(self.test_report)
        variants = [
            {"layer": "native-service"},
            {"tests_run": 0},
            {"tests_run": len(original["cases"]) + 1},
            {"expected_failures": 1},
            {"preflight_error": "fixture"},
            {"cases": original["cases"] + [original["cases"][0]],
             "tests_run": len(original["cases"]) + 1},
        ]
        for change in variants:
            with self.subTest(change=change):
                self.test_report.write_text(json.dumps(dict(original, **change)))
                result = self.gate()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(read_json(self.output)["automated_ok"])

    def test_manifest_test_ids_match_real_discovery(self):
        suite = unittest.defaultTestLoader.discover(
            str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))

        def identifiers(group):
            for item in group:
                if isinstance(item, unittest.TestSuite):
                    yield from identifiers(item)
                else:
                    yield item.id()

        missing = self.required - set(identifiers(suite))
        self.assertFalse(missing, f"Security tests absent from actual runner: {sorted(missing)}")

    def test_gate_rejects_unsafe_static_configuration(self):
        source = self.root / "source"
        files = (
            ".dockerignore",
            ".gitignore",
            "Dockerfile",
            "config/Caddyfile",
            "config/sshd_config",
            "docker-compose.yml",
            "docker-compose.access.yml",
            "docker-compose.public.yml",
            "docker-compose.maintenance.yml",
            "contracts/runtime.json",
        )
        for name in files:
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, target)
        compose = source / "docker-compose.access.yml"
        compose.write_text(compose.read_text() + "\n# fixture\nservices:\n  cgit:\n    privileged: true\n")
        result = self.gate(root=source)
        self.assertNotEqual(result.returncode, 0)
        report = read_json(self.output)
        self.assertFalse(report["automated_ok"])
        self.assertFalse(report["static_checks"]["container_hardening"]["ok"])


if __name__ == "__main__":
    unittest.main()
