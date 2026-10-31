#!/usr/bin/env python3
"""Run explicit verification layers and write an honest, machine-readable result."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []
        self.started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, outcome, detail=None):
        item = {"id": test.id(), "outcome": outcome,
                "seconds": round(time.monotonic() - self.started.get(test.id(), time.monotonic()), 3)}
        if detail:
            item["detail"] = detail
        self.cases.append(item)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.record(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure", self._exc_info_to_string(err, test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(subtest, "failed", self._exc_info_to_string(err, test))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--driver", choices=("cgi", "lighttpd", "docker"), default="cgi")
    parser.add_argument("--suite", choices=("all", "contracts"), default="all")
    parser.add_argument("--require-no-skips", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/tests.json")
    args = parser.parse_args()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    os.environ["CGIT_TEST_DRIVER"] = args.driver
    report = {"schema_version": 1, "driver": args.driver, "suite": args.suite,
              "layer": {"cgi": "local-protocol-partial", "lighttpd": "native-service", "docker": "linux-container"}[args.driver],
              "ok": False, "complete": False, "production_verified": False}
    try:
        production_files = sorted([*ROOT.glob("internal/**/*.go"), *ROOT.glob("cmd/**/*.go"),
                                   *ROOT.glob("config/*"), *ROOT.glob("docker-*.sh"),
                                   ROOT / "go.mod", ROOT / "go.sum", ROOT / "Dockerfile", ROOT / "Dockerfile.maintenance"])
        fingerprint = hashlib.sha256()
        for path in production_files:
            if path.is_file():
                fingerprint.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
        report["source_tree_sha256"] = fingerprint.hexdigest()
        runtime = Path(os.environ.get("CGIT_TEST_RUNTIME", str(ROOT / "bin/cgit-runtime")))
        if not runtime.is_file():
            raise RuntimeError("Build the Go runtime first: go build -o bin/cgit-runtime ./cmd/cgit-runtime")
        report["runtime_sha256"] = hashlib.sha256(runtime.read_bytes()).hexdigest()
        report["source_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        if sys.version_info < (3, 11):
            raise RuntimeError("Python 3.11+ is required for this verification harness")
        for executable in ("git", "git-lfs"):
            if not shutil.which(executable):
                raise RuntimeError(f"Required test client is missing: {executable}")
        if args.driver == "docker":
            if not shutil.which("docker"):
                raise RuntimeError("Docker is required; full-container tests cannot be skipped")
            checked = subprocess.run(["docker", "info", "--format", "{{.OSType}}"],
                                     capture_output=True, text=True, timeout=20)
            if checked.returncode or checked.stdout.strip() != "linux":
                raise RuntimeError("A working Linux Docker engine is required")
        if args.driver == "lighttpd":
            for variable in ("CGIT_TEST_LIGHTTPD", "CGIT_TEST_CGIT", "CGIT_TEST_FILTERS"):
                value = os.environ.get(variable)
                if not value or not Path(value).exists():
                    raise RuntimeError(f"Native-service verification requires {variable}")
        sys.path.insert(0, str(ROOT / "tests"))
        start = ROOT / "tests" / "contracts" if args.suite == "contracts" else ROOT / "tests"
        suite = unittest.defaultTestLoader.discover(str(start), top_level_dir=str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
        report.update(tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                      passed=sum(case["outcome"] == "passed" for case in result.cases),
                      expected_failures=len(result.expectedFailures),
                      skipped=[{"id": test.id(), "reason": reason} for test, reason in result.skipped],
                      cases=result.cases)
        strict = args.require_no_skips or args.driver == "docker"
        pending = bool(result.skipped or result.expectedFailures)
        report["ok"] = result.wasSuccessful() and result.testsRun > 0 and (not strict or not pending)
        report["complete"] = report["ok"] and not pending and args.driver != "cgi"
        if strict and pending:
            print("Verification failed: required layer contains skipped/expected-failure tests", file=sys.stderr)
    except Exception as exc:
        report["preflight_error"] = f"{type(exc).__name__}: {exc}"
        print(report["preflight_error"], file=sys.stderr)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
