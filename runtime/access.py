#!/usr/bin/env python3
"""Personal identities and a fail-closed adapter to Gitolite's authoritative ACL.

The identity snapshot contains hashes and compiled-policy fingerprints, never a
second set of read/write rules. Administrators publish it after compiling ACLs.
"""

import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

from common import RequestError, digest_file, safe_path
from lfs_token import ensure_secret as ensure_lfs_token

USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]*(?:/[A-Za-z0-9][A-Za-z0-9._@+-]*)*\Z")
HEX = re.compile(r"[a-f0-9]{64}\Z")
SECRET_HASH = re.compile(r"pbkdf2_sha256\$600000\$([a-f0-9]{32})\$([a-f0-9]{64})\Z")
INTERNAL = {"gitolite-admin"}
LIMIT = 1024 * 1024
CGI_KEYS = {
    "REQUEST_METHOD", "REQUEST_URI", "QUERY_STRING", "SERVER_PROTOCOL", "SERVER_NAME",
    "SERVER_PORT", "SERVER_ADDR", "REMOTE_ADDR", "REMOTE_PORT", "GATEWAY_INTERFACE",
    "CONTENT_TYPE", "CONTENT_LENGTH", "PATH_INFO", "SCRIPT_NAME", "DOCUMENT_ROOT",
    "HTTP_HOST", "HTTP_USER_AGENT", "HTTP_ACCEPT", "HTTP_ACCEPT_ENCODING", "HTTP_RANGE",
    "HTTP_IF_RANGE", "HTTP_IF_NONE_MATCH", "HTTP_IF_MODIFIED_SINCE", "HTTP_GIT_PROTOCOL",
}


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Access configuration exceeds size limit")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Access configuration must be an object")
    return value


def hash_secret(secret):
    if not isinstance(secret, bytes) or not 16 <= len(secret) <= 1024:
        raise ValueError("Use a credential of 16 to 1024 bytes; generated random tokens are recommended")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret, salt, 600000)
    return f"pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_secret(secret, encoded):
    match = SECRET_HASH.fullmatch(encoded)
    if not match or len(secret) > 1024:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", secret, bytes.fromhex(match[1]), 600000)
    return hmac.compare_digest(actual, bytes.fromhex(match[2]))


def validate_users(users):
    if not isinstance(users, dict) or not users:
        raise ValueError("At least one registered owner is required")
    owners = 0
    for name, value in users.items():
        if not USER.fullmatch(name) or name in ("anonymous", "root") or not isinstance(value, dict):
            raise ValueError("Invalid or reserved identity")
        if set(value) - {"enabled", "owner", "secret_hash", "keys"}:
            raise ValueError("Unknown identity field; permissions belong in Gitolite config")
        if type(value.get("enabled")) is not bool or type(value.get("owner", False)) is not bool:
            raise ValueError("Identity enabled/owner fields must be booleans")
        secret = value.get("secret_hash")
        if secret is not None and (not isinstance(secret, str) or not SECRET_HASH.fullmatch(secret)):
            raise ValueError("Unsupported credential hash")
        keys = value.get("keys", [])
        if not isinstance(keys, list) or any(not isinstance(k, str) or not k.startswith("SHA256:")
                                           or len(k) > 128 or any(c.isspace() for c in k) for k in keys):
            raise ValueError("Identity keys must contain OpenSSH SHA256 fingerprints")
        owners += bool(value["enabled"] and value.get("owner"))
    if not owners:
        raise ValueError("At least one enabled owner is required")


def clean_environment(home=None):
    # Never inherit GL_BYPASS_ACCESS_CHECKS, G3T_RC, GIT_CONFIG_PARAMETERS,
    # SSH_ORIGINAL_COMMAND, client identity headers, or CGI HTTP_PROXY.
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "LANG": "C", "TZ": os.environ.get("TZ", "UTC"),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    # Gitolite derives GL_ADMIN_BASE from HOME. The adapter's access root is
    # deliberately separate from the runtime user's home and must be explicit.
    if home is not None:
        env["HOME"] = str(home)
    return env


class Access:
    def __init__(self, cfg):
        self.cfg = cfg
        self.options = cfg["access"]
        self.root = Path(self.options["root"])
        self.binary = Path(self.options["gitolite"])
        self._gitolite_directories = None
        self._write_fd = None
        try:
            if not self.root.is_absolute():
                raise ValueError("Access root must be an absolute path")
            if (self.root / "maintenance").exists():
                raise ValueError("Access policy is under maintenance")
            self.state = read_json(self.root / "identity.json")
            if self.state.get("schema_version") != 1:
                raise ValueError("Unsupported identity snapshot")
            generation = self.state.get("generation", "")
            unsigned = {k: v for k, v in self.state.items() if k != "generation"}
            if not HEX.fullmatch(generation) or hashlib.sha256(canonical(unsigned)).hexdigest() != generation:
                raise ValueError("Identity snapshot fingerprint mismatch")
            validate_users(self.state.get("users"))
            repos = self.state.get("repositories")
            if not isinstance(repos, dict) or any(not REPO.fullmatch(k) or k in INTERNAL
                                                 or not isinstance(v, str) or not HEX.fullmatch(v)
                                                 for k, v in repos.items()):
                raise ValueError("Invalid compiled repository manifest")
            self._check(self.root / ".gitolite/conf/gitolite.conf-compiled.pm", self.state.get("common_digest", ""))
            if not self.binary.is_absolute() or not os.access(self.binary, os.X_OK):
                raise ValueError("Gitolite executable is unavailable")
        except (OSError, ValueError, TypeError) as exc:
            raise RequestError(503, "Access configuration unavailable; inspect service logs") from exc

    def _check(self, path, expected):
        if not isinstance(expected, str) or not HEX.fullmatch(expected) or digest_file(path) != expected:
            raise ValueError("Compiled policy fingerprint mismatch; apply a complete snapshot")

    def current(self):
        if (self.root / "maintenance").exists():
            raise RequestError(503, "Access policy is under maintenance")

    def authenticate(self, username, secret):
        user = self.state["users"].get(username, {})
        # Perform the same bounded KDF for unknown/disabled identities.
        encoded = user.get("secret_hash") or "pbkdf2_sha256$600000$" + "00" * 16 + "$" + "00" * 32
        valid = verify_secret(secret, encoded)
        return bool(valid and user.get("enabled") is True and user.get("secret_hash"))

    def allowed(self, name, principal, operation="R"):
        self.current()
        logical = name[:-4] if name.endswith(".git") else name
        if operation not in ("R", "W") or logical not in self.state["repositories"]:
            return False
        if principal is not None and not self.state["users"].get(principal, {}).get("enabled"):
            return False
        if principal is None and operation != "R":
            return False
        try:
            # A process can serve multiple requests/commands. Recheck both
            # compiled fragments immediately before each query instead of
            # trusting an earlier successful read of the same path.
            self._check(self.root / ".gitolite/conf/gitolite.conf-compiled.pm",
                        self.state.get("common_digest", ""))
            repo = safe_path(self.cfg["repos"], logical + ".git")
            self._check(repo / "gl-conf", self.state["repositories"][logical])
            result = subprocess.run([str(self.binary), "access", "-q", logical, principal or "anonymous",
                                     operation, "any"], env=clean_environment(self.root),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            if result.returncode not in (0, 1):
                raise ValueError("Gitolite query failed")
            return result.returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RequestError(503, "Authorization backend unavailable") from exc

    def readable(self, principal=None, repositories=None):
        """Return the readable subset using one bounded Gitolite batch query."""
        self.current()
        user = principal or "anonymous"
        if principal is not None and not self.state["users"].get(principal, {}).get("enabled"):
            return set()
        names = sorted(self.state["repositories"] if repositories is None else repositories)
        if any(not isinstance(name, str) or name not in self.state["repositories"] for name in names):
            raise RequestError(503, "Compiled repository manifest does not match requested scope")
        if not names:
            return set()
        try:
            self._check(self.root / ".gitolite/conf/gitolite.conf-compiled.pm",
                        self.state.get("common_digest", ""))
            for logical in names:
                repo = safe_path(self.cfg["repos"], logical + ".git")
                self._check(repo / "gl-conf", self.state["repositories"][logical])
            result = subprocess.run(
                [str(self.binary), "access", "%", user, "R", "any"],
                input=("".join(f"{name}\n" for name in names)).encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=clean_environment(self.root),
                timeout=30,
            )
            if result.returncode:
                raise ValueError("Gitolite batch access query failed")
            readable = {}
            for line in result.stdout.decode("utf-8", "strict").splitlines():
                fields = line.split("\t")
                if len(fields) != 3 or fields[0] not in names or fields[1] != user:
                    raise ValueError("Unexpected Gitolite batch access response")
                readable[fields[0]] = "DENIED" not in fields[2]
            if set(readable) != set(names):
                raise ValueError("Incomplete Gitolite batch access response")
            return {name for name, allowed in readable.items() if allowed}
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
            raise RequestError(503, "Authorization backend unavailable") from exc

    def require(self, name, principal, operation="R"):
        if not self.allowed(name, principal, operation):
            raise RequestError(401 if principal is None else 404,
                               "Authentication required" if principal is None else "Not found")
        if operation == "W":
            self._hold_write()

    def _hold_write(self):
        if self._write_fd is not None:
            return
        path = self.root / "write.lock"
        if path.is_symlink():
            raise RequestError(503, "Write lock path is unsafe")
        fd = None
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_SH)
            self._write_fd = fd
            os.set_inheritable(fd, True)
            # A backup/apply can enter after the ACL query but before the lock.
            try:
                self.current()
            except BaseException:
                os.close(fd)
                self._write_fd = None
                raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise RequestError(503, "Write coordination unavailable") from exc

    def cgi_environment(self, principal_name=None, cgit_config=None):
        env = clean_environment()
        env.update({key: value for key, value in os.environ.items() if key in CGI_KEYS})
        env["CGIT_RUNTIME_CONFIG"] = os.environ.get("CGIT_RUNTIME_CONFIG", "/var/cache/cgit/runtime/settings.json")
        if principal_name is not None:
            env["CGIT_PERSONAL_USER"] = principal_name
        if cgit_config is not None:
            env["CGIT_CONFIG"] = str(cgit_config)
        return env

    def gitolite_directories(self):
        if self._gitolite_directories is not None:
            return self._gitolite_directories
        try:
            values = []
            for key in ("GL_BINDIR", "GL_LIBDIR"):
                result = subprocess.run(
                    [str(self.binary), "query-rc", key],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=clean_environment(self.root),
                    timeout=15,
                )
                value = result.stdout.decode("utf-8", "strict").strip()
                if (result.returncode or not value or "\n" in value
                        or not Path(value).is_absolute() or not Path(value).is_dir()):
                    raise ValueError(f"Gitolite did not provide a valid {key}")
                values.append(Path(value))
            self._gitolite_directories = tuple(values)
            return self._gitolite_directories
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
            raise RequestError(503, "Gitolite runtime environment unavailable") from exc


def session(cfg):
    if "_access_session" not in cfg:
        cfg["_access_session"] = Access(cfg)
    return cfg["_access_session"]


def principal(cfg):
    # A child filter only receives this value from the authenticated CGI parent.
    return cfg.get("_principal", os.environ.get("CGIT_PERSONAL_USER") or None)


def publish_snapshot(root, repos, users):
    """Publish fingerprints of an already compiled policy, with no ACL duplication.

    The caller must hold maintenance across config compilation and this command.
    Failure intentionally leaves maintenance enabled rather than serving a mixture.
    """
    root, repos = Path(root), Path(repos)
    if not root.is_absolute() or not repos.is_absolute():
        raise ValueError("Access root and repository root must be absolute paths")
    validate_users(users)
    if not (root / "maintenance").is_file():
        raise ValueError("Enter maintenance before publishing access configuration")
    compiled = root / ".gitolite/conf/gitolite.conf-compiled.pm"
    inventory = {}
    for path in repos.rglob("gl-conf"):
        if path.is_symlink() or path.parent.suffix != ".git":
            raise ValueError("Unsupported compiled repository path")
        relative = path.parent.relative_to(repos).as_posix()[:-4]
        if relative in INTERNAL:
            continue
        if not REPO.fullmatch(relative):
            raise ValueError("Repository names must be canonical Gitolite paths")
        safe_path(repos, relative + ".git")
        inventory[relative] = digest_file(path)
    state = dict(schema_version=1, users=users, common_digest=digest_file(compiled), repositories=inventory)
    state["generation"] = hashlib.sha256(canonical(state)).hexdigest()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".identity-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(canonical(state) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "identity.json")
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return state["generation"]


def key_fingerprint(path):
    result = subprocess.run(["ssh-keygen", "-lf", str(path), "-E", "sha256"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=15)
    if result.returncode:
        raise ValueError(f"Cannot fingerprint SSH key {path.name}")
    fields = result.stdout.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise ValueError(f"Unexpected SSH fingerprint for {path.name}")
    return fields[1]


def render_authorized_keys(root, command, users):
    """Render forced-command keys from Gitolite's keydir after policy compile."""
    root = Path(root)
    if not root.is_absolute():
        raise ValueError("Access root must be an absolute path")
    if not (root / "maintenance").is_file():
        raise ValueError("Enter maintenance before rendering SSH keys")
    command_path = Path(command)
    if (not command_path.is_absolute() or any(c.isspace() or c in ",\"'"
                                             for c in str(command_path))):
        raise ValueError("SSH command path must be absolute and quote-free")
    validate_users(users)
    keydir = root / ".gitolite/keydir"
    if not keydir.is_dir() or keydir.is_symlink():
        raise ValueError("Gitolite keydir is missing")
    rendered = {}
    for path in sorted(keydir.rglob("*.pub")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("Unsupported SSH key file")
        name = path.name[:-4].split("@", 1)[0]
        if not USER.fullmatch(name) or name not in users or users[name].get("enabled") is not True:
            raise ValueError("SSH key does not belong to an enabled identity")
        lines = path.read_text().splitlines()
        if len(lines) != 1 or not lines[0] or any(ord(c) < 32 for c in lines[0]):
            raise ValueError("SSH key files must contain exactly one public key")
        key_type = lines[0].split(" ", 1)[0]
        if not key_type.startswith(("ssh-", "ecdsa-", "sk-")):
            raise ValueError("Unsupported SSH public key type")
        fingerprint = key_fingerprint(path)
        expected = users[name].get("keys", [])
        if fingerprint not in expected:
            raise ValueError("SSH key fingerprint is not registered in identity.json")
        if fingerprint in rendered:
            raise ValueError("Duplicate SSH key fingerprint")
        rendered[fingerprint] = (
            f'command="{command_path} {name}",'
            "no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty,no-user-rc "
            f"{lines[0]}")
    if not rendered:
        raise ValueError("At least one enabled owner SSH key is required")
    owners = {f for name, value in users.items() if value.get("enabled") and value.get("owner")
              for f in value.get("keys", [])}
    if not owners.intersection(rendered):
        raise ValueError("At least one enabled owner SSH key is required")
    destination_dir = root / "ssh"
    destination_dir.mkdir(mode=0o700, exist_ok=True)
    if destination_dir.is_symlink():
        raise ValueError("SSH state directory must not be a symlink")
    os.chmod(destination_dir, 0o700)
    destination = destination_dir / "authorized_keys"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=destination_dir, prefix=".authorized-",
                                         delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write("\n".join(rendered.values()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(rendered)


def apply_access(root, repos, users, gitolite, ssh_command):
    """Compile and publish one access generation while writes are fenced."""
    root, repos, gitolite = Path(root), Path(repos), Path(gitolite)
    if not all(path.is_absolute() for path in (root, repos, gitolite)):
        raise ValueError("Access roots and Gitolite executable must be absolute")
    if not os.access(gitolite, os.X_OK):
        raise ValueError("Gitolite executable is unavailable")
    marker = root / "maintenance"
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("Access maintenance is already active; inspect the previous apply") from exc
    else:
        try:
            os.write(fd, f"apply pid={os.getpid()}\n".encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    try:
        validate_users(users)
        result = subprocess.run(
            [str(gitolite), "compile"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_environment(root),
            timeout=60,
        )
        if result.returncode:
            raise ValueError(
                f"Gitolite compile failed: {result.stderr.decode('utf-8', 'replace').strip()}")
        render_authorized_keys(root, ssh_command, users)
        ensure_lfs_token(root)
        generation = publish_snapshot(root, repos, users)
    except BaseException:
        # Keep requests fenced. An administrator must inspect diagnostics and
        # either retry after removing the marker or restore the previous state.
        raise
    marker.unlink()
    try:
        cfg = {
            "repos": str(repos),
            "access": {"root": str(root), "gitolite": str(gitolite)},
        }
        Access(cfg).readable(None)
    except BaseException:
        marker.touch()
        raise
    return generation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    hashed = commands.add_parser("hash-secret")
    hashed.add_argument("--secret-file", type=Path, required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--directory", type=Path, required=True)
    snapshot.add_argument("--repos", type=Path, required=True)
    snapshot.add_argument("--identities", type=Path, required=True)
    ssh = commands.add_parser("authorized-keys")
    ssh.add_argument("--directory", type=Path, required=True)
    ssh.add_argument("--command", default="/usr/local/bin/cgit-ssh")
    ssh.add_argument("--identities", type=Path, required=True)
    token = commands.add_parser("init-lfs-token")
    token.add_argument("--directory", type=Path, required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--directory", type=Path, required=True)
    apply.add_argument("--repos", type=Path, required=True)
    apply.add_argument("--identities", type=Path, required=True)
    apply.add_argument("--gitolite", type=Path, required=True)
    apply.add_argument("--ssh-command", default="/usr/local/bin/cgit-ssh")
    args = parser.parse_args()
    if args.command == "hash-secret":
        print(hash_secret(args.secret_file.read_bytes().rstrip(b"\r\n")))
    elif args.command == "snapshot":
        generation = publish_snapshot(args.directory, args.repos, read_json(args.identities))
        print(json.dumps({"ok": True, "generation": generation, "maintenance": True}))
    elif args.command == "authorized-keys":
        count = render_authorized_keys(args.directory, args.command, read_json(args.identities))
        print(json.dumps({"ok": True, "keys": count, "maintenance": True}))
    elif args.command == "init-lfs-token":
        ensure_lfs_token(args.directory)
        print(json.dumps({"ok": True, "lfs_token_key": True, "maintenance": True}))
    else:
        generation = apply_access(
            args.directory, args.repos, read_json(args.identities),
            args.gitolite, args.ssh_command)
        print(json.dumps({"ok": True, "generation": generation, "maintenance": False}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RequestError) as exc:
        sys.exit(f"Access configuration error: {exc}")
