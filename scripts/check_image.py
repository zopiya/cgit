#!/usr/bin/env python3
"""Verify an empty Linux container's real entrypoint, static assets and healthcheck."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=30).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--report", type=Path, default=Path("artifacts/image-smoke.json"))
    args = parser.parse_args()
    name = f"cgit-image-check-{os.getpid()}"
    started = False
    report = {"schema_version": 1, "layer": "linux-container-empty-startup", "ok": False}
    try:
        if run("docker", "info", "--format", "{{.OSType}}") != "linux":
            raise RuntimeError("Linux engine required")
        run("docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8080",
            "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE",
            "--cap-add", "FOWNER", "--cap-add", "SETGID", "--cap-add", "SETUID",
            "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp",
            "--health-interval=1s", "--health-start-period=1s", "--health-retries=3", args.image)
        started = True
        port = run("docker", "port", name, "8080").rsplit(":", 1)[1]
        base = "http://127.0.0.1:" + port
        for _ in range(100):
            state = run("docker", "inspect", name, "--format", "{{.State.Health.Status}}")
            if state == "healthy":
                break
            if state == "unhealthy":
                raise RuntimeError("Image HEALTHCHECK became unhealthy")
            time.sleep(.1)
        else:
            raise RuntimeError("Image HEALTHCHECK did not become healthy")
        with urlopen(base + "/custom.css", timeout=5) as response:
            if response.status != 200 or b"font-family" not in response.read():
                raise RuntimeError("Custom CSS not served")
        with urlopen(base + "/_health", timeout=5) as response:
            if response.status != 200 or json.load(response)["status"] != "ok":
                raise RuntimeError("Dynamic health failed")
        # Empty repositories must be healthy; startup must not require fixture data.
        report.update(ok=True, image_id=run("docker", "image", "inspect", args.image, "--format", "{{.Id}}"),
                      cgit=run("docker", "exec", name, "/usr/local/libexec/cgit.cgi", "--version").splitlines()[0],
                      git=run("docker", "exec", name, "git", "--version"), health=state)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        if started:
            try:
                logs = subprocess.run(["docker", "logs", "--tail", "60", name], capture_output=True,
                                      text=True, timeout=20)
                report["synthetic_container_log"] = logs.stdout + logs.stderr
            except Exception:
                pass
        print(report["error"], file=sys.stderr)
    finally:
        if started:
            subprocess.run(["docker", "rm", "-fv", name], capture_output=True, timeout=30)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
