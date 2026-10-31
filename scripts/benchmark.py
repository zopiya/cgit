#!/usr/bin/env python3
"""Measure real native HTTP pages on disposable Gitolite repositories only.

Requires the same CGIT_TEST_LIGHTTPD/CGIT/GITOLITE tools as the native suite.
Never points at existing repositories; no Docker installation is needed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT / "bin/cgit-runtime")
    parser.add_argument("--repositories", type=int, default=30)
    parser.add_argument("--requests", type=int, default=7)
    parser.add_argument("--report", type=Path, default=ROOT / "artifacts/performance.json")
    args = parser.parse_args()
    if not 1 <= args.repositories <= 500 or not 3 <= args.requests <= 100:
        parser.error("repositories must be 1..500 and requests 3..100")
    runtime = args.runtime.resolve()
    os.environ["CGIT_TEST_RUNTIME"] = str(runtime)
    os.environ["CGIT_TEST_DRIVER"] = "lighttpd"
    sys.path.insert(0, str(ROOT / "tests"))
    from contract_support import Service, ENV, run
    service = Service(access=True, start=False)
    try:
        policy = service.access_root / ".gitolite/conf/gitolite.conf"
        with policy.open("a") as f:
            for i in range(args.repositories):
                f.write(f"\nrepo extra/{i}\n    R = reader owner\n    RW+ = owner\n")
        run(str(runtime), "access", "apply", "--directory", str(service.access_root),
            "--repos", str(service.repos), "--identities", str(service.identities_file),
            "--gitolite", str(service.gitolite), "--ssh-command", service.ssh_command_path, env=ENV)
        service._start_local(False, True)
        counter = service.root / "queries"
        wrapper = service.root / "count-gitolite"
        wrapper.write_text("#!/bin/sh\nif [ \"$1\" = access ]; then printf 'query\\n' >> " +
                           shlex.quote(str(counter)) + "; fi\nexec " +
                           shlex.quote(str(service.gitolite)) + ' "$@"\n')
        wrapper.chmod(0o700)
        settings = json.loads(service.settings.read_text())
        settings["access"]["gitolite"] = str(wrapper)
        service.settings.write_text(json.dumps(settings))
        result = {"schema_version": 1, "layer": "native-synthetic-pages", "platform": platform.platform(),
                  "runtime_sha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
                  "checkout_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                  "checkout_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)),
                  "additional_repositories": args.repositories, "requests": args.requests,
                  "large_file_or_production_verified": False, "measurements": {}}
        for label, path in (("repository", "/group/demo/tree/"), ("legacy_query", "/?r=group/demo&p=tree"), ("index", "/")):
            status, _, body = service.request(path, user="reader")  # Warmup.
            assert status == 200 and body, (path, status)
            elapsed, queries = [], []
            for _ in range(args.requests):
                counter.write_text("")
                start = time.perf_counter()
                status, _, body = service.request(path, user="reader")
                elapsed.append((time.perf_counter() - start) * 1000)
                assert status == 200 and body, (path, status)
                queries.append(len(counter.read_text().splitlines()))
            ordered = sorted(elapsed)
            result["measurements"][label] = {"median_ms": round(statistics.median(elapsed), 3),
                "p95_ms": round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3),
                "authorization_queries": queries}
        # Keep the permission boundary in the same measured fixture.
        assert service.request("/group/demo/tree/")[0] == 401
        _, _, anonymous = service.request("/")
        assert b"href='/group/demo/'" not in anonymous
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result["measurements"], indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
