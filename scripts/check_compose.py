#!/usr/bin/env python3
"""Validate documented Compose combinations on a host with Docker Compose v2.

Runs `config` only. Does not contact the engine or start any service.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    variants = [
        ([], "public", False, False),
        (["write"], "private", True, False),
        (["public"], "public", False, True),
        (["access"], "access", True, False),
        (["access", "public"], "access", True, True),
        (["maintenance"], "public", False, False),
        (["write", "maintenance"], "private", True, False),
        (["access", "maintenance"], "access", True, False),
        (["access", "public", "maintenance"], "access", True, True),
    ]
    with tempfile.TemporaryDirectory(prefix="cgit-compose-config-") as directory:
        temporary = Path(directory)
        envfile = temporary / "empty.env"
        envfile.write_text("")
        password = temporary / "password"
        password.write_text("synthetic-config-validation-only\n")
        password.chmod(0o600)
        override = temporary / "override.json"
        override.write_text(json.dumps({
            "secrets": {"cgit_password": {"file": str(password)}}
        }))
        # Never inherit local CGIT settings, COMPOSE_FILE or a user's .env.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CGIT_", "COMPOSE_"))
               and key not in {"PUID", "PGID", "TZ"}}
        env.update(CGIT_DOMAIN="git.example.invalid", CGIT_ACME_EMAIL="admin@example.invalid")
        for extras, mode, push, edge in variants:
            command = ["docker", "compose", "--env-file", str(envfile),
                       "--project-name", "cgit-config-check", "--profile", "ssh",
                       "--profile", "maintenance", "-f", str(ROOT / "docker-compose.yml")]
            for name in extras:
                command += ["-f", str(ROOT / f"docker-compose.{name}.yml")]
            command += ["-f", str(override), "config", "--format", "json"]
            result = subprocess.run(command, env=env, capture_output=True, text=True,
                                    timeout=30, check=True)
            services = json.loads(result.stdout)["services"]
            web = services["cgit"]
            assert web["environment"]["CGIT_AUTH_MODE"] == mode, extras
            assert str(web["environment"]["CGIT_HTTP_PUSH"]) == str(int(push)), extras
            assert "container_name" not in web, extras
            repo = next(volume for volume in web["volumes"] if volume["target"] == "/repos")
            assert bool(repo.get("read_only", False)) == (mode == "public"), extras
            if edge:
                assert not web.get("ports"), extras
                assert web["environment"]["CGIT_BASE_URL"] == "https://git.example.invalid", extras
            else:
                port = web["ports"][0]
                assert port["target"] == 8080 and str(port["published"]) == "8080", extras
            assert ("ssh" in services) == ("access" in extras), extras
            assert ("maintenance" in services) == ("maintenance" in extras), extras
            if "maintenance" in extras:
                targets = {volume["target"] for volume in services["maintenance"]["volumes"]}
                assert {"/repos", "/lfs", "/coordination", "/var/cache/cgit"} <= targets, extras
            print("OK: base" + "".join(" + " + name for name in extras))
        # Separate project/port bindings must be possible without editing YAML.
        env.update(CGIT_HTTP_BIND="127.0.0.1", CGIT_HTTP_PORT="18080",
                   CGIT_SSH_BIND="127.0.0.1", CGIT_SSH_PORT="12222",
                   CGIT_LFS_MAX_SIZE="107374182400", CGIT_HTTP_MAX_REQUEST_SIZE="214748364800",
                   CGIT_GIT_PACK_THREADS="2", CGIT_GIT_PACK_WINDOW_MEMORY="67108864")
        command = ["docker", "compose", "--env-file", str(envfile), "--project-name", "second-instance",
                   "--profile", "ssh", "-f", str(ROOT / "docker-compose.yml"),
                   "-f", str(ROOT / "docker-compose.access.yml"), "config", "--format", "json"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=True)
        services = json.loads(result.stdout)["services"]
        for name in ("cgit", "ssh"):
            for key in ("CGIT_LFS_MAX_SIZE", "CGIT_HTTP_MAX_REQUEST_SIZE", "CGIT_GIT_PACK_THREADS", "CGIT_GIT_PACK_WINDOW_MEMORY"):
                assert str(services[name]["environment"][key]) == env[key], (name, key)
        for name, port in (("cgit", "18080"), ("ssh", "12222")):
            binding = services[name]["ports"][0]
            assert binding["host_ip"] == "127.0.0.1" and str(binding["published"]) == port, name
        print("OK: independent project and custom HTTP/SSH bindings")
    print("Compose configuration validated; no containers started.")


if __name__ == "__main__":
    main()
