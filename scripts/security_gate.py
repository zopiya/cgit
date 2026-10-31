#!/usr/bin/env python3
"""Evaluate automated security evidence and preserve explicit manual gaps."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUTCOMES = {"passed", "failed", "error", "skipped", "expected_failure"}
ARTIFACTS = {"image", "secrets"}


class GateError(ValueError):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"Cannot read JSON evidence {path}: {exc}") from exc


def text(path):
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise GateError(f"Cannot read required source {path}: {exc}") from exc


def require_strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GateError(f"{label} must be a list of nonempty strings")
    return value


def validate_manifest(manifest):
    if manifest.get("schema_version") != 1:
        raise GateError("Unsupported security manifest schema")
    controls = manifest.get("controls")
    gates = manifest.get("gates")
    if not isinstance(controls, dict) or set(controls) != {f"S0{i}" for i in range(1, 9)}:
        raise GateError("Security manifest must define exactly S01 through S08")
    if not isinstance(gates, dict) or set(gates) != {f"G{i}" for i in range(1, 9)}:
        raise GateError("Security manifest must define exactly G1 through G8")
    for control_id, control in controls.items():
        if not isinstance(control, dict):
            raise GateError(f"{control_id} must be an object")
        for key in ("contracts", "tests", "static", "artifacts", "manual"):
            require_strings(control.get(key, []), f"{control_id}.{key}")
        for name in control.get("static", []):
            if name not in STATIC_CHECKS:
                raise GateError(f"Unknown static security check: {name}")
        for name in control.get("artifacts", []):
            if name not in ARTIFACTS:
                raise GateError(f"Unknown security artifact: {name}")
    for gate_id, gate in gates.items():
        if not isinstance(gate, dict) or not isinstance(gate.get("name"), str):
            raise GateError(f"{gate_id} must have a name")
        dependencies = require_strings(gate.get("controls"), f"{gate_id}.controls")
        if any(item not in controls for item in dependencies):
            raise GateError(f"{gate_id} references an unknown control")
    return controls, gates


def contract_tests(runtime_manifest):
    if runtime_manifest.get("schema_version") != 1:
        raise GateError("Unsupported runtime contract schema")
    result = {}
    for item in runtime_manifest.get("requirements", []):
        entries = item.get("tests")
        if entries is None:
            entry = item.get("test")
            entries = [] if entry is None else [entry]
        result[item.get("id")] = require_strings(entries, f"{item.get('id')}.tests")
    return result


def load_test_reports(paths):
    reports = [read_json(path) for path in paths]
    complete = []
    outcomes = {}
    for report in reports:
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            raise GateError("Unsupported test report schema")
        cases = report.get("cases", [])
        if not isinstance(cases, list):
            raise GateError("Test cases must be a list")
        seen = set()
        for case in cases:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
                raise GateError("Malformed test case in evidence report")
            if case["id"] in seen:
                raise GateError(f"Duplicate test case: {case['id']}")
            seen.add(case["id"])
            if case.get("outcome") not in OUTCOMES:
                raise GateError(f"Unknown test outcome: {case.get('outcome')}")
        eligible = (
            report.get("driver") == "docker"
            and report.get("layer") == "linux-container"
            and report.get("complete") is True
            and report.get("ok") is True
            and type(report.get("tests_run")) is int
            and report["tests_run"] == len(cases) > 0
            and report.get("failures") == 0
            and report.get("errors") == 0
            and report.get("expected_failures", 0) == 0
            and report.get("skipped") == []
            and not report.get("preflight_error")
            and all(case["outcome"] == "passed" for case in cases)
        )
        if eligible:
            complete.append(report)
        for case in cases:
            # Partial/native evidence cannot supply a Linux pass. Negative
            # observations still veto a pass from another report.
            if eligible or case["outcome"] != "passed":
                outcomes.setdefault(case["id"], []).append(case["outcome"])
    return bool(complete), outcomes


def evaluate_tests(required, complete, outcomes):
    result = {}
    for test_id in sorted(required):
        observed = outcomes.get(test_id, [])
        if not complete:
            result[test_id] = {"ok": False, "detail": "No complete Docker test report"}
        elif not observed:
            result[test_id] = {"ok": False, "detail": "Required test was not observed"}
        elif any(outcome != "passed" for outcome in observed):
            result[test_id] = {"ok": False, "detail": "Observed outcomes: " + ", ".join(observed)}
        else:
            result[test_id] = {"ok": True, "detail": "passed"}
    return result


def full_line_option(configuration, name, expected):
    return re.search(
        rf"(?mi)^\s*{re.escape(name)}\s+{re.escape(expected)}\s*$",
        configuration,
    ) is not None


def check_caddy_web_edge(root):
    configuration = text(root / "config/Caddyfile")
    required = (
        "Strict-Transport-Security",
        "X-Content-Type-Options",
        "reverse_proxy cgit:8080",
    )
    ok = full_line_option(configuration, "admin", "off") and all(
        fragment in configuration for fragment in required)
    return ok, "admin off and required HTTPS/proxy headers are present" if ok else (
        "Caddy admin, HTTPS headers or proxy boundary is missing")


def check_backend_port_isolation(root):
    public = text(root / "docker-compose.public.yml")
    view = re.search(
        r"(?ms)^  cgit:\n(?:(?:    .*)?\n)*?^    ports:\s*!reset\s+\[\]\s*$", public)
    ok = view is not None
    return ok, "public overlay removes cgit host ports" if ok else (
        "public overlay does not explicitly reset cgit host ports")


def check_container_hardening(root):
    files = [
        "docker-compose.yml", "docker-compose.access.yml",
        "docker-compose.public.yml", "docker-compose.maintenance.yml",
    ]
    configuration = "\n".join(text(root / name) for name in files)
    forbidden = {
        "privileged": re.search(r"(?mi)^\s+privileged:\s*true\s*$", configuration),
        "host networking": re.search(r"(?mi)^\s+network_mode:\s*host\s*$", configuration),
        "host PID": re.search(r"(?mi)^\s+pid:\s*host\s*$", configuration),
        "Docker socket": re.search(r"/var/run/docker\.sock", configuration),
    }
    unsafe = [name for name, match in forbidden.items() if match]
    hardened = configuration.count("no-new-privileges:true")
    cap_drops = configuration.count("cap_drop:")
    ok = not unsafe and hardened >= 4 and cap_drops >= 4
    detail = (
        "no privileged/host controls or Docker socket; four roles drop capabilities"
        if ok else
        f"unsafe={unsafe or 'none'}, no_new_privileges={hardened}, cap_drop={cap_drops}")
    return ok, detail


def check_sshd_restricted(root):
    configuration = text(root / "config/sshd_config")
    required = {
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "PermitRootLogin": "no",
        "PubkeyAuthentication": "yes",
        "AuthenticationMethods": "publickey",
        "PermitTTY": "no",
        "PermitUserEnvironment": "no",
        "PermitTunnel": "no",
        "AllowAgentForwarding": "no",
        "AllowTcpForwarding": "no",
        "X11Forwarding": "no",
        "DisableForwarding": "yes",
    }
    missing = [name for name, value in required.items()
               if not full_line_option(configuration, name, value)]
    ok = not missing
    return ok, "restricted OpenSSH policy is present" if ok else (
        "Missing/incorrect SSH options: " + ", ".join(missing))


def check_build_context_excludes_state(root):
    dockerignore = {
        line.strip() for line in text(root / ".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    gitignore = {
        line.strip().rstrip("/") for line in text(root / ".gitignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {"secrets", ".env", "repos", "lfs", "access", "ssh-host-keys",
                "caddy-data", "caddy-config", "job-state", "job-work"}
    missing_docker = sorted(required - {item.rstrip("/") for item in dockerignore})
    missing_git = sorted(required - gitignore)
    ok = not missing_docker and not missing_git
    return ok, "state and credentials are excluded from build and Git" if ok else (
        f"dockerignore missing={missing_docker}, gitignore missing={missing_git}")


def check_ci_actions_pinned(root):
    workflow = text(root / ".github/workflows/docker-build.yml")
    unpinned = []
    for number, line in enumerate(workflow.splitlines(), 1):
        match = re.search(r"\buses:\s*([^\s#]+)", line)
        if match and not re.search(r"@[0-9a-f]{40}$", match.group(1)):
            unpinned.append(f"line {number}: {match.group(1)}")
    ok = not unpinned
    return ok, "all workflow actions use full commit pins" if ok else (
        "Unpinned actions: " + "; ".join(unpinned))


def check_runtime_sources_pinned(root):
    dockerfile = text(root / "Dockerfile")
    public = text(root / "docker-compose.public.yml")
    requirements = {
        "cgit commit": r"(?m)^ARG CGIT_COMMIT=[0-9a-f]{40}$",
        "Git source SHA-256": r"(?m)^ARG GIT_SHA256=[0-9a-f]{64}$",
        "Gitolite source SHA-256": r"(?m)^ARG GITOLITE_SHA256=[0-9a-f]{64}$",
        "Caddy image digest": r"caddy:2\.10\.2-alpine@sha256:[0-9a-f]{64}",
    }
    missing = [name for name, pattern in requirements.items()
               if not (re.search(pattern, dockerfile) or re.search(pattern, public))]
    ok = not missing
    return ok, "runtime source/image references are pinned" if ok else (
        "Missing pins: " + ", ".join(missing))


STATIC_CHECKS = {
    "caddy_web_edge": check_caddy_web_edge,
    "backend_port_isolation": check_backend_port_isolation,
    "container_hardening": check_container_hardening,
    "sshd_restricted": check_sshd_restricted,
    "build_context_excludes_state": check_build_context_excludes_state,
    "ci_actions_pinned": check_ci_actions_pinned,
    "runtime_sources_pinned": check_runtime_sources_pinned,
}


def source_identity(root):
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        return {"commit": commit, "dirty": bool(status)}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def evaluate(args):
    root = args.root.resolve()
    manifest = read_json(args.manifest)
    controls, gates = validate_manifest(manifest)
    runtime = read_json(root / "contracts/runtime.json")
    contract_map = contract_tests(runtime)
    complete, outcomes = load_test_reports(args.test_report)

    required_tests = set()
    for control in controls.values():
        required_tests.update(control.get("tests", []))
        for contract_id in control.get("contracts", []):
            if contract_id not in contract_map:
                raise GateError(f"Unknown runtime contract in security manifest: {contract_id}")
            required_tests.update(contract_map[contract_id])
    test_results = evaluate_tests(required_tests, complete, outcomes)

    artifact_paths = {"image": args.image_report, "secrets": args.secret_report}
    artifacts = {}
    for name, path in artifact_paths.items():
        try:
            value = read_json(path)
            if name == "image":
                ok = (
                    value.get("schema_version") == 1
                    and value.get("layer") == "linux-container-empty-startup"
                    and value.get("ok") is True
                    and all(value.get(key) for key in ("image_id", "cgit", "git", "health"))
                )
                detail = "Linux image smoke evidence passed" if ok else (
                    "Image report is incomplete or failed")
            else:
                ok = isinstance(value, list) and not value
                detail = "Secret scan report is empty" if ok else (
                    "Secret scan is missing findings data or contains findings")
            artifacts[name] = {"ok": ok, "detail": detail, "path": str(path)}
        except GateError as exc:
            artifacts[name] = {"ok": False, "detail": str(exc), "path": str(path)}

    static_results = {}
    for name, checker in STATIC_CHECKS.items():
        try:
            ok, detail = checker(root)
        except GateError as exc:
            ok, detail = False, str(exc)
        static_results[name] = {"ok": ok, "detail": detail}

    def evaluate_control(control_id, control):
        requirements = {}
        manual = control.get("manual", [])
        for contract_id in control.get("contracts", []):
            for test_id in contract_map[contract_id]:
                requirements[test_id] = test_results[test_id]
        for test_id in control.get("tests", []):
            if test_id not in test_results:
                raise GateError(f"Security manifest tests entry was not collected: {test_id}")
            requirements[test_id] = test_results[test_id]
        for name in control.get("static", []):
            requirements[f"static:{name}"] = static_results[name]
        for name in control.get("artifacts", []):
            requirements[name] = artifacts[name]
        return {
            "name": control["name"],
            "automated_ok": all(item["ok"] for item in requirements.values()),
            "requirements": requirements,
            "manual_pending": manual,
        }

    control_results = {
        control_id: evaluate_control(control_id, control)
        for control_id, control in controls.items()
    }
    gate_results = {}
    for gate_id, gate in gates.items():
        controls_for_gate = gate["controls"]
        automated_ok = all(control_results[name]["automated_ok"] for name in controls_for_gate)
        manual = []
        for name in controls_for_gate:
            manual.extend(control_results[name]["manual_pending"])
        gate_results[gate_id] = {
            "name": gate["name"],
            "controls": controls_for_gate,
            "automated_ok": automated_ok,
            "manual_pending": list(dict.fromkeys(manual)),
        }

    automated_ok = all(item["automated_ok"] for item in control_results.values())
    manual_pending = any(item["manual_pending"] for item in control_results.values())
    return {
        "schema_version": 1,
        "source": source_identity(root),
        "automated_ok": automated_ok,
        "complete": automated_ok and not manual_pending,
        "production_verified": False,
        "test_report_complete": complete,
        "controls": control_results,
        "gates": gate_results,
        "static_checks": static_results,
        "artifacts": artifacts,
        "manual_pending": sorted({
            item for result in control_results.values()
            for item in result["manual_pending"]
        }),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=ROOT / "contracts/security.json")
    parser.add_argument("--test-report", type=Path, action="append", required=True)
    parser.add_argument("--image-report", type=Path, required=True)
    parser.add_argument("--secret-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/security.json")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    try:
        report = evaluate(args)
        exit_code = 0 if report["automated_ok"] else 1
        if args.require_complete and not report["complete"]:
            exit_code = max(exit_code, 2)
        if args.report_only:
            exit_code = 0
    except (GateError, OSError, TypeError, KeyError) as exc:
        report = {
            "schema_version": 1,
            "automated_ok": False,
            "complete": False,
            "production_verified": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        exit_code = 1
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
